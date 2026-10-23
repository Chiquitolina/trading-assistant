#!/usr/bin/env python3
"""Offline PRE-Reaction pilot parity: independent native 15m and scanner journal.

Read-only inputs. Does not claim missing observed scanner records mean false positives.
Creates only a new parity report directory, never overwrites pilot or LIVE data.
"""
from __future__ import annotations
import argparse
from collections import Counter
import csv
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from pre_reaction_opportunity_lab import (
    load_native_1m, contiguous_15m_from_1m, sha, iso_ms, ro_conn,
)
VERSION = 'pre-reaction-pilot-parity-v1.0'
FIELDS = ('open', 'high', 'low', 'close', 'volume')


def price_close(a, b, field):
    # Comparable OHLCV from independently sourced native 15m bars.
    return math.isclose(float(a), float(b), rel_tol=2e-6 if field != 'volume' else 1e-4,
                        abs_tol=1e-10 if field != 'volume' else 1e-6)


def compare_native_15m(aggregated, native):
    """One row per aggregated complete 15m interval. Missing native != mismatch."""
    by_time = {int(r['open_ts']): r for r in native}
    output = []
    for r in aggregated:
        ts = int(r['timestamp'])
        target = by_time.get(ts)
        if target is None:
            output.append({'open_ts': ts, 'status': 'MISSING_NATIVE_15M', 'bad_fields': ''})
            continue
        bad = [k for k in FIELDS if not price_close(r[k], target[k], k)]
        output.append({'open_ts': ts, 'status': 'MISMATCH' if bad else 'MATCH',
                       'bad_fields': ','.join(bad)})
    return output


def reaction_journal_comparison(opportunities, observed):
    """Match by exact symbol, side and retest time. Never mark unmatched as failure.

    Scanner journal is observational and may have missing coverage. A same-minute
    match demonstrates identity alignment, not complete coverage of all events.
    """
    journal_keys = {(str(r['symbol']), str(r['side']), int(r['retest_ts'])) for r in observed}
    rows = []
    for item in opportunities:
        if item['status'] != 'REACTION':
            continue
        touch = int(item['first_touch_ts'])
        key = (str(item['symbol']), str(item['side']), touch)
        rows.append({'opportunity_key': str(item['opportunity_key']), 'symbol': key[0],
                     'side': key[1], 'first_touch_ts': touch,
                     'scanner_status': 'EXACT_SCANNER_MATCH' if key in journal_keys
                     else 'NOT_IN_OBSERVED_SCANNER_JOURNAL'})
    return rows


def write_csv(path, rows, columns):
    with path.open('w', newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def checked_pilot(root, pilot):
    meta_path = pilot / 'manifest.json'
    csv_path = pilot / 'opportunities_PRE.csv'
    if not meta_path.is_file() or not csv_path.is_file():
        raise FileNotFoundError(f'Missing frozen pilot manifest or CSV: {pilot}')
    meta = json.loads(meta_path.read_text())
    if sha(csv_path) != meta['opportunities_sha256']:
        raise RuntimeError('Pilot CSV SHA differs from frozen manifest')
    for raw, digest in meta.get('source_hashes', {}).items():
        p = Path(raw)
        if not p.is_file():
            p = root / raw
        if not p.is_file() or sha(p) != digest:
            raise RuntimeError(f'Historical 1m source absent/changed since pilot: {p}')
    if not meta.get('symbols'):
        raise RuntimeError('No symbols in frozen pilot')
    return meta, csv_path


def scanner_events(con, symbols, start, end):
    q = ','.join('?' for _ in symbols)
    return [dict(zip(('symbol', 'side', 'retest_ts', 'observation_type'), r))
            for r in con.execute(
                f'SELECT symbol, side, retest_ts, observation_type FROM events WHERE symbol IN ({q}) AND retest_ts>=? AND retest_ts<? ORDER BY retest_ts',
                [*symbols, start, end]
            ).fetchall()]


def run(root, pilot, output, cache, plan=False):
    meta, csv_path = checked_pilot(root, pilot)
    start = iso_ms(meta['start_utc']); end = iso_ms(meta['end_exclusive_utc'])
    symbols = meta['symbols']
    candle_db = root / 'reports/reaction_lab/reaction_full_context_v1.sqlite3'
    scanner_db = root / 'reports/reaction_lab/scanner_events_v1.sqlite3'
    if not candle_db.is_file() or not scanner_db.is_file():
        raise FileNotFoundError('Missing canonical native15m or scanner journal SQLite')
    if plan:
        print('PRE_REACTION_PARITY_PLAN', json.dumps({'version': VERSION, 'pilot': str(pilot),
            'symbols': symbols, 'native_15m_db': str(candle_db), 'scanner_db': str(scanner_db),
            'output': str(output), 'read_only_inputs': True}))
        return
    if output.exists():
        raise RuntimeError(f'Immutable report already exists: {output}')
    import pandas as pd
    opp = pd.read_csv(csv_path).to_dict('records')
    detailed = []
    symbol_summary = []
    for symbol in symbols:
        base = start-3*60*60*1000
        data_end = end+360*60*1000+60_000
        mins, _ = load_native_1m(root, cache, symbol, base, data_end)
        fifteen = contiguous_15m_from_1m(mins)
        if fifteen.empty:
            raise RuntimeError(f'No complete 15m reconstructed from 1m: {symbol}')
        lo=int(fifteen.timestamp.min());hi=int(fifteen.timestamp.max())+900_000
        with ro_conn(candle_db) as con:
            native = [dict(zip(('open_ts', *FIELDS), r)) for r in con.execute('''
                SELECT open_ts,open,high,low,close,volume FROM candles
                WHERE symbol=? AND timeframe='15m' AND open_ts>=? AND open_ts<? ORDER BY open_ts
            ''',(symbol,lo,hi))]
        records=compare_native_15m(fifteen.to_dict('records'),native)
        detailed.extend(dict(symbol=symbol, **v) for v in records)
        counts=Counter(v['status'] for v in records)
        symbol_summary.append({'symbol': symbol, 'agg_15m':len(fifteen),
                               'native_15m_in_range':len(native),'match':counts['MATCH'],
                               'mismatch':counts['MISMATCH'], 'missing_native':counts['MISSING_NATIVE_15M']})
    with ro_conn(scanner_db) as con:
        seen=scanner_events(con, symbols, start, end)
    mapped = reaction_journal_comparison(opp, seen)
    outputs=Counter(row['scanner_status'] for row in mapped)
    coverage={'version': VERSION, 'pilot':str(pilot),'events_scanner_journal_in_window':len(seen),
              'pilot_total':len(opp), 'pilot_statuses':dict(Counter(r['status'] for r in opp)),
              'pilot_reaction_candidates':len(mapped),'exact_matches':outputs['EXACT_SCANNER_MATCH'],
              'not_in_observed_scanner_journal':outputs['NOT_IN_OBSERVED_SCANNER_JOURNAL'],
              '15m_by_symbol':symbol_summary,
              'interpretation_note': 'Absent scanner journal event is NOT a proven mismatch. Scanner is an observational, non-exhaustive journal; this audit is diagnostic, not canonical full-replay parity.',
              'limits':['Native 15m cache may be sparse; missing candles are not mismatches.',
                        'Pilot reaction classification must be independently replayed through extracted canonical scanner before claiming event-by-event parity.']}
    output.mkdir(parents=True)
    native_path=output/'native_15m_comparison.csv'
    scanner_path=output/'scanner_reaction_matches.csv'
    write_csv(native_path,detailed,('symbol','open_ts','status','bad_fields'))
    write_csv(scanner_path,mapped,('opportunity_key','symbol','side','first_touch_ts','scanner_status'))
    coverage['inputs_sha256']={'pilot':sha(csv_path),'native_db':sha(candle_db),'scanner_db':sha(scanner_db)}
    coverage['file_sha256']={'native_comparison':sha(native_path), 'scanner_matches':sha(scanner_path)}
    (output/'parity_report.json').write_text(json.dumps(coverage,indent=2,ensure_ascii=False))
    print('PRE_REACTION_PARITY_RESULT', json.dumps({k:coverage[k] for k in ('pilot_total','pilot_reaction_candidates','exact_matches','not_in_observed_scanner_journal','15m_by_symbol')},ensure_ascii=False))
    print('PRE_REACTION_PARITY_PATH',output)


def audit(output):
    meta=json.loads((output/'parity_report.json').read_text())
    for name,file in [('native_comparison','native_15m_comparison.csv'),('scanner_matches','scanner_reaction_matches.csv')]:
        if sha(output/file)!=meta['file_sha256'][name]:
            raise RuntimeError(f'Parity CSV integrity failed: {name}')
    with (output/'native_15m_comparison.csv').open(newline='') as fh:
        statuses=Counter(r['status'] for r in csv.DictReader(fh))
    expected=sum(s['match']+s['mismatch']+s['missing_native'] for s in meta['15m_by_symbol'])
    if sum(statuses.values())!=expected:
        raise RuntimeError('Native 15m row totals differ from report')
    with (output/'scanner_reaction_matches.csv').open(newline='') as fh:
        scanner_count=sum(1 for _ in csv.DictReader(fh))
    if scanner_count!=meta['pilot_reaction_candidates']:
        raise RuntimeError('Scanner match row totals differ')
    print('PRE_REACTION_PARITY_AUDIT',json.dumps({'success':True,'rows_15m':expected,'pilot_reaction_candidates':scanner_count,
        'native_statuses':dict(statuses),'folder':str(output)}))


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ops=ap.add_mutually_exclusive_group(required=True)
    ops.add_argument('--plan',action='store_true');ops.add_argument('--run',action='store_true');ops.add_argument('--audit',action='store_true')
    ap.add_argument('--project',type=Path,default=Path.cwd())
    ap.add_argument('--pilot',type=Path,default=Path('reports/reaction_lab/pre_reaction_opportunity_lab_v0/pilot_20261012_20261013_HANAUSDT_TAKEUSDT_ETHUSDT'))
    ap.add_argument('--cache',type=Path,default=Path('reports/reaction_lab/pre_reaction_opportunity_lab_v0/native_1m_pilot_cache'))
    ap.add_argument('--output',type=Path,default=Path('reports/reaction_lab/pre_reaction_opportunity_lab_v0/parity_20261012_v1'))
    args=ap.parse_args();root=args.project.resolve()
    def resolve(p):return (p if p.is_absolute() else root/p).resolve()
    pilot=resolve(args.pilot);output=resolve(args.output)
    if args.audit:audit(output)
    else:run(root,pilot,output,args.cache,plan=args.plan)

if __name__=='__main__':main()
