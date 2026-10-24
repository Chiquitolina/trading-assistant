#!/usr/bin/env python3
"""Persistent, independent closed-1m paths and MFE/MAE for Continuous V1 first touches.

Never mutates the causal first-touch journal, existing REACTION-only Auto Paths,
Market Data, or LIVE. Redis closed 1m only; no REST/imagined candles. One locked
writer; incremental, contiguous paths (360 bars) in a separate WAL SQLite DB.
"""
from __future__ import annotations
import argparse
from contextlib import closing
import fcntl
import json
import math
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import struct
import time
import zlib

BAR=struct.Struct('<qdddd')
MINUTE=60_000
HORIZONS=(15,30,60,120,180,360)
MAX_BARS=360
REDIS_LIMIT=400
# Gross structural TP/SL; no fill cost, spread, funding, latency or slippage.
TP_SL=((0.5,0.5),(0.75,0.75),(1.0,1.0),(2.0,1.0),(2.0,1.5),(3.0,1.5))
SCHEMA='''
CREATE TABLE IF NOT EXISTS paths(
 touch_key TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL,
 status TEXT NOT NULL, retest_ts INTEGER NOT NULL, entry_ts INTEGER NOT NULL,
 retest_close REAL, first_seen_ms INTEGER NOT NULL,
 state TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
 last_ts INTEGER, path_blob BLOB, last_error TEXT, updated_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ft_paths_state_symbol ON paths(state,symbol);
CREATE TABLE IF NOT EXISTS metrics(
 touch_key TEXT NOT NULL, horizon INTEGER NOT NULL, perspective TEXT NOT NULL,
 mfe_pct REAL NOT NULL, mae_pct REAL NOT NULL, return_pct REAL NOT NULL,
 reference_price REAL NOT NULL, entry_open REAL NOT NULL,
 PRIMARY KEY(touch_key,horizon,perspective)
);
CREATE INDEX IF NOT EXISTS idx_ft_metrics_horizon ON metrics(horizon,perspective);
CREATE TABLE IF NOT EXISTS outcomes(
 touch_key TEXT NOT NULL, horizon INTEGER NOT NULL, perspective TEXT NOT NULL,
 tp REAL NOT NULL, sl REAL NOT NULL, outcome TEXT NOT NULL, exit_minute INTEGER,
 gross_return_pct REAL,
 PRIMARY KEY(touch_key,horizon,perspective,tp,sl)
);
CREATE INDEX IF NOT EXISTS idx_ft_outcomes_grid ON outcomes(horizon,perspective,tp,sl,outcome);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
'''


def source_db(project):
    return Path(project)/'reports/reaction_lab/scanner_first_touches_v1.sqlite3'


def destination_db(project):
    return Path(project)/'reports/reaction_lab/first_touch_paths_v2.sqlite3'


def open_db(path):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(path,timeout=30)
    con.execute('PRAGMA busy_timeout=30000')
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA synchronous=NORMAL')
    con.executescript(SCHEMA)
    con.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version','2')")
    version=con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    if version!='2':
        con.close(); raise RuntimeError('Wrong first touch paths schema '+version)
    con.commit()
    return con


def decode(blob):
    if not blob:return []
    raw=zlib.decompress(blob)
    if len(raw)%BAR.size:raise ValueError('Corrupted path blob')
    return [BAR.unpack_from(raw,i) for i in range(0,len(raw),BAR.size)]


def encode(bars):
    return zlib.compress(b''.join(BAR.pack(*b) for b in bars),6) if bars else None


def normalize_bars(raws,now_ms):
    out={}
    for raw in raws:
        try:
            d=json.loads(raw)
            ts=int(d['timestamp'])
            close_ts=int(d.get('close_timestamp',ts+MINUTE-1))
            o,h,l,c=(float(d[k]) for k in ('open','high','low','close'))
            if close_ts>now_ms or ts>now_ms or ts%MINUTE or not all(math.isfinite(v) and v>0 for v in (o,h,l,c)) or h<max(o,c,l) or l>min(o,c,h):continue
            out[ts]=(ts,o,h,l,c)
        except (KeyError,TypeError,ValueError,OverflowError,json.JSONDecodeError):continue
    return out


def evaluate(bars, side, reference):
    """MFE/MAE are positive magnitudes. Includes HIGH/LOW of next 1m candle."""
    hi=max(b[2] for b in bars);lo=min(b[3] for b in bars);last=bars[-1][4]
    if side=='LONG':return max(0,(hi/reference-1)*100),max(0,(1-lo/reference)*100),(last/reference-1)*100
    return max(0,(1-lo/reference)*100),max(0,(hi/reference-1)*100),(1-last/reference)*100


def simulate(bars,side,tp,sl):
    """Entry at next 1m OPEN; both barriers in same 1m => conservative SL."""
    ref=bars[0][1]
    target=ref*(1+tp/100) if side=='LONG' else ref*(1-tp/100)
    stop=ref*(1-sl/100) if side=='LONG' else ref*(1+sl/100)
    for minute, (_,op,hi,lo,cl) in enumerate(bars,1):
        hit_tp=hi>=target if side=='LONG' else lo<=target
        hit_sl=lo<=stop if side=='LONG' else hi>=stop
        if hit_sl and hit_tp:return 'SL_AMBIGUOUS',minute,-sl
        if hit_sl:return 'SL_FIRST',minute,-sl
        if hit_tp:return 'TP_FIRST',minute,tp
    last=bars[-1][4]
    ret=(last/ref-1)*100 if side=='LONG' else (1-last/ref)*100
    return 'TIME_EXIT',len(bars),ret


def derive(con,key,side,reference,bars):
    """Immutable completed horizon metrics; no peek beyond each horizon."""
    done=[]
    for h in HORIZONS:
        if len(bars)<h:continue
        window=bars[:h]; entry=window[0][1]
        sides={'ORIGINAL_OPEN':side,'INVERSE':'SHORT' if side=='LONG' else 'LONG'}
        if reference is not None and math.isfinite(reference) and reference>0:
            sides['LEGACY_ORIGINAL']=side
        for mode,direction in sides.items():
            ref=reference if mode=='LEGACY_ORIGINAL' else entry
            mfe,mae,ret=evaluate(window,direction,ref)
            con.execute('''INSERT OR IGNORE INTO metrics
             (touch_key,horizon,perspective,mfe_pct,mae_pct,return_pct,reference_price,entry_open)
             VALUES(?,?,?,?,?,?,?,?)''',(key,h,mode,mfe,mae,ret,ref,entry))
            if mode!='LEGACY_ORIGINAL':
                for tp,sl in TP_SL:
                    outcome,minute,gross=simulate(window,direction,tp,sl)
                    con.execute('''INSERT OR IGNORE INTO outcomes
                     (touch_key,horizon,perspective,tp,sl,outcome,exit_minute,gross_return_pct)
                     VALUES(?,?,?,?,?,?,?,?)''',(key,h,mode,tp,sl,outcome,minute,gross))
        done.append(h)
    return done


def register(con,source,now_ms):
    added=stale=0
    with closing(sqlite3.connect(f'file:{Path(source).resolve()}?mode=ro',uri=True,timeout=10)) as src:
        src.execute('PRAGMA query_only=ON')
        rows=src.execute('''SELECT touch_key,symbol,side,status,retest_ts,first_seen_ms,causal_json
          FROM first_touches ORDER BY retest_ts DESC''')
        for key,symbol,side,status,ts,seen,payload in rows:
            entry=int(ts)+MINUTE
            state='STALE_BEFORE_COLLECTOR' if now_ms-entry>REDIS_LIMIT*MINUTE else 'WAITING'
            row=json.loads(payload)
            try:ref=float(row.get('retest_close'))
            except (ValueError,TypeError):ref=None
            if ref is not None and (not math.isfinite(ref) or ref<=0):ref=None
            res=con.execute('''INSERT OR IGNORE INTO paths
              (touch_key,symbol,side,status,retest_ts,entry_ts,retest_close,first_seen_ms,state,count,updated_ms)
              VALUES (?,?,?,?,?,?,?,?,?,0,?)''',
              (key,symbol,side,status,int(ts),entry,ref,int(seen),state,now_ms))
            if res.rowcount:added+=1;stale+=state=='STALE_BEFORE_COLLECTOR'
    con.commit()
    return {'registered':added,'stale_new':stale}


def progress(con,redis_client,now_ms):
    rows=con.execute("SELECT touch_key,symbol,side,retest_close,entry_ts,state,count,path_blob FROM paths WHERE state IN ('WAITING','ACTIVE','GAP_WAITING') ORDER BY symbol,entry_ts").fetchall()
    groups={}
    for r in rows:groups.setdefault(r[1],[]).append(r)
    result={'active':len(rows),'symbols':len(groups),'extended':0,'completed_360':0,'gap_expired':0,'missing_entry':0,'redis_errors':0}
    for symbol,items in groups.items():
        required=min(REDIS_LIMIT,max(16,(now_ms-min(r[4]+r[6]*MINUTE for r in items))//MINUTE+5))
        try:by_ts=normalize_bars(redis_client.lrange(f'market-data:v1:history:{symbol}:1m',-int(required),-1),now_ms)
        except Exception as exc:
            print(f'FT_PATH_REDIS_ERROR symbol={symbol} error={exc}',flush=True)
            result['redis_errors']+=1;continue
        if not by_ts:continue
        first,last=min(by_ts),max(by_ts)
        for key,_,side,reference,entry,state,count,blob in items:
            saved=decode(blob)
            if count!=len(saved) or any(b[0]!=entry+i*MINUTE for i,b in enumerate(saved)):
                raise ValueError('Noncontiguous or corrupt saved event '+key)
            needed=entry+count*MINUTE
            newstate=state;reason=None
            if first>needed:
                newstate='MISSING_ENTRY' if count==0 else 'GAP_EXPIRED'
                reason=f'Required 1m {needed} older than Redis earliest {first}'
                result['missing_entry' if count==0 else 'gap_expired']+=1
            else:
                while len(saved)<MAX_BARS:
                    item=by_ts.get(entry+len(saved)*MINUTE)
                    if item is None:break
                    saved.append(item)
                if len(saved)>count:result['extended']+=1
                if len(saved)==MAX_BARS:
                    newstate='COMPLETE';result['completed_360']+=state!='COMPLETE'
                elif last>=entry+len(saved)*MINUTE:
                    newstate='GAP_WAITING';reason='Awaiting producer repair of missing closed 1m'
                else:newstate='ACTIVE' if saved else 'WAITING'
            if len(saved)!=count or newstate!=state:
                # Complete metrics are immutable; recompute if newly extended only.
                if len(saved)>count:derive(con,key,side,reference,saved)
                con.execute('''UPDATE paths SET state=?,count=?,last_ts=?,path_blob=?,last_error=?,updated_ms=? WHERE touch_key=?''',
                    (newstate,len(saved),saved[-1][0] if saved else None,encode(saved),reason,now_ms,key))
        con.commit()
    return result


def status(path):
    if not Path(path).is_file():return {'database_exists':False}
    with closing(sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro',uri=True)) as con:
        return {'database_exists':True,
                'paths':dict(con.execute('SELECT state,COUNT(*) FROM paths GROUP BY state')),
                'evaluable':{f'{p}_{h}':n for p,h,n in con.execute('SELECT perspective,horizon,COUNT(*) FROM metrics GROUP BY perspective,horizon')},
                'stored_1m_bars':con.execute('SELECT COALESCE(SUM(count),0) FROM paths').fetchone()[0]}


def run_once(project,db,client,now_ms,min_free_mb=1024):
    if Path(db).resolve()==source_db(project).resolve():raise ValueError('Refusing to overwrite causal journal')
    if not source_db(project).exists():raise FileNotFoundError(source_db(project))
    parent=Path(db).parent if Path(db).parent.exists() else Path(project)
    free=shutil.disk_usage(parent).free//(1024*1024)
    if free<min_free_mb:raise RuntimeError(f'Free disk {free} MB below {min_free_mb} MB')
    with closing(open_db(db)) as con:
        a=register(con,source_db(project),now_ms)
        b=progress(con,client,now_ms)
    return {'registration':a,'progress':b,'disk_free_mb':free}


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--project',type=Path,default=Path.cwd())
    ap.add_argument('--db',type=Path)
    mode=ap.add_mutually_exclusive_group()
    mode.add_argument('--check',action='store_true')
    mode.add_argument('--status',action='store_true')
    mode.add_argument('--run-once',action='store_true')
    mode.add_argument('--loop',action='store_true')
    ap.add_argument('--poll-seconds',type=int,default=60)
    ap.add_argument('--min-free-mb',type=int,default=1024)
    args=ap.parse_args(argv)
    project=args.project.resolve(); db=(args.db or destination_db(project)).resolve()
    if args.status:print(json.dumps(status(db),ensure_ascii=False,indent=2));return 0
    if not args.loop and not args.run_once:
        print(json.dumps({'mode':'CHECK','source_exists':source_db(project).is_file(),'dest_exists':db.is_file(),
                          'source':str(source_db(project)),'destination':str(db),'no_mutations':True},indent=2));return 0
    import redis
    try:
        from dotenv import load_dotenv
        load_dotenv(project/'.env')
    except ImportError:pass
    client=redis.Redis(host=os.getenv('REDIS_HOST','127.0.0.1'),port=int(os.getenv('REDIS_PORT','6379')),
                       db=int(os.getenv('REDIS_DB','0')),socket_timeout=12)
    client.ping()
    lock=db.with_suffix(db.suffix+'.lock');lock.parent.mkdir(parents=True,exist_ok=True)
    halted=False
    def stop(signum,frame):
        nonlocal halted
        halted=True
    signal.signal(signal.SIGINT,stop);signal.signal(signal.SIGTERM,stop)
    with lock.open('a+') as handle:
        try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('First-touch paths worker already running')
        while not halted:
            try:
                print('FT_PATH_CYCLE '+json.dumps(run_once(project,db,client,int(time.time()*1000),args.min_free_mb),ensure_ascii=False),flush=True)
            except Exception as exc:
                print('FT_PATH_CYCLE_ERROR '+repr(exc),flush=True)
                if not args.loop:raise
            if not args.loop:break
            for _ in range(max(1,args.poll_seconds)):
                if halted:break
                time.sleep(1)
    return 0

if __name__=='__main__':raise SystemExit(main())
