#!/usr/bin/env python3
"""Audit the six V1 collision cases against single/global historical scans.
Read-only: no Redis, DB writes, downloads, or strategy changes.
"""
from pathlib import Path
from datetime import datetime, timezone, timedelta
import csv
import json
import sqlite3
import sys

import numpy as np
import pandas as pd

PROJECT = Path('/home/multiservices-1m')
sys.path.insert(0, str(PROJECT))
import reaction_lab_continuous_scanner as C
import replay_continuous_v1 as R

CACHE = Path('/root/continuous_v1_klines_cache')
PARITY = Path('/root/continuous_v1_parity_6_1m/replay_events.csv')
DB = PROJECT / 'reports/reaction_lab/scanner_events_v1.sqlite3'
START = R.iso_to_ms('2026-10-13T00:00:00-03:00')
END = R.iso_to_ms('2026-10-14T00:00:00-03:00')
ONE_START = ((START - 430 * R.ONE_MIN) // R.ONE_MIN) * R.ONE_MIN
FIFTEEN_START = ((START - 410 * R.FIFTEEN_MIN) // R.FIFTEEN_MIN) * R.FIFTEEN_MIN
FETCH_END = END + R.FIFTEEN_MIN
KEYS = (
    'AGTUSDT|LONG|1791877200000',
    'BNBUSDT|LONG|1791941820000',
    'CATIUSDT|SHORT|1791935700000',
    'SUSDT|SHORT|1791937140000',
    'UAIUSDT|SHORT|1791904800000',
    'USUSDT|LONG|1791936000000',
)

def fmt(ms):
    return datetime.fromtimestamp(int(ms)/1000, timezone(timedelta(hours=-3))).strftime('%d/%m %H:%M:%S')

if not PARITY.exists():
    raise SystemExit(f'Missing 1m replay output: {PARITY}')
with PARITY.open(newline='',encoding='utf-8') as fh:
    saved = {r['event_key']: r for r in csv.DictReader(fh) if r['event_key'] in KEYS}
if len(saved) != len(KEYS):
    raise SystemExit('The replay CSV does not contain all six events')
symbols = C.get_symbols(PROJECT)
if len(symbols) != 290:
    raise SystemExit(f'Unexpected universe of {len(symbols)}; audit stopped')
hist = {}
for sym in symbols:
    for tf, from_ts in (('1m', ONE_START), ('15m', FIFTEEN_START)):
        file = CACHE / R.cache_name(sym, tf, from_ts, FETCH_END)
        if not file.exists():
            raise SystemExit(f'Historical cache missing; NO redownload attempted: {file}')
        raw = json.loads(file.read_text())
        if (raw.get('symbol'),raw.get('timeframe'),raw.get('start_ms'),raw.get('end_ms')) != (sym,tf,from_ts,FETCH_END):
            raise SystemExit(f'Cache provenance mismatch: {file}')
        frame = pd.DataFrame(raw['candles'])
        hist[(sym,tf)] = (frame,frame['timestamp'].to_numpy(dtype=np.int64))
scan, fp = C.compile_canonical_scanner(PROJECT/'dashboard/app.py', lambda **kw: None)
print('CANONICAL HASH',fp,'SYMBOLS',len(symbols),'CASES',len(KEYS),flush=True)
con = sqlite3.connect(f'file:{DB.resolve()}?mode=ro',uri=True)
stats={'single_live':0,'global_live':0,'latest_live':0,'multiple_single':0,'multiple_global':0}
try:
    for key in KEYS:
        sym,side,ts_text = key.split('|')
        retest_ts = int(ts_text)
        raw = con.execute('SELECT first_seen_ms,source_hash,causal_json FROM events WHERE event_key=?',(key,)).fetchone()
        if not raw:
            raise SystemExit(f'Original event missing: {key}')
        first_seen,source_hash,payload = raw
        if source_hash != fp:
            raise SystemExit(f'Original detector fingerprint mismatch: {key}')
        live = json.loads(payload)
        tick = first_seen // R.ONE_MIN * R.ONE_MIN
        replay_seen = int(saved[key]['first_replay_observed_ms'])
        snapshots = {}
        for k,(df,times) in hist.items():
            tf_ms = R.ONE_MIN if k[1]=='1m' else R.FIFTEEN_MIN
            end_index = int(np.searchsorted(times,tick-tf_ms,side='right'))
            snapshots[k] = df.iloc[max(0,end_index-400):end_index]
        def loader(symbol,timeframe,limit):
            return snapshots[(str(symbol).upper(),str(timeframe))].tail(min(400,int(limit)))
        scan.__globals__['load_volume_exhaustion_research_candles'] = loader
        print('\n'+'='*64)
        print('EVENT',key,'LIVE_SEEN',fmt(first_seen),'TICK',fmt(tick),'REPLAY_FIRST',fmt(replay_seen))
        print('LIVE PIVOT',fmt(live['pivot_timestamp']),'DEPARTURE',live.get('max_departure_pct'))
        for label,universe in (('SINGLE',(sym,)),('GLOBAL_290',symbols)):
            frame = scan(symbols=universe,swing_timeframes=('15m',),swing_detector_items=(('15m','3x3'),),
                         min_swing_prominence_pct=0.0,retest_tolerance_pct=0.10,
                         min_departure_pct=0.20,max_age_minutes=360,max_retest_age_minutes=4320)
            choices = frame.loc[(frame['symbol']==sym)&(frame['signal']==side)&(frame['status']=='REACTION')&
                                (pd.to_numeric(frame['retest_timestamp'],errors='coerce')==retest_ts)]
            selected = C.normalize_reactions(frame,tick).get(key)
            newest = max((int(x) for x in choices['pivot_timestamp']),default=None)
            selected_pivot = selected.get('pivot_timestamp') if selected else None
            matches = selected_pivot == live['pivot_timestamp']
            print(label,'candidates=',len(choices),'selected=',fmt(selected_pivot) if selected_pivot else None,
                  'matches_live=',matches,'newest_matches_live=',newest==live['pivot_timestamp'])
            if len(choices)>1:
                stats['multiple_single' if label=='SINGLE' else 'multiple_global']+=1
                for _,r in choices.iterrows():
                    print('  option',fmt(r['pivot_timestamp']),'swing=',r['swing_price'],'departure=',r['max_departure_pct'])
            if matches:
                stats['single_live' if label=='SINGLE' else 'global_live']+=1
            if label=='GLOBAL_290' and newest == live['pivot_timestamp']:
                stats['latest_live']+=1
finally:
    con.close()
print('\nSUMMARY',stats)
print('Audit only. No tie-break changes, no production mutation, no verified backfill.')
