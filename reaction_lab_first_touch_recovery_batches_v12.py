#!/usr/bin/env python3
"""Resume-safe, budgeted historical First Touch V1.1 recovery batches (OFFLINE).

--plan reads the frozen audit, current FT SQLite, and previously verified batch
manifests; it never writes or downloads. --run fetches ONLY via the existing
V1.1 recoverer, into brand-new separate batch folders, with explicit budgets.

It never updates FIRST_TOUCH_V2, Auto Paths, scanners, Redis, PM2 or LIVE.
No parallel workers. Completed batches are recognized from their signed-file
manifests and full 360m records. Partial symbols are skipped for manual review,
so a historical touch_key can never be silently generated in two batches.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time

from reaction_lab_first_touch_historical_recovery_v11 import (
    ALLOWED_STATES, ALLOWED_STATUSES, ROOT, load_selected, plan_requests,
    main as single_batch_main,
)
from reaction_lab_runner_discovery_all_touches_v1 import recovery_index

SCHEMA = 'FIRST_TOUCH_RECOVERY_BATCHES_V12'
DEFAULT_ROOT = 'reports/reaction_lab/first_touch_historical_recovery_v11'


def audit_symbols(audit_path):
    with Path(audit_path).open(encoding='utf-8', newline='') as f:
        reader = csv.DictReader(f)
        needed = {'touch_key', 'symbol', 'status', 'ft_state', 'audit_state'}
        if not needed.issubset(reader.fieldnames or []):
            raise ValueError('Invalid frozen audit columns')
        return {r['symbol'] for r in reader if r['status'] in ALLOWED_STATUSES
                and r['ft_state'] in ALLOWED_STATES and r['audit_state'] == 'NO_AUTO_MATCH'}


def build_batches(events, completed, *, max_events=300, max_pages=60, max_minutes=70000):
    """Greedy whole-symbol batches; exclude completed and partially done symbols."""
    by_symbol = defaultdict(list)
    for event in events:
        by_symbol[event['symbol']].append(event)

    partial = []
    oversized = []
    pending = {}
    for symbol, group in sorted(by_symbol.items()):
        keys = {r['touch_key'] for r in group}
        old = keys & completed
        if old:
            if len(old) != len(keys):
                partial.append({'symbol': symbol, 'completed': len(old), 'pending': len(keys) - len(old)})
            continue
        spans, pages = plan_requests(group)
        minutes = sum((end - start) // 60000 for _, start, end in spans)
        if len(group) > max_events or len(pages) > max_pages or minutes > max_minutes:
            oversized.append({'symbol': symbol, 'events': len(group), 'pages': len(pages), 'minutes': minutes})
            continue
        pending[symbol] = group

    batches = []
    current = []
    for symbol, group in pending.items():
        candidate = current + group
        spans, pages = plan_requests(candidate)
        minute_count = sum((end - start) // 60000 for _, start, end in spans)
        if current and (len(candidate) > max_events or len(pages) > max_pages or minute_count > max_minutes):
            batches.append(_describe(current))
            current = group[:]
        else:
            current = candidate
    if current:
        batches.append(_describe(current))
    return batches, partial, oversized


def _describe(rows):
    windows, pages = plan_requests(rows)
    symbols = sorted({r['symbol'] for r in rows})
    minutes = sum((end - start) // 60000 for _, start, end in windows)
    key_material = '\n'.join(sorted(r['touch_key'] for r in rows))
    batch_id = hashlib.sha256(key_material.encode('utf-8')).hexdigest()[:12]
    return {'name': 'batch_' + batch_id, 'symbols': symbols,
            'events': len(rows), 'pages': len(pages),
            'unique_symbol_minutes': minutes, 'merged_windows': len(windows)}


def choose_batches(batches, max_batches, max_total_pages, max_total_minutes):
    chosen = []
    pages = minutes = 0
    for batch in batches:
        if len(chosen) >= max_batches:
            break
        if pages + batch['pages'] > max_total_pages or minutes + batch['unique_symbol_minutes'] > max_total_minutes:
            break
        chosen.append(batch)
        pages += batch['pages']
        minutes += batch['unique_symbol_minutes']
    return chosen


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path.cwd())
    parser.add_argument('--audit-events', type=Path, required=True)
    parser.add_argument('--recovery-root', type=Path, default=Path(DEFAULT_ROOT))
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--plan', action='store_true')
    action.add_argument('--run', action='store_true')
    parser.add_argument('--max-batches', type=int, default=5)
    parser.add_argument('--max-total-pages', type=int, default=300)
    parser.add_argument('--max-total-symbol-minutes', type=int, default=350000)
    parser.add_argument('--max-events-per-batch', type=int, default=300)
    parser.add_argument('--max-pages-per-batch', type=int, default=60)
    parser.add_argument('--max-symbol-minutes-per-batch', type=int, default=70000)
    parser.add_argument('--min-free-mb', type=int, default=8192)
    parser.add_argument('--pause', type=float, default=0.5)
    parser.add_argument('--timeout', type=float, default=30)
    args = parser.parse_args(argv)
    if any(getattr(args, name) <= 0 for name in (
            'max_batches', 'max_total_pages', 'max_total_symbol_minutes',
            'max_events_per_batch', 'max_pages_per_batch', 'max_symbol_minutes_per_batch',
            'min_free_mb')) or args.pause < 0 or args.timeout <= 0:
        parser.error('Invalid safety limits')

    project = args.project.resolve()
    audit = (project / args.audit_events).resolve()
    root = (project / args.recovery_root).resolve()
    if not audit.is_file():
        parser.error(f'Audit file missing: {audit}')
    if root == project or not root.is_relative_to(project / ROOT):
        parser.error('Recovery output must be under project/reports/reaction_lab')
    symbols = sorted(audit_symbols(audit))
    current, rejected, frozen = load_selected(
        audit, project / ROOT / 'first_touch_paths_v2.sqlite3', symbols,
        int(time.time()*1000))
    completed, completed_batches = recovery_index(root)
    batches, partial, oversized = build_batches(
        current, set(completed), max_events=args.max_events_per_batch,
        max_pages=args.max_pages_per_batch, max_minutes=args.max_symbol_minutes_per_batch)
    chosen = choose_batches(batches, args.max_batches, args.max_total_pages,
                             args.max_total_symbol_minutes)
    summary = {
        'schema': SCHEMA, 'mode': 'PLAN' if args.plan else 'RUN',
        'audit_eligible': frozen, 'current_eligible': len(current),
        'already_full_360': len(completed), 'completed_batches': completed_batches,
        'rejected_current': rejected, 'partial_symbols_skipped': partial,
        'oversized_symbols_skipped': oversized, 'batches_remaining': len(batches),
        'next_batches': chosen, 'selected_events': sum(x['events'] for x in chosen),
        'selected_pages': sum(x['pages'] for x in chosen),
        'selected_unique_symbol_minutes': sum(x['unique_symbol_minutes'] for x in chosen),
        'first_touch_sqlite_read_only': True,
    }
    print('FT_RECOVERY_BATCHES_PLAN ' + json.dumps(summary, sort_keys=True, ensure_ascii=False), flush=True)
    if args.plan or not chosen:
        return 0

    if not (project / 'reaction_lab_first_touch_historical_recovery_v11.py').is_file():
        raise SystemExit('Missing V1.1 original recoverer')
    for idx, batch in enumerate(chosen, 1):
        if shutil.disk_usage(project).free < args.min_free_mb * 1024**2:
            raise SystemExit('BATCH_STOP: disk space below required minimum')
        destination = root / batch['name']
        if destination.exists():
            raise SystemExit(f'BATCH_STOP: output exists, refusing overwrite: {destination}')
        print(f'FT_RECOVERY_BATCH_START {idx}/{len(chosen)} ' + json.dumps(batch, sort_keys=True), flush=True)
        single_batch_main([
            '--project', str(project), '--audit-events', str(audit),
            '--symbols', ','.join(batch['symbols']), '--max-events', str(args.max_events_per_batch),
            '--max-pages', str(args.max_pages_per_batch),
            '--max-symbol-minutes', str(args.max_symbol_minutes_per_batch),
            '--pause', str(args.pause), '--timeout', str(args.timeout),
            '--out', str(destination), '--run',
        ])
        # Verify on-disk results before continuing to the next group.
        manifest = destination / 'manifest.json'
        if not manifest.is_file():
            raise SystemExit(f'BATCH_STOP: no verified output: {destination}')
        loaded = json.loads(manifest.read_text(encoding='utf-8'))
        counts = loaded.get('counts', {})
        if counts.get('state_FULL_360', 0) != loaded.get('selected_events'):
            raise SystemExit(f'BATCH_STOP: incomplete batch needs review: {destination}')
        print(f'FT_RECOVERY_BATCH_DONE {idx}/{len(chosen)} '
              + json.dumps({'out_dir': str(destination), 'events': counts.get('state_FULL_360')},sort_keys=True),flush=True)
    print('FT_RECOVERY_BATCHES_FINISHED '
          + json.dumps({'batches': len(chosen), 'events': sum(b['events'] for b in chosen)},sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
