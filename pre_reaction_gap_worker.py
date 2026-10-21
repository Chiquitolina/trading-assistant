#!/usr/bin/env python3
"""Automatic, isolated, audited repair of missing PRE-REACTION context candles.

Read-only: scanner DB, Auto Paths DB, Redis (not accessed), Market Data.
Writes ONLY: context DB (missing candles / affected window coverage) and a sidecar
worker-state SQLite database. Uses Binance USD-M Futures REST after a grace period.
No inferred/filled prices, no overwrites, no LIVE retroactive relabeling.

Requires installed pre_reaction_context_recorder.py and
repair_pre_reaction_context_gaps.py in the project root.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import math
from datetime import datetime, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import sys
import time
from urllib.error import HTTPError, URLError

from pre_reaction_context_recorder import TFS, ro_db
from repair_pre_reaction_context_gaps import (
    DEFAULT_AUTOPATHS, DEFAULT_DB, SOURCE,
    autopaths_evidence, fetch_binance, gaps, matching_ohlc,
    repair_one, verify_aggregated_15m,
)

LOG = logging.getLogger('pre-reaction-gap-worker')
DEFAULT_STATE = Path('reports/reaction_lab/pre_reaction_gap_worker_v1.sqlite3')
TF_ORDER = ('1m', '15m')
STATE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS tasks (
 symbol TEXT NOT NULL, timeframe TEXT NOT NULL, open_ts INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('RETRY','BLOCKED','DONE')),
 attempts INTEGER NOT NULL DEFAULT 0, next_retry_ms INTEGER NOT NULL DEFAULT 0,
 last_attempt_ms INTEGER, resolved_ms INTEGER, last_error TEXT,
 source TEXT, auto_paths_check TEXT, aggregated_15m_check TEXT,
 PRIMARY KEY(symbol,timeframe,open_ts)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_worker_state ON tasks(state,next_retry_ms);
'''


def utc(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec='seconds')


def open_state(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=30)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA busy_timeout=30000')
    db.executescript(STATE_SCHEMA)
    db.commit()
    return db


def pause_until(state):
    if state is None: return 0
    row = state.execute("SELECT value FROM meta WHERE key='api_pause_until_ms'").fetchone()
    return int(row[0]) if row else 0


def mark_pause(state, until_ms):
    with state:
        row = pause_until(state)
        state.execute('''INSERT INTO meta(key,value) VALUES ('api_pause_until_ms',?)
                         ON CONFLICT(key) DO UPDATE SET value=excluded.value''',
                      (str(max(row,until_ms)),))


def select_gap_candidates(ctx, state, now_ms, grace_ms):
    """Discover all referenced missing timestamps; de-duplicate within each symbol/tf.

    A missing candle becomes eligible only after its close AND the first related
    event capture have aged by the grace period. Never repairs flagged conflicts.
    """
    todo = []
    for tf in TF_ORDER:
        todo.extend(gaps(ctx, tf))
    # Earliest initial capture among events referencing this symbol+TF candle.
    # Only incomplete windows can trigger a task. 1/15m are handled identically.
    capture_map = {}
    raw = ctx.execute('''SELECT e.symbol,w.timeframe,w.end_exclusive_ms,
          w.expected_count,MIN(e.captured_at_ms)
          FROM windows w JOIN events e ON e.event_key=w.event_key
          WHERE w.status IN ('PARTIAL','NONE')
          GROUP BY e.symbol,w.timeframe,w.end_exclusive_ms,w.expected_count''')
    ranges = defaultdict(list)
    for symbol,tf,end,count,captured in raw:
        start = end - count*TFS[tf][0]
        ranges[(symbol,tf)].append((start,end,int(captured)))
    # The number of distinct unfinished events is normally small. Never
    # use final outcome timestamps or downstream runner labels here.
    for sym,tf,ts in todo:
        captured = min((c for a,b,c in ranges[(sym,tf)] if a <= ts < b), default=now_ms)
        min_eligible = max(ts + TFS[tf][0], captured) + grace_ms
        row = None if state is None else state.execute('''SELECT state,attempts,next_retry_ms,last_error
          FROM tasks WHERE symbol=? AND timeframe=? AND open_ts=?''',(sym,tf,ts)).fetchone()
        task_status = row[0] if row else 'NEW'
        if task_status == 'BLOCKED': reason = 'BLOCKED'
        elif now_ms < min_eligible: reason = 'GRACE'
        elif row and now_ms < row[2]: reason = 'BACKOFF'
        else: reason = 'READY'
        todo_state = {'symbol':sym,'timeframe':tf,'open_ts':ts,
              'utc':utc(ts),'first_capture_ms':captured,
              'eligible_at_ms':min_eligible,'reason':reason,
              'attempts':int(row[1]) if row else 0,
              'last_error':row[3] if row else None}
        capture_map[(sym,tf,ts)] = todo_state
    return sorted(capture_map.values(),key=lambda t:(t['open_ts'],t['symbol'],t['timeframe']))


def classify_failure(exc):
    """Return permanent?, global-pause-ms. Preserve strict OHLC conflicts."""
    if isinstance(exc, HTTPError):
        if exc.code in (418,429):return False, 60*60_000
        if exc.code in (403,451):return False, 6*60*60_000
        if exc.code in (400,404,422):return True,0
        return False,0
    if isinstance(exc, (URLError, TimeoutError, ConnectionError, OSError, sqlite3.OperationalError)):
        return False,0
    if isinstance(exc, ValueError):
        msg=str(exc)
        if any(marker in msg for marker in (
             'OHLC_DIFFER', 'CONTRADICT', 'MISMATCH',
             'CONFLICT_ALREADY_FLAGGED', 'CONCURRENT_OR_STORED_PRICE_MISMATCH',
             'Auto Paths contradicts itself', 'Invalid OHLCV',
             'timestamp mismatch', 'Timestamp unaligned', 'Bad Binance kline structure',
             'Non-finite values')):
            return True,0
        return False,0
    return False,0


def record_attempt(state, task, now_ms, *, success=False, error=None,
                   permanent=False, auto_check=None, agg_check=None,
                   stored_source=None):
    attempts=task['attempts']+1
    # After persistent errors, quarantine rather than retry indefinitely.
    permanent = permanent or (not success and attempts >= 12)
    # Schedule exponential retries, bounded by 6 hours; never hammer the API.
    retry_delay = min(6*3600_000, 5*60_000*2**min(attempts-1,10))
    with state:
        state.execute('''INSERT INTO tasks
             (symbol,timeframe,open_ts,state,attempts,next_retry_ms,
              last_attempt_ms,resolved_ms,last_error,source,auto_paths_check,aggregated_15m_check)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
             ON CONFLICT(symbol,timeframe,open_ts) DO UPDATE SET
              state=excluded.state,attempts=excluded.attempts,
              next_retry_ms=excluded.next_retry_ms,last_attempt_ms=excluded.last_attempt_ms,
              resolved_ms=excluded.resolved_ms,last_error=excluded.last_error,
              source=excluded.source,auto_paths_check=excluded.auto_paths_check,
              aggregated_15m_check=excluded.aggregated_15m_check''',
              (task['symbol'],task['timeframe'],task['open_ts'],
               'DONE' if success else ('BLOCKED' if permanent else 'RETRY'),
               attempts,0 if success or permanent else now_ms+retry_delay,
               now_ms,now_ms if success else None,
               str(error)[:500] if error else None,(stored_source or SOURCE) if success else None,
               auto_check,agg_check))


def verify_15m_against_stored_1m(ctx, symbol, ts, incoming):
    """Reject recovered 15m candle when complete, valid stored 1m disagrees."""
    one=60_000
    interval=900_000
    rows=ctx.execute('''SELECT c.open_ts,c.open,c.high,c.low,c.close,c.volume
        FROM candles c LEFT JOIN conflicts f
        ON c.symbol=f.symbol AND c.timeframe=f.timeframe AND c.open_ts=f.open_ts
        WHERE c.symbol=? AND c.timeframe='1m' AND c.open_ts>=?
            AND c.open_ts<? AND f.open_ts IS NULL''',
        (symbol,ts,ts+interval)).fetchall()
    bars={r[0]:r[1:] for r in rows}
    if len(bars)!=15 or any(ts+i*one not in bars for i in range(15)):
        return 'NOT_COMPARABLE'
    seq=[bars[ts+i*one] for i in range(15)]
    calculated=(seq[0][0],max(r[1] for r in seq),min(r[2] for r in seq),
                seq[-1][3],sum(r[4] for r in seq))
    if not all(math.isclose(a,b,rel_tol=1e-7,abs_tol=1e-9)
               for a,b in zip(calculated,incoming[:5])):
        raise ValueError('BINANCE_15M_VS_STORED_1M_MISMATCH')
    return 'MATCH'


def run_once(context_path, auto_path, state_path, *, now_ms=None,
             grace_minutes=5, max_bars=40, max_symbols=25,
             pace_seconds=0.4, min_free_mb=512, fetch=fetch_binance,
             read_only=False):
    """One bounded cycle; --plan has no network, sidecar creation or DB writes."""
    now_ms=int(time.time()*1000) if now_ms is None else int(now_ms)
    if not context_path.is_file():raise FileNotFoundError(f'Context DB missing: {context_path}')
    if not read_only:
        available_mb=shutil.disk_usage(context_path.parent).free//(1024*1024)
        if available_mb < min_free_mb:
            raise RuntimeError(f'Only {available_mb} MB free (min {min_free_mb} MB); no writes')
    state = ro_db(state_path) if read_only and state_path.is_file() else (
        None if read_only else open_state(state_path))
    ctx=ro_db(context_path) if read_only else sqlite3.connect(str(context_path),timeout=30)
    if not read_only:
        ctx.execute('PRAGMA busy_timeout=30000')
        ctx.execute('PRAGMA foreign_keys=ON')
    auto=None
    try:
        candidates=select_gap_candidates(ctx,state,now_ms,grace_minutes*60_000)
        counts=Counter(x['reason'] for x in candidates)
        by_tf=Counter(x['timeframe'] for x in candidates)
        all_windows=dict(ctx.execute('''SELECT status,COUNT(*) FROM windows GROUP BY status'''))
        conflict_count=ctx.execute('SELECT COUNT(*) FROM conflicts').fetchone()[0]
        out={'mode':'PLAN' if read_only else 'RUN',
             'now_utc':utc(now_ms),'missing_unique_bars':len(candidates),
             'missing_by_timeframe':dict(by_tf), 'candidate_state':dict(counts),
             'window_statuses':all_windows,'conflict_rows':conflict_count,
             'api_pause_until_utc':utc(pause_until(state)) if pause_until(state)>now_ms else None,
             'requested':0,'inserted':0,'already_present':0,
             'checked_auto_paths':0,'no_auto_paths_overlap':0,
             'matched_aggregated_15m':0,'no_15m_comparison':0,
             'matched_15m_against_1m':0,'no_1m_comparison_for_15m':0,
             'refreshed_windows':0,'rejected':[], 'blocked':0,
             'examples':[{'symbol':x['symbol'],'tf':x['timeframe'],
                          'utc':x['utc'],'reason':x['reason']}
                         for x in candidates[:12]]}
        if read_only:return out
        if pause_until(state)>now_ms:
            out['action']='API_PAUSED'
            return out
        ready=[x for x in candidates if x['reason']=='READY']
        chosen=[]; symbols=set()
        for x in ready:
            if len(chosen)>=max_bars:break
            if x['symbol'] not in symbols and len(symbols)>=max_symbols:continue
            chosen.append(x); symbols.add(x['symbol'])
        if not chosen:
            out['action']='IDLE';return out
        out['action']='FETCH'
        if auto_path.is_file():auto=ro_db(auto_path)
        evidence={}; contradictory={}
        for sym in symbols:
            wanted={x['open_ts'] for x in chosen if x['symbol']==sym and x['timeframe']=='1m'}
            if wanted:
                evidence[sym],contradictory[sym]=autopaths_evidence(auto,sym,wanted)
        for item in chosen:
            sym,tf,ts=item['symbol'],item['timeframe'],item['open_ts']
            out['requested']+=1
            try:
                if tf=='1m' and ts in contradictory.get(sym,set()):
                    raise ValueError('Auto Paths contradicts itself for candle')
                bar=fetch(sym,tf,ts,now_ms)
                auto_check='NOT_COMPARABLE'
                if tf=='1m' and ts in evidence.get(sym,{}):
                    if not matching_ohlc(evidence[sym][ts],bar):
                        raise ValueError('BINANCE_VS_AUTO_PATHS_OHLC_DIFFER')
                    auto_check='MATCH'
                    out['checked_auto_paths']+=1
                else:out['no_auto_paths_overlap']+=1
                agg_check='NOT_COMPARABLE'
                if tf=='1m':
                    agg_check=verify_aggregated_15m(ctx,sym,ts,bar)
                    if agg_check=='MATCH':out['matched_aggregated_15m']+=1
                    else:out['no_15m_comparison']+=1
                if tf=='15m':
                    agg_check=verify_15m_against_stored_1m(ctx,sym,ts,bar)
                    if agg_check=='MATCH':out['matched_15m_against_1m']+=1
                    else:out['no_1m_comparison_for_15m']+=1
                inserted,refreshed=repair_one(ctx,sym,tf,ts,bar,int(time.time()*1000))
                out['inserted']+=int(inserted)
                out['already_present']+=int(not inserted)
                out['refreshed_windows']+=refreshed
                record_attempt(state,item,int(time.time()*1000),success=True,
                               auto_check=auto_check,agg_check=agg_check,
                               stored_source=SOURCE if inserted else 'ALREADY_PRESENT')
            except Exception as exc:
                permanent,pause_ms=classify_failure(exc)
                permanent = permanent or item['attempts']+1 >= 12
                out['rejected'].append({'symbol':sym,'tf':tf,'utc':utc(ts),
                                        'reason':f'{type(exc).__name__}: {exc}',
                                        'state':'BLOCKED' if permanent else 'RETRY'})
                out['blocked']+=int(permanent)
                record_attempt(state,item,int(time.time()*1000),error=exc,permanent=permanent)
                if pause_ms:
                    mark_pause(state,int(time.time()*1000)+pause_ms)
                    out['action']='API_PAUSED_AFTER_ERROR'
                    break
                if isinstance(exc,sqlite3.OperationalError):
                    out['action']='STORAGE_ERROR';break
            if pace_seconds:time.sleep(pace_seconds)
        return out
    finally:
        if auto:auto.close()
        ctx.close()
        if state:state.close()


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true',help='Strictly read-only, no network')
    mode.add_argument('--run-once',action='store_true',help='Repair one bounded batch')
    mode.add_argument('--loop',action='store_true',help='Continuously monitor and repair')
    p.add_argument('--project',type=Path,default=Path.cwd())
    p.add_argument('--db',type=Path)
    p.add_argument('--auto-paths-db',type=Path)
    p.add_argument('--state-db',type=Path)
    p.add_argument('--poll-seconds',type=int,default=300)
    p.add_argument('--grace-minutes',type=int,default=5)
    p.add_argument('--max-bars-per-cycle',type=int,default=40)
    p.add_argument('--max-symbols-per-cycle',type=int,default=25)
    p.add_argument('--pace-seconds',type=float,default=0.4)
    p.add_argument('--min-free-mb',type=int,default=512)
    args=p.parse_args(argv)
    if (args.poll_seconds<10 or args.grace_minutes<0 or args.max_bars_per_cycle<1 or
        args.max_symbols_per_cycle<1 or args.pace_seconds<0 or args.min_free_mb<0):
        p.error('Invalid arguments')
    root=args.project.resolve()
    ctx=(args.db or root/DEFAULT_DB).resolve()
    auto=(args.auto_paths_db or root/DEFAULT_AUTOPATHS).resolve()
    state=(args.state_db or root/DEFAULT_STATE).resolve()
    protected={root/'reports/reaction_lab/scanner_events_v1.sqlite3',
               auto,root/'reports/reaction_lab/runners_v1.sqlite3'}
    if ctx==state or state in protected or ctx in protected:
        p.error('Refusing DB writes to scanner / runners / Auto Paths or same context/state DB')
    if args.plan:
        result=run_once(ctx,auto,state,read_only=True,grace_minutes=args.grace_minutes)
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    lock=state.with_suffix(state.suffix+'.lock')
    lock.parent.mkdir(parents=True,exist_ok=True)
    with lock.open('a+') as handle:
        try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('Another pre-reaction gap worker is active')
        stop=False
        def shutdown(signum,frame):
            nonlocal stop
            stop=True
        signal.signal(signal.SIGTERM,shutdown)
        signal.signal(signal.SIGINT,shutdown)
        while not stop:
            try:
                result=run_once(ctx,auto,state,grace_minutes=args.grace_minutes,
                    max_bars=args.max_bars_per_cycle,max_symbols=args.max_symbols_per_cycle,
                    pace_seconds=args.pace_seconds,min_free_mb=args.min_free_mb)
                print(json.dumps(result,ensure_ascii=False),flush=True)
            except Exception:
                LOG.exception('Worker cycle failed; other services remain untouched')
                if args.run_once:return 1
            if args.run_once:
                return 2 if result.get('rejected') else 0
            for _ in range(args.poll_seconds):
                if stop:break
                time.sleep(1)
    return 0


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    sys.exit(main())
