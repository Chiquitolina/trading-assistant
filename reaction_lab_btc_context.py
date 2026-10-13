#!/usr/bin/env python3
"""Prospective, append-only BTC correlation and velocity research for ALL Base REACTIONs.

Uses the two original analytical classes delivered for this feature. A read-only
Redis-as-of adapter supplies only fully CLOSED, contiguous historical candles.
No trading, no changes to scanner/paths/core-context databases and no backfill
before the first production activation timestamp (frozen in this sidecar DB).
"""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import sys
import time
import zlib

from reaction_lab_btc_correlation_reference import BTCCorrelationAnalyzer
from reaction_lab_btc_velocity_reference import BTCVelocityContextService

TF_MS = {"1m":60_000, "5m":300_000, "15m":900_000,
         "30m":1_800_000, "1h":3_600_000, "4h":14_400_000}
READ_COUNTS = {"1m":400, "5m":400, "15m":288, "30m":240, "1h":168, "4h":150}
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS btc_events (
    event_key TEXT PRIMARY KEY, known_ts INTEGER NOT NULL,
    captured_at_ms INTEGER NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
    observation_type TEXT NOT NULL, status TEXT NOT NULL,
    source_hash TEXT NOT NULL, features_json TEXT NOT NULL,
    quality_json TEXT NOT NULL, inputs_zlib BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS btc_events_known ON btc_events(known_ts);
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at_ms INTEGER NOT NULL, checked INTEGER NOT NULL,
    inserted INTEGER NOT NULL, ready INTEGER NOT NULL, partial INTEGER NOT NULL,
    error TEXT
);
"""


def now_ms():
    return int(time.time()*1000)


def readonly(path:Path):
    return sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=20)


def open_writer(path:Path, *, activation_ms=None):
    path.parent.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(str(path),timeout=30)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA busy_timeout=30000')
    con.executescript(SCHEMA)
    moment=now_ms() if activation_ms is None else int(activation_ms)
    with con:
        con.execute('INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)', ('schema_version','1'))
        if con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]!='1':
            raise RuntimeError('Unsupported BTC context schema version')
        con.execute('INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)',('feature_start_ms',str(moment)))
    return con


def source_fingerprint(project:Path):
    names=('reaction_lab_btc_correlation_reference.py','reaction_lab_btc_velocity_reference.py',
           'reaction_lab_btc_context.py')
    digest=hashlib.sha256()
    for name in names:
        path=project/name
        digest.update(name.encode());digest.update(path.read_bytes())
    return digest.hexdigest()[:20]


def finite_json(value):
    """Clean numpy/nan/inf for strict lossless-enough numeric JSON storage."""
    import numpy as np
    if isinstance(value,dict): return {str(k):finite_json(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [finite_json(v) for v in value]
    if isinstance(value,np.generic):return finite_json(value.item())
    if isinstance(value,float) and not math.isfinite(value):return None
    if value is None or isinstance(value,(str,int,float,bool)):return value
    raise TypeError(f'Unexpected metrics type: {type(value).__name__}')


def dumps(value):
    return json.dumps(finite_json(value),ensure_ascii=False,separators=(',',':'),allow_nan=False)


def parse_redis_rows(rows):
    """Open-time timestamp and close-only evidence; raw Redis may be bytes."""
    result={}
    for raw in rows:
        try:
            item=json.loads(raw) if isinstance(raw,(str,bytes)) else dict(raw)
            ts=int(item['timestamp']);close=float(item['close'])
            if ts>=0 and math.isfinite(close) and close>0:
                result[ts]={'timestamp':ts,'close':close}
        except (ValueError,TypeError,KeyError,OverflowError,UnicodeDecodeError):
            continue
    return [result[k] for k in sorted(result)]


def contiguous_asof(raw_rows, tf, known_ts):
    """Return a single contiguous closed tail ending EXACTLY at the signal boundary.

    Never replaces missing history with later candles. Any gap truncates the
    prefix so that the original analyzers must report insufficient samples.
    """
    period=TF_MS[tf]
    expected=int(known_ts)//period*period-period
    closed=[bar for bar in parse_redis_rows(raw_rows)
            if int(bar['timestamp'])+period <= int(known_ts)]
    if not closed or closed[-1]['timestamp'] != expected:
        return []
    start=len(closed)-1
    while start>0 and closed[start]['timestamp']-closed[start-1]['timestamp']==period:
        start-=1
    return closed[start:]


class RedisAsofBuffer:
    """Original classes' get_candles adapter, with strict event-time causality."""
    def __init__(self, client, known_ts, shared_cache=None):
        self.client=client
        self.known_ts=int(known_ts)
        self.shared_cache=shared_cache if shared_cache is not None else {}
        self._filtered={}

    def get_candles(self,symbol,timeframe,as_of_ts=None):
        symbol=str(symbol).upper();tf=str(timeframe)
        if tf not in TF_MS: return []
        asof=min(self.known_ts,int(as_of_ts)) if as_of_ts is not None else self.known_ts
        cache_key=(symbol,tf,asof)
        if cache_key not in self._filtered:
            shared_key=(symbol,tf)
            if shared_key not in self.shared_cache:
                from engine.live.data.redis_market_data_protocol import history_key
                self.shared_cache[shared_key]=self.client.lrange(history_key(symbol,tf), -READ_COUNTS[tf], -1)
            self._filtered[cache_key]=contiguous_asof(self.shared_cache[shared_key],tf,asof)
        # Return copies: canonical code must not mutate the buffered snapshot.
        return [dict(bar) for bar in self._filtered[cache_key]]

    def evidence(self,symbol):
        symbols=['BTCUSDT'] if str(symbol).upper()=='BTCUSDT' else ['BTCUSDT',str(symbol).upper()]
        proof={sym:{} for sym in symbols}
        quality={}
        for tf in TF_MS:
            expected=(self.known_ts//TF_MS[tf]-1)*TF_MS[tf]
            fields={}
            for sym in symbols:
                data=self.get_candles(sym,tf)
                proof[sym][tf]=[[int(c['timestamp']),float(c['close'])] for c in data]
                fields[sym]={'bars':len(data),'latest_open_ts':data[-1]['timestamp'] if data else None,
                             'required_latest_open_ts':expected}
            quality[tf]=fields
        return proof,quality


def compute_event(event, buffer, fingerprint):
    """Pure per-signal computation using original BTC classes on as-of evidence."""
    key,symbol,side,known,observation=event
    correlation=BTCCorrelationAnalyzer(buffer).analyze(symbol)
    velocity_service=BTCVelocityContextService()
    velocity=velocity_service.evaluate(buffer,as_of_ts=int(known))
    anchor_corr=correlation.get('btc_corr_5m_4h')
    anchor_beta=correlation.get('btc_beta_5m_4h')
    relationship=velocity_service.evaluate_trade_relationship(
        velocity,side,correlation=anchor_corr,beta=anchor_beta)
    frozen={**correlation,
            'btc_velocity_15m':velocity.velocity_15m,
            'btc_velocity_1h':velocity.velocity_1h,
            'btc_signed_move_15m_pct':velocity.signed_move_15m_pct,
            'btc_signed_move_1h_pct':velocity.signed_move_1h_pct,
            'btc_direction_15m':velocity.direction_15m,
            'btc_direction_1h':velocity.direction_1h,
            'btc_direction_alignment':velocity.direction_alignment,
            'btc_velocity_state':velocity.state,
            'btc_velocity_reason':velocity.reason,
            **relationship,
            'btc_context_known_ts':int(known),
            'btc_context_schema_version':1,
            'btc_context_source_hash':fingerprint,
            'btc_context_observation_type':observation}
    required=[*('btc_corr_available_'+tf for tf in ('5m','15m','30m','1h','4h')),
              *('btc_corr_available_'+name for name in ('5m_1h','5m_4h','5m_24h'))]
    unavailable=[name for name in required if not correlation.get(name)]
    if velocity.state=='UNKNOWN':unavailable.append('btc_velocity_state')
    state='READY' if not unavailable else 'PARTIAL'
    frozen['btc_context_status']=state
    frozen['btc_context_missing']=unavailable
    proof,quality=buffer.evidence(symbol)
    quality={'snapshots':quality,'missing_features':unavailable,'reference_version':fingerprint}
    packed=zlib.compress(dumps({'known_ts':int(known),'price_closes_asof':proof}).encode('utf-8'),6)
    return state,frozen,quality,packed


def events_after_cutoff(scanner_db:Path,cutoff:int,existing:set[str],limit:int):
    """Entire scanner Base cohort, no V1/V2 filters or runner/outcome conditions."""
    if not scanner_db.is_file():return []
    with closing(readonly(scanner_db)) as con:
        rows=con.execute('''SELECT event_key,symbol,side,known_ts,observation_type
                  FROM events WHERE known_ts>=? ORDER BY known_ts DESC''',(int(cutoff),)).fetchall()
    return [r for r in rows if r[0] not in existing][:int(limit)]


def run_once(project:Path,db:Path,scanner:Path,limit:int=50,*,pilot_lookback_minutes=0,
             redis_client=None,min_free_mb=1024):
    if shutil.disk_usage(project).free < min_free_mb*1024*1024:
        raise RuntimeError('Less than configured free disk safety margin')
    con=open_writer(db,activation_ms=now_ms()-int(pilot_lookback_minutes)*60_000)
    try:
        cutoff=int(con.execute("SELECT value FROM meta WHERE key='feature_start_ms'").fetchone()[0])
        have={r[0] for r in con.execute('SELECT event_key FROM btc_events')}
        incoming=events_after_cutoff(scanner,cutoff,have,limit)
        if not incoming:
            return {'feature_start_ms':cutoff,'checked':0,'inserted':0,'ready':0,'partial':0,
                    'events_total':len(have),'note':'Only post-activation scanner REACTIONs are eligible'}
        client=redis_client
        if client is None:
            import redis
            client=redis.Redis(host=os.getenv('REDIS_HOST','127.0.0.1'),
                port=int(os.getenv('REDIS_PORT','6379')),db=int(os.getenv('REDIS_DB','0')),
                socket_timeout=15)
            client.ping()
        fingerprint=source_fingerprint(project)
        shared={}
        checked=ready=partial=inserted=0
        for event in incoming:
            checked+=1
            key,symbol,side,known,observation=event
            try:
                buffer=RedisAsofBuffer(client,int(known),shared)
                state,metrics,quality,inputs=compute_event(event,buffer,fingerprint)
                with con:
                    con.execute('''INSERT OR IGNORE INTO btc_events
                         (event_key,known_ts,captured_at_ms,symbol,side,observation_type,
                          status,source_hash,features_json,quality_json,inputs_zlib)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                         (key,int(known),now_ms(),symbol,side,observation,state,fingerprint,
                          dumps(metrics),dumps(quality),inputs))
                inserted+=1;ready+=state=='READY';partial+=state!='READY'
            except Exception as exc:
                # Transient Redis/computation failure should remain eligible for retry.
                print(dumps({'event_key':key,'error':type(exc).__name__+': '+str(exc)}),flush=True)
        with con:
            con.execute('INSERT INTO runs(at_ms,checked,inserted,ready,partial,error) VALUES(?,?,?,?,?,NULL)',
                        (now_ms(),checked,inserted,ready,partial))
        total=con.execute('SELECT COUNT(*) FROM btc_events').fetchone()[0]
        return {'feature_start_ms':cutoff,'checked':checked,'inserted':inserted,
                'ready':ready,'partial':partial,'events_total':total,'reference_hash':fingerprint}
    finally:
        con.close()


def status(db:Path):
    if not db.is_file(): return {'database_exists':False}
    with closing(readonly(db)) as con:
        states=dict(con.execute('SELECT status,COUNT(*) FROM btc_events GROUP BY status'))
        cutoff=con.execute("SELECT value FROM meta WHERE key='feature_start_ms'").fetchone()
        last=con.execute('SELECT at_ms,checked,inserted,ready,partial FROM runs ORDER BY run_id DESC LIMIT 1').fetchone()
    return {'database_exists':True,'feature_start_ms':int(cutoff[0]) if cutoff else None,
            'total':sum(states.values()),'states':states,'last_run':last}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',type=Path,default=Path.cwd())
    parser.add_argument('--db',type=Path)
    parser.add_argument('--scanner-db',type=Path)
    parser.add_argument('--limit',type=int,default=50)
    parser.add_argument('--poll-seconds',type=int,default=60)
    parser.add_argument('--min-free-mb',type=int,default=1024)
    parser.add_argument('--pilot-lookback-minutes',type=int,default=0)
    mode=parser.add_mutually_exclusive_group(required=True)
    for name in ('check','once','loop','status'):mode.add_argument('--'+name,action='store_true')
    args=parser.parse_args(argv)
    root=args.project.resolve()
    default_db=root/'reports/reaction_lab/btc_context_v1.sqlite3'
    db=(args.db or default_db).resolve()
    scanner=(args.scanner_db or root/'reports/reaction_lab/scanner_events_v1.sqlite3').resolve()
    if args.status:
        print(json.dumps(status(db),indent=2,ensure_ascii=False));return 0
    if args.limit <1 or args.poll_seconds<1:parser.error('Positive limits required')
    if args.pilot_lookback_minutes:
        if not args.once or args.db is None or db==default_db or not 0<args.pilot_lookback_minutes<=90:
            parser.error('--pilot-lookback-minutes requires --once and a separate pilot DB; max 90 minutes')
    if args.check:
        print(dumps({'check':'PASS','scanner_exists':scanner.is_file(),
              'reference_source_hash':source_fingerprint(root),
              'production_db_exists':default_db.is_file(),
              'note':'Check reads files only; no SQLite mutation'}));return 0
    try:
        from dotenv import load_dotenv
        load_dotenv(root/'.env')
    except ImportError:pass
    db.parent.mkdir(parents=True,exist_ok=True)
    with db.with_suffix(db.suffix+'.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        stopping=False
        def stop(sig,frame):
            nonlocal stopping
            stopping=True
        signal.signal(signal.SIGTERM,stop)
        signal.signal(signal.SIGINT,stop)
        while not stopping:
            begin=time.monotonic()
            try:
                result=run_once(root,db,scanner,args.limit,
                         pilot_lookback_minutes=args.pilot_lookback_minutes,
                         min_free_mb=args.min_free_mb)
                print(dumps(result),flush=True)
            except Exception as exc:
                print(dumps({'run_error':type(exc).__name__+': '+str(exc)}),flush=True)
                if args.once:return 1
            if args.once:break
            while not stopping and time.monotonic()<begin+args.poll_seconds:time.sleep(.25)
    return 0

if __name__=='__main__':
    raise SystemExit(main())
