#!/usr/bin/env python3
"""Durable, post-signal (>1%) runner catalog for ALL Continuous V1 Base REACTIONs.

Reads scanner, context, auto-path SQLite; writes ONLY independent runners_v1.sqlite3.
Does not alter candidate criteria or claim MFE was knowable at signal time.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import shutil
import signal
import sqlite3
import time
import zlib
from contextlib import closing
from pathlib import Path

from reaction_lab_auto_paths import decode_path
from reaction_lab_unified_adapter import observed_reaction_run

MINUTE_MS = 60_000
THRESHOLD = 1.0  # strictly > 1% as requested
SCANNER = 'reports/reaction_lab/scanner_events_v1.sqlite3'
PATHS = 'reports/reaction_lab/auto_paths_v1.sqlite3'
CONTEXT = 'reports/reaction_lab/causal_context_v1.sqlite3'
RUNNERS = 'reports/reaction_lab/runners_v1.sqlite3'
SCHEMA_VERSION = '1'

SCHEMA = """
CREATE TABLE IF NOT EXISTS runners (
 event_key TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL,
 retest_ts INTEGER NOT NULL, known_ts INTEGER NOT NULL,
 observation_type TEXT NOT NULL, source_hash TEXT,
 retest_close REAL NOT NULL, threshold_pct REAL NOT NULL,
 first_qualified_at_ms INTEGER NOT NULL, threshold_cross_ts INTEGER NOT NULL,
 peak_run_pct REAL NOT NULL, peak_ts INTEGER NOT NULL,
 max_adverse_pct REAL NOT NULL, observed_min INTEGER NOT NULL,
 path_state TEXT NOT NULL, path_count INTEGER NOT NULL, path_blob BLOB,
 scanner_causal_json TEXT NOT NULL,
 context_state TEXT, context_json TEXT, context_notes_json TEXT, candles_json TEXT,
 v1_regime_status TEXT, v1_regime_json TEXT,
 followup_state TEXT, followup_json TEXT,
 updated_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runners_peak ON runners(peak_run_pct DESC);
CREATE INDEX IF NOT EXISTS idx_runners_retest ON runners(retest_ts DESC);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
"""


def readonly(path: Path):
    return sqlite3.connect(f'file:{path.resolve().as_posix()}?mode=ro', uri=True, timeout=15)


def numeric(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError, OverflowError):
        return None


def analyze_path(bars, *, retest_ts, retest_close, side):
    """Same MFE as canonical observed_reaction_run; add temporal evidence and MAE."""
    ref = observed_reaction_run(bars, retest_timestamp=retest_ts,
                                retest_close=retest_close, signal=side)
    if ref is None:
        return None
    high_score = 0.0
    adverse = 0.0
    peak_ts = None
    first_cross_ts = None
    expected = int(retest_ts) + MINUTE_MS
    baseline = float(retest_close)
    side = side.upper()
    for item in bars[:int(ref['reaction_observed_min'])]:
        ts, o, h, l, c = item[:5]
        if int(ts) != expected:
            raise ValueError('Unexpected path discontinuity after canonical validation')
        o, h, l, c = (numeric(x) for x in (o, h, l, c))
        if any(v is None or v <= 0 for v in (o, h, l, c)) or h < max(o,c,l) or l > min(o,c,h):
            raise ValueError('Invalid OHLC in archived path')
        favor = (h / baseline - 1) * 100 if side == 'LONG' else (1 - l / baseline) * 100
        against = (1 - l / baseline) * 100 if side == 'LONG' else (h / baseline - 1) * 100
        if favor > high_score:
            high_score = favor
            peak_ts = int(ts)
        if first_cross_ts is None and favor > THRESHOLD:
            first_cross_ts = int(ts)
        adverse = max(adverse, against)
        expected += MINUTE_MS
    if abs(high_score - ref['reaction_mfe_pct']) > 1e-9:
        raise ValueError('Runner MFE diverges from canonical observed run')
    if first_cross_ts is None:
        return None
    return {
        'peak_run_pct': high_score, 'peak_ts': peak_ts,
        'threshold_cross_ts': first_cross_ts,
        'max_adverse_pct': adverse,
        'observed_min': int(ref['reaction_observed_min']),
    }


def cohort_cutoff(scan: sqlite3.Connection, root: Path) -> int:
    first = scan.execute('SELECT MIN(started_ms) FROM cycles WHERE error IS NULL').fetchone()[0]
    if first is None:
        raise RuntimeError('Scanner has no successful completed cycle: cannot establish cohort cutoff')
    cutoff = int(first)
    manifest = root / 'reports/reaction_lab/continuous_v1_cohort.json'
    if manifest.is_file():
        data = json.loads(manifest.read_text(encoding='utf8'))
        frozen = int(data['start_ms'])
        if frozen < cutoff:
            raise RuntimeError('Manifest cutoff precedes scanner first successful cycle')
        cutoff = frozen
    return cutoff


def lookup_keys(con, keys, table, fields):
    out = {}
    for off in range(0, len(keys), 350):
        part = keys[off:off+350]
        if not part:
            continue
        placeholders = ','.join('?' for _ in part)
        for row in con.execute(f'SELECT event_key,{fields} FROM {table} WHERE event_key IN ({placeholders})', part):
            out[str(row[0])] = row[1:]
    return out


def db_init(con):
    con.executescript(SCHEMA)
    con.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('schema_version', SCHEMA_VERSION))
    version = con.execute('SELECT value FROM meta WHERE key=?', ('schema_version',)).fetchone()[0]
    if version != SCHEMA_VERSION:
        raise RuntimeError('Unknown runner archive schema version: ' + str(version))
    con.commit()


def _supplement(root, dst, keys, now_ms, refresh_followups=()):
    """Backfill context/sector/outcomes when they arrive AFTER runner qualification."""
    ctx_db = root / CONTEXT
    if not ctx_db.exists() or not keys:
        return 0
    n = 0
    refresh_followups = set(refresh_followups)
    keys = list(dict.fromkeys(keys))
    with closing(readonly(ctx_db)) as ctx:
        core = lookup_keys(ctx, keys, 'contexts', 'status,context_json,notes_json,candles_json')
        tables = {x[0] for x in ctx.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        regimes = lookup_keys(ctx, keys, 'v1_regime_events', 'status,metrics_json') if 'v1_regime_events' in tables else {}
        follows = lookup_keys(ctx, keys, 'followups', 'path_state,followup_json') if 'followups' in tables else {}
    for key in keys:
        values = core.get(key)
        regime = regimes.get(key)
        follow = follows.get(key)
        if not any((values,regime,follow)):
            continue
        cur = dst.execute('''UPDATE runners SET
           context_state=COALESCE(context_state,?),context_json=COALESCE(context_json,?),
           context_notes_json=COALESCE(context_notes_json,?),candles_json=COALESCE(candles_json,?),
           v1_regime_status=COALESCE(v1_regime_status,?),v1_regime_json=COALESCE(v1_regime_json,?),
           followup_state=CASE WHEN ? THEN COALESCE(?,followup_state)
                               ELSE COALESCE(followup_state,?) END,
           followup_json=CASE WHEN ? THEN COALESCE(?,followup_json)
                              ELSE COALESCE(followup_json,?) END
           WHERE event_key=? AND (context_json IS NULL OR v1_regime_json IS NULL
                OR followup_json IS NULL OR ? )''',
           (values[0] if values else None, values[1] if values else None,
            values[2] if values else None, values[3] if values else None,
            regime[0] if regime else None, regime[1] if regime else None,
            int(key in refresh_followups),follow[0] if follow else None,follow[0] if follow else None,
            int(key in refresh_followups),follow[1] if follow else None,follow[1] if follow else None,
            key,int(key in refresh_followups)))
        n += int(cur.rowcount > 0)
    return n


def run_once(root: Path, dst_path: Path, now_ms: int, min_free_mb: int = 512):
    scan_db, paths_db = root / SCANNER, root / PATHS
    if not scan_db.is_file() or not paths_db.is_file():
        raise RuntimeError('Required scanner/paths SQLite databases missing')
    if shutil.disk_usage(dst_path.parent if dst_path.parent.exists() else root).free < min_free_mb * 1024**2:
        raise RuntimeError('Not enough free disk space for runner archive')
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(dst_path),timeout=20)) as dst:
        dst.execute('PRAGMA journal_mode=WAL')
        dst.execute('PRAGMA busy_timeout=20000')
        db_init(dst)
        last = dst.execute("SELECT value FROM meta WHERE key='last_path_updated_ms'").fetchone()
        checkpoint = int(last[0]) if last else 0
        with closing(readonly(scan_db)) as scan, closing(readonly(paths_db)) as paths:
            cutoff = cohort_cutoff(scan, root)
            # Inclusive checkpoint makes same-millisecond changes safe; idempotent.
            changed = paths.execute('''SELECT event_key,path_blob,count,state,updated_utc_ms
                                       FROM events WHERE count>0 AND updated_utc_ms>=?
                                       ORDER BY updated_utc_ms,event_key''', (checkpoint,)).fetchall()
            keys = [str(row[0]) for row in changed]
            scanned = lookup_keys(scan, keys, 'events',
                                  'symbol,side,retest_ts,known_ts,observation_type,source_hash,causal_json')
            existing = lookup_keys(dst, keys, 'runners', 'peak_run_pct,observed_min,path_count')
            new_count = updated = eligible = path_errors = 0
            path_error_samples = []
            max_seen = checkpoint
            for key, blob, count, path_state, timestamp in changed:
                max_seen = max(max_seen, int(timestamp))
                original = scanned.get(str(key))
                if original is None:
                    continue  # Legacy path only, not a continuous Base REACTION
                symbol,side,retest_ts,known,observation,source_hash,raw = original
                if int(known) < cutoff:
                    continue
                eligible += 1
                if key != f'{str(symbol).upper()}|{str(side).upper()}|{int(retest_ts)}':
                    raise RuntimeError('Corrupt Event ID: ' + str(key))
                payload = json.loads(raw)
                baseline = numeric(payload.get('retest_close'))
                if baseline is None or baseline <= 0 or not blob:
                    continue
                try:
                    bars = decode_path(blob)
                    if len(bars) != int(count):
                        raise RuntimeError('Source path count mismatch')
                    metric = analyze_path(bars,retest_ts=int(retest_ts),retest_close=baseline,side=side)
                    previous = existing.get(key)
                    if previous is not None and int(count) < int(previous[2]):
                        raise RuntimeError('Archived path regression')
                    if previous is not None and metric is not None and metric['peak_run_pct'] + 1e-9 < float(previous[0]):
                        raise RuntimeError('Archived peak regression')
                except (ValueError,RuntimeError,IndexError,TypeError,zlib.error) as exc:
                    path_errors += 1
                    if len(path_error_samples) < 5:
                        path_error_samples.append(f'{key}: {type(exc).__name__}: {exc}')
                    continue
                if metric is None:
                    continue
                if previous is not None and (int(count) == int(previous[2])):
                    continue
                was_new = previous is None
                dst.execute('''INSERT INTO runners (
                  event_key,symbol,side,retest_ts,known_ts,observation_type,source_hash,
                  retest_close,threshold_pct,first_qualified_at_ms,threshold_cross_ts,
                  peak_run_pct,peak_ts,max_adverse_pct,observed_min,path_state,path_count,path_blob,
                  scanner_causal_json,updated_at_ms)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(event_key) DO UPDATE SET
                    peak_run_pct=excluded.peak_run_pct,peak_ts=excluded.peak_ts,
                    max_adverse_pct=excluded.max_adverse_pct,observed_min=excluded.observed_min,
                    path_state=excluded.path_state,path_count=excluded.path_count,path_blob=excluded.path_blob,
                    updated_at_ms=excluded.updated_at_ms''', (
                    key,symbol,side,int(retest_ts),int(known),observation,source_hash,
                    baseline,THRESHOLD,now_ms,metric['threshold_cross_ts'],
                    metric['peak_run_pct'],metric['peak_ts'],metric['max_adverse_pct'],metric['observed_min'],
                    path_state,int(count),blob,raw,now_ms))
                new_count += was_new
                updated += not was_new
            dst.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('last_path_updated_ms',str(max_seen)))
            pending_keys=[str(x[0]) for x in dst.execute('''SELECT event_key FROM runners
                      WHERE context_json IS NULL OR v1_regime_json IS NULL OR followup_json IS NULL''')]
            supplements = _supplement(root,dst,pending_keys + [k for k in keys if k in existing or k in scanned],
                                      now_ms, refresh_followups=set(keys))
            dst.commit()
            status = dict(dst.execute('SELECT path_state,COUNT(*) FROM runners GROUP BY path_state'))
            total = dst.execute('SELECT COUNT(*) FROM runners').fetchone()[0]
            return {'examined_changed_paths':len(changed),'continuous_with_path':eligible,
                    'new_runners':new_count,'runners_updated':updated,
                    'path_errors':path_errors,'path_error_samples':path_error_samples,
                    'metadata_supplements':supplements,'runner_total':total,
                    'runner_path_states':status,'cursor_ms':max_seen,
                    'cohort_cutoff_ms':cutoff}


def status(db: Path):
    if not db.is_file():
        return {'database_exists':False}
    with closing(readonly(db)) as con:
        return {'database_exists':True,'runners':con.execute('SELECT COUNT(*) FROM runners').fetchone()[0],
                'by_side':dict(con.execute('SELECT side,COUNT(*) FROM runners GROUP BY side')),
                'path_states':dict(con.execute('SELECT path_state,COUNT(*) FROM runners GROUP BY path_state')),
                'over_2pct':con.execute('SELECT COUNT(*) FROM runners WHERE peak_run_pct>=2').fetchone()[0],
                'max_run_pct':con.execute('SELECT MAX(peak_run_pct) FROM runners').fetchone()[0]}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',type=Path,default=Path.cwd())
    parser.add_argument('--db',type=Path)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--check',action='store_true')
    mode.add_argument('--status',action='store_true')
    mode.add_argument('--once',action='store_true')
    mode.add_argument('--loop',action='store_true')
    parser.add_argument('--poll-seconds',type=int,default=60)
    parser.add_argument('--min-free-mb',type=int,default=512)
    a=parser.parse_args(argv)
    root=a.project.resolve()
    db=(a.db or root/RUNNERS).resolve()
    if db in {(root/SCANNER).resolve(),(root/PATHS).resolve(),(root/CONTEXT).resolve()} or db.suffix!='.sqlite3':
        parser.error('Runner archive must be independent SQLite')
    if a.status:
        print(json.dumps(status(db),ensure_ascii=False,indent=2));return 0
    if not a.once and not a.loop:
        print('RUNNER ARCHIVE CHECK: read-only; source scanner=',(root/SCANNER).is_file(),
              'paths=',(root/PATHS).is_file(),'destination exists=',db.exists());return 0
    lockfile=db.with_suffix('.sqlite3.lock')
    lockfile.parent.mkdir(parents=True,exist_ok=True)
    with lockfile.open('a+') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        halted=False
        def stop(_sig,_frame):
            nonlocal halted
            halted=True
        signal.signal(signal.SIGINT,stop)
        signal.signal(signal.SIGTERM,stop)
        while not halted:
            try:
                print(json.dumps(run_once(root,db,int(time.time()*1000),a.min_free_mb),ensure_ascii=False),flush=True)
            except Exception as exc:
                print('RUNNER ARCHIVE ERROR:',type(exc).__name__,str(exc),flush=True)
                if not a.loop:return 1
            if not a.loop:break
            for _ in range(max(1,a.poll_seconds)):
                if halted:break
                time.sleep(1)
    return 0

if __name__=='__main__':
    raise SystemExit(main())
