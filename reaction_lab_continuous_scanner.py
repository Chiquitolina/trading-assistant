#!/usr/bin/env python3
"""Continuous Redis-only 15m/3x3 REACTION scanner, independent of Streamlit.

Canonical detection rules are extracted from dashboard/app.py at startup (AST),
not reimplemented. The persisted journal contains ONLY causal fields, never
forward-derived MFE/outcome metrics. No Binance REST, orders or legacy CSV writes.
"""
from __future__ import annotations
import argparse
import ast
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import sys
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
 event_key TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL,
 retest_ts INTEGER NOT NULL, known_ts INTEGER NOT NULL,
 first_seen_ms INTEGER NOT NULL, last_seen_ms INTEGER NOT NULL,
 observation_type TEXT NOT NULL, source_hash TEXT NOT NULL,
 causal_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scan_retest ON events(retest_ts);
CREATE TABLE IF NOT EXISTS cycles (
 cycle_id INTEGER PRIMARY KEY AUTOINCREMENT, started_ms INTEGER NOT NULL,
 ended_ms INTEGER NOT NULL, symbols_requested INTEGER NOT NULL,
 symbols_with_1m INTEGER NOT NULL, symbols_with_15m INTEGER NOT NULL,
 latest_1m_ts INTEGER, events_seen INTEGER NOT NULL, new_events INTEGER NOT NULL,
 slow_cycle INTEGER NOT NULL, source_hash TEXT NOT NULL,
 error TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
CANONICAL_ROOT = 'scan_confirmed_swing_retests_all_symbols'
# These are pure helpers used by the dashboard's canonical scanner.
HELPERS = (
 '_prepare_confirmed_swing_retest_candles',
 '_build_confirmed_swing_retest_forward_cache',
 '_find_confirmed_swing_retest',
 'get_volume_exhaustion_swing_confirmation_info',
 '_confirmed_swing_volume_features_at',
 '_attach_confirmed_swing_volume_context',
 '_attach_confirmed_swing_reaction_run',
 '_attach_confirmed_swing_order_recovery_analysis',
 '_confirmed_swing_first_touch_key',
 CANONICAL_ROOT,
)
CAUSAL_FIELDS = (
 'symbol', 'signal', 'status', 'timeframe', 'detector',
 'pivot_timestamp', 'actionable_timestamp', 'pivot_price', 'swing_price',
 'entry_price', 'prominence_pct', 'pivot_to_confirmation_pct',
 'departure_timestamp', 'departure_price', 'max_departure_pct',
 'retest_timestamp', 'retest_price', 'retest_distance_pct',
 'penetration_pct', 'retest_open', 'retest_high', 'retest_low',
 'retest_close', 'retest_close_location', 'confirmed_to_retest_min',
 'pivot_to_retest_min', 'reaction', 'retest_age_min',
 'triggering_swing_count', 'relative_volume', 'retest_volume',
)
LOG = logging.getLogger('reaction-lab-scanner')


def compile_canonical_scanner(app_path: Path, loader):
    """Compile ONLY named function ASTs, never execute Streamlit top-level code.

    The two optional POST-retest analysis attachments are skipped; they do not
    influence whether an Event ID exists, but can access future post-retest bars.
    Confirmation, departure and first retest logic are unchanged.
    """
    import numpy as np
    import pandas as pd
    from engine.live.research.swing_detector import SwingDetector
    source = app_path.read_text(encoding='utf-8-sig')
    tree = ast.parse(source, filename=str(app_path))
    defs = {n.name:n for n in tree.body if isinstance(n, ast.FunctionDef)}
    missing = set(HELPERS)-set(defs)
    if missing:
        raise RuntimeError(f'Canonical app.py scanner functions missing: {sorted(missing)}')
    selected=[]
    for name in HELPERS:
        if name == 'load_volume_exhaustion_research_candles':
            continue
        n = defs[name]
        # strip Streamlit caches/decorators; never import app.py
        n.decorator_list=[]
        if name == CANONICAL_ROOT:
            class StripFutureMetrics(ast.NodeTransformer):
                def visit_Assign(self, node):
                    if (isinstance(node.value, ast.Call)
                        and isinstance(node.value.func,ast.Name)
                        and node.value.func.id in (
                            '_attach_confirmed_swing_reaction_run',
                            '_attach_confirmed_swing_order_recovery_analysis')):
                        # identity/retest fields remain those of original scanner
                        return ast.copy_location(ast.Pass(), node)
                    return self.generic_visit(node)
            n = StripFutureMetrics().visit(n)
        selected.append(n)
    # preserve all helper code names; constants match dashboard definitions
    globals_={
        'pd':pd, 'np':np, 'SwingDetector':SwingDetector,
        'TZ':'America/Argentina/Buenos_Aires',
        'VOLUME_EXHAUSTION_RESEARCH_CANDLE_LIMIT':5000,
        'MICRO_REACTION_SCALP_HORIZONS':(1,3,5,10,15,30),
        'MICRO_REACTION_5M_HIT_THRESHOLDS':(0.10,0.20,0.30),
        'MICRO_REACTION_SCALP_TP_GRID':(0.10,0.15,0.20,0.25,0.30),
        'MICRO_REACTION_SCALP_SL_GRID':(0.10,0.15,0.20),
        'MICRO_REACTION_SCALP_MATRIX_HORIZONS':(1,3,5),
        'CONFIRMED_SWING_FIRST_TOUCH_TP_GRID':(0.25,0.50,0.75,1.00,1.50,2.00),
        'CONFIRMED_SWING_FIRST_TOUCH_SL_GRID':(0.25,0.50,0.75,1.00,1.50,2.00),
        'CONFIRMED_SWING_TIME_EXIT_HORIZONS':(60,120,180,240,360),
        'load_volume_exhaustion_research_candles':loader,
    }
    module=ast.fix_missing_locations(ast.Module(body=selected,type_ignores=[]))
    exec(compile(module,str(app_path),'exec'), globals_)
    fingerprint=hashlib.sha256(ast.dump(module,include_attributes=False).encode()).hexdigest()[:20]
    return globals_[CANONICAL_ROOT], fingerprint


def get_symbols(project: Path) -> tuple[str,...]:
    # Same universe source used by dashboard.load_volume_exhaustion_symbol_universe
    import csv
    path=project/'volume_exhaustion_events.csv'
    if not path.is_file():
        raise FileNotFoundError(f'Required canonical symbol source missing: {path}')
    with path.open(encoding='utf-8-sig',newline='') as fh:
        reader=csv.DictReader(fh)
        if 'symbol' not in (reader.fieldnames or []):
            raise ValueError(f'Missing symbol column: {path}')
        symbols={str(r.get('symbol') or '').strip().upper() for r in reader}
    symbols.discard('')
    if not symbols: raise RuntimeError('Empty canonical symbol universe')
    return tuple(sorted(symbols))


def db_open(db:Path):
    db.parent.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(str(db),timeout=30)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA busy_timeout=30000')
    con.executescript(SCHEMA)
    con.execute('INSERT OR IGNORE INTO meta VALUES (?,?)',('schema_version','1'))
    if con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]!='1':
        raise RuntimeError('Unexpected scanner SQLite schema')
    con.commit()
    return con


def coerce_jsonable(v):
    import numpy as np
    import pandas as pd
    if v is None or (isinstance(v,float) and not np.isfinite(v)):
        return None
    if isinstance(v,(int,str,float,bool)):
        return v
    if isinstance(v,np.generic):return coerce_jsonable(v.item())
    if isinstance(v,pd.Timestamp):return v.isoformat()
    try:
        if pd.isna(v):return None
    except (ValueError,TypeError):pass
    return str(v)


def normalize_reactions(frame, now_ms:int):
    """Deduplicate causal Event IDs and only accept 1m candles already CLOSED."""
    output={}
    if frame is None or frame.empty:return output
    for r in frame.to_dict('records'):
        if str(r.get('status') or '')!='REACTION':continue
        sym=str(r.get('symbol') or '').upper(); side=str(r.get('signal') or '').upper()
        if not sym or side not in ('LONG','SHORT'):continue
        try:ts=int(r['retest_timestamp'])
        except (TypeError,ValueError):continue
        if ts+60_000>now_ms:continue
        key=f'{sym}|{side}|{ts}'
        payload={k:coerce_jsonable(r.get(k)) for k in CAUSAL_FIELDS if k in r}
        payload.update(symbol=sym,signal=side,status='REACTION',retest_timestamp=ts,
                       candidate_v1_event_key=key,candidate_v2_event_key=key)
        output.setdefault(key,payload)
    return output


def save_cycle(con, events, now_ms, elapsed_start, symbol_count, counts, fingerprint, last_ts):
    # Must preserve first-seen context; never rewrite causal metadata with hindsight.
    n=0
    with con:
        for key,r in events.items():
            ts=int(r['retest_timestamp'])
            observation='RECOVERED_WINDOW' if ts+120_000<elapsed_start else 'LIVE_OBSERVED'
            result=con.execute('''INSERT OR IGNORE INTO events
              (event_key,symbol,side,retest_ts,known_ts,first_seen_ms,last_seen_ms,
               observation_type,source_hash,causal_json)
              VALUES (?,?,?,?,?,?,?,?,?,?)''',
              (key,r['symbol'],r['signal'],ts,ts+60_000,now_ms,now_ms,
               observation,fingerprint,json.dumps(r,ensure_ascii=False,allow_nan=False)))
            if result.rowcount:n+=1
            else:
                con.execute('UPDATE events SET last_seen_ms=? WHERE event_key=?',(now_ms,key))
        con.execute('''INSERT INTO cycles(started_ms,ended_ms,symbols_requested,
          symbols_with_1m,symbols_with_15m,latest_1m_ts,events_seen,new_events,
          slow_cycle,source_hash,error) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
          (elapsed_start,now_ms,symbol_count,counts[0],counts[1],last_ts,
           len(events),n,int(now_ms-elapsed_start>120_000),fingerprint,None))
    return n


def scan_once(project, redis_client, scan, fingerprint, db, symbols, dry=False,
              min_free_mb=1024):
    from dashboard.services.geometry_scanner_data_service import GeometryScannerDataService
    import pandas as pd
    from engine.live.data.redis_market_data_protocol import history_key
    start=int(time.time()*1000)
    service=GeometryScannerDataService(redis_client=redis_client)
    # CLOSED candle snapshots are read once per symbol/timeframe in this cycle.
    frame_cache={};read_ok=[0,0];latest=0
    def loader(symbol,timeframe,limit):
        nonlocal latest
        sym=str(symbol).upper(); tf=str(timeframe)
        k=(sym,tf)
        if k not in frame_cache:
            values=service.get_closed_candles(symbol=sym,timeframe=tf,limit=min(400,int(limit)))
            values=values if values is not None else pd.DataFrame()
            frame_cache[k]=values
            if not values.empty:
                read_ok[0 if tf=='1m' else 1]+=1
                if tf=='1m':latest=max(latest,int(values['timestamp'].max()))
        return frame_cache[k]
    scan.__globals__['load_volume_exhaustion_research_candles']=loader
    # Canonical scan, configured same as frozen V2: 15m 3x3 and 0.20%/0.10%/360m
    frame=scan(symbols=symbols,swing_timeframes=('15m',),
               swing_detector_items=(('15m','3x3'),),
               min_swing_prominence_pct=0.0,retest_tolerance_pct=0.10,
               min_departure_pct=0.20,max_age_minutes=360,
               max_retest_age_minutes=4320)
    now=int(time.time()*1000)
    events=normalize_reactions(frame,now)
    if not dry and read_ok[0]==0:
        raise RuntimeError('No closed 1m histories available; refusing empty scan commit')
    if dry:
        return {'mode':'DRY','events_seen':len(events),'sample_event_keys':list(events)[:5],
                'symbols':len(symbols),'redis_1m_ok':read_ok[0],
                'redis_15m_ok':read_ok[1],'last_1m_ts':latest,
                'duration_seconds':round((now-start)/1000,2),'source_hash':fingerprint}
    space=shutil.disk_usage(db.parent if db.parent.exists() else project).free/(1024**2)
    if space<min_free_mb:raise RuntimeError('Disk free MB %.0f less than %d'%(space,min_free_mb))
    con=db_open(db)
    try:
        added=save_cycle(con,events,now,start,len(symbols),read_ok,fingerprint,latest)
        total=con.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        return {'events_seen':len(events),'new_events':added,'total_events':total,
                'symbols_requested':len(symbols),'redis_1m_ok':read_ok[0],
                'redis_15m_ok':read_ok[1],'last_1m_ts':latest,
                'latest_lag_seconds':round((now-(latest+60_000))/1000,2) if latest else None,
                'duration_seconds':round((now-start)/1000,2),
                'source_hash':fingerprint,'db':str(db)}
    finally:con.close()


def status(db):
    if not db.exists():return {'database_exists':False}
    con=sqlite3.connect(f'file:{db}?mode=ro',uri=True)
    try:
        total=con.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        observed=con.execute('SELECT observation_type,COUNT(*) FROM events GROUP BY 1').fetchall()
        last=con.execute('''SELECT started_ms,ended_ms,symbols_requested,
           symbols_with_1m,symbols_with_15m,new_events,slow_cycle
           FROM cycles ORDER BY cycle_id DESC LIMIT 1''').fetchone()
        return {'database_exists':True,'total_events':total,'observation_types':dict(observed),
                'last_cycle':last}
    finally:con.close()


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project',type=Path,default=Path.cwd())
    p.add_argument('--db',type=Path,default=None)
    p.add_argument('--symbols-file',type=Path,default=None,
                   help='Optional frozen symbol subset for piloting; normal mode uses current research universe')
    mode=p.add_mutually_exclusive_group()
    mode.add_argument('--check',action='store_true')
    mode.add_argument('--dry-scan',action='store_true',help='Real Redis scan; no database writes')
    mode.add_argument('--scan-once',action='store_true')
    mode.add_argument('--loop',action='store_true')
    mode.add_argument('--status',action='store_true')
    p.add_argument('--poll-seconds',type=int,default=60)
    p.add_argument('--min-free-mb',type=int,default=1024)
    args=p.parse_args(argv)
    project=args.project.resolve()
    db=(args.db or project/'reports/reaction_lab/scanner_events_v1.sqlite3').resolve()
    if db.suffix not in ('.db','.sqlite3'):p.error('SQLite database path required')
    if args.status:
        print(json.dumps(status(db),indent=2,ensure_ascii=False));return 0
    # loading app AST is read-only. --check checks same canonical functions and symbol universe.
    # Redis credentials follow project configuration; never print them.
    sys.path.insert(0,str(project))
    try:
        from dotenv import load_dotenv
        load_dotenv(project/'.env')
    except ImportError:pass
    symbols=(tuple(sorted({s.strip() for s in args.symbols_file.read_text().splitlines() if s.strip()}))
             if args.symbols_file else get_symbols(project))
    if not symbols:raise RuntimeError('Empty symbols list')
    scan,fingerprint=compile_canonical_scanner(project/'dashboard/app.py',lambda **kw:None)
    print('MODE:', 'LOOP' if args.loop else 'SCAN-ONCE' if args.scan_once else 'DRY-SCAN' if args.dry_scan else 'CHECK')
    print('CANONICAL APP SCAN:', fingerprint, 'SYMBOLS:',len(symbols),'DB:',db)
    if not args.scan_once and not args.loop and not args.dry_scan:
        print('CHECK OK: no file or Redis mutations; run --scan-once after review')
        return 0
    import redis
    client=redis.Redis(host=os.getenv('REDIS_HOST','127.0.0.1'),port=int(os.getenv('REDIS_PORT','6379')),
                       db=int(os.getenv('REDIS_DB','0')),socket_timeout=10)
    client.ping()
    if args.dry_scan:
        print(json.dumps(scan_once(project,client,scan,fingerprint,db,symbols,dry=True),ensure_ascii=False,indent=2))
        return 0
    db.parent.mkdir(parents=True,exist_ok=True)
    lock=db.with_suffix(db.suffix+'.lock')
    with lock.open('a+') as fh:
        try:fcntl.flock(fh,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('Another scanner is already running for this DB')
        stopped=False
        def stop(signum,frame):
            nonlocal stopped
            stopped=True
        signal.signal(signal.SIGINT,stop)
        signal.signal(signal.SIGTERM,stop)
        while not stopped:
            next_due=time.monotonic()+max(1,args.poll_seconds)
            try:
                report=scan_once(project,client,scan,fingerprint,db,symbols,
                                 min_free_mb=args.min_free_mb)
                print(json.dumps(report,ensure_ascii=False),flush=True)
                if (report['redis_1m_ok']<len(symbols)*0.95 or
                    report['redis_15m_ok']<len(symbols)*0.95 or
                    report['duration_seconds']>args.poll_seconds):
                    LOG.warning('SCAN COVERAGE/LATENCY WARNING: %s',report)
            except Exception:
                LOG.exception('Scanner cycle failed; data are not assumed complete')
                if not args.loop:return 1
            if not args.loop:break
            while not stopped and time.monotonic()<next_due:
                time.sleep(min(1,max(0.01,next_due-time.monotonic())))
    return 0

if __name__=='__main__':sys.exit(main())
