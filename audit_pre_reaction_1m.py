#!/usr/bin/env python3
"""Auditoria SOLO LECTURA de 1m antes de REACTION.

Origen 1m: cache mensual Binance USD-M del replay Candidate Long Horizon:
  reports/candidate_long_horizon/candle_cache/1m/SYMBOL/YYYY-MM.pkl.gz
Cruza con el scanner causal y contrasta OHLC compartidos contra auto_paths.
No descarga, completa, escribe ni modifica ningun dato.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import math
from pathlib import Path
import sqlite3
import struct
import zlib

ONE = 60_000
BAR = struct.Struct('<qdddd')
NEEDED = frozenset(('timestamp','open','high','low','close'))


def ro_db(path: Path):
    if not path.is_file():
        raise FileNotFoundError(path)
    return sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=10)


def month(ts: int):
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime('%Y-%m')


def date(ts: int):
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime('%Y-%m-%d')


def closed_window(asof: int, length: int):
    """Conservador: usar velas cuyo minuto termino antes del asof."""
    last = (int(asof) // ONE - 1) * ONE
    return [last - i*ONE for i in range(length-1,-1,-1)]


def cache_for_symbol(cache_dir: Path, symbol: str, event_times: list[int]):
    """Carga solo particiones mensuales relevantes; un simbolo por vez."""
    import pandas as pd
    needed_months = set()
    for ts in event_times:
        for x in closed_window(ts, 60):
            needed_months.add(month(x))
        # Para paridad se requieren hasta 360 minutos despues del retest.
        needed_months.add(month(ts + 360*ONE))
    lookup = {}
    source_files = []
    issues = Counter()
    for m in sorted(needed_months):
        file = cache_dir / symbol / (m+'.pkl.gz')
        if not file.is_file():
            issues['missing_month_files'] += 1
            continue
        source_files.append(str(file))
        try:
            df = pd.read_pickle(file, compression='gzip')
            if not NEEDED.issubset(df.columns):
                issues['invalid_schema_files'] += 1
                continue
            for rec in df[['timestamp','open','high','low','close']].itertuples(index=False, name=None):
                try:
                    t = int(rec[0]); vals = tuple(float(v) for v in rec[1:])
                    o,h,l,c = vals
                    if (t % ONE or not all(math.isfinite(v) and v>0 for v in vals)
                        or h < max(o,l,c) or l>min(o,h,c)):
                        issues['bad_bars'] += 1
                        continue
                    if t in lookup:
                        if lookup[t] != vals:
                            issues['conflicting_duplicate_bars'] += 1
                        else:
                            issues['duplicate_bars'] += 1
                    else:
                        lookup[t] = vals
                except (TypeError, ValueError, OverflowError):
                    issues['bad_bars'] += 1
        except Exception as exc:
            issues['unreadable_files'] += 1
            print('ERROR_CACHE', file, type(exc).__name__, str(exc)[:150])
    return lookup, source_files, issues


def select_events(rows, sample_per_day: int):
    if sample_per_day == 0:
        return rows
    groups = defaultdict(list)
    for row in rows:
        groups[date(int(row[3]))].append(row)
    sampled = []
    for day, events in sorted(groups.items()):
        # Recorrer eventos a distancias uniformes en tiempo, no solo los primeros.
        events = sorted(events, key=lambda r: (int(r[3]), r[0]))
        n = min(len(events),sample_per_day)
        if n:
            sampled.extend(events[(i*len(events))//n] for i in range(n))
    return sampled


def audit(project: Path, sample_per_day: int):
    scanner = project / 'reports/reaction_lab/scanner_events_v1.sqlite3'
    paths = project / 'reports/reaction_lab/auto_paths_v1.sqlite3'
    cache = project / 'reports/candidate_long_horizon/candle_cache/1m'
    if not scanner.is_file():
        raise FileNotFoundError(scanner)

    with ro_db(scanner) as con:
        all_rows = con.execute('''SELECT event_key,symbol,side,retest_ts,known_ts,
            observation_type FROM events WHERE retest_ts IS NOT NULL
            ORDER BY retest_ts,event_key''').fetchall()
    events = select_events(all_rows, sample_per_day)
    print('SOURCE_CACHE', cache, 'exists=',cache.is_dir())
    print('SCANNER_TOTAL',len(all_rows),'AUDITED',len(events),
          'MODE', 'FULL' if sample_per_day == 0 else 'SAMPLE')
    print('TIMESPAN_UTC',date(all_rows[0][3]) if all_rows else '-',
          '->',date(all_rows[-1][3]) if all_rows else '-')
    print('NOTE: pre-retest=60 cierres anteriores al timestamp del retest;')
    print('      pre-known=60 cierres anteriores al known_ts, sin velas incompletas.')
    by_symbol = defaultdict(list)
    for row in events: by_symbol[row[1]].append(row)
    outcomes = Counter()
    per_day = defaultdict(Counter)
    per_origin = defaultdict(Counter)
    file_issues = Counter()
    bad_parity = []
    comparable = identical = differences = 0
    sample_lags = Counter()
    path_con = ro_db(paths) if paths.is_file() else None
    try:
        for symbol, evs in sorted(by_symbol.items()):
            timestamps = [int(r[3]) for r in evs]
            timestamps += [int(r[4]) for r in evs if r[4] is not None]
            bars, filenames, issues = cache_for_symbol(cache,symbol,timestamps)
            file_issues.update(issues)
            for key,sym,side,retest,known,origin in evs:
                day = date(retest)
                for group in (outcomes, per_day[day],per_origin[str(origin)]):
                    group['events'] += 1
                for label,asof in [('retest',retest),('known',known)]:
                    if asof is None:
                        outcome = 'UNKNOWN_TIMESTAMP'
                    else:
                        wants = closed_window(int(asof),60)
                        present = sum(t in bars for t in wants)
                        outcome = 'COMPLETE_60' if present == 60 else ('PARTIAL_60' if present else 'NONE_60')
                    outcomes[label+'_'+outcome] += 1
                    per_day[day][label+'_'+outcome] += 1
                    per_origin[str(origin)][label+'_'+outcome] += 1
                if known is not None:
                    diff = round((int(known)-int(retest))/ONE)
                    if diff < -5: sample_lags['known_before_retest_over_5m'] += 1
                    if diff > 5: sample_lags['known_after_retest_over_5m'] += 1
            # Comparar OHLC del cache con auto-paths, max 2 eventos por simbolo en la muestra.
            if path_con and bars:
                candidates = path_con.execute('''SELECT event_key,entry_ts,count,path_blob
                    FROM events WHERE symbol=? AND state='COMPLETE'
                    ORDER BY entry_ts DESC LIMIT 3''',(symbol,)).fetchall()
                matched = 0
                for ek, entry, count, blob in candidates:
                    if blob is None:continue
                    try:
                        raw = zlib.decompress(blob)
                        if len(raw) != BAR.size*int(count):
                            file_issues['bad_autopath_length']+=1;continue
                        for t,o,h,l,c in BAR.iter_unpack(raw):
                            if t not in bars: continue
                            comparable += 1
                            got = bars[t]
                            src = (o,h,l,c)
                            if all(math.isclose(g,s,rel_tol=1e-8,abs_tol=1e-10)
                                   for g,s in zip(got,src)):
                                identical+=1
                            else:
                                differences+=1
                                if len(bad_parity)<12:
                                    bad_parity.append((ek,t,tuple(round(a,10) for a in got),
                                                       tuple(round(a,10) for a in src)))
                        matched+=1
                    except (ValueError,TypeError,zlib.error,struct.error):
                        file_issues['autopath_decode_error']+=1
                    if matched>=2:break
    finally:
        if path_con: path_con.close()

    print('\n=== COVERAGE 60 CLOSED 1M BARS ===')
    for label in ('retest','known'):
        print(label, {k:outcomes[label+'_'+k] for k in
            ('COMPLETE_60','PARTIAL_60','NONE_60','UNKNOWN_TIMESTAMP')})
    print('\n=== BY RETEST DATE UTC ===')
    for d,s in sorted(per_day.items()):
        print(d,'N=',s['events'],'pre_retest_complete=',s['retest_COMPLETE_60'],
              'pre_known_complete=',s['known_COMPLETE_60'])
    print('\n=== BY OBSERVATION TYPE ===')
    for d,s in sorted(per_origin.items()):
        print(d,'N=',s['events'],'pre_retest_complete=',s['retest_COMPLETE_60'],
              'pre_known_complete=',s['known_COMPLETE_60'])
    print('\n=== CROSSCHECK CACHE vs AUTO_PATHS (OVERLAPPING BARS) ===')
    print('compared=',comparable,'MATCH=',identical,'DIFFER=',differences)
    for example in bad_parity: print('MISMATCH',*example)
    print('\n=== DATA QUALITY / CLOCK CHECKS ===')
    print('CACHE_ISSUES',dict(file_issues),'KNOWN_TS_LAGS',dict(sample_lags))
    if not comparable:
        print('NOTE: No comparable post-REACTION candles found in cached partitions. '
              'No OHLC source-parity conclusion is possible.')
    print('\nNo files were modified or downloaded.')


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--project', type=Path, default=Path('/home/multiservices-1m'))
    ap.add_argument('--sample-per-day', type=int,default=12,
                    help='12=diagnostic sample; 0=audit all events; both read-only')
    args=ap.parse_args()
    if args.sample_per_day < 0:
        ap.error('--sample-per-day must be >= 0')
    audit(args.project.resolve(),args.sample_per_day)


if __name__=='__main__':
    main()
