#!/usr/bin/env python3
"""Strict V1 vs experimental V2 event parity gate (never modifies either run)."""
from __future__ import annotations
import argparse
import csv
import json
from pathlib import Path
import sys

FIELDS = ('event_key', 'symbol', 'side', 'retest_ts', 'known_ts',
          'first_replay_observed_ms', 'observation_type', 'source_hash', 'causal_json')


def load_run(folder):
    summary_path = folder / 'summary.json'
    events_path = folder / 'replay_events.csv'
    summary = json.loads(summary_path.read_text(encoding='utf-8'))
    with events_path.open(newline='', encoding='utf-8') as fh:
        reader = csv.DictReader(fh)
        missing = set(FIELDS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f'{events_path}: missing fields {sorted(missing)}')
        rows = {}
        for row in reader:
            key = row['event_key']
            if key in rows:
                raise ValueError(f'{events_path}: duplicate event_key {key}')
            try:
                canonical = json.loads(row['causal_json'])
                # Canonical text distinguishes boolean true from numeric 1.
                row['causal_json'] = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
            except ValueError as exc:
                raise ValueError(f'Invalid causal_json: {key}') from exc
            rows[key] = {field: row[field] for field in FIELDS}
    return summary, rows


def compare(baseline_dir, candidate_dir, subset=None):
    a, left = load_run(baseline_dir)
    b, right = load_run(candidate_dir)
    expected = ('start_ms', 'end_ms', 'step_minutes', 'canonical_hash', 'mode')
    incompatible = {x: (a.get(x), b.get(x)) for x in expected if a.get(x) != b.get(x)}
    if incompatible:
        raise ValueError(f'Incompatible baseline/candidate windows/hash/mode: {incompatible}')
    if a.get('mode') != 'research':
        raise ValueError('Only mode=research is currently supported by this comparison gate')
    if subset is not None:
        symbols = {line.strip().upper() for line in subset.read_text().splitlines() if line.strip()}
        if not symbols:
            raise ValueError('Empty subset file')
        if b.get('symbols_total') != len(symbols):
            raise ValueError('Candidate symbols_total does not match subset')
        left = {k:v for k,v in left.items() if v['symbol'] in symbols}
        if any(r['symbol'] not in symbols for r in right.values()):
            raise ValueError('Candidate includes event outside requested subset')
    elif a.get('symbols_total') != b.get('symbols_total'):
        raise ValueError('Different universe sizes: use --symbols-file to compare a subset')
    if b.get('symbols_failed') != 0 or b.get('data_gaps') != 0:
        raise ValueError('Candidate has failed symbols or data gaps')
    missing = sorted(left.keys() - right.keys())
    added = sorted(right.keys() - left.keys())
    altered = [k for k in sorted(left.keys() & right.keys()) if left[k] != right[k]]
    result = {
        'baseline_events': len(left), 'candidate_events': len(right),
        'missing_event_keys': len(missing), 'added_event_keys': len(added),
        'changed_rows_including_timestamps_and_causal_json': len(altered),
        'strict_parity': not (missing or added or altered),
        'examples_missing': missing[:5], 'examples_added': added[:5],
        'examples_changed': altered[:5],
        'candidate_engine': b.get('historical_replay_engine'),
    }
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--baseline', type=Path, required=True)
    ap.add_argument('--candidate', type=Path, required=True)
    ap.add_argument('--symbols-file', type=Path, default=None,
                    help='Optional deterministic subset (baseline may include more symbols)')
    args = ap.parse_args(argv)
    try:
        result = compare(args.baseline, args.candidate, args.symbols_file)
    except Exception as exc:
        print(f'PARITY GATE FAILED: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['strict_parity'] else 3


if __name__ == '__main__':
    sys.exit(main())
