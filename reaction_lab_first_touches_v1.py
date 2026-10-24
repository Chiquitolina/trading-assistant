#!/usr/bin/env python3
"""Append-only causal journal of ALL confirmed-swing first retests.

The continuous scanner owns writes; this module also offers read-only --status,
--recent CLI. This store is intentionally separate from scanner_events_v1.sqlite3
because existing downstream Candidate/Auto Paths readers assume every row in
that older database is a REACTION.

No future MFE, TP/SL, path outcome or Binance REST calls are stored here.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3

STATUSES = frozenset(('REACTION', 'TOUCH_INDECISIVE', 'TOUCH_FAILED'))
SCHEMA_VERSION = '1'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS first_touches (
 touch_key TEXT PRIMARY KEY,
 legacy_event_key TEXT NOT NULL,
 symbol TEXT NOT NULL,
 side TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('REACTION','TOUCH_INDECISIVE','TOUCH_FAILED')),
 timeframe TEXT NOT NULL,
 detector TEXT NOT NULL,
 pivot_ts INTEGER NOT NULL,
 actionable_ts INTEGER NOT NULL,
 retest_ts INTEGER NOT NULL,
 known_ts INTEGER NOT NULL,
 observation_type TEXT NOT NULL,
 first_seen_ms INTEGER NOT NULL,
 last_seen_ms INTEGER NOT NULL,
 source_hash TEXT NOT NULL,
 causal_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_first_touches_retest ON first_touches(retest_ts);
CREATE INDEX IF NOT EXISTS idx_first_touches_status_retest ON first_touches(status,retest_ts);
CREATE INDEX IF NOT EXISTS idx_first_touches_symbol_retest ON first_touches(symbol,retest_ts);
CREATE TABLE IF NOT EXISTS cycles (
 cycle_id INTEGER PRIMARY KEY AUTOINCREMENT,
 started_ms INTEGER NOT NULL,
 ended_ms INTEGER NOT NULL,
 detected INTEGER NOT NULL,
 inserted INTEGER NOT NULL,
 conflicts INTEGER NOT NULL,
 statuses_json TEXT NOT NULL,
 source_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS classification_conflicts (
 touch_key TEXT NOT NULL,
 original_status TEXT NOT NULL,
 observed_status TEXT NOT NULL,
 first_conflict_ms INTEGER NOT NULL,
 last_conflict_ms INTEGER NOT NULL,
 count INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(touch_key,observed_status)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
'''


def default_db(project: Path) -> Path:
    return project / 'reports/reaction_lab/scanner_first_touches_v1.sqlite3'


def _positive_integer(value, name: str) -> int:
    if value is None or isinstance(value, bool):
        raise ValueError(f'Missing {name}')
    number = int(value)
    if number <= 0:
        raise ValueError(f'Invalid {name}: {number}')
    return number


def touch_identity(payload: dict) -> tuple[str, str, str, str, str, str, int, int, int]:
    """First-touch identity is swing-specific; never includes mutable status.

    Legacy candidate IDs (symbol|side|retest_ts) aren't unique across two
    different confirmed pivots that touch during one candle.
    """
    symbol = str(payload.get('symbol') or '').strip().upper()
    side = str(payload.get('signal') or '').strip().upper()
    timeframe = str(payload.get('timeframe') or '').strip()
    detector = str(payload.get('detector') or '').strip()
    if not symbol or side not in ('LONG', 'SHORT') or not timeframe or not detector:
        raise ValueError('Missing swing identity: symbol/side/timeframe/detector')
    pivot_ts = _positive_integer(payload.get('pivot_timestamp'), 'pivot_timestamp')
    actionable_ts = _positive_integer(payload.get('actionable_timestamp'), 'actionable_timestamp')
    retest_ts = _positive_integer(payload.get('retest_timestamp'), 'retest_timestamp')
    if actionable_ts > retest_ts or pivot_ts > actionable_ts:
        raise ValueError('Non-causal pivot/actionable/retest ordering')
    touch_key = '|'.join(map(str, (
        symbol, side, timeframe, detector, pivot_ts, actionable_ts, retest_ts
    )))
    return (touch_key, f'{symbol}|{side}|{retest_ts}', symbol, side,
            timeframe, detector, pivot_ts, actionable_ts, retest_ts)


def normalize_first_touches(frame, now_ms: int, causal_fields, coerce_jsonable) -> dict:
    """Keep all three CLOSED statuses, without inventing missing timestamps."""
    events = {}
    if frame is None or frame.empty:
        return events
    for raw in frame.to_dict('records'):
        status = str(raw.get('status') or '').strip().upper()
        if status not in STATUSES:
            continue
        try:
            (key, legacy_key, symbol, side, timeframe, detector,
             pivot_ts, actionable_ts, retest_ts) = touch_identity(raw)
        except (TypeError, ValueError, OverflowError):
            continue
        # A retest becomes known at the CLOSE of its 1m candle.
        if retest_ts + 60_000 > now_ms:
            continue
        payload = {k:coerce_jsonable(raw.get(k)) for k in causal_fields if k in raw}
        payload.update(symbol=symbol, signal=side, status=status,
                       timeframe=timeframe, detector=detector,
                       pivot_timestamp=pivot_ts,
                       actionable_timestamp=actionable_ts,
                       retest_timestamp=retest_ts, touch_key=key,
                       legacy_event_key=legacy_key,
                       known_timestamp=retest_ts+60_000)
        # These three mutually exclusive flags are all known at candle close.
        payload['reaction'] = status == 'REACTION'
        payload['touch_failed'] = status == 'TOUCH_FAILED'
        payload['touch_indecisive'] = status == 'TOUCH_INDECISIVE'
        previous = events.get(key)
        if previous is not None and previous['status'] != status:
            raise ValueError(f'Conflicting classification of touch {key} in same scan')
        events.setdefault(key, payload)
    return events


def db_open(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True,exist_ok=True)
    con = sqlite3.connect(str(db), timeout=30)
    try:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('PRAGMA busy_timeout=30000')
        con.executescript(SCHEMA)
        con.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('schema_version', SCHEMA_VERSION))
        version = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if not version or version[0] != SCHEMA_VERSION:
            raise RuntimeError('Unexpected first-touch SQLite schema version')
        con.commit()
    except Exception:
        con.close()
        raise
    return con


def save_cycle(con: sqlite3.Connection, touches: dict, now_ms: int,
               started_ms: int, fingerprint: str) -> dict:
    inserted = conflicts = 0
    counts = dict(Counter(t['status'] for t in touches.values()))
    with con:
        for key, row in touches.items():
            # Independently revalidate identity; never silently re-key a touch.
            if touch_identity(row)[0] != key:
                raise ValueError('Inconsistent first-touch key: ' + str(key))
            ts = int(row['retest_timestamp'])
            observation = ('RECOVERED_WINDOW' if ts + 120_000 < started_ms
                           else 'LIVE_OBSERVED')
            result = con.execute('''INSERT OR IGNORE INTO first_touches
                (touch_key,legacy_event_key,symbol,side,status,timeframe,detector,
                 pivot_ts,actionable_ts,retest_ts,known_ts,observation_type,
                 first_seen_ms,last_seen_ms,source_hash,causal_json)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (key,row['legacy_event_key'],row['symbol'],row['signal'],
                 row['status'],row['timeframe'],row['detector'],
                 row['pivot_timestamp'],row['actionable_timestamp'],ts,ts+60_000,
                 observation,now_ms,now_ms,fingerprint,
                 json.dumps(row,ensure_ascii=False,allow_nan=False,sort_keys=True)))
            if result.rowcount:
                inserted += 1
            else:
                previous = con.execute('SELECT status FROM first_touches WHERE touch_key=?',
                                       (key,)).fetchone()
                if previous is None:
                    raise RuntimeError('Missing touch after ignored insert: ' + key)
                if previous[0] != row['status']:
                    conflicts += 1
                    con.execute('''INSERT INTO classification_conflicts
                       (touch_key,original_status,observed_status,first_conflict_ms,last_conflict_ms,count)
                       VALUES (?,?,?,?,?,1)
                       ON CONFLICT(touch_key,observed_status) DO UPDATE SET
                       last_conflict_ms=excluded.last_conflict_ms,
                       count=count+1''',
                       (key,previous[0],row['status'],now_ms,now_ms))
                con.execute('UPDATE first_touches SET last_seen_ms=? WHERE touch_key=?',
                            (now_ms,key))
        con.execute('''INSERT INTO cycles
            (started_ms,ended_ms,detected,inserted,conflicts,statuses_json,source_hash)
            VALUES (?,?,?,?,?,?,?)''',
            (started_ms,now_ms,len(touches),inserted,conflicts,
             json.dumps(counts,sort_keys=True),fingerprint))
    return {'detected':len(touches),'new':inserted,
            'status_counts':counts,'classification_conflicts':conflicts}


def status(db: Path) -> dict:
    if not db.is_file():
        return {'database_exists': False, 'total_touches': 0, 'status_counts': {}}
    con = sqlite3.connect(f'file:{db}?mode=ro',uri=True,timeout=10)
    try:
        counts = dict(con.execute('SELECT status,COUNT(*) FROM first_touches GROUP BY status'))
        observation = dict(con.execute('SELECT observation_type,COUNT(*) FROM first_touches GROUP BY observation_type'))
        total_conflicts = con.execute('SELECT COALESCE(SUM(count),0) FROM classification_conflicts').fetchone()[0]
        last = con.execute('''SELECT started_ms,ended_ms,detected,inserted,conflicts,statuses_json
                             FROM cycles ORDER BY cycle_id DESC LIMIT 1''').fetchone()
        return {'database_exists':True,'total_touches':sum(counts.values()),
                'status_counts':{s:counts.get(s,0) for s in ('REACTION','TOUCH_INDECISIVE','TOUCH_FAILED')},
                'observation_types':observation, 'classification_conflicts':total_conflicts,
                'last_cycle':list(last) if last else None}
    finally:
        con.close()


def recent(db:Path, status_filter:str | None, limit:int) -> list[dict]:
    if not db.is_file():
        return []
    if status_filter is not None and status_filter not in STATUSES:
        raise ValueError('Unsupported status filter: ' + status_filter)
    con = sqlite3.connect(f'file:{db}?mode=ro',uri=True,timeout=10)
    try:
        rows = con.execute('''SELECT touch_key,status,symbol,side,retest_ts,
                              pivot_ts,actionable_ts,first_seen_ms,observation_type,causal_json
                            FROM first_touches
                            WHERE (? IS NULL OR status=?)
                            ORDER BY retest_ts DESC,touch_key LIMIT ?''',
                           (status_filter,status_filter,max(1,min(int(limit),1000)))).fetchall()
        return [dict(touch_key=row[0],status=row[1],symbol=row[2],side=row[3],
                     retest_ts=row[4],pivot_ts=row[5],actionable_ts=row[6],
                     first_seen_ms=row[7],observation_type=row[8],
                     retest_close=json.loads(row[9]).get('retest_close')) for row in rows]
    finally:
        con.close()


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project',type=Path,default=Path.cwd())
    p.add_argument('--db',type=Path)
    mode=p.add_mutually_exclusive_group()
    mode.add_argument('--status',action='store_true')
    mode.add_argument('--recent',action='store_true')
    p.add_argument('--filter',choices=sorted(STATUSES),default=None)
    p.add_argument('--limit',type=int,default=20)
    args=p.parse_args(argv)
    db=(args.db or default_db(args.project.resolve())).resolve()
    if db.suffix.lower() not in ('.db','.sqlite3'):
        p.error('Require an SQLite database path')
    print(json.dumps(recent(db,args.filter,args.limit) if args.recent else status(db),
                     ensure_ascii=False,indent=2))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
