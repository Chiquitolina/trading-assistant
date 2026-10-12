#!/usr/bin/env python3
"""Prospective Reaction Lab persisted-candidate 1m path collector (isolated SQLite store).

This process reads all persisted Reaction Lab Candidate V1 / V2 sources
and the central Redis *closed* 1m histories. It NEVER writes to the existing dashboard paths_1m.pkl.
It does not discover REACTIONs or backfill missing candles from Binance.

Modes:
  --check       Read-only inspection, DEFAULT.
  --run-once    Persist one batch into isolated SQLite DB.
  --loop        Persist once per poll interval until SIGINT/SIGTERM.
  --status      Inspect SQLite counts without modification.

One physical REACTION has one Event ID, even if it belongs to V1 and V2.
No historical paths are made up: a path starts at retest+60_000ms and stops
at the first missing minute. 360 consecutive bars mark COMPLETE.
"""
from __future__ import annotations

import argparse
import csv
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
import zlib

LOGGER = logging.getLogger('reaction-lab-auto-paths')
BAR = struct.Struct('<qdddd')
ONE_MINUTE_MS = 60_000
HORIZON = 360
REDIS_LIMIT = 400
VERSION = 1

# Canonical persisted Candidate research sources found in dashboard/app.py.
# The V1 RAW universe includes prefilter/forward candidates, while V1 frozen
# SHORT/LONG histories must also be included in case the RAW aggregate lags.
SOURCE_FILES = {
    'v1_raw': 'reports/candidate_v1_analysis/legacy_v1_raw_monitor.csv',
    'v1_short_frozen': 'candidate_v1_frozen_monitor.csv',
    'v1_long_frozen': 'candidate_v1_long_frozen_monitor.csv',
    'v2': 'candidate_v2_monitor.csv',
    'continuous_scan': 'reports/reaction_lab/scanner_events_v1.sqlite3',
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  event_key TEXT PRIMARY KEY,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  cohort TEXT NOT NULL,
  retest_ts INTEGER NOT NULL,
  entry_ts INTEGER NOT NULL,
  state TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 0,
  last_ts INTEGER,
  path_blob BLOB,
  last_error TEXT,
  first_seen_utc_ms INTEGER NOT NULL,
  updated_utc_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_state_symbol ON events(state,symbol);
CREATE TABLE IF NOT EXISTS event_sources (
  event_key TEXT NOT NULL,
  source TEXT NOT NULL,
  cohort TEXT NOT NULL,
  PRIMARY KEY(event_key,source),
  FOREIGN KEY(event_key) REFERENCES events(event_key)
);
CREATE INDEX IF NOT EXISTS idx_sources_source ON event_sources(source);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def decode_path(blob: bytes | None) -> list[tuple[int, float, float, float, float]]:
    if not blob:
        return []
    raw = zlib.decompress(blob)
    if len(raw) % BAR.size:
        raise ValueError('Corrupt auto-path payload: invalid byte count')
    return [BAR.unpack_from(raw, i) for i in range(0, len(raw), BAR.size)]


def encode_path(rows: list[tuple[int, float, float, float, float]]) -> bytes | None:
    if not rows:
        return None
    return zlib.compress(b''.join(BAR.pack(*row) for row in rows), 6)


def monitor_events(path: Path) -> dict[str, tuple[str, str, str, int]]:
    """Normalize one persisted monitor to causal symbol|side|retest_ts IDs."""
    if not path.is_file():
        raise FileNotFoundError(f'Persisted monitor missing: {path}')
    events = {}
    with path.open('r', encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            raise ValueError(f'Monitor CSV has no header: {path}')
        required = {'symbol', 'retest_timestamp'}
        if not required.issubset(reader.fieldnames):
            raise ValueError(f'{path}: missing columns {required - set(reader.fieldnames)}')
        for row in reader:
            # Read only actual causal REACTION candidates, never draft watches.
            status = str(row.get('status') or '').strip().upper()
            if status and status != 'REACTION':
                continue
            symbol = str(row.get('symbol') or '').strip().upper()
            side = str(row.get('signal') or row.get('side') or '').strip().upper()
            if side not in ('LONG', 'SHORT') or not symbol:
                continue
            try:
                retest = int(float(row['retest_timestamp']))
            except (TypeError, ValueError, OverflowError):
                continue
            if retest <= 0:
                continue
            key = f'{symbol}|{side}|{retest}'
            supplied_key = str(row.get('candidate_v2_event_key') or
                               row.get('candidate_v1_event_key') or '').strip()
            if supplied_key and supplied_key != key:
                # Old monitors can contain noncanonical timestamps; do not
                # silently associate these with a different REACTION.
                continue
            cohort = str(row.get('candidate_v2_cohort') or
                         row.get('candidate_v1_cohort') or 'FORWARD').strip().upper()
            if cohort not in ('DISCOVERY', 'FORWARD'):
                cohort = 'FORWARD'
            identity = (symbol, side, cohort, retest)
            old = events.get(key)
            if old is not None and (old[0], old[1], old[3]) != (symbol, side, retest):
                raise ValueError(f'Conflicting repeated event ID: {key}')
            events[key] = identity
    return events


def scanner_events(path: Path) -> dict[str, tuple[str, str, str, int]]:
    """Read canonical standalone scanner journal, without acquiring write lock."""
    if not path.exists():
        raise FileNotFoundError(path)
    con = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=20)
    try:
        records = con.execute(
            'SELECT event_key,symbol,side,retest_ts FROM events'
        ).fetchall()
    finally:
        con.close()
    result = {}
    for key, symbol, side, ts in records:
        side = str(side).upper()
        symbol = str(symbol).upper()
        if key != f'{symbol}|{side}|{int(ts)}' or side not in ('LONG','SHORT'):
            raise ValueError('Invalid event identity in scanner journal: '+str(key))
        result[key] = (symbol, side, 'FORWARD', int(ts))
    return result


def read_sources(project: Path) -> tuple[dict, dict, dict]:
    """Read all available persisted candidates. Deduplicate across profiles.

    Returns: unique event map, event->source/cohort membership, per-source
    diagnostics. A missing optional legacy source is reported, never invented.
    A malformed existing source raises BEFORE any database writes.
    """
    events: dict[str, tuple[str, str, str, int]] = {}
    memberships: dict[str, dict[str, str]] = {}
    diagnostics = {}
    for name, relative_path in SOURCE_FILES.items():
        source_path = project / relative_path
        if not source_path.is_file():
            diagnostics[name] = {'file': relative_path, 'exists': False, 'events': 0}
            continue
        parsed = scanner_events(source_path) if name == 'continuous_scan' else monitor_events(source_path)
        diagnostics[name] = {'file': relative_path, 'exists': True, 'events': len(parsed)}
        for key, (symbol, side, cohort, retest) in parsed.items():
            old = events.get(key)
            if old is not None and (old[0], old[1], old[3]) != (symbol, side, retest):
                raise ValueError(f'Cross-source event identity conflict: {key}')
            if old is None or name == 'v2':
                events[key] = (symbol, side, cohort, retest)
            memberships.setdefault(key, {})[name] = cohort
    if not any(d['exists'] for d in diagnostics.values()):
        raise FileNotFoundError('No Candidate V1/V2 persisted sources found in ' + str(project))
    return events, memberships, diagnostics

def closed_redis_bars(raw_items: list[bytes | str], now_ms: int) -> dict[int, tuple[int, float, float, float, float]]:
    """Match GeometryScannerDataService closed-candle rules; reject open/invalid."""
    candles = {}
    for raw in raw_items:
        try:
            c = json.loads(raw)
            ts = int(c['timestamp'])
            close_ts = int(c.get('close_timestamp', ts + ONE_MINUTE_MS - 1))
            o, h, l, cl = (float(c[k]) for k in ('open', 'high', 'low', 'close'))
            if (close_ts > now_ms or ts > now_ms or
                not all(math.isfinite(x) and x > 0 for x in (o, h, l, cl)) or
                h < max(o, l, cl) or l > min(o, h, cl)):
                continue
            candles[ts] = (ts, o, h, l, cl)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
    return candles


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=20)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA busy_timeout=20000')
    con.execute('PRAGMA synchronous=NORMAL')
    con.executescript(SCHEMA)
    con.execute('INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)', ('schema_version', str(VERSION)))
    version = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    if int(version) != VERSION:
        con.close()
        raise RuntimeError(f'Unsupported auto-path DB schema version: {version}')
    con.commit()
    return con


def register_events(con: sqlite3.Connection, events: dict,
                    now_ms: int, memberships: dict | None = None) -> dict[str, int]:
    # Old events remain audit evidence. Never backfill imaginary candles.
    new = stale = source_links = 0
    memberships = memberships or {}
    for key, (symbol, side, cohort, retest) in events.items():
        entry_ts = retest + ONE_MINUTE_MS
        state = ('STALE_BEFORE_COLLECTOR'
                 if now_ms - entry_ts > REDIS_LIMIT * ONE_MINUTE_MS else 'WAITING')
        result = con.execute(
            '''INSERT OR IGNORE INTO events
               (event_key,symbol,side,cohort,retest_ts,entry_ts,state,count,first_seen_utc_ms,updated_utc_ms)
               VALUES (?,?,?,?,?,?,?,0,?,?)''',
            (key, symbol, side, cohort, retest, entry_ts, state, now_ms, now_ms),
        )
        if result.rowcount:
            new += 1
            stale += state == 'STALE_BEFORE_COLLECTOR'
        for source, source_cohort in memberships.get(key, {}).items():
            res = con.execute(
                '''INSERT INTO event_sources(event_key,source,cohort) VALUES (?,?,?)
                   ON CONFLICT(event_key,source) DO UPDATE SET cohort=excluded.cohort''',
                (key, source, source_cohort),
            )
            source_links += (res.rowcount > 0)
    con.commit()
    return {'new': new, 'stale_new': stale, 'source_memberships_upserted': source_links}

def advance_path(existing: list, by_ts: dict, entry_ts: int) -> list:
    """Append only an exact contiguous prefix; never skip gaps or reorder history."""
    if len(existing) >= HORIZON:
        return existing
    if any(int(bar[0]) != entry_ts + offset * ONE_MINUTE_MS for offset, bar in enumerate(existing)):
        raise ValueError('Existing path not contiguous from causal entry')
    out = list(existing)
    while len(out) < HORIZON:
        next_ts = entry_ts + len(out) * ONE_MINUTE_MS
        candle = by_ts.get(next_ts)
        if candle is None:
            break
        out.append(candle)
    return out


def process_active(con: sqlite3.Connection, redis_client, now_ms: int) -> dict:
    # Group events by symbol; request each active symbol's Redis history once.
    rows = con.execute('''SELECT event_key,symbol,entry_ts,state,count,path_blob
        FROM events WHERE state IN ('WAITING','ACTIVE','GAP_WAITING') ORDER BY symbol,entry_ts''').fetchall()
    groups = {}
    for row in rows:
        groups.setdefault(row[1], []).append(row)
    updated = complete = missing_entry = gaps = no_redis = 0
    for symbol, events in groups.items():
        try:
            # On steady-state ticks, read just a small recent tail. A newly
            # discovered event may require a longer catch-up, up to 400m.
            oldest_next = min(entry + saved * ONE_MINUTE_MS for _, _, entry, _, saved, _ in events)
            required = min(REDIS_LIMIT, max(16, (now_ms - oldest_next) // ONE_MINUTE_MS + 5))
            raw = redis_client.lrange(f'market-data:v1:history:{symbol}:1m', -int(required), -1)
        except Exception as exc:
            LOGGER.warning('Redis read failed: %s: %s', symbol, exc)
            no_redis += len(events)
            continue
        by_ts = closed_redis_bars(raw, now_ms)
        if not by_ts:
            no_redis += len(events)
            continue
        first_ts, last_ts = min(by_ts), max(by_ts)
        for key, _, entry_ts, state, count, blob in events:
            path = decode_path(blob)
            if len(path) != count:
                raise ValueError(f'Stored count mismatch for {key}')
            next_ts = entry_ts + count * ONE_MINUTE_MS
            new_state = state
            error = None
            if count == 0 and first_ts > entry_ts:
                new_state, error = 'MISSING_ENTRY', 'Entry minute already outside Redis buffer'
                missing_entry += 1
            elif count and first_ts > next_ts and last_ts >= next_ts:
                new_state, error = 'GAP_EXPIRED', 'Next required candle fell outside Redis buffer'
                gaps += 1
            else:
                expanded = advance_path(path, by_ts, entry_ts)
                if len(expanded) > count:
                    path = expanded
                    new_state = 'COMPLETE' if len(path) == HORIZON else 'ACTIVE'
                    complete += new_state == 'COMPLETE'
                    updated += 1
                elif last_ts >= next_ts and next_ts >= first_ts:
                    # The market-data producer may subsequently repair a gap;
                    # keep retrying while the missing timestamp is in buffer.
                    new_state, error = 'GAP_WAITING', 'Missing 1m candle; awaiting producer repair'
                    gaps += 1
                elif last_ts < next_ts:
                    new_state = 'ACTIVE' if count else 'WAITING'
            if len(path) != count or new_state != state:
                con.execute('''UPDATE events SET state=?,count=?,last_ts=?,path_blob=?,last_error=?,updated_utc_ms=?
                  WHERE event_key=?''', (
                    new_state, len(path), path[-1][0] if path else None,
                    encode_path(path), error, now_ms, key
                ))
        con.commit()
    return {'active_read': len(rows), 'redis_symbols': len(groups), 'extended': updated,
            'completed_now': complete, 'missing_entry_now': missing_entry,
            'gaps_now': gaps, 'unavailable_redis': no_redis}


def db_status(con) -> dict:
    counts = {name: count for name, count in con.execute('SELECT state, COUNT(*) FROM events GROUP BY state')}
    bars = con.execute('SELECT COALESCE(SUM(count),0) FROM events').fetchone()[0]
    profiles = {name: count for name, count in con.execute('SELECT source,COUNT(*) FROM event_sources GROUP BY source')}
    return {'events': sum(counts.values()), 'states': counts, 'saved_bars': bars, 'profile_memberships': profiles}


def db_status_readonly(db_path: Path) -> dict:
    if not db_path.is_file():
        return {'events': 0, 'states': {}, 'saved_bars': 0, 'database_exists': False}
    con = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=10)
    try:
        return dict(db_status(con), database_exists=True)
    finally:
        con.close()


def run_once(project: Path, db_path: Path, redis_client,
             now_ms: int, min_free_mb: int) -> dict:
    events, memberships, sources = read_sources(project)  # parse before DB write
    disk_parent = db_path.parent if db_path.parent.exists() else project
    free_mb = shutil.disk_usage(disk_parent).free / (1024 ** 2)
    if free_mb < min_free_mb:
        raise RuntimeError(f'Low free disk space: {free_mb:.0f}MB < {min_free_mb}MB; refusing writes')
    con = open_db(db_path)
    try:
        registered = register_events(con, events, now_ms, memberships)
        progress = process_active(con, redis_client, now_ms)
        return {'unique_persisted_events': len(events), 'sources': sources,
                'registered': registered, 'progress': progress,
                'database': db_status(con),
                'disk_free_mb_before': round(free_mb)}
    finally:
        con.close()

def read_auto_paths_dataframe(db_path: Path, event_keys: set[str] | None = None, source: str | None = None):
    """Read-only future dashboard bridge: same columns as Candidate fast paths.

    This does NOT merge with or write to dashboard paths_1m.pkl. UI integration
    must be reviewed separately before enabling it.
    """
    import pandas as pd
    fields = ['candidate_v1_event_key','candidate_v1_cohort','symbol','side',
              'reaction_timestamp','entry_timestamp','bar_offset','timestamp','open','high','low','close']
    if source is not None and source not in SOURCE_FILES:
        raise ValueError('Unknown Candidate source: ' + source)
    if not db_path.exists():
        return pd.DataFrame(columns=fields)
    con = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        rows = con.execute('''SELECT e.event_key, COALESCE(s.cohort,e.cohort),
                                      e.symbol,e.side,e.retest_ts,e.entry_ts,e.path_blob
                               FROM events e LEFT JOIN event_sources s
                                 ON s.event_key=e.event_key AND s.source=?
                               WHERE e.count > 0
                               AND (? IS NULL OR s.source IS NOT NULL)''', (source, source))
        output = []
        for key, cohort, symbol, side, retest, entry, blob in rows:
            if event_keys is not None and key not in event_keys:
                continue
            for offset, (ts, o, h, l, cl) in enumerate(decode_path(blob)):
                output.append((key, cohort, symbol, side, retest, entry, offset, ts, o, h, l, cl))
        return pd.DataFrame(output, columns=fields)
    finally:
        con.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path.cwd())
    parser.add_argument('--db', type=Path, default=None,
                        help='Isolated SQLite store; NEVER the existing dashboard pickle')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='Read-only monitor/Redis check (default)')
    mode.add_argument('--status', action='store_true', help='Read-only SQLite status')
    mode.add_argument('--run-once', action='store_true', help='ONE writable cycle')
    mode.add_argument('--loop', action='store_true', help='Continuous writable service')
    parser.add_argument('--poll-seconds', type=int, default=60)
    parser.add_argument('--min-free-mb', type=int, default=1024)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    project = args.project.resolve()
    db_path = (args.db if args.db else project / 'reports/reaction_lab/auto_paths_v1.sqlite3').resolve()
    legacy_paths = [(project / p).resolve() for p in (
        'reports/candidate_v1_analysis/paths_1m.pkl',
        'reports/candidate_v1_analysis/matched_reaction_paths_1m.pkl',
        'reports/candidate_v2_analysis/paths_1m.pkl',
    )]
    if db_path in legacy_paths or db_path.suffix.lower() not in ('.sqlite3', '.db'):
        parser.error('Refusing to use dashboard pickle as collector storage')
    print('REACTION LAB AUTO PATHS v1 | mode:', 'LOOP' if args.loop else 'RUN-ONCE' if args.run_once else 'STATUS' if args.status else 'CHECK')
    print('Project:', project)
    print('Collector DB:', db_path)
    print('Dashboard pickles UNTOUCHED:', [str(p) for p in legacy_paths])
    if args.status:
        print(json.dumps(db_status_readonly(db_path), ensure_ascii=False, indent=2))
        return 0
    if not args.run_once and not args.loop:
        found, memberships, source_status = read_sources(project)
        print('Persisted unique REACTION Event IDs:', len(found))
        print('Source counts:', json.dumps(source_status, ensure_ascii=False))
        print('Cross-source duplicated Event IDs:', sum(len(x)-1 for x in memberships.values()))
        print('No files modified. Next step: --run-once after reviewing output.')
        return 0
    # A collector must have a live Redis connection: never silently proceed with
    # a missing feed and misleading progress.
    try:
        import redis
    except ImportError as exc:
        raise RuntimeError('Python redis module is required on the VPS') from exc
    try:
        from dotenv import load_dotenv
        load_dotenv(project / '.env')
    except ImportError:
        pass  # Default Redis settings work without python-dotenv.
    client = redis.Redis(host=os.getenv('REDIS_HOST', '127.0.0.1'),
                         port=int(os.getenv('REDIS_PORT', '6379')),
                         db=int(os.getenv('REDIS_DB', '0')), socket_timeout=12)
    # Use the project's Redis configuration without exposing any secret.
    client.ping()
    lockfile = db_path.with_suffix(db_path.suffix + '.lock')
    lockfile.parent.mkdir(parents=True, exist_ok=True)
    with lockfile.open('a+') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another auto-path collector is already running')
        halted = False
        def stop(signum, _frame):
            nonlocal halted
            halted = True
            LOGGER.info('Received signal %s; stopping after current cycle', signum)
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        while not halted:
            now_ms = int(time.time() * 1000)
            try:
                data = run_once(project, db_path, client, now_ms, args.min_free_mb)
                print(json.dumps(data, ensure_ascii=False), flush=True)
            except Exception:
                LOGGER.exception('Collection cycle failed; preserving existing data')
                if not args.loop:
                    return 1
            if not args.loop:
                break
            # Allows SIGTERM to take effect promptly without timers/threads.
            for _ in range(max(1, args.poll_seconds)):
                if halted:
                    break
                time.sleep(1)
    return 0

if __name__ == '__main__':
    sys.exit(main())
