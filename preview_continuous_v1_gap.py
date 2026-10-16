#!/usr/bin/env python3
"""Read-only, exploratory Continuous V1 missing-window audit.

Runs native and latest-pivot selection on the same historical candles, compares
IDs to the production journal in SQLite read-only, and flags swing collisions.
The output is NOT a validated backfill and must NEVER be marked LIVE/FORWARD.
Does not bypass the strict parity gate in the canonical replay program.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys

FIELDS = ('pivot_timestamp','actionable_timestamp','departure_timestamp',
          'swing_price','entry_price','max_departure_pct','retest_distance_pct')


def atomic_json(path, value):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False), encoding='utf-8')
    os.replace(tmp, path)


def csv_write(path, fields, rows):
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w', newline='', encoding='utf-8') as out:
        writer = csv.DictWriter(out, fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


def cmp_payload(a, b):
    if a is None or b is None:
        return ['ONLY_ONE_POLICY']
    diffs = []
    for k in FIELDS:
        x, y = a.get(k), b.get(k)
        if x is None and y is None:
            continue
        try:
            import math
            same = math.isclose(float(x), float(y), rel_tol=1e-8, abs_tol=1e-8*max(1, abs(float(x)), abs(float(y))))
        except (TypeError, ValueError, OverflowError):
            same = x == y
        if not same:
            diffs.append(k)
    return diffs


def bundle_event(event):
    """Store only the causal payload and first virtual observation; no outcomes."""
    return {'first_replay_observed_ms': event['first_replay_observed_ms'], 'payload': event['payload']}


def gap_info(candles, tf_ms, start, end):
    """Catch both internal and boundary holes in the queried interval."""
    first = ((start + tf_ms - 1) // tf_ms) * tf_ms
    expected = range(first, end, tf_ms)
    actual = {int(x['timestamp']) for x in candles if first <= int(x['timestamp']) < end}
    missing = [t for t in expected if t not in actual]
    return {'expected_bars': len(expected), 'actual_bars': len(actual),
            'missing_bars': len(missing), 'first_missing_ms': missing[0] if missing else None}


def summarize(native, latest, existing):
    ids = sorted(set(native) | set(latest))
    rows = []
    for key in ids:
        a, b = native.get(key), latest.get(key)
        pa = a['payload'] if a else None
        pb = b['payload'] if b else None
        diffs = cmp_payload(pa, pb)
        symbol, side, ts = key.split('|', 2)
        if diffs:
            quality = 'POLICY_DISAGREEMENT'
        else:
            quality = 'AGREEMENT_UNVERIFIED'
        rows.append({
            'event_key': key, 'symbol': symbol, 'side': side, 'retest_ts': int(ts),
            'already_in_journal': int(key in existing),
            'native_present': int(a is not None), 'latest_present': int(b is not None),
            'quality': quality, 'different_fields': ';'.join(diffs),
            'native_pivot_ts': pa.get('pivot_timestamp') if pa else '',
            'latest_pivot_ts': pb.get('pivot_timestamp') if pb else '',
            'native_departure_pct': pa.get('max_departure_pct') if pa else '',
            'latest_departure_pct': pb.get('max_departure_pct') if pb else '',
            'native_causal_json': json.dumps(pa, ensure_ascii=False, allow_nan=False) if pa else '',
            'latest_causal_json': json.dumps(pb, ensure_ascii=False, allow_nan=False) if pb else '',
        })
    missing = [r for r in rows if not r['already_in_journal']]
    quality_counts = {q: sum(r['quality'] == q for r in missing) for q in ('AGREEMENT_UNVERIFIED','POLICY_DISAGREEMENT')}
    return rows, missing, quality_counts


def get_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--project', type=Path, default=Path('/home/multiservices-1m'))
    ap.add_argument('--replay-module', type=Path, default=Path('/root/v1_collision_experiment/replay_continuous_v1.py'))
    ap.add_argument('--start', required=True, help='Inclusive timezone-aware ISO timestamp')
    ap.add_argument('--end', required=True, help='Exclusive timezone-aware ISO timestamp')
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--cache-dir', type=Path, default=Path('/root/continuous_v1_klines_cache'))
    ap.add_argument('--step-minutes', type=int, default=15)
    ap.add_argument('--max-symbols', type=int, default=0, help='Pilot only, 0 for entire canonical universe')
    ap.add_argument('--resume', action='store_true')
    return ap.parse_args(argv)


def main(argv=None):
    a = get_args(argv)
    replay_path = a.replay_module.resolve()
    if not replay_path.is_file():
        raise RuntimeError(f'Experimental replay module missing: {replay_path}')
    spec = importlib.util.spec_from_file_location('read_only_experiment', replay_path)
    R = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(R)
    for api in ('fetch_klines', 'run_symbol', 'checkpoints', 'load_journal',
                'iso_to_ms', 'cache_name'):
        if not callable(getattr(R, api, None)):
            raise RuntimeError(f'Incompatible replay module: {api} missing')
    start, end = R.iso_to_ms(a.start), R.iso_to_ms(a.end)
    if end <= start or a.step_minutes < 1 or a.step_minutes > 30:
        raise ValueError('Invalid start/end/step')
    project, output, cache = a.project.resolve(), a.output.resolve(), a.cache_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(project))
    import reaction_lab_continuous_scanner as C
    symbols = C.get_symbols(project)
    if a.max_symbols:
        symbols = symbols[:a.max_symbols]
    scanner, fingerprint = C.compile_canonical_scanner(project / 'dashboard/app.py', lambda **kw: None)
    journal_path = project / 'reports/reaction_lab/scanner_events_v1.sqlite3'
    journal = R.load_journal(journal_path, start, end, symbols)
    ticks = list(R.checkpoints(start, end, a.step_minutes))
    one_start = ((start - 430*R.ONE_MIN)//R.ONE_MIN)*R.ONE_MIN
    fifteen_start = ((start - 410*R.FIFTEEN_MIN)//R.FIFTEEN_MIN)*R.FIFTEEN_MIN
    fetch_end = ticks[-1] + R.FIFTEEN_MIN
    signature_payload = {'start': start, 'end': end, 'step_minutes': a.step_minutes,
                         'project':str(project), 'cache_dir':str(cache),
                         'symbols':symbols, 'source_hash':fingerprint,
                         'replay_module_hash':hashlib.sha256(replay_path.read_bytes()).hexdigest(),
                         'version':1}
    signature = hashlib.sha256(json.dumps(signature_payload, sort_keys=True).encode()).hexdigest()
    manifest = output/'preview_manifest.json'
    states = output/'per_symbol'
    if a.resume:
        if not manifest.is_file() or json.loads(manifest.read_text()).get('signature') != signature:
            raise RuntimeError('Cannot --resume: missing/mismatched manifest; use original options')
    else:
        if manifest.exists() or states.exists():
            raise RuntimeError('Existing output: choose new --output, or --resume')
        states.mkdir()
        atomic_json(manifest, {'signature':signature, 'inputs':signature_payload, 'classification':'RESEARCH_ONLY'})
    print(f'PREVIEW ONLY symbols={len(symbols)} checkpoints={len(ticks)} fingerprint={fingerprint}', flush=True)
    print(f'WINDOW={a.start} -> {a.end}; PRODUCTION SQLITE READ-ONLY; NEVER FORWARD', flush=True)
    native, latest, errors, holes = {}, {}, [], []
    for i, sym in enumerate(symbols,1):
        path = states / (hashlib.sha256(sym.encode()).hexdigest()[:16]+'.json')
        if a.resume and path.is_file():
            record = json.loads(path.read_text(encoding='utf-8'))
            print(f'[{i}/{len(symbols)}] RESUME {sym} {record.get("status")}', flush=True)
        else:
            record = {'symbol':sym, 'status':'failed'}
            try:
                c1, hit1 = R.fetch_klines(sym,'1m',one_start,fetch_end,cache)
                c15, hit15= R.fetch_klines(sym,'15m',fifteen_start,fetch_end,cache)
                g1 = gap_info(c1,R.ONE_MIN,start,end)
                g15= gap_info(c15,R.FIFTEEN_MIN,start,end)
                if g1['missing_bars'] or g15['missing_bars']:
                    record.update(status='incomplete', history_1m=g1, history_15m=g15)
                    print(f'[{i}/{len(symbols)}] INCOMPLETE {sym}: 1m={g1["missing_bars"]} 15m={g15["missing_bars"]}',flush=True)
                else:
                    n = R.run_symbol(sym,scanner,C.normalize_reactions,ticks,c1,c15,start,end,a.step_minutes,'native')
                    l = R.run_symbol(sym,scanner,C.normalize_reactions,ticks,c1,c15,start,end,a.step_minutes,'latest-pivot')
                    record.update(status='ok', native={k:bundle_event(v) for k,v in n.items()},
                                  latest={k:bundle_event(v) for k,v in l.items()})
                    print(f'[{i}/{len(symbols)}] {sym}: native={len(n)} latest={len(l)} cached={hit1}/{hit15}',flush=True)
            except Exception as exc:
                record.update(error=f'{type(exc).__name__}: {exc}')
                print(f'[{i}/{len(symbols)}] FAILED {sym}: {exc}',flush=True)
            atomic_json(path, record)
        if record['symbol'] != sym:
            raise RuntimeError('Resume file symbol mismatch')
        if record['status'] == 'ok':
            for key, v in record['native'].items():
                native[key] = v
            for key, v in record['latest'].items():
                latest[key] = v
        elif record['status'] == 'incomplete':
            holes.append({'symbol':sym, **{'one_min_missing':record['history_1m']['missing_bars'],
                                            'fifteen_min_missing':record['history_15m']['missing_bars']}})
        else:
            errors.append({'symbol':sym, 'error':record.get('error','Unknown failure')})
    rows, missing, quality = summarize(native,latest,journal)
    full_fields = ('event_key','symbol','side','retest_ts','already_in_journal','native_present',
                   'latest_present','quality','different_fields','native_pivot_ts','latest_pivot_ts',
                   'native_departure_pct','latest_departure_pct','native_causal_json','latest_causal_json')
    csv_write(output/'preview_all_ids.csv', full_fields, rows)
    csv_write(output/'preview_missing_ids.csv', full_fields, missing)
    csv_write(output/'preview_policy_disagreements.csv', full_fields,
              [r for r in rows if r['quality']=='POLICY_DISAGREEMENT'])
    csv_write(output/'preview_errors.csv', ('symbol','error'), errors)
    csv_write(output/'preview_gaps.csv', ('symbol','one_min_missing','fifteen_min_missing'), holes)
    summary = {'mode':'GAP_PREVIEW_UNVERIFIED','start':a.start,'end':a.end,'fingerprint':fingerprint,
               'symbols_total':len(symbols),'symbols_complete':len(symbols)-len(errors)-len(holes),
               'symbols_failed':len(errors),'symbols_incomplete':len(holes),
               'journal_ids_in_window':len(journal),
               'replay_union_ids':len(rows),'ids_not_in_journal':len(missing),
               'missing_ids_policy_agreement_unverified':quality['AGREEMENT_UNVERIFIED'],
               'missing_ids_policy_disagreement':quality['POLICY_DISAGREEMENT'],
               'all_replay_ids_policy_disagreement':sum(r['quality']=='POLICY_DISAGREEMENT' for r in rows),
               'canonical_numeric_parity_proven':False,
               'disclaimer':'Exploratory identity count only; variants not validated; not LIVE/FORWARD; no context reconstructed'}
    atomic_json(output/'preview_summary.json',summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if holes or errors:
        print('INCOMPLETE COVERAGE: do not interpret ids_not_in_journal as exhaustive',flush=True)
        return 2
    return 0

if __name__ == '__main__':
    sys.exit(main())
