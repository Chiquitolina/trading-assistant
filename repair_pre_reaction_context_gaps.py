#!/usr/bin/env python3
"""Fill missing pre-reaction 1m/15m candles from official Binance USD-M REST.

This is an explicit, bounded, auditable, ON-DEMAND repair; it never modifies
scanner, Auto Paths, Redis, or Market Data. It writes only the dedicated
pre_reaction_context_v1.sqlite3 DB; source is permanently tagged as historical
backfill, never as LIVE_CAPTURED/REDIS_CLOSED.

--plan is strictly read-only. --run contacts fapi.binance.com for missing bars.
If overlapping OHLC in Auto Paths disagrees, the candidate is REJECTED.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import struct
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
import zlib

from pre_reaction_context_recorder import TFS, window_coverage, same_bars, ro_db

DEFAULT_DB = Path('reports/reaction_lab/pre_reaction_context_v1.sqlite3')
DEFAULT_AUTOPATHS = Path('reports/reaction_lab/auto_paths_v1.sqlite3')
SOURCE = 'BINANCE_FUTURES_REST_BACKFILL'
HOST = 'https://fapi.binance.com'
UNPACK = struct.Struct('<qdddd')


def iso(ms):
    return datetime.fromtimestamp(ms/1000, tz=timezone.utc).isoformat(timespec='minutes')


def check_bar(fields, ts: int, tf: str, now_ms: int):
    interval, _ = TFS[tf]
    if ts % interval or ts + interval > now_ms:
        raise ValueError('Timestamp unaligned or candle not yet closed')
    if not isinstance(fields, (list, tuple)) or len(fields) < 8:
        raise ValueError('Bad Binance kline structure')
    if int(fields[0]) != ts or int(fields[6]) != ts + interval-1:
        raise ValueError('Binance timestamp mismatch')
    o,h,l,c,vol = (float(fields[i]) for i in (1,2,3,4,5))
    quote_volume = float(fields[7])
    if not all(map(math.isfinite,(o,h,l,c,vol,quote_volume))):
        raise ValueError('Non-finite values')
    if min(o,h,l,c) <= 0 or vol < 0 or quote_volume < 0 or h < max(o,l,c) or l > min(o,h,c):
        raise ValueError('Invalid OHLCV')
    return (o,h,l,c,vol,quote_volume)


def fetch_binance(symbol, tf, ts, now_ms, *, retries=3, opener=urlopen, base_url=HOST):
    ms, _ = TFS[tf]
    query=urlencode({'symbol':symbol,'interval':tf,'startTime':ts,'endTime':ts+ms-1,'limit':1})
    req=Request(base_url+'/fapi/v1/klines?'+query,
                headers={'User-Agent':'pre-reaction-context-audited-gap-repair/1.0',
                         'Accept':'application/json'})
    for attempt in range(retries):
        try:
            with opener(req, timeout=20) as resp:
                data=json.load(resp)
            if not isinstance(data,list) or len(data) != 1:
                raise ValueError('Binance returned zero or multiple candles')
            return check_bar(data[0],ts,tf,now_ms)
        except (HTTPError, URLError, TimeoutError, ConnectionError) as exc:
            if isinstance(exc,HTTPError) and exc.code not in (418,429,500,502,503,504):
                raise
            if attempt == retries-1:raise
            time.sleep(min(10,2**attempt))
    raise AssertionError('unreachable')


def gaps(db, tf='1m'):
    """Every currently absent candle referenced by an incomplete event window."""
    ms,_ = TFS[tf]
    windows=db.execute('''SELECT e.symbol,w.end_exclusive_ms,w.expected_count
       FROM windows w JOIN events e ON e.event_key=w.event_key
       WHERE w.timeframe=? AND w.status IN ('PARTIAL','NONE')''',(tf,)).fetchall()
    wanted=defaultdict(set)
    for sym,end,count in windows:
        start=end-count*ms
        known={r[0] for r in db.execute('''SELECT c.open_ts FROM candles c
             LEFT JOIN conflicts f ON c.symbol=f.symbol AND c.timeframe=f.timeframe
                                   AND c.open_ts=f.open_ts
             WHERE c.symbol=? AND c.timeframe=? AND c.open_ts>=? AND c.open_ts<?
               AND f.open_ts IS NULL''',(sym,tf,start,end))}
        for ts in range(start,end,ms):
            if ts not in known:
                wanted[sym].add(ts)
    result=[]
    for sym,ts_set in sorted(wanted.items()):
        for ts in sorted(ts_set):
            # Conflict rows cannot be cleared via a historical insertion.
            conflict=db.execute('''SELECT reason FROM conflicts
                  WHERE symbol=? AND timeframe=? AND open_ts=?''',(sym,tf,ts)).fetchone()
            if conflict is None:
                result.append((sym,tf,ts))
    return result


def autopaths_evidence(auto, symbol, wanted_ts):
    """Return overlapping auto path OHLCs, rejecting disagreements among paths.

    Only a bounded set of symbols are queried. No Auto Paths modifications.
    """
    if auto is None:return {},set()
    matches={}; contradictions=set()
    for (blob,) in auto.execute('''SELECT path_blob FROM events
                    WHERE symbol=? AND path_blob IS NOT NULL''',(symbol,)):
        try:
            raw=zlib.decompress(blob)
            if len(raw)%UNPACK.size: continue
            for t,o,h,l,c in UNPACK.iter_unpack(raw):
                if t not in wanted_ts:continue
                v=(o,h,l,c)
                if t in matches and not all(math.isclose(x,y,rel_tol=1e-9,abs_tol=1e-11)
                                             for x,y in zip(matches[t],v)):
                    contradictions.add(t)
                matches[t]=v
        except (zlib.error,ValueError):
            continue
    return matches,contradictions


def matching_ohlc(a,b):
    return all(math.isclose(x,y,rel_tol=1e-9,abs_tol=1e-11) for x,y in zip(a[:4],b[:4]))


def verify_aggregated_15m(db, symbol, ts, incoming_bar):
    """Compare restored 1m with recorded 15m, only when all 15 one-minute
    candles and the closed 15m candle are already persisted and conflict-free.

    Returns MATCH, NOT_COMPARABLE, or raises on a mismatch.
    """
    start=(ts//900_000)*900_000
    ht=db.execute('''SELECT c.open,c.high,c.low,c.close,c.volume
        FROM candles c LEFT JOIN conflicts f ON c.symbol=f.symbol
             AND c.timeframe=f.timeframe AND c.open_ts=f.open_ts
        WHERE c.symbol=? AND c.timeframe='15m' AND c.open_ts=?
          AND f.open_ts IS NULL''',(symbol,start)).fetchone()
    if ht is None:return 'NOT_COMPARABLE'
    rows=db.execute('''SELECT c.open_ts,c.open,c.high,c.low,c.close,c.volume
        FROM candles c LEFT JOIN conflicts f ON c.symbol=f.symbol
             AND c.timeframe=f.timeframe AND c.open_ts=f.open_ts
        WHERE c.symbol=? AND c.timeframe='1m' AND c.open_ts>=?
          AND c.open_ts<? AND f.open_ts IS NULL''',
        (symbol,start,start+900_000)).fetchall()
    bars={r[0]:tuple(r[1:]) for r in rows}
    bars[ts]=incoming_bar[:5]
    if len(bars)!=15 or any(start+i*60_000 not in bars for i in range(15)):
        return 'NOT_COMPARABLE'
    seq=[bars[start+i*60_000] for i in range(15)]
    agg=(seq[0][0],max(x[1] for x in seq),min(x[2] for x in seq),
         seq[-1][3],sum(x[4] for x in seq))
    if not all(math.isclose(x,y,rel_tol=1e-7,abs_tol=1e-9)
               for x,y in zip(agg,ht)):
        raise ValueError(f'REST_VS_AGGREGATED_15M_MISMATCH: {agg} != {ht}')
    return 'MATCH'


def repair_one(db, symbol, tf, ts, bar, recorded_ms):
    """One SQLite transaction; validate existing row, insert if missing, refresh windows."""
    ms,_=TFS[tf]
    with db:
        row=db.execute('''SELECT open,high,low,close,volume,quote_volume
             FROM candles WHERE symbol=? AND timeframe=? AND open_ts=?''',
             (symbol,tf,ts)).fetchone()
        if row is not None and not same_bars(row,bar):
            raise ValueError('CONCURRENT_OR_STORED_PRICE_MISMATCH')
        flagged=db.execute('''SELECT reason FROM conflicts WHERE symbol=? AND timeframe=?
                                  AND open_ts=?''',(symbol,tf,ts)).fetchone()
        if flagged:raise ValueError('CONFLICT_ALREADY_FLAGGED: '+flagged[0])
        inserted=False
        if row is None:
            db.execute('''INSERT INTO candles
              (symbol,timeframe,open_ts,open,high,low,close,volume,quote_volume,
               first_recorded_ms,source) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
               (symbol,tf,ts,*bar,recorded_ms,SOURCE))
            inserted=True
        affected=db.execute('''SELECT w.event_key,w.anchor_type,w.end_exclusive_ms,
                   w.expected_count FROM windows w JOIN events e ON e.event_key=w.event_key
                   WHERE e.symbol=? AND w.timeframe=? AND w.end_exclusive_ms>?
                     AND w.end_exclusive_ms-w.expected_count*?<=?''',
                   (symbol,tf,ts,ms,ts)).fetchall()
        for key,anchor,end,count in affected:
            status,saved,first_missing=window_coverage(db,symbol,tf,end-count*ms,end,count)
            db.execute('''UPDATE windows SET status=?,saved_count=?,first_missing_ts=?,
                   updated_ms=? WHERE event_key=? AND anchor_type=? AND timeframe=?''',
                   (status,saved,first_missing,recorded_ms,key,anchor,tf))
        return inserted,len(affected)


def execute(db_path,auto_path,tf,max_bars,pace,run,limit_symbols=0,fetch=fetch_binance):
    # --plan intentionally never opens writable DB or the network.
    if not run:
        db=ro_db(db_path)
        try:todo=gaps(db,tf)
        finally:db.close()
        grouped=defaultdict(list)
        for sym,_,ts in todo:grouped[sym].append(ts)
        out={'mode':'PLAN','timeframe':tf,'missing_unique_bars':len(todo),
             'affected_symbols':len(grouped),'sample':[
              {'symbol':s,'missing_times_utc':[iso(t) for t in ts[:8]]}
              for s,ts in list(grouped.items())[:15]],
             'note':'No network requests and no database writes'}
        return out
    now_ms=int(time.time()*1000)
    db=sqlite3.connect(str(db_path),timeout=30)
    db.execute('PRAGMA busy_timeout=30000')
    db.execute('PRAGMA foreign_keys=ON')
    auto=ro_db(auto_path) if auto_path.is_file() else None
    result={'mode':'RUN','timeframe':tf,'requested':0,'inserted':0,'already_present':0,
            'checked_against_auto_paths':0,'no_auto_paths_overlap':0,
            'rejected':[],'refreshed_windows':0,'source':SOURCE,
            'matched_aggregated_15m':0,'without_full_15m_comparison':0}
    try:
        todo=gaps(db,tf)
        if max_bars and len(todo)>max_bars:
            raise ValueError(f'{len(todo)} missing bars exceed --max-bars {max_bars}; review --plan first')
        symbols=sorted({s for s,_,_ in todo})
        if limit_symbols and len(symbols)>limit_symbols:
            raise ValueError(f'{len(symbols)} symbols exceed --max-symbols {limit_symbols}')
        evidence={};bad_evidence={}
        for s in symbols:
            wanted={t for sym,_,t in todo if sym==s}
            evidence[s],bad_evidence[s]=autopaths_evidence(auto,s,wanted) if tf=='1m' else ({},set())
        for symbol,tf,ts in todo:
            result['requested']+=1
            try:
                if ts in bad_evidence[symbol]:
                    raise ValueError('Auto Paths contradicts itself for candle')
                bar=fetch(symbol,tf,ts,now_ms)
                check_bar([ts,*[str(x) for x in bar[:5]],ts+TFS[tf][0]-1,
                           str(bar[5])],ts,tf,now_ms)
                if ts in evidence[symbol]:
                    if not matching_ohlc(evidence[symbol][ts],bar):
                        raise ValueError('BINANCE_VS_AUTO_PATHS_OHLC_DIFFER')
                    result['checked_against_auto_paths']+=1
                else:result['no_auto_paths_overlap']+=1
                if tf=='1m':
                    check_15m=verify_aggregated_15m(db,symbol,ts,bar)
                    if check_15m=='MATCH':result['matched_aggregated_15m']+=1
                    else:result['without_full_15m_comparison']+=1
                inserted,affected=repair_one(db,symbol,tf,ts,bar,int(time.time()*1000))
                result['inserted']+=int(inserted)
                result['already_present']+=int(not inserted)
                result['refreshed_windows']+=affected
            except Exception as exc:
                result['rejected'].append({'symbol':symbol,'time_utc':iso(ts),
                                           'reason':f'{type(exc).__name__}: {exc}'})
            if pace and result['requested'] < len(todo):time.sleep(pace)
        result['remaining_missing_unique_bars']=len(gaps(db,tf))
        return result
    finally:
        if auto:auto.close()
        db.close()


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true')
    mode.add_argument('--run',action='store_true')
    p.add_argument('--db',type=Path,default=DEFAULT_DB)
    p.add_argument('--auto-paths-db',type=Path,default=DEFAULT_AUTOPATHS)
    p.add_argument('--timeframe',choices=('1m','15m'),default='1m')
    p.add_argument('--max-bars',type=int,default=100,
                   help='Safety limit; refuse if more missing bars (0 = no limit)')
    p.add_argument('--max-symbols',type=int,default=40,
                   help='Safety limit; refuse if more symbols (0 = no limit)')
    p.add_argument('--pace-seconds',type=float,default=0.35)
    args=p.parse_args(argv)
    if args.max_bars<0 or args.max_symbols<0 or args.pace_seconds<0:p.error('Invalid limits')
    db=args.db.resolve(); auto=args.auto_paths_db.resolve()
    if not db.is_file():p.error('Context database missing: '+str(db))
    out=execute(db,auto,args.timeframe,args.max_bars,args.pace_seconds,args.run,args.max_symbols)
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 2 if args.run and (out['rejected'] or out['remaining_missing_unique_bars']) else 0


if __name__=='__main__':sys.exit(main())
