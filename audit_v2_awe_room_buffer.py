#!/usr/bin/env python3
"""Read-only AWEUSDT HTF Room audit: full historical vs effective Redis caps.

Requires repository data at /home/multiservices-1m. Never writes market caches,
reports, or strategy files. Uses the exact existing SwingDetector and HTF Room
selection function extracted from dashboard/app.py (without importing Streamlit).
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path('/home/multiservices-1m')
RUN = '20261003_20261010_e348dd341244'
SYMBOL = 'AWEUSDT'
LIMITS = {'30m': 1000, '1h': 750, '4h': 500}
TF_MS = {'30m': 1_800_000, '1h': 3_600_000, '4h': 14_400_000}
TOL = 1e-6
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from engine.live.research.swing_detector import SwingDetector
from dashboard.services.geometry_scanner_data_service import GeometryScannerDataService


def read_distances_function():
    path = ROOT / 'dashboard/app.py'
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
             and n.name == '_reaction_driver_swing_distances']
    if len(nodes) != 1:
        raise RuntimeError('Expected exactly one original HTF Room distance function')
    function = nodes[0]
    function.decorator_list = []
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    ns = {'np': np, 'pd': pd}
    exec(compile(module, str(path), 'exec'), ns)
    return ns['_reaction_driver_swing_distances']


def candles_for(tf):
    folder = ROOT / 'reports/candidate_long_horizon/candle_cache' / tf / SYMBOL
    files = sorted(folder.glob('*.pkl.gz'))
    if not files:
        raise RuntimeError(f'No historical candle files found in {folder}')
    parts = [pd.read_pickle(p, compression='gzip') for p in files]
    frame = pd.concat(parts, ignore_index=True)
    required = {'timestamp', 'open', 'high', 'low', 'close', 'volume'}
    if not required.issubset(frame.columns):
        raise RuntimeError(f'{tf} candle columns absent: {required - set(frame.columns)}')
    frame['timestamp'] = pd.to_numeric(frame['timestamp'], errors='coerce')
    return (frame.dropna(subset=['timestamp'])
            .sort_values('timestamp', kind='stable')
            .drop_duplicates('timestamp', keep='last')
            .reset_index(drop=True))


def safe_get_redis_caps():
    load_dotenv(ROOT / '.env')
    service = GeometryScannerDataService(
        host=os.getenv('REDIS_HOST', '127.0.0.1'),
        port=int(os.getenv('REDIS_PORT', '6379')),
        db=int(os.getenv('REDIS_DB', '0')),
    )
    caps = {}
    for tf in LIMITS:
        try:
            frame = service.get_closed_candles(
                symbol=SYMBOL, timeframe=tf, limit=LIMITS[tf]
            )
            n = 0 if frame is None else len(frame)
            if n <= 0:
                raise RuntimeError('empty Redis frame')
            caps[tf] = min(int(n), LIMITS[tf])
        except Exception as e:
            raise RuntimeError(f'Redis {tf} unavailable: {e}. Re-run when available') from e
    return caps


def room_for(row, limits, candles, distances_fn):
    known = int(row['candidate_v1_reaction_known_ts'])
    price = float(row['retest_close'])
    side = str(row['signal']).upper()
    candidates = []
    per_tf = {}
    for tf, count in limits.items():
        interval_ms = TF_MS[tf]
        frame = candles[tf]
        # Only candles whose full timeframe has closed by the decision instant.
        window = frame.loc[(frame.timestamp + interval_ms) <= known].tail(count)
        if window.empty:
            per_tf[tf] = np.nan
            continue
        points = SwingDetector(
            left_bars=5, right_bars=5, min_prominence_pct=0.0,
        ).detect_all(window.to_dict(orient='records'))
        distance = distances_fn(points, known, interval_ms, price, side)
        room = distance['opposing_room_pct']
        per_tf[tf] = room
        if pd.notna(room):
            candidates.append((float(room), tf,
                               distance['opposing_swing_pivot_timestamp']))
    if not candidates:
        return (np.nan, '-', np.nan), per_tf
    return min(candidates), per_tf


def aligned(a, b):
    return (pd.isna(a) and pd.isna(b)) or (pd.notna(a) and pd.notna(b)
                                           and abs(float(a) - float(b)) <= TOL)


def main():
    replay_path = (ROOT / 'reports/candidate_long_horizon/runs'
                   / RUN / 'events.csv')
    monitor_path = ROOT / 'candidate_v2_monitor.csv'
    replay = pd.read_csv(replay_path, low_memory=False)
    monitor = pd.read_csv(monitor_path, low_memory=False)
    replay = replay.loc[replay['symbol'].eq(SYMBOL)].copy()
    monitor = monitor.loc[monitor['symbol'].eq(SYMBOL)].copy()
    common = replay.merge(
        monitor[['candidate_v2_event_key', 'nearest_opposing_room_pct']],
        on='candidate_v2_event_key', suffixes=('_replay', '_monitor'),
        validate='one_to_one',
    ).sort_values('retest_timestamp')
    print('Matched replay/monitor events:', len(common))
    print('Reconstruction source: cached 30m / 1h / 4h native Binance candles')
    caps = safe_get_redis_caps()
    print('Requested limits:', LIMITS)
    print('Actual Redis lengths TODAY (hypothetical historical caps):', caps)
    frames = {tf: candles_for(tf) for tf in LIMITS}
    for tf, df in frames.items():
        print(f'Historical {tf} cache candles: {len(df)}')
    distances = read_distances_function()
    base_matches = 0
    short_matches = 0
    failures = []
    rows = []
    for _, row in common.iterrows():
        original = float(row['nearest_opposing_room_pct_replay'])
        stored = float(row['nearest_opposing_room_pct_monitor'])
        full, per_tf_full = room_for(row, LIMITS, frames, distances)
        short, per_tf_short = room_for(row, caps, frames, distances)
        base_matches += int(aligned(full[0], original))
        short_matches += int(aligned(short[0], stored))
        if not aligned(full[0], original):
            failures.append((row['candidate_v2_event_key'], original, full[0], per_tf_full))
        rows.append({
            'event': row['candidate_v2_event_key'],
            'monitor': stored, 'replay': original,
            'full_recomputed': full[0], 'cap_recomputed': short[0],
            'cap_tf': short[1], 'cap_matches_monitor': aligned(short[0], stored),
            'full_matches_replay': aligned(full[0], original),
        })
    print('\nFull-limit reproduction of original replay:', base_matches, '/', len(common))
    if failures:
        print('STOP: the full-limit control did not reproduce the historical replay.')
        for f in failures[:8]:
            print('FULL BASELINE MISMATCH:', f)
        print('Do not interpret capped window matches until baseline discrepancy is resolved.')
        return
    print('Capped historical as-of Room vs stored V2 monitor:', short_matches, '/', len(common))
    print('Capped Room is an EXPERIMENT, not proof of actual historical Redis capacity.')
    df = pd.DataFrame(rows)
    print('\nPer-event Room comparison (all amounts are percent):')
    print(df.to_string(index=False, float_format=lambda v: f'{v:.6f}'))
    print('\nAUDIT COMPLETE (read-only)')


if __name__ == '__main__':
    main()
