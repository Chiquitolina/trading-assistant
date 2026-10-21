#!/usr/bin/env python3
"""Prospective 1m/15m PRE-REACTION context recorder. Scanner and Redis are READ-ONLY.

Never reads a current unfinished candle or uses a later snapshot as if recorded
at event detection. Does not modify LIVE services, trading strategies, or their DBs.

Three explicit anchors per event:
 PRE_RETEST   strictly before retest candle opens
 PRE_KNOWN    candles already closed by known_ts (includes 1m retest candle)
 PRE_OBSERVED candles already closed at scanner first_seen_ms

A window is complete only if EVERY required timestamp has a valid, conflict-free
candle. Capture punctuality is independently labeled ON_TIME / DELAYED; neither
label implies retroactively that the recorder ran at retest_ts.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import struct
import sys
import time
from urllib.parse import quote
import zlib

from engine.live.data.redis_market_data_protocol import HISTORY_MAXLEN, history_key

LOG = logging.getLogger('pre-reaction-context')
TFS = {'1m': (60_000, 120), '15m': (900_000, 96)}
ANCHORS = ('PRE_RETEST', 'PRE_KNOWN', 'PRE_OBSERVED')
VERSION = '1'
DEFAULT_STORE = 'reports/reaction_lab/pre_reaction_context_v1.sqlite3'
DEFAULT_SCANNER = 'reports/reaction_lab/scanner_events_v1.sqlite3'
DEFAULT_PATHS = 'reports/reaction_lab/auto_paths_v1.sqlite3'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  event_key TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL,
  retest_ts INTEGER NOT NULL, known_ts INTEGER NOT NULL,
  first_seen_ms INTEGER NOT NULL, observation_type TEXT NOT NULL,
  scanner_source_hash TEXT NOT NULL,
  captured_at_ms INTEGER NOT NULL, first_capture_lag_ms INTEGER NOT NULL,
  capture_timeliness TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  last_checked_ms INTEGER, finalized INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_context_events_seen ON events(first_seen_ms);
CREATE TABLE IF NOT EXISTS candles (
  symbol TEXT NOT NULL, timeframe TEXT NOT NULL, open_ts INTEGER NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
  volume REAL NOT NULL, quote_volume REAL,
  first_recorded_ms INTEGER NOT NULL, source TEXT NOT NULL DEFAULT 'REDIS_CLOSED',
  PRIMARY KEY (symbol,timeframe,open_ts)
);
CREATE TABLE IF NOT EXISTS conflicts (
  symbol TEXT NOT NULL, timeframe TEXT NOT NULL, open_ts INTEGER NOT NULL,
  reason TEXT NOT NULL, detected_ms INTEGER NOT NULL,
  PRIMARY KEY (symbol,timeframe,open_ts)
);
CREATE TABLE IF NOT EXISTS windows (
  event_key TEXT NOT NULL, anchor_type TEXT NOT NULL,
  timeframe TEXT NOT NULL, anchor_ms INTEGER NOT NULL,
  end_exclusive_ms INTEGER NOT NULL, expected_count INTEGER NOT NULL,
  saved_count INTEGER NOT NULL, status TEXT NOT NULL,
  first_missing_ts INTEGER, source_invalid_rows INTEGER NOT NULL DEFAULT 0,
  updated_ms INTEGER NOT NULL,
  PRIMARY KEY (event_key,anchor_type,timeframe),
  FOREIGN KEY(event_key) REFERENCES events(event_key)
);
CREATE INDEX IF NOT EXISTS idx_context_windows_status ON windows(status);
'''


def ro_db(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f'Required read-only database not found: {path}')
    con = sqlite3.connect('file:' + quote(str(path.resolve()), safe='/') + '?mode=ro',
                          uri=True, timeout=20)
    con.execute('PRAGMA query_only=ON')
    return con


def open_store(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=30)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA busy_timeout=30000')
    con.execute('PRAGMA foreign_keys=ON')
    con.executescript(SCHEMA)
    con.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('schema_version', VERSION))
    version = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    if version != VERSION:
        con.close()
        raise RuntimeError('Incompatible recorder schema: ' + str(version))
    con.commit()
    return con


def meta_get(con, key, default=None):
    row = con.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return row[0] if row is not None else default


def initialize_start(con, now_ms: int, lookback_minutes: int):
    if meta_get(con, 'start_ms') is None:
        with con:
            con.execute('INSERT INTO meta(key,value) VALUES (?,?)',
                        ('start_ms', str(now_ms - max(0, lookback_minutes) * 60_000)))
            con.execute('INSERT INTO meta(key,value) VALUES (?,?)', ('cursor_seen_ms', '-1'))
            con.execute('INSERT INTO meta(key,value) VALUES (?,?)', ('cursor_event_key', ''))
    return int(meta_get(con, 'start_ms'))


def fetch_new_events(scanner, store, limit: int):
    start = int(meta_get(store, 'start_ms'))
    cursor_ts = int(meta_get(store, 'cursor_seen_ms', '-1'))
    cursor_key = meta_get(store, 'cursor_event_key', '')
    rows = scanner.execute('''SELECT event_key,symbol,side,retest_ts,known_ts,
                                    first_seen_ms,observation_type,source_hash
      FROM events
      WHERE first_seen_ms>=? AND
            (first_seen_ms>? OR (first_seen_ms=? AND event_key>?))
      ORDER BY first_seen_ms,event_key LIMIT ?''',
      (start, cursor_ts, cursor_ts, cursor_key, limit)).fetchall()
    out = []
    for row in rows:
        k, sym, side, retest, known, seen, observation, source_hash = row
        sym = str(sym).upper(); side = str(side).upper()
        if (k != f'{sym}|{side}|{int(retest)}' or side not in ('LONG','SHORT') or
                int(retest) % 60_000 or int(known) < int(retest)+60_000 or
                int(seen) < int(known) or not source_hash):
            raise ValueError('Invalid scanner event identity/clock: '+str(k))
        out.append(tuple(row))
    return out


def fetch_retries(store, now_ms: int, retry_ms: int, limit: int, excluded: set[str]):
    rows = store.execute('''SELECT event_key,symbol,side,retest_ts,known_ts,
         first_seen_ms,observation_type,scanner_source_hash
         FROM events WHERE finalized=0 AND first_seen_ms>=?
         ORDER BY first_seen_ms,event_key LIMIT ?''',
         (now_ms-retry_ms, limit+len(excluded))).fetchall()
    return [r for r in rows if r[0] not in excluded][:limit]


def anchor_value(event, name: str) -> int:
    if name == 'PRE_RETEST': return int(event[3])
    if name == 'PRE_KNOWN': return int(event[4])
    if name == 'PRE_OBSERVED': return int(event[5])
    raise ValueError(name)


def window_spec(event, anchor: str, tf: str):
    ms, expected = TFS[tf]
    anchor_ms = anchor_value(event, anchor)
    end_excl = (anchor_ms // ms) * ms
    return anchor_ms, end_excl, expected, end_excl - expected*ms


def parse_redis_bars(raw, symbol: str, tf: str, now_ms: int):
    """Strict parser: rejects malformed, misaligned, still-open and conflicting bars.

    Returns normalized ts->OHLCV plus conflicting timestamps and invalid count.
    No historical/future completion is inferred from Redis list membership.
    """
    ms, _ = TFS[tf]
    good = {}; conflicted = set(); invalid = 0
    for item in raw or []:
        try:
            c = json.loads(item)
            if not isinstance(c, dict): raise ValueError('not a dict')
            ts = int(c['timestamp'])
            if ts < 1_500_000_000_000 or ts % ms:
                raise ValueError('invalid/open timestamp')
            if c.get('symbol') and str(c['symbol']).upper() != symbol:
                raise ValueError('symbol mismatch')
            if c.get('timeframe') and str(c['timeframe']).lower() != tf:
                raise ValueError('timeframe mismatch')
            close_ts = int(c.get('close_timestamp', ts+ms-1))
            if close_ts != ts+ms-1:
                raise ValueError('noncanonical close_timestamp')
            if ts+ms > now_ms:
                continue  # unfinished candle, never ingest it
            o, h, l, close = (float(c[k]) for k in ('open','high','low','close'))
            volume = float(c.get('volume', 0))
            if not all(math.isfinite(v) for v in (o,h,l,close,volume)):
                raise ValueError('non-finite OHLCV')
            if min(o,h,l,close) <= 0 or volume < 0 or h<max(o,l,close) or l>min(o,h,close):
                raise ValueError('invalid OHLCV')
            q = c.get('quoteVolume', c.get('quote_volume',c.get('quote_asset_volume')))
            q = None if q is None else float(q)
            if q is not None and (not math.isfinite(q) or q < 0):
                raise ValueError('invalid quote volume')
            bar = (o,h,l,close,volume,q)
            if ts in good and not same_bars(good[ts],bar):
                conflicted.add(ts)
            else:
                good[ts]=bar
        except (ValueError,TypeError,KeyError,OverflowError):
            invalid += 1
    for ts in conflicted:
        good.pop(ts,None)
    return good, conflicted, invalid


def almost_equal(a, b):
    return math.isclose(a,b,rel_tol=1e-9,abs_tol=1e-11)


def same_bars(a,b):
    for x,y in zip(a[:5],b[:5]):
        if not almost_equal(x,y):return False
    # Source can legitimately omit quote volume in an older cached candle.
    if a[5] is not None and b[5] is not None and not almost_equal(a[5],b[5]):
        return False
    return True


def collect_redis(redis_client, events, now_ms):
    grouped = {(str(e[1]).upper(),tf) for e in events for tf in TFS}
    result={}; errors={}
    for symbol,tf in sorted(grouped):
        try:
            items=redis_client.lrange(history_key(symbol,tf), -HISTORY_MAXLEN,-1)
            result[(symbol,tf)] = parse_redis_bars(items, symbol,tf,now_ms)
        except Exception as exc:
            errors[(symbol,tf)]=f'{type(exc).__name__}: {exc}'
            result[(symbol,tf)]=({},set(),0)
    return result, errors


def insert_candles_and_conflicts(store, events, market, now_ms):
    """Save only bars falling inside a requested event window; never overwrite OHLCV."""
    allowed={}
    for e in events:
        symbol=str(e[1]).upper()
        for tf,(ms,_) in TFS.items():
            ranges=allowed.setdefault((symbol,tf),[])
            for anchor in ANCHORS:
                _,end,_,start=window_spec(e,anchor,tf)
                ranges.append((start,end))
    conflicts_added=0; bars_added=0; newly_conflicted=set()
    for (symbol,tf), ranges in allowed.items():
        bars,bad_ts,_=market[(symbol,tf)]
        for ts in bad_ts:
            if any(start<=ts<end for start,end in ranges):
                result=store.execute('INSERT OR IGNORE INTO conflicts VALUES (?,?,?,?,?)',
                              (symbol,tf,ts,'CONTRADICTORY_REDIS_HISTORY',now_ms))
                if result.rowcount:
                    conflicts_added+=1
                    newly_conflicted.add((symbol,tf,ts))
        for ts, bar in bars.items():
            if not any(start<=ts<end for start,end in ranges):
                continue
            existing=store.execute('''SELECT open,high,low,close,volume,quote_volume
                   FROM candles WHERE symbol=? AND timeframe=? AND open_ts=?''',
                   (symbol,tf,ts)).fetchone()
            if existing is not None:
                if not same_bars(existing,bar):
                    result=store.execute('INSERT OR IGNORE INTO conflicts VALUES (?,?,?,?,?)',
                                  (symbol,tf,ts,'CONTRADICTORY_RECORDED_OHLCV',now_ms))
                    if result.rowcount:
                        conflicts_added+=1
                        newly_conflicted.add((symbol,tf,ts))
                continue
            store.execute('''INSERT INTO candles
              (symbol,timeframe,open_ts,open,high,low,close,volume,quote_volume,
               first_recorded_ms) VALUES (?,?,?,?,?,?,?,?,?,?)''',
              (symbol,tf,ts,*bar,now_ms))
            bars_added+=1
    # A contradictory candle may invalidate earlier *completed* event windows.
    # Keep their persisted coverage in sync rather than silently leaving COMPLETE.
    for symbol,tf,ts in newly_conflicted:
        tf_ms,_=TFS[tf]
        rows=store.execute('''SELECT w.event_key,w.anchor_type,w.end_exclusive_ms,
              w.expected_count FROM windows w JOIN events e ON e.event_key=w.event_key
              WHERE e.symbol=? AND w.timeframe=? AND w.end_exclusive_ms>?
              AND w.end_exclusive_ms-w.expected_count*?<=?''',
              (symbol,tf,ts,tf_ms,ts)).fetchall()
        for key,anchor,end,count in rows:
            state,saved,missing=window_coverage(store,symbol,tf,end-count*tf_ms,end,count)
            store.execute('''UPDATE windows SET saved_count=?,status=?,first_missing_ts=?,
                       updated_ms=? WHERE event_key=? AND anchor_type=? AND timeframe=?''',
                       (saved,state,missing,now_ms,key,anchor,tf))
    return bars_added,conflicts_added


def window_coverage(store, symbol, tf, start_ms, end_ms, expected):
    ms,_=TFS[tf]
    rows=store.execute('''SELECT c.open_ts, f.open_ts
      FROM candles c LEFT JOIN conflicts f
        ON f.symbol=c.symbol AND f.timeframe=c.timeframe AND f.open_ts=c.open_ts
      WHERE c.symbol=? AND c.timeframe=? AND c.open_ts>=? AND c.open_ts<?
      ORDER BY c.open_ts''',(symbol,tf,start_ms,end_ms)).fetchall()
    valid = {ts for ts, bad in rows if bad is None}
    missing = next((start_ms+i*ms for i in range(expected)
                    if start_ms+i*ms not in valid),None)
    saved = len(valid)
    if saved == expected and missing is None:
        state='COMPLETE'
    elif saved:
        state='PARTIAL'
    else:
        state='NONE'
    flagged = store.execute('''SELECT 1 FROM conflicts WHERE symbol=? AND timeframe=?
                           AND open_ts>=? AND open_ts<? LIMIT 1''',
                           (symbol,tf,start_ms,end_ms)).fetchone()
    if flagged is not None:
        state='CONFLICT'
    return state,saved,missing


def save_events(store, events, market, now_ms, on_time_ms, retry_ms, advance_new=False):
    """Commit one event and (if new) its scanner cursor atomically."""
    bars_added=conflicts_added=complete=0
    with store:
        new_bars,new_conflicts=insert_candles_and_conflicts(store,events,market,now_ms)
        bars_added+=new_bars; conflicts_added+=new_conflicts
        for e in events:
            key,sym,side,retest,known,seen,observation,source_hash = e
            sym=str(sym).upper(); seen=int(seen)
            late=max(0,now_ms-seen)
            timeliness='ON_TIME' if late<=on_time_ms else 'DELAYED'
            store.execute('''INSERT OR IGNORE INTO events
              (event_key,symbol,side,retest_ts,known_ts,first_seen_ms,
               observation_type,scanner_source_hash,captured_at_ms,
               first_capture_lag_ms,capture_timeliness)
              VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
              (key,sym,side,retest,known,seen,observation,source_hash,now_ms,late,timeliness))
            existing=store.execute('''SELECT symbol,side,retest_ts,known_ts,first_seen_ms,
                     observation_type,scanner_source_hash FROM events WHERE event_key=?''',
                                   (key,)).fetchone()
            if existing != (sym,side,retest,known,seen,observation,source_hash):
                raise ValueError('Scanner event metadata changed after initial capture: '+key)
            all_complete=True
            for tf in TFS:
                for anchor in ANCHORS:
                    anchor_ms,end,expected,start=window_spec(e,anchor,tf)
                    state,saved,missing=window_coverage(store,sym,tf,start,end,expected)
                    invalid=market[(sym,tf)][2]
                    store.execute('''INSERT INTO windows
                      (event_key,anchor_type,timeframe,anchor_ms,end_exclusive_ms,
                       expected_count,saved_count,status,first_missing_ts,
                       source_invalid_rows,updated_ms)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(event_key,anchor_type,timeframe) DO UPDATE SET
                        saved_count=excluded.saved_count,status=excluded.status,
                        first_missing_ts=excluded.first_missing_ts,
                        source_invalid_rows=excluded.source_invalid_rows,
                        updated_ms=excluded.updated_ms''',
                      (key,anchor,tf,anchor_ms,end,expected,saved,state,missing,invalid,now_ms))
                    all_complete &= state=='COMPLETE'
                    complete += state=='COMPLETE'
            store.execute('''UPDATE events SET attempts=attempts+1,last_checked_ms=?,
                            finalized=? WHERE event_key=?''',
                          (now_ms,int(all_complete or now_ms-seen>retry_ms),key))
            if advance_new:
                store.execute("UPDATE meta SET value=? WHERE key='cursor_seen_ms'",
                              (str(seen),))
                store.execute("UPDATE meta SET value=? WHERE key='cursor_event_key'",
                              (str(key),))
        # The cursor advances only through NEW scanner records, never through retries.
    return bars_added, conflicts_added, complete


def run_cycle(scanner, store, redis_client, now_ms, batch_size=120,
              max_capture_lag_ms=180_000, retry_ms=90*60_000):
    new=fetch_new_events(scanner,store,batch_size)
    retries=fetch_retries(store,now_ms,retry_ms,max(0,batch_size-len(new)),{e[0] for e in new})
    pending=new+retries
    if not pending:
        with store:
            store.execute('''UPDATE events SET finalized=1
                 WHERE finalized=0 AND first_seen_ms<?''',(now_ms-retry_ms,))
        return {'new_events':0,'retried_events':0,'bars_saved':0,'conflicts':0,'read_errors':0}
    market,errors=collect_redis(redis_client,pending,now_ms)
    # A transient Redis failure must NEVER allow us to advance the scanner cursor.
    if errors:
        raise RuntimeError('Redis errors; cursor unchanged: '+json.dumps(errors,ensure_ascii=False))
    bars=conflicts=complete=0
    # Save one at a time, pairing each scanner event with its durable cursor.
    # Bulk parsed histories are reusable across all events in the batch.
    for e in pending:
        b,c,k=save_events(store,[e],market,now_ms,max_capture_lag_ms,retry_ms,
                          advance_new=e in new)
        bars+=b;conflicts+=c;complete+=k
    with store:
        store.execute('''UPDATE events SET finalized=1
                       WHERE finalized=0 AND first_seen_ms<?''',(now_ms-retry_ms,))
    return {'new_events':len(new),'retried_events':len(retries),
            'bars_saved':bars,'conflicts_encountered':conflicts,
            'complete_windows_checked':complete, 'read_errors':len(errors),
            'new_event_keys_sample':[e[0] for e in new[:3]]}


def status(db_path):
    if not db_path.is_file():
        return {'database_exists':False}
    db=ro_db(db_path)
    try:
        out={'database_exists':True,'start_ms':meta_get(db,'start_ms'),
             'cursor_seen_ms':meta_get(db,'cursor_seen_ms')}
        out['events']=db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        out['events_by_observation']=dict(db.execute('''SELECT observation_type,COUNT(*)
                                                     FROM events GROUP BY 1'''))
        out['events_by_timeliness']=dict(db.execute('''SELECT capture_timeliness,COUNT(*)
                                                    FROM events GROUP BY 1'''))
        out['windows']=dict(db.execute('''SELECT anchor_type||':'||timeframe||':'||status,
                                         COUNT(*) FROM windows GROUP BY 1'''))
        out['saved_candles']=dict(db.execute('SELECT timeframe,COUNT(*) FROM candles GROUP BY 1'))
        out['conflicting_candles']=db.execute('SELECT COUNT(*) FROM conflicts').fetchone()[0]
        out['pending_retry']=db.execute('SELECT COUNT(*) FROM events WHERE finalized=0').fetchone()[0]
        return out
    finally:db.close()


def audit(db_path, scanner_path=None, auto_path=None, auto_paths_limit=300):
    """Read-only recomputation of every window; original DB not mutated."""
    db=ro_db(db_path)
    try:
        totals={'events':db.execute('SELECT COUNT(*) FROM events').fetchone()[0],
                'windows':0,'complete':0,'partial':0,'none':0,'conflict':0,
                'stale_window_rows':0,'missing_window_rows':0,
                'invalid_event_windows':[], 'scanner_missing_since_start':None,
                'scanner_causal_mismatches':0}
        for e in db.execute('SELECT event_key,symbol,retest_ts,known_ts,first_seen_ms FROM events'):
            k,sym,retest,known,seen=e
            for anchor in ANCHORS:
                for tf in TFS:
                    spec=(k,sym,'',retest,known,seen,'','')
                    a,end,count,start=window_spec(spec,anchor,tf)
                    state,saved,first_missing=window_coverage(db,sym,tf,start,end,count)
                    row=db.execute('''SELECT anchor_ms,end_exclusive_ms,expected_count,
                      saved_count,status,first_missing_ts FROM windows
                      WHERE event_key=? AND anchor_type=? AND timeframe=?''',
                      (k,anchor,tf)).fetchone()
                    totals['windows']+=1
                    totals[state.lower()]+=1
                    expected=(a,end,count,saved,state,first_missing)
                    if row is None:
                        totals['missing_window_rows']+=1
                    elif row != expected:
                        totals['stale_window_rows']+=1
                    if state!='COMPLETE' and len(totals['invalid_event_windows'])<10:
                        totals['invalid_event_windows'].append({'event_key':k,'anchor':anchor,
                            'timeframe':tf,'status':state,'saved':saved,'expected':count,
                            'first_missing_ts':first_missing})
        if scanner_path is not None and scanner_path.is_file():
            scan=ro_db(scanner_path)
            try:
                since=int(meta_get(db,'start_ms','0'))
                totals['scanner_events_since_start']=scan.execute('''SELECT COUNT(*) FROM events
                                        WHERE first_seen_ms>=?''',(since,)).fetchone()[0]
                totals['scanner_missing_since_start']=scan.execute('''SELECT COUNT(*)
                  FROM events WHERE first_seen_ms>=?''',(since,)).fetchone()[0]-totals['events']
                count=0
                for key,sym,retest,seen in db.execute('''SELECT event_key,symbol,retest_ts,first_seen_ms
                                               FROM events'''):
                    row=scan.execute('''SELECT symbol,retest_ts,first_seen_ms FROM events
                                           WHERE event_key=?''',(key,)).fetchone()
                    count += int(row!=(sym,retest,seen))
                totals['scanner_causal_mismatches']=count
            finally:scan.close()
        if auto_path is not None and auto_path.is_file():
            totals['auto_paths_parity']=crosscheck_autopaths(db,auto_path,auto_paths_limit)
        totals['success']=(totals['windows']>0 and totals['partial']==totals['none']==
            totals['conflict']==totals['stale_window_rows']==totals['missing_window_rows']==
            totals['scanner_causal_mismatches']==0 and
            (totals['scanner_missing_since_start'] in (0,None)) and
            (totals.get('auto_paths_parity',{}).get('differ',0)==0))
        return totals
    finally:db.close()


def crosscheck_autopaths(context_db, auto_path, limit=300):
    auto=ro_db(auto_path)
    unpack=struct.Struct('<qdddd')
    result={'paths_read':0,'compared':0,'matched':0,'differ':0,'bad_paths':0,'sample_diffs':[]}
    try:
        for symbol,blob in auto.execute('''SELECT symbol,path_blob FROM events
                                WHERE path_blob IS NOT NULL
                                ORDER BY first_seen_utc_ms DESC LIMIT ?''',(limit,)):
            try:
                decoded=zlib.decompress(blob)
                if len(decoded)%unpack.size:raise ValueError('bad compressed size')
                path=list(unpack.iter_unpack(decoded))
                if not path:continue
                rows=context_db.execute('''SELECT open_ts,open,high,low,close
                          FROM candles WHERE symbol=? AND timeframe='1m'
                          AND open_ts>=? AND open_ts<=?''',
                          (symbol,path[0][0],path[-1][0])).fetchall()
                index={r[0]:r[1:] for r in rows}
                conflicts={r[0] for r in context_db.execute('''SELECT open_ts FROM conflicts
                  WHERE symbol=? AND timeframe='1m' AND open_ts>=? AND open_ts<=?''',
                  (symbol,path[0][0],path[-1][0]))}
                for ts,*values in path:
                    if ts not in index or ts in conflicts:continue
                    result['compared']+=1
                    if all(almost_equal(x,y) for x,y in zip(values,index[ts])):
                        result['matched']+=1
                    else:
                        result['differ']+=1
                        if len(result['sample_diffs'])<8:
                            result['sample_diffs'].append((symbol,ts,values,index[ts]))
                result['paths_read']+=1
            except (ValueError,zlib.error):
                result['bad_paths']+=1
    finally:auto.close()
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project',type=Path,default=Path.cwd())
    p.add_argument('--scanner-db',type=Path)
    p.add_argument('--db',type=Path)
    p.add_argument('--auto-paths-db',type=Path)
    mode=p.add_mutually_exclusive_group()
    mode.add_argument('--check',action='store_true',help='Read-only prerequisites (default)')
    mode.add_argument('--status',action='store_true',help='Read-only progress')
    mode.add_argument('--audit',action='store_true',help='Read-only full consistency audit')
    mode.add_argument('--run-once',action='store_true')
    mode.add_argument('--loop',action='store_true')
    p.add_argument('--poll-seconds',type=int,default=20)
    p.add_argument('--batch-size',type=int,default=120)
    p.add_argument('--bootstrap-lookback-minutes',type=int,default=0,
                   help='Only on first run: include recent existing scanner signals as DELAYED')
    p.add_argument('--max-on-time-seconds',type=int,default=180)
    p.add_argument('--retry-minutes',type=int,default=90)
    p.add_argument('--min-free-mb',type=int,default=1024)
    p.add_argument('--auto-paths-limit',type=int,default=300)
    args=p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    project=args.project.resolve()
    scanner=(args.scanner_db or project/DEFAULT_SCANNER).resolve()
    store=(args.db or project/DEFAULT_STORE).resolve()
    auto=(args.auto_paths_db or project/DEFAULT_PATHS).resolve()
    protected={scanner,auto,project/'reports/reaction_lab/runners_v1.sqlite3'}
    if store in protected or store.suffix.lower() not in ('.db','.sqlite3'):
        p.error('Refusing to write to scanner, auto-paths, runners, or non-SQLite path')
    if args.batch_size<1 or args.poll_seconds<1 or args.retry_minutes<1 or args.min_free_mb<0:
        p.error('Invalid positive numeric flags')
    if args.bootstrap_lookback_minutes<0 or args.max_on_time_seconds<0:
        p.error('Invalid lookback/lag flags')
    if args.status:
        print(json.dumps(status(store),indent=2,ensure_ascii=False));return 0
    if args.audit:
        if not store.is_file():
            print('Recorder database not found; no audit possible');return 2
        report=audit(store,scanner,auto,args.auto_paths_limit)
        print(json.dumps(report,indent=2,ensure_ascii=False));return 0 if report['success'] else 2
    scan=ro_db(scanner)
    try:
        total=scan.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        recent=scan.execute('SELECT symbol,first_seen_ms FROM events ORDER BY first_seen_ms DESC LIMIT 1').fetchone()
        print('SCANNER read-only:',str(scanner),'events:',total,'newest:',recent)
        print('STORE isolated:',store)
        print('WINDOWS 1m:120 and 15m:96 | ANCHORS:',ANCHORS)
        try:
            from dotenv import load_dotenv
            load_dotenv(project/'.env')
        except ImportError:pass
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError('redis Python module required on the VPS') from exc
        client=redis.Redis(host=os.getenv('REDIS_HOST','127.0.0.1'),
                           port=int(os.getenv('REDIS_PORT','6379')),
                           db=int(os.getenv('REDIS_DB','0')),socket_timeout=10)
        client.ping()
        if not args.run_once and not args.loop:
            sample = {}
            if recent:
                for tf in TFS:
                    raw = client.lrange(history_key(str(recent[0]),tf),-HISTORY_MAXLEN,-1)
                    valid,bad,invalid=parse_redis_bars(raw,str(recent[0]),tf,int(time.time()*1000))
                    sample[tf]={'raw_items':len(raw),'valid_closed':len(valid),
                                'conflicting_timestamps':len(bad),'invalid_rows':invalid}
            print('REDIS reachable, sample:',json.dumps(sample,ensure_ascii=False))
            print('CHECK OK: no files/Redis modified; run --loop to record future events')
            return 0
        store.parent.mkdir(parents=True,exist_ok=True)
        lock=store.with_suffix(store.suffix+'.lock')
        with lock.open('a+') as lock_handle:
            try:fcntl.flock(lock_handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RuntimeError('Another context recorder is running')
            db=open_store(store)
            try:
                start=initialize_start(db,int(time.time()*1000),args.bootstrap_lookback_minutes)
                print('PERSISTED_START_MS',start,'(initial bootstrap; persists across restarts)')
                stop=False
                def shutdown(signum,frame):
                    nonlocal stop
                    stop=True
                signal.signal(signal.SIGINT,shutdown)
                signal.signal(signal.SIGTERM,shutdown)
                while not stop:
                    try:
                        base=store.parent if store.parent.exists() else project
                        free=shutil.disk_usage(base).free//(1024*1024)
                        if free<args.min_free_mb:
                            raise RuntimeError(f'Low disk space {free} MB, required {args.min_free_mb} MB')
                        now=int(time.time()*1000)
                        result=run_cycle(scan,db,client,now,args.batch_size,
                            args.max_on_time_seconds*1000,args.retry_minutes*60_000)
                        print(json.dumps(result,ensure_ascii=False),flush=True)
                    except Exception:
                        LOG.exception('Cycle failure: no scanner/Redis writes; cursor preserved')
                        if not args.loop:return 1
                    if not args.loop:break
                    for _ in range(args.poll_seconds):
                        if stop:break
                        time.sleep(1)
            finally:db.close()
    finally:scan.close()
    return 0


if __name__=='__main__':sys.exit(main())
