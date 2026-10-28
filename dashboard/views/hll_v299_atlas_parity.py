#!/usr/bin/env python3
"""HLL V2.9.9 - read-only parity audit of V2.1 atlas for core15 vs 290.

Verify that causal formations and events in the original cohort remain unchanged
when the scanner processes the larger symbol universe. Comparison is limited to
2026-09-07..2026-10-01 UTC, open interval on the right. Historical files are
never mutated. No future outcomes are read and no live systems are accessed.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

VERSION = 'HLL_V299_ATLAS_PARITY'
BASE = Path('reports/horizontal_level_lifecycle_v21/core15_extended_sep_oct_v21')
EXTENDED = Path('reports/horizontal_level_lifecycle_v21/sep290_aug20_oct04_frozen_v21')
OUT = Path('reports/horizontal_level_parity_v299/runs/core15_vs_sep290_v299')
EXCLUDE = {'source_30m'}  # semantic source tag is different between frozen archives


def epoch_ms(iso: str) -> int:
    dt = datetime.fromisoformat(iso.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('UTC timezone suffix required')
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def load_rows(file: Path, key: str, time_col: str, begin: int, end: int):
    if not file.is_file():
        raise FileNotFoundError(file)
    records = {}
    with file.open(newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        if not {key, time_col}.issubset(reader.fieldnames or []):
            raise ValueError(f'Invalid columns for {file}')
        for row in reader:
            ts = int(row[time_col])
            if not begin <= ts < end:
                continue
            item_key = row[key]
            if not item_key or item_key in records:
                raise ValueError(f'Duplicate or empty {key} in {file}: {item_key}')
            records[item_key] = {k: v for k, v in row.items() if k not in EXCLUDE}
    return records


def compare_rows(old, new, typ, sym, max_examples=6):
    a, b = set(old), set(new)
    removed, added = sorted(a - b), sorted(b - a)
    differences = sorted(k for k in a & b if old[k] != new[k])
    examples = []
    for label, keys in [('OLD_ONLY', removed), ('NEW_ONLY', added), ('VALUE_CHANGED', differences)]:
        for k in keys[:max_examples]:
            diff_fields = ''
            if label == 'VALUE_CHANGED':
                diff_fields = ','.join(sorted(f for f in set(old[k]) | set(new[k])
                                              if old[k].get(f) != new[k].get(f)))
            examples.append({'symbol': sym, 'record_type': typ, 'mismatch_type': label,
                             'record_id': k, 'fields': diff_fields})
    return {'old': len(old), 'new': len(new), 'old_only': len(removed),
            'new_only': len(added), 'changed': len(differences)}, examples


def assess(old_dir: Path, new_dir: Path, begin: int, end: int):
    old_cfg = read_json(old_dir / 'run_config.json')
    new_cfg = read_json(new_dir / 'run_config.json')
    old_syms = old_cfg.get('symbols')
    new_syms = new_cfg.get('symbols')
    if not isinstance(old_syms, list) or not isinstance(new_syms, list):
        raise ValueError('Both run_configs must list symbols')
    if len(old_syms) != 15 or len(set(old_syms)) != 15:
        raise ValueError(f'Expected original 15 symbols, got {len(old_syms)}')
    if len(new_syms) != 290 or len(set(new_syms)) != 290:
        raise ValueError(f'Expected extended 290 symbols, got {len(new_syms)}')
    if not set(old_syms).issubset(new_syms):
        raise ValueError(f'Original symbols missing in 290: {sorted(set(old_syms)-set(new_syms))}')
    same_cfg = old_cfg.get('config') == new_cfg.get('config')
    same_version = old_cfg.get('version') == new_cfg.get('version')
    rows, examples = [], []
    for sym in sorted(old_syms):
        counts = {}
        for typ, key, tscol in [('formation', 'level_id', 'formation_closed_ts'),
                                ('events', 'event_id', 'observed_closed_ts')]:
            old = load_rows(old_dir / 'shards' / f'{sym}.{typ}.csv', key, tscol, begin, end)
            new = load_rows(new_dir / 'shards' / f'{sym}.{typ}.csv', key, tscol, begin, end)
            counts[typ], few = compare_rows(old, new, typ, sym)
            examples.extend(few)
        mismatch = sum(counts[t][k] for t in counts for k in ('old_only', 'new_only', 'changed'))
        rows.append({'symbol': sym,
                     **{f'{t}_{k}': v for t, values in counts.items() for k, v in values.items()},
                     'status': 'MATCH' if not mismatch else 'MISMATCH'})
    summary = {'version': VERSION, 'baseline': str(old_dir), 'extended': str(new_dir),
               'begin_ms': begin, 'end_ms_exclusive': end,
               'config_equal': same_cfg, 'code_version_equal': same_version,
               'original_symbols': len(old_syms), 'extended_symbols': len(new_syms),
               'matched_symbols': sum(x['status'] == 'MATCH' for x in rows),
               'mismatch_symbols': sum(x['status'] != 'MATCH' for x in rows),
               'formations_checked_original': sum(x['formation_old'] for x in rows),
               'events_checked_original': sum(x['events_old'] for x in rows),
               'formations_checked_extended': sum(x['formation_new'] for x in rows),
               'events_checked_extended': sum(x['events_new'] for x in rows),
               'mismatch_by_type': {t: {k:sum(x[f'{t}_{k}'] for x in rows)
                                           for k in ('old_only', 'new_only', 'changed')}
                                    for t in ('formation', 'events')},
               'note': 'Comparison excludes source_30m provenance string and ends shards. Only timestamps in [begin,end).'
               }
    summary['pass'] = bool(same_cfg and same_version and summary['mismatch_symbols'] == 0)
    return summary, rows, examples


def atomic_csv(path: Path, rows, cols):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', newline='', dir=path.parent,
                                     prefix='.tmp_', delete=False) as tmp:
        name = tmp.name
        wr = csv.DictWriter(tmp, fieldnames=cols)
        wr.writeheader()
        wr.writerows(rows)
    os.replace(name, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument('--plan', action='store_true')
    group.add_argument('--run', action='store_true')
    ap.add_argument('--baseline', type=Path, default=BASE)
    ap.add_argument('--extended', type=Path, default=EXTENDED)
    ap.add_argument('--output', type=Path, default=OUT)
    ap.add_argument('--start-utc', default='2026-09-07T00:00:00Z')
    ap.add_argument('--end-utc', default='2026-10-01T00:00:00Z')
    args = ap.parse_args(argv)
    begin, end = epoch_ms(args.start_utc), epoch_ms(args.end_utc)
    if begin >= end:
        ap.error('start must precede end')
    if args.baseline.resolve() == args.extended.resolve():
        ap.error('Cannot compare directory with itself')
    cfg = {'version': VERSION, 'baseline': str(args.baseline), 'extended': str(args.extended),
           'start_utc': args.start_utc, 'end_utc_exclusive': args.end_utc,
           'baseline_manifest_found': (args.baseline / 'run_config.json').is_file(),
           'extended_manifest_found': (args.extended / 'run_config.json').is_file(),
           'output': str(args.output), 'mode': 'READ_ONLY_PLAN' if args.plan else 'PARITY_AUDIT'}
    print('HLL_V299_PLAN', json.dumps(cfg, ensure_ascii=False), flush=True)
    if args.plan:
        return 0
    if args.output.exists():
        raise SystemExit('OUTPUT_EXISTS: refusing overwrite, use a new --output')
    summary, rows, examples = assess(args.baseline, args.extended, begin, end)
    args.output.mkdir(parents=True, exist_ok=False)
    atomic_csv(args.output / 'parity_by_symbol.csv', rows, list(rows[0]))
    atomic_csv(args.output / 'mismatch_examples.csv', examples,
               ['symbol','record_type','mismatch_type','record_id','fields'])
    (args.output / 'parity_summary.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding='utf-8')
    print('HLL_V299_PARITY', json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if summary['pass'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
