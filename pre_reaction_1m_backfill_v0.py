#!/usr/bin/env python3
"""Bounded, independent native Binance USDM 1m history cache for Pre-Reaction Lab.

Writes ONLY to --cache (default separate research cache); never changes Candidate
cache, scanner DB, Redis, LIVE, or application code. Month files intentionally
contain ONLY the requested pilot window, NOT a full month. Coverage is proven by
per-symbol manifests. Read-only --plan and --audit; network only in --run.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ONE = 60000
VERSION = 'pre-reaction-1m-pilot-backfill-v0.1'
DEFAULT_CACHE = Path('reports/reaction_lab/pre_reaction_opportunity_lab_v0/native_1m_pilot_cache')
API = 'https://fapi.binance.com/fapi/v1/klines'
COLS = ('timestamp', 'open', 'high', 'low', 'close', 'volume')


def as_ms(s):
    dt = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    if dt.tzinfo is None: raise ValueError('UTC offset required')
    return int(dt.timestamp()*1000)


def iso(ts): return datetime.fromtimestamp(ts/1000, timezone.utc).isoformat()


def file_sha(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(1048576),b''): h.update(chunk)
    return h.hexdigest()


def range_ms(start,end):
    if start>=end or start%ONE or end%ONE: raise ValueError('Start/end must be aligned UTC 1m and start < end')
    # Three hours to warm up confirmed 3x3 swings; six hours to resolve retests
    return start-180*ONE, end+361*ONE


def months_for(a,b):
    out=set()
    for ts in range(a,b,ONE):
        dt=datetime.fromtimestamp(ts/1000,timezone.utc)
        out.add(f'{dt.year:04d}-{dt.month:02d}')
    return sorted(out)


def target_files(cache,symbol,a,b):
    return [cache / symbol / (m+'.pkl.gz') for m in months_for(a,b)]


def read_symbol(cache,symbol,a,b,verify_hash=True):
    import pandas as pd
    manifest_file=cache/'manifests'/f'{symbol}.json'
    if not manifest_file.exists(): raise FileNotFoundError(f'No completed manifest for {symbol}')
    manifest=json.loads(manifest_file.read_text(encoding='utf-8'))
    if manifest.get('version')!=VERSION or manifest.get('symbol')!=symbol or manifest.get('requested_start_ms')!=a or manifest.get('requested_end_exclusive_ms')!=b:
        raise RuntimeError(f'Cache identity mismatch {symbol}. Select a different --cache or restore original window.')
    expected_files=target_files(cache,symbol,a,b)
    frames=[]
    for p in expected_files:
        if not p.exists(): raise RuntimeError(f'Missing partition {p}')
        rel=str(p.relative_to(cache))
        if verify_hash and file_sha(p)!=manifest['sha256'].get(rel): raise RuntimeError(f'Partition hash changed: {p}')
        df=pd.read_pickle(p,compression='gzip')
        if list(df.columns)!=list(COLS): raise RuntimeError(f'Schema mismatch: {p}')
        frames.append(df)
    frame=pd.concat(frames,ignore_index=True).sort_values('timestamp').reset_index(drop=True)
    if len(frame)!=(b-a)//ONE: raise RuntimeError(f'Incomplete {symbol}: {len(frame)}/{(b-a)//ONE} 1m bars')
    stamp=frame.timestamp.astype('int64').to_numpy()
    if stamp[0]!=a or stamp[-1]!=b-ONE or (stamp[1:]-stamp[:-1]!=ONE).any():
        raise RuntimeError(f'Gap or duplication in {symbol} native 1m')
    if frame[list(COLS[1:])].isna().any().any() or (frame[['open','high','low','close']]<=0).any().any() or (frame.volume<0).any() or (frame.high < frame[['open','close','low']].max(axis=1)).any() or (frame.low > frame[['open','close','high']].min(axis=1)).any():
        raise RuntimeError(f'Invalid OHLCV: {symbol}')
    return frame,manifest


def parse_page(raw,symbol,start,end):
    if not isinstance(raw,list) or len(raw)>1000: raise RuntimeError(f'Invalid Binance response {symbol}')
    out={}
    for item in raw:
        if not isinstance(item,list) or len(item)<7: raise RuntimeError(f'Invalid kline shape {symbol}')
        ts=int(item[0])
        if not (start<=ts<end) or ts%ONE!=0: raise RuntimeError(f'Unexpected timestamp {symbol} {ts}')
        if int(item[6]) != ts+ONE-1: raise RuntimeError(f'Bad closeTime {symbol} {ts}')
        nums=tuple(float(item[i]) for i in (1,2,3,4,5))
        if not all(__import__('math').isfinite(v) for v in nums):raise RuntimeError(f'Nonfinite kline {symbol} {ts}')
        o,h,l,c,v=nums
        if o<=0 or h<=0 or l<=0 or c<=0 or v<0 or h<max(o,l,c) or l>min(o,h,c):raise RuntimeError(f'Bad OHLCV {symbol} {ts}')
        rec=(ts,o,h,l,c,v)
        if ts in out and out[ts]!=rec: raise RuntimeError(f'Conflict {symbol} {ts}')
        out[ts]=rec
    return [out[t] for t in sorted(out)]


def request_page(symbol,start,end,tries=3):
    params=urlencode({'symbol':symbol,'interval':'1m','startTime':start,'endTime':end-1,'limit':1000})
    for attempt in range(tries):
        try:
            req=Request(API+'?'+params,headers={'User-Agent':'pre-reaction-opportunity-research/0.1'})
            with urlopen(req,timeout=25) as res: raw=json.loads(res.read(600_000))
            return parse_page(raw,symbol,start,end)
        except HTTPError as exc:
            if exc.code in (418,429):raise RuntimeError(f'Binance rate limit HTTP {exc.code}: stop') from exc
            if attempt==tries-1:raise
        except (URLError,TimeoutError,ValueError):
            if attempt==tries-1:raise
        time.sleep(min(8,2**attempt))
    raise RuntimeError('fetch exhausted')


def download_symbol(symbol,a,b,fetcher=request_page,delay=0.15):
    values=[]
    for pos in range(a,b,1000*ONE):
        last=min(pos+1000*ONE,b)
        rows=fetcher(symbol,pos,last)
        if not rows:
            raise RuntimeError(f'No Binance candles {symbol} {iso(pos)}..{iso(last)}; missing listing or historical gap')
        # Exact completeness required, no silent continuation over empty periods.
        expected=list(range(pos,last,ONE))
        actual=[x[0] for x in rows]
        if actual != expected:
            first=next(((i,x) for i,(x,y) in enumerate(zip(actual,expected)) if x!=y),None)
            raise RuntimeError(f'Incomplete native 1m {symbol} {iso(pos)}..{iso(last)} got={len(actual)} expected={len(expected)} first_mismatch={first}')
        values.extend(rows)
        if delay:time.sleep(delay)
    return values


def check_disk(cache,min_free):
    existing=cache if cache.exists() else cache.parent
    while not existing.exists():existing=existing.parent
    free=shutil.disk_usage(existing).free/(1024**3)
    if free<min_free:raise RuntimeError(f'Low disk: {free:.2f}GiB free < {min_free}GiB')
    return round(free,2)


def write_symbol(cache,symbol,a,b,rows):
    import pandas as pd
    files=target_files(cache,symbol,a,b)
    manifest_file=cache/'manifests'/f'{symbol}.json'
    if manifest_file.exists():
        frame,manifest=read_symbol(cache,symbol,a,b)
        return {'symbol':symbol,'status':'EXISTING_VALID','rows':len(frame)}
    if any(p.exists() for p in files):
        raise RuntimeError(f'Untracked / partial partitions for {symbol}; refusing overwrite: {[str(p) for p in files if p.exists()]}')
    # Split month partitions, no modification of any existing cache.
    df=pd.DataFrame(rows,columns=COLS)
    if len(df)!=(b-a)//ONE or df.timestamp.iloc[0]!=a or df.timestamp.iloc[-1]!=b-ONE:
        raise RuntimeError('Downloaded window is incomplete')
    df['month']=pd.to_datetime(df.timestamp,unit='ms',utc=True).dt.strftime('%Y-%m')
    digest={}
    try:
        for p in files:
            p.parent.mkdir(parents=True,exist_ok=True)
            part=df[df.month==p.stem.split('.')[0]][list(COLS)].copy()
            if part.empty:raise RuntimeError(f'Empty requested partition: {p}')
            tmp=p.with_name(p.name+'.tmp')
            part.to_pickle(tmp,compression='gzip')
            os.replace(tmp,p)
            digest[str(p.relative_to(cache))]=file_sha(p)
        manifest={'version':VERSION,'symbol':symbol,'requested_start_ms':a,'requested_end_exclusive_ms':b,
                  'origin':'Binance USD-M native futures /fapi/v1/klines 1m',
                  'monthly_files_are_PARTIAL_window':True,'candle_count':len(rows), 'sha256':digest}
        manifest_file.parent.mkdir(parents=True,exist_ok=True)
        tmp=manifest_file.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(manifest,indent=2),encoding='utf-8')
        os.replace(tmp,manifest_file)
        verified,_=read_symbol(cache,symbol,a,b)
    except Exception:
        # Do not automatically delete data; leave files for operator inspection.
        raise
    return {'symbol':symbol,'status':'DOWNLOADED_VERIFIED','rows':len(verified),'files':len(files)}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true');mode.add_argument('--run',action='store_true');mode.add_argument('--audit',action='store_true')
    p.add_argument('--start',default='2026-10-12T00:00:00Z');p.add_argument('--end',default='2026-10-13T00:00:00Z')
    p.add_argument('--symbols',default='HANAUSDT,TAKEUSDT,ETHUSDT')
    p.add_argument('--cache',type=Path,default=DEFAULT_CACHE)
    p.add_argument('--min-free-gb',type=float,default=4.0)
    args=p.parse_args(argv)
    start,end=as_ms(args.start),as_ms(args.end)
    a,b=range_ms(start,end)
    cache=args.cache.resolve();symbols=list(dict.fromkeys(x.strip().upper() for x in args.symbols.split(',') if x.strip()))
    if not symbols:raise ValueError('No symbols')
    if any(not __import__('re').fullmatch(r'[A-Z0-9]{2,24}',x) for x in symbols):raise ValueError('Invalid symbol')
    if args.plan:
        print('PRE_REACTION_1M_BACKFILL_PLAN',json.dumps({'cache':str(cache),'pilot_start':iso(start),'pilot_end_exclusive':iso(end),
             'data_start':iso(a),'data_end_exclusive':iso(b),'required_bars_per_symbol':(b-a)//ONE,
             'symbols':[{ 'symbol':s,'monthly_partitions':[str(x) for x in target_files(cache,s,a,b)],
                  'completed':(cache/'manifests'/f'{s}.json').is_file()} for s in symbols],
             'disk_gb_free':check_disk(cache,0), 'month_files_partial':True,'origin':'Binance USD-M native 1m'},indent=2))
        return
    if args.audit:
        for s in symbols:
            frame,manifest=read_symbol(cache,s,a,b)
            print('PRE_REACTION_1M_BACKFILL_AUDIT',json.dumps({'symbol':s,'success':True,'rows':len(frame),'from':iso(a),'to_exclusive':iso(b)}))
        return
    cache.mkdir(parents=True,exist_ok=True)
    with (cache/'.pilot_backfill.lock').open('a+') as handle:
        try:fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:raise RuntimeError('Backfill already running') from exc
        for symbol in symbols:
            check_disk(cache,args.min_free_gb)
            manifest_file=cache/'manifests'/f'{symbol}.json'
            if manifest_file.exists():
                frame,_=read_symbol(cache,symbol,a,b)
                print('PRE_REACTION_1M_BACKFILL',json.dumps({'symbol':symbol,'status':'EXISTING_VALID','rows':len(frame)}),flush=True)
                continue
            if any(x.exists() for x in target_files(cache,symbol,a,b)):
                raise RuntimeError(f'Partial pre-existing partitions for {symbol}; refusing overwrite')
            print('PRE_REACTION_1M_DOWNLOADING',symbol,'bars=',(b-a)//ONE,flush=True)
            rows=download_symbol(symbol,a,b)
            check_disk(cache,args.min_free_gb)
            print('PRE_REACTION_1M_BACKFILL',json.dumps(write_symbol(cache,symbol,a,b,rows)),flush=True)


if __name__=='__main__':main()
