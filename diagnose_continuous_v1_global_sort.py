#!/usr/bin/env python3
"""Compare full-universe vs single-symbol tie ordering for two Continuous V1 collisions.

Read-only: uses historical Binance cache and production SQLite read-only.
"""
from pathlib import Path
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import sys
import numpy as np
import pandas as pd

project = Path('/home/multiservices-1m')
sys.path.insert(0, str(project))
import reaction_lab_continuous_scanner as C
import replay_continuous_v1 as R

cache = Path('/root/continuous_v1_klines_cache')
start = R.iso_to_ms('2026-10-13T00:00:00-03:00')
end = R.iso_to_ms('2026-10-14T00:00:00-03:00')
one_start = ((start - 430 * R.ONE_MIN) // R.ONE_MIN) * R.ONE_MIN
fifteen_start = ((start - 410 * R.FIFTEEN_MIN) // R.FIFTEEN_MIN) * R.FIFTEEN_MIN
fetch_end = end + R.FIFTEEN_MIN
symbols = C.get_symbols(project)

cases = (
    ('UAIUSDT', 'SHORT', 1791904800000, '2026-10-13T12:22:00-03:00'),
    ('BNBUSDT', 'LONG', 1791941820000, '2026-10-13T22:38:00-03:00'),
)

print('SYMBOLS', len(symbols), 'BOTH_CASES', len(cases), flush=True)
if len(symbols) != 290:
    raise RuntimeError('Symbol universe changed from validated 290; stop for review')

missing = []
hist = {}
for sym in symbols:
    for tf, from_ts in (('1m', one_start), ('15m', fifteen_start)):
        path = cache / R.cache_name(sym, tf, from_ts, fetch_end)
        if not path.exists():
            missing.append((sym, tf, str(path)))
            continue
        payload = json.loads(path.read_text())
        if (payload.get('symbol') != sym or payload.get('timeframe') != tf
                or payload.get('start_ms') != from_ts or payload.get('end_ms') != fetch_end):
            raise RuntimeError(f'Cache identity mismatch: {path}')
        frame = pd.DataFrame(payload['candles'])
        hist[(sym, tf)] = (frame, frame['timestamp'].to_numpy(dtype=np.int64))
if missing:
    print('MISSING CACHE FILES:', len(missing))
    for item in missing[:10]:
        print(item)
    raise SystemExit('Missing cached history; this diagnostic does NOT redownload')

scan, fingerprint = C.compile_canonical_scanner(project / 'dashboard/app.py', lambda **kw: None)
print('FINGERPRINT', fingerprint, flush=True)
con = sqlite3.connect(
    f"file:{(project/'reports/reaction_lab/scanner_events_v1.sqlite3').resolve()}?mode=ro",
    uri=True,
)

for sym, side, ts, instant in cases:
    tick = R.iso_to_ms(instant)
    snapshots = {}
    for key, (df, times) in hist.items():
        tf_ms = R.ONE_MIN if key[1] == '1m' else R.FIFTEEN_MIN
        i = int(np.searchsorted(times, tick - tf_ms, side='right'))
        snapshots[key] = df.iloc[max(0, i - 400):i]

    def loader(symbol, timeframe, limit):
        return snapshots[(str(symbol).upper(), str(timeframe))].tail(min(400, int(limit)))

    scan.__globals__['load_volume_exhaustion_research_candles'] = loader

    def evaluate(universe):
        result = scan(
            symbols=universe,
            swing_timeframes=('15m',),
            swing_detector_items=(('15m', '3x3'),),
            min_swing_prominence_pct=0.0,
            retest_tolerance_pct=0.10,
            min_departure_pct=0.20,
            max_age_minutes=360,
            max_retest_age_minutes=4320,
        )
        candidates = result.loc[
            (result['symbol'] == sym)
            & (result['signal'] == side)
            & (pd.to_numeric(result['retest_timestamp'], errors='coerce') == ts)
            & (result['status'] == 'REACTION')
        ]
        return candidates, C.normalize_reactions(result, tick).get(f'{sym}|{side}|{ts}')

    print('\n' + '='*65)
    print('CASE', sym, 'TICK', instant, 'EVENT', f'{sym}|{side}|{ts}', flush=True)
    r = con.execute('SELECT causal_json FROM events WHERE event_key=?',
                    (f'{sym}|{side}|{ts}',)).fetchone()
    if r is None:
        raise RuntimeError('Event missing in original journal')
    live = json.loads(r[0])
    print('JOURNAL PIVOT', live['pivot_timestamp'], 'SWING', live['swing_price'])

    for scope, universe in (('SINGLE', (sym,)), ('GLOBAL_290', symbols)):
        candidates, selected = evaluate(universe)
        print('SCOPE', scope, 'CANDIDATES', len(candidates), flush=True)
        for _, candidate in candidates.iterrows():
            print('  PIVOT', int(candidate['pivot_timestamp']),
                  'SWING', float(candidate['swing_price']),
                  'DEPARTURE', round(float(candidate['max_departure_pct']), 5))
        if selected:
            print('SELECTED PIVOT', selected.get('pivot_timestamp'),
                  'MATCHES_LIVE', selected.get('pivot_timestamp') == live['pivot_timestamp'],
                  flush=True)
        else:
            print('NO SELECTED REACTION', flush=True)

con.close()
