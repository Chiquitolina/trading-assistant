#!/usr/bin/env python3
"""Universal Runner Confluence V1: isolated, auditable 15m/30m Binance USD-M candles.

Read-only toward existing DBs, Redis, and LIVE. Downloads only in explicit
--download mode; --plan and --audit use no network and do not mutate anything.
No fill/interpolation. Incomplete symbol listings are recorded, not called FULL.

Usage:
 python3 universal_runner_horizontal_history_v1.py --plan
 python3 universal_runner_horizontal_history_v1.py --download --symbols AAVEUSDT,BTCUSDT
 python3 universal_runner_horizontal_history_v1.py --audit --symbols AAVEUSDT,BTCUSDT
"""
from __future__ import annotations
import argparse
import csv
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

VERSION = 'universal-runner-horizontal-history-v1.0'
BASE = Path('reports/reaction_lab/universal_runner_horizontal_history_v1')
TOUCHES = Path('reports/reaction_lab/scanner_first_touches_v1.sqlite3')
ENDPOINT = 'https://fapi.binance.com/fapi/v1/klines'
TF = {'15m':900_000, '30m':1_800_000}
FIELDS = ('timestamp','open','high','low','close','volume','quoteVolume')
DEFAULT_START = '2026-10-22T00:00:00Z'  # >= 40h before first retest 24-Oct
DEFAULT_END = '2026-11-08T01:00:00Z'  # frozen snapshot, 8-Nov 00:59 UTC latest


def to_ms(s):
    if not s.endswith('Z'):
        raise ValueError('Use UTC with trailing Z')
    return int(datetime.fromisoformat(s[:-1]+'+00:00').timestamp()*1000)


def utc(t):
    return datetime.fromtimestamp(int(t)/1000,timezone.utc).isoformat()


def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()


def symbols_for(project,explicit=''):
    if explicit:
        names=[s.strip().upper() for s in explicit.split(',') if s.strip()]
    else:
        path=project/TOUCHES
        if not path.is_file():raise FileNotFoundError(f'Cannot read cohort: {path}')
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as c:
            c.execute('PRAGMA query_only=ON')
            names=[r[0] for r in c.execute("SELECT DISTINCT symbol FROM first_touches WHERE status IN ('REACTION','TOUCH_INDECISIVE','TOUCH_FAILED')")]
    if not names or len(names)!=len(set(names)):
        raise ValueError('Missing or duplicate symbols')
    if any(not re.fullmatch(r'[A-Z0-9\u3400-\u9FFF]{2,40}',s) for s in names):
        raise ValueError('Invalid symbol list')
    return sorted(names)


def folder_for(project,start,end,output=None):
    if output:
        return output.resolve()
    d1=datetime.fromtimestamp(start/1000,timezone.utc).strftime('%Y%m%dT%H%M')
    d2=datetime.fromtimestamp(end/1000,timezone.utc).strftime('%Y%m%dT%H%M')
    return project/BASE/f'run_{d1}_{d2}'


def validate_row(row,sym,tf):
    try:
        ts=int(row['timestamp'])
        vals=[float(row[k]) for k in FIELDS[1:]]
    except (KeyError,ValueError,TypeError) as exc:
        raise ValueError(f'Invalid candle fields {sym} {tf}: {row}') from exc
    if ts%TF[tf] or not all(math.isfinite(v) for v in vals):
        raise ValueError(f'Unaligned/nonfinite OHLCV {sym} {tf} {ts}')
    o,h,l,c,v,q=vals
    if min(o,h,l,c)<=0 or min(v,q)<0 or h<max(o,l,c) or l>min(o,h,c):
        raise ValueError(f'Bad OHLCV {sym} {tf} {ts}')
    return (ts,*vals)


def validate_csv(path,sym,tf,start,end):
    if not path.is_file():raise FileNotFoundError(str(path))
    count=0;first=None;last=None
    with path.open(newline='',encoding='utf-8-sig') as f:
        rd=csv.DictReader(f)
        if tuple(rd.fieldnames or ())!=FIELDS:
            raise ValueError(f'Unexpected CSV schema: {path}')
        for raw in rd:
            row=validate_row(raw,sym,tf)
            ts=row[0]
            if not start<=ts<end:
                raise ValueError(f'Outside requested interval {path}: {ts}')
            if last is not None and ts!=last+TF[tf]:
                raise ValueError(f'Gap/duplicate {path}: {utc(last)} => {utc(ts)}')
            if first is None:first=ts
            last=ts;count+=1
    category=('EMPTY' if count==0 else 'FULL' if first==start and last==end-TF[tf] else 'PARTIAL_LISTING_OR_END')
    return {'status':category,'rows':count,'first_open':first,'last_open':last}


def verify_symbol(folder,sym,start,end):
    location=folder/'symbols'/sym
    meta_file=location/'manifest.json'
    if not meta_file.is_file():return None
    meta=json.loads(meta_file.read_text(encoding='utf-8'))
    if meta.get('version')!=VERSION or meta.get('symbol')!=sym or meta.get('start')!=start or meta.get('end')!=end:
        raise RuntimeError(f'Wrong frozen identity {sym}')
    for tf in TF:
        info=meta['timeframes'][tf]
        file=location/f'{tf}.csv'
        if sha(file)!=info['sha256']:
            raise RuntimeError(f'Checksum mismatch {sym} {tf}')
        result=validate_csv(file,sym,tf,start,end)
        if result!=info['validation']:
            raise RuntimeError(f'Coverage mismatch {sym} {tf}')
    return meta


def fetch_page(sym,tf,cursor,end,sleep_s):
    query=urlencode({'symbol':sym,'interval':tf,'startTime':cursor,'endTime':end-1,'limit':1000})
    url=ENDPOINT+'?'+query
    for attempt in range(5):
        try:
            with urlopen(Request(url,headers={'User-Agent':'Universal-Runner-Horizontal-Offline/1.0'}),timeout=30) as f:
                data=json.load(f)
            if not isinstance(data,list):raise ValueError(f'Unexpected Binance response {sym} {tf}')
            if sleep_s:time.sleep(sleep_s)
            return data
        except HTTPError as exc:
            if exc.code==418:
                raise RuntimeError('Binance HTTP 418: stop immediately') from exc
            if exc.code not in (429,500,502,503,504):
                raise RuntimeError(f'Binance HTTP {exc.code} for {sym} {tf}') from exc
            try:retry_after=int(exc.headers.get('Retry-After','60'))
            except (ValueError,TypeError):retry_after=60
            pause=max(60,retry_after) if exc.code==429 else min(30,2**(attempt+1))
        except (URLError,TimeoutError,OSError) as exc:
            pause=min(30,2**(attempt+1))
        if attempt<4:time.sleep(min(pause,180))
    raise RuntimeError(f'Failed Binance download {sym} {tf}')


def download_tf(sym,tf,start,end,sleep_s=.30,fetcher=fetch_page):
    cursor=start;rows=[];last=None
    while cursor<end:
        page=fetcher(sym,tf,cursor,end,sleep_s)
        if not page:break
        for item in page:
            if len(item)<8:raise ValueError(f'Incomplete Binance kline {sym} {tf}')
            row=validate_row(dict(zip(FIELDS,(int(item[0]),item[1],item[2],item[3],item[4],item[5],item[7]))),sym,tf)
            ts=row[0]
            if not cursor<=ts<end:raise ValueError(f'Non-monotonic/out-of-range candle {sym} {tf}: {ts}')
            if last is not None and ts!=last+TF[tf]:raise ValueError(f'Internal Binance gap {sym} {tf}: {utc(last)} => {utc(ts)}')
            rows.append(row);last=ts
        next_cursor=rows[-1][0]+TF[tf]
        if next_cursor<=cursor:raise ValueError(f'Pagination did not advance for {sym} {tf}')
        cursor=next_cursor
    return rows


def write_csv(path,rows):
    with path.open('w',newline='',encoding='utf-8') as f:
        w=csv.writer(f);w.writerow(FIELDS);w.writerows(rows)
        f.flush();os.fsync(f.fileno())


def download_symbol(folder,sym,start,end,sleep_s,fetcher=fetch_page):
    dest=folder/'symbols'/sym
    if dest.exists():
        if verify_symbol(folder,sym,start,end) is None:
            raise RuntimeError(f'Incomplete pre-existing output directory: {dest}')
        return 'ALREADY_VERIFIED'
    folder.mkdir(parents=True,exist_ok=True)
    (folder/'symbols').mkdir(exist_ok=True)
    stage=folder/'staging';stage.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f'{sym}.',dir=stage) as tmp:
        tmp=Path(tmp);details={}
        for tf in TF:
            rows=download_tf(sym,tf,start,end,sleep_s,fetcher=fetcher)
            dest_file=tmp/f'{tf}.csv'
            write_csv(dest_file,rows)
            details[tf]={'sha256':sha(dest_file),'validation':validate_csv(dest_file,sym,tf,start,end)}
        manifest={'version':VERSION,'symbol':sym,'start':start,'end':end,'timeframes':details,
                  'downloaded_utc':utc(int(time.time()*1000)), 'source':'BINANCE_USDM_PUBLIC_KLINES'}
        (tmp/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding='utf-8')
        if dest.exists():raise RuntimeError(f'Concurrent output exists {dest}')
        os.rename(tmp,dest)
    verify_symbol(folder,sym,start,end)
    return 'DOWNLOADED_VERIFIED'


def lock(folder):
    folder.mkdir(parents=True,exist_ok=True)
    f=(folder/'.download.lock').open('a+')
    try:fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        f.close();raise RuntimeError('Another horizontal downloader is running')
    return f


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    mode=p.add_mutually_exclusive_group(required=True)
    for flag in ('plan','download','audit'):mode.add_argument('--'+flag,action='store_true')
    p.add_argument('--project',type=Path,default=Path(__file__).resolve().parent)
    p.add_argument('--start',default=DEFAULT_START)
    p.add_argument('--end',default=DEFAULT_END)
    p.add_argument('--symbols',default='')
    p.add_argument('--output-dir',type=Path)
    p.add_argument('--batch-size',type=int,default=10)
    p.add_argument('--min-free-gb',type=float,default=10.)
    p.add_argument('--sleep-seconds',type=float,default=.30)
    a=p.parse_args(argv)
    root=a.project.resolve();start=to_ms(a.start);end=to_ms(a.end)
    if end<=start or any(start%v or end%v for v in TF.values()):raise ValueError('Align start/end to 30m UTC, start < end')
    if a.batch_size<=0 or a.min_free_gb<0 or not 0<=a.sleep_seconds<=10:raise ValueError('Bad batch/disk/sleep values')
    syms=symbols_for(root,a.symbols)
    folder=folder_for(root,start,end,a.output_dir)
    if a.download and end>int(time.time()*1000)//1_800_000*1_800_000:
        raise ValueError('End is not a past fully closed 30m boundary; refuse incomplete current candle')
    verified=[];missing=[];issues=[];cover=Counter()
    for sym in syms:
        try:
            m=verify_symbol(folder,sym,start,end)
            if m is None:missing.append(sym)
            else:
                verified.append(sym)
                cover.update(m['timeframes'][tf]['validation']['status'] for tf in TF)
        except Exception as exc:
            issues.append({'symbol':sym,'error':str(exc)})
    result={'version':VERSION,'symbols':len(syms),'verified_symbols':len(verified),
            'missing_symbols':len(missing),'examples_missing':missing[:10],
            'issues':issues[:20], 'coverage_timeframes':dict(cover),
            'start':utc(start),'end_exclusive':utc(end), 'output_dir':str(folder),
            'disk_free_gb':round(shutil.disk_usage(root).free/1024**3,2)}
    if a.plan:
        result.update(mode='PLAN_READ_ONLY',network=False)
        print('UNIVERSAL_HORIZONTAL_PLAN',json.dumps(result,ensure_ascii=False));return 0
    if a.audit:
        result.update(mode='AUDIT_READ_ONLY',success=not missing and not issues)
        print('UNIVERSAL_HORIZONTAL_AUDIT',json.dumps(result,ensure_ascii=False));return 0 if result['success'] else 2
    if issues:raise RuntimeError(f'Corrupted source: {issues[:3]}')
    with lock(folder):
        count=0
        for sym in missing:
            if count>=a.batch_size:break
            free=shutil.disk_usage(root).free/1024**3
            if free<a.min_free_gb:raise RuntimeError(f'Only {free:.2f} GB free, minimum {a.min_free_gb} GB')
            print('UNIVERSAL_HORIZONTAL_DOWNLOADING',sym,flush=True)
            status=download_symbol(folder,sym,start,end,a.sleep_seconds)
            count+=1
            print('UNIVERSAL_HORIZONTAL_SYMBOL',json.dumps({'symbol':sym,'status':status},ensure_ascii=False),flush=True)
        print('UNIVERSAL_HORIZONTAL_BATCH',json.dumps({'new_symbols':count,'initially_missing':len(missing)},ensure_ascii=False),flush=True)
    return 0


if __name__=='__main__':raise SystemExit(main())
