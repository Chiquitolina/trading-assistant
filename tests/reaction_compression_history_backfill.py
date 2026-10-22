#!/usr/bin/env python3
"""Explicit, resumable Binance USDM 15m/30m cache for historical compression research.

NEVER changes Full Context, Auto Paths, Redis, Market Data, or LIVE execution.
--plan is fully offline. --fetch alone authorizes REST requests and writes only
reports/reaction_lab/reaction_compression_history_v1.sqlite3.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time
from datetime import datetime, timezone

from reaction_compression_lineage import ROOT_REL, CTX_NAME, HISTORY_NAME, TF, M, ro

SCHEMA='''CREATE TABLE IF NOT EXISTS candles (
 symbol TEXT NOT NULL, timeframe TEXT NOT NULL, open_ts INTEGER NOT NULL,
 open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
 volume REAL NOT NULL, quote_volume REAL NOT NULL,
 PRIMARY KEY(symbol,timeframe,open_ts));
CREATE INDEX IF NOT EXISTS idx_lineage_bars ON candles(symbol,timeframe,open_ts);
'''
URL='https://fapi.binance.com/fapi/v1/klines'


def tasks(root):
    with closing(ro(root/ROOT_REL/CTX_NAME)) as con:
        rows=con.execute('SELECT symbol,MIN(retest_ts),MAX(retest_ts) FROM events GROUP BY symbol ORDER BY symbol').fetchall()
    # 120h box + 48h age + 12h safety = 180h prior to earliest event.
    return [(s,int(lo)-180*60*M,int(hi)) for s,lo,hi in rows]


def own_db(path):
    path.parent.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(path,timeout=40)
    con.execute('PRAGMA journal_mode=WAL');con.executescript(SCHEMA)
    return con


def validate_kline(symbol,tf,k,start,end):
    ts=int(k[0]);o,h,l,c,v,q=[float(k[x]) for x in (1,2,3,4,5,7)]
    import math
    if ts<TF[tf]*(ts//TF[tf]) or ts%TF[tf] or ts<start or ts>end or not all(map(math.isfinite,(o,h,l,c,v,q))):
        raise ValueError(f'Invalid kline timestamp/numeric {symbol} {tf} {ts}')
    if min(o,h,l,c)<=0 or min(v,q)<0 or h<max(o,l,c) or l>min(o,h,c):
        raise ValueError(f'Invalid kline OHLC {symbol} {tf} {ts}')
    return symbol,tf,ts,o,h,l,c,v,q


def fetch_batches(session,symbol,tf,start,end,pause):
    cursor=(start//TF[tf])*TF[tf]
    while cursor<=end:
        params={'symbol':symbol,'interval':tf,'startTime':cursor,
                'endTime':end,'limit':1500}
        resp=session.get(URL,params=params,timeout=30)
        if resp.status_code in (418,429):
            raise RuntimeError(f'Binance rate limited {resp.status_code}: stop, no retry storm')
        resp.raise_for_status()
        candles=resp.json()
        if not isinstance(candles,list):raise RuntimeError(f'Unexpected response {symbol} {tf}: {str(candles)[:160]}')
        if not candles:break
        result=[validate_kline(symbol,tf,k,cursor,end) for k in candles]
        if any(result[i][2] >=result[i+1][2] for i in range(len(result)-1)):
            raise RuntimeError('Out of order or duplicate exchange candles')
        yield result
        newcursor=result[-1][2]+TF[tf]
        if newcursor<=cursor:raise RuntimeError('Non-advancing API cursor')
        cursor=newcursor
        if pause:time.sleep(pause)
        if len(candles)<1500:break


def backfill(root,max_symbols=20,pause=.45):
    import requests
    all_tasks=tasks(root)
    if max_symbols:all_tasks=all_tasks[:max_symbols]
    cache=root/ROOT_REL/HISTORY_NAME
    with closing(own_db(cache)) as con, requests.Session() as session:
        for i,(symbol,start,end) in enumerate(all_tasks,1):
            for tf in ('15m','30m'):
                saved=con.execute('SELECT MIN(open_ts),MAX(open_ts),COUNT(*) FROM candles WHERE symbol=? AND timeframe=?',(symbol,tf)).fetchone()
                # Re-run is safe and uses upsert-ignore only after numeric equality check.
                # Already covered date ranges skip REST; detected holes are audited by build.
                if saved[2] and saved[0]<=((start//TF[tf])*TF[tf]) and saved[1]>=((end//TF[tf])*TF[tf]):
                    print('HISTORY_SKIPPED',symbol,tf,'already covers range',flush=True);continue
                for batch in fetch_batches(session,symbol,tf,start,end,pause):
                    with con:
                        for row in batch:
                            previous=con.execute('SELECT open,high,low,close,volume,quote_volume FROM candles WHERE symbol=? AND timeframe=? AND open_ts=?',row[:3]).fetchone()
                            if previous is not None:
                                import math
                                if any(not math.isclose(a,b,abs_tol=1e-10,rel_tol=1e-9) for a,b in zip(previous,row[3:])):
                                    raise RuntimeError(f'Provider revised archived candle; refusal to overwrite {symbol} {tf} {row[2]}')
                            else:con.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',row)
            print('HISTORY_PROGRESS',json.dumps({'symbol':symbol,'index':i,'total_selected':len(all_tasks)}),flush=True)
    return {'symbols_processed':len(all_tasks),'source':'BINANCE_USDM_NATIVE_15m_30m',
       'db':str(cache),'separate_from_frozen_sources':True}


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    mode=ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true');mode.add_argument('--fetch',action='store_true')
    ap.add_argument('--root',type=Path,default=Path('/home/multiservices-1m'))
    ap.add_argument('--max-symbols',type=int,default=20,help='0=all; 20 pilot default')
    ap.add_argument('--pause',type=float,default=.45)
    args=ap.parse_args();root=args.root.resolve()
    if args.max_symbols<0 or args.pause<0:ap.error('max-symbols, pause >=0')
    if args.plan:
        group=tasks(root)
        result={'symbols':len(group),'timeframes':['15m','30m'],
          'required_minimum_prior_hours':180,'default_pilot_symbols':20,
          'optional_native_cache':str(root/ROOT_REL/HISTORY_NAME),
          'network_calls':False,'writes':False,
          'notes':['These bars are a separate research cache, not a modification of frozen Full Context.',
            'An existing 15m bar differing from Binance historical REST stops offline merge instead of silently replacing data.']}
    else:result=backfill(root,args.max_symbols,args.pause)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
