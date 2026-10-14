#!/usr/bin/env python3
"""Continuous Base REACTION enrichment; isolated SQLite, research-only.

Immutable causal sidecar reads archived scanner/context evidence. Mutable
separate outcome rows follow the contiguous post-retest path. No orders,
no live replay, no rewriting scanner/HTF/BTC/path/runner databases.
"""
from __future__ import annotations
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import traceback
from reaction_lab_deep_metrics import causal_features, forward_metrics

SCHEMA='''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS event_features(
 event_key TEXT PRIMARY KEY, known_ts INTEGER NOT NULL, captured_at_ms INTEGER NOT NULL,
 status TEXT NOT NULL, features_json TEXT NOT NULL, evidence_sha256 TEXT NOT NULL,
 input_status_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_deep_known ON event_features(known_ts);
CREATE TABLE IF NOT EXISTS event_outcomes(
 event_key TEXT PRIMARY KEY, updated_at_ms INTEGER NOT NULL,
 source_updated_ms INTEGER NOT NULL, path_state TEXT NOT NULL,
 metrics_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
 id INTEGER PRIMARY KEY AUTOINCREMENT, at_ms INTEGER NOT NULL,
 inserted INTEGER NOT NULL, outcomes_updated INTEGER NOT NULL, skipped INTEGER NOT NULL,
 errors_json TEXT NOT NULL);
'''


def stamp():return int(time.time()*1000)


def db_ro(path):
    return sqlite3.connect(f'file:{Path(path).resolve().as_posix()}?mode=ro',uri=True,timeout=20)


def output_db(project):return Path(project)/'reports/reaction_lab/deep_research_v1.sqlite3'


def open_writer(path, activation=None):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(path,timeout=30)
    con.execute('PRAGMA journal_mode=WAL');con.execute('PRAGMA busy_timeout=30000')
    con.executescript(SCHEMA)
    with con:
        con.execute('INSERT OR IGNORE INTO meta VALUES (?,?)',('version','1'))
        if con.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0]!='1':
            raise ValueError('Deep research schema mismatch')
        con.execute('INSERT OR IGNORE INTO meta VALUES (?,?)',('feature_start_ms',str(stamp() if activation is None else activation)))
    return con


def frozen_core(con, keys):
    records={}
    for start in range(0,len(keys),350):
        part=keys[start:start+350]
        if not part:continue
        placeholders=','.join('?'*len(part))
        for k,known,status,context,candles in con.execute(
            f'SELECT event_key,known_ts,status,context_json,candles_json FROM contexts WHERE event_key IN ({placeholders})',part):
            records[k]=(int(known),status,json.loads(context),json.loads(candles))
    return records


def telemetry_asof(tcon,symbol,known,limit_age_ms=10*60_000):
    """Only snapshots actually RECEIVED before known_ts, not historical guesses."""
    if tcon is None:return None
    tables={r[0] for r in tcon.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'snapshots' not in tables and not {'liquidation_heartbeats','liquidations'}<=tables:
        return None
    row=(tcon.execute('''SELECT observed_ms,received_ms,features_json FROM snapshots
       WHERE symbol=? AND observed_ms<=? AND received_ms<=? ORDER BY observed_ms DESC LIMIT 1''',
       (symbol,int(known),int(known))).fetchone() if 'snapshots' in tables else None)
    if not row or known-row[0]>limit_age_ms:
        metrics={}
    else:
        metrics={**json.loads(row[2]),'telemetry_age_sec':round((known-row[0])/1000,1),
                 'telemetry_observed_ms':row[0]}
    current_oi=metrics.get('oi_contracts')
    if isinstance(current_oi,(int,float)) and current_oi>0:
        for minutes in (5,15):
            target=int(row[0])-minutes*60_000
            old=tcon.execute("""SELECT observed_ms,features_json FROM snapshots
                WHERE symbol=? AND observed_ms<=? AND received_ms<=?
                ORDER BY observed_ms DESC LIMIT 1""",
                (symbol,target,int(known))).fetchone()
            if old and target-old[0]<=2*60_000:
                previous=json.loads(old[1]).get('oi_contracts')
                if isinstance(previous,(int,float)) and previous>0:
                    metrics[f'oi_change_{minutes}m_pct']=100*(current_oi/previous-1)
    # Optional forceOrder SNAPSHOTS, only if the WS connection was witnessed
    # continuously over the entire previous 15-minute window. No stream means
    # UNKNOWN rather than zero liquidations.
    tables={r[0] for r in tcon.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if {'liquidation_heartbeats','liquidations'}<=tables:
        from_time=int(known)-15*60_000
        marks=[r[0] for r in tcon.execute('''SELECT received_ms FROM liquidation_heartbeats
           WHERE received_ms BETWEEN ? AND ? ORDER BY received_ms''',(from_time,int(known)))]
        if marks and marks[0]-from_time<=90_000 and known-marks[-1]<=90_000 and all(
            b-a<=90_000 for a,b in zip(marks,marks[1:])):
            value=tcon.execute('''SELECT COALESCE(SUM(notional_usdt),0) FROM liquidations
                WHERE symbol=? AND received_ms>? AND received_ms<=? AND event_ms<=?''',
                (symbol,from_time,int(known),int(known))).fetchone()[0]
            metrics['liquidations_15m_usdt']=float(value)
            metrics['liquidation_data_kind']='binance_forceorder_snapshots_not_total'
    return metrics or None


def breadth_momentum_asof(core_db,known):
    """Completed 1h/4h historical breadth acceleration, no future candles."""
    import math
    tables={x[0] for x in core_db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'breadth' not in tables:return {}
    values={}
    for tf,period in (('1h',3_600_000),('4h',14_400_000)):
        boundary=(int(known)//period-1)*period
        rows=list(core_db.execute(
           'SELECT boundary_ts,snapshot_json FROM breadth WHERE timeframe=? AND boundary_ts<=? ORDER BY boundary_ts DESC LIMIT 3',
           (tf,boundary)))
        if len(rows)!=3 or any(int(rows[i][0])-int(rows[i+1][0])!=period for i in range(2)):
            continue
        try:series=[float(json.loads(raw).get('market_breadth_pct')) for _,raw in rows]
        except (ValueError,TypeError):continue
        if any(not math.isfinite(x) for x in series):continue
        values[f'breadth_acceleration_{tf}']=series[0]-2*series[1]+series[2]
        values[f'breadth_slope_2bars_{tf}']=(series[0]-series[2])/2
    return values


def snapshot_density(scanner_rows,record,core,known,sector_by_event=None):
    symbol=record[1];side=record[2]
    sector=core.get('primary_sector') or core.get('sector')
    cutoff_15=known-15*60_000;cutoff_60=known-60*60_000
    eligible=[r for r in scanner_rows if r[0]!=record[0] and r[4]<=known and r[5]<=known]
    density={'reaction_density_market_15m':sum(cutoff_15<=r[4] for r in eligible),
             'reaction_density_market_60m':sum(cutoff_60<=r[4] for r in eligible),
             'reaction_density_same_symbol_60m':sum(r[1]==symbol and cutoff_60<=r[4] for r in eligible),
             'reaction_density_same_side_15m':sum(r[2]==side and cutoff_15<=r[4] for r in eligible)}
    # Only sectors CAPTURED before this signal count, otherwise unavailable.
    density['sector_reaction_density_15m']=(sum(
        cutoff_15<=r[4] and r[0]!=record[0] and sector_by_event.get(r[0])==sector
        for r in eligible) if sector and sector_by_event else None)
    return density


def run_once(project, *, limit=100, activation=None, db=None):
    project=Path(project)
    scanner=project/'reports/reaction_lab/scanner_events_v1.sqlite3'
    core=project/'reports/reaction_lab/causal_context_v1.sqlite3'
    paths=project/'reports/reaction_lab/auto_paths_v1.sqlite3'
    if not scanner.is_file() or not core.is_file() or not paths.is_file():
        raise FileNotFoundError('Scanner, HTF Context and auto-path SQLite are required')
    out=Path(db) if db else output_db(project)
    count_inserted=count_changed=skipped=0; errors=[]
    with closing(open_writer(out,activation)) as dst, closing(db_ro(scanner)) as scan, closing(db_ro(core)) as ctx,closing(db_ro(paths)) as pc:
        start=int(dst.execute("SELECT value FROM meta WHERE key='feature_start_ms'").fetchone()[0])
        existing={r[0] for r in dst.execute('SELECT event_key FROM event_features')}
        # Chronological order, not the newest event first; limit only caps this cycle.
        all_events=scan.execute('''SELECT event_key,symbol,side,retest_ts,known_ts,first_seen_ms,causal_json
           FROM events WHERE known_ts>=? ORDER BY known_ts ASC''',(start,)).fetchall()
        pending=[r for r in all_events if r[0] not in existing][:limit]
        contexts=frozen_core(ctx,[r[0] for r in pending])
        sector_by_event={}
        if pending:
            earliest=min(r[4] for r in pending)-60*60_000
            latest=max(r[4] for r in pending)
            for event_id, captured, raw_ctx in ctx.execute(
                'SELECT event_key,captured_at_ms,context_json FROM contexts WHERE known_ts BETWEEN ? AND ?',
                (earliest,latest)):
                try:
                    parsed=json.loads(raw_ctx)
                    sector_by_event[event_id]=(parsed.get('primary_sector') or parsed.get('sector'),int(captured))
                except (TypeError,ValueError):pass
        telepath=project/'reports/reaction_lab/telemetry_v1.sqlite3'
        tcon=db_ro(telepath) if telepath.is_file() else None
        try:
            for r in pending:
                key,symbol,side,retest,known,first_seen,raw=r
                if key not in contexts:
                    skipped+=1;continue  # upstream context has not completed
                try:
                    source_known,core_state,context,candles=contexts[key]
                    if source_known!=int(known):
                        raise ValueError('Context known_ts mismatch: '+key)
                    event=json.loads(raw)
                    telemetry=telemetry_asof(tcon,symbol,known)
                    sectors_available={key:sec for key,(sec,when) in sector_by_event.items() if when<=known}
                    density=snapshot_density(all_events,r,context,known,sectors_available)
                    density.update(breadth_momentum_asof(ctx,known))
                    features=causal_features(event,context,candles,known,density,telemetry)
                    features['deep_context_status']=core_state
                    # Baseline metadata/correct auditability.
                    evidence=hashlib.sha256(json.dumps([event,candles,telemetry],sort_keys=True,
                        separators=(',',':'),allow_nan=False).encode()).hexdigest()
                    status='CAUSAL_READY' if features['deep_causal_input_status']=='READY' else 'PARTIAL'
                    missing={'telemetry_available':telemetry is not None,'bars_1m':features['deep_asof_bars_1m'],
                             'upstream_context_status':core_state,'observed_late_ms':stamp()-int(known)}
                    with dst:
                        dst.execute('''INSERT OR IGNORE INTO event_features
                            (event_key,known_ts,captured_at_ms,status,features_json,evidence_sha256,input_status_json)
                            VALUES (?,?,?,?,?,?,?)''',
                            (key,known,stamp(),status,json.dumps(features,allow_nan=False),evidence,json.dumps(missing)))
                    count_inserted+=1
                except Exception as e:
                    errors.append({'event':key,'error':repr(e)})
        finally:
            if tcon:tcon.close()
        from reaction_lab_auto_paths import decode_path
        # Only path rows changed since their outcome snapshots, retaining
        # outcomes for all events regardless of whether they became runners.
        # On-disk schema is owned by auto_paths; event prices are scanner frozen.
        scanmap={r[0]:r for r in all_events}
        snapshot=dict(dst.execute('SELECT event_key,source_updated_ms FROM event_outcomes'))
        eligible=[row for row in pc.execute('''SELECT event_key,side,retest_ts,state,path_blob,updated_utc_ms
             FROM events ORDER BY updated_utc_ms DESC''') if row[0] in scanmap and row[0] in (existing|set(contexts))
             and snapshot.get(row[0],-1)<int(row[5])]
        for key,side,retest,state,blob,updated in eligible[:max(limit*3,150)]:
            try:
                event=json.loads(scanmap[key][6]);baseline=event.get('retest_close')
                metrics=forward_metrics(decode_path(blob),side,baseline,retest)
                with dst:
                    dst.execute('''INSERT INTO event_outcomes VALUES (?,?,?,?,?)
                      ON CONFLICT(event_key) DO UPDATE SET updated_at_ms=excluded.updated_at_ms,
                      source_updated_ms=excluded.source_updated_ms,path_state=excluded.path_state,
                      metrics_json=excluded.metrics_json''',
                      (key,stamp(),updated,state,json.dumps(metrics,allow_nan=False)))
                count_changed+=1
            except Exception as e:errors.append({'outcome':key,'error':repr(e)})
        with dst:
            dst.execute('INSERT INTO runs(at_ms,inserted,outcomes_updated,skipped,errors_json) VALUES (?,?,?,?,?)',
                        (stamp(),count_inserted,count_changed,skipped,json.dumps(errors[:20])))
        count=dst.execute('SELECT COUNT(*) FROM event_features').fetchone()[0]
    return {'feature_start_ms':start,'causal_new':count_inserted,'outcomes_updated':count_changed,
            'context_pending':skipped,'errors':errors[:5],'causal_total':count}


def status(project,db=None):
    path=Path(db) if db else output_db(project)
    if not path.is_file():return {'exists':False}
    with closing(db_ro(path)) as c:
        return {'exists':True,'feature_start_ms':c.execute("SELECT value FROM meta WHERE key='feature_start_ms'").fetchone()[0],
          'causal':dict(c.execute('SELECT status,COUNT(*) FROM event_features GROUP BY status')),
          'path_outcomes':dict(c.execute('SELECT path_state,COUNT(*) FROM event_outcomes GROUP BY path_state')),
          'recent':c.execute('SELECT at_ms,inserted,outcomes_updated,skipped,errors_json FROM runs ORDER BY id DESC LIMIT 1').fetchone()}


def load_deep_frame(project,keys):
    """Immutable Deep fields plus separately frozen microstructure sidecar.

    A later observation never rewrites either SQLite. Read-time merge only;
    per-source timestamps are verified by reaction_lab_telemetry_link.
    """
    import pandas as pd
    path=output_db(project)
    rows=[]
    if path.is_file():
        with closing(db_ro(path)) as c:
            for i in range(0,len(keys),350):
                part=list(keys[i:i+350]);q=','.join('?'*len(part))
                if not q:continue
                for key,raw,status in c.execute(f'SELECT event_key,features_json,status FROM event_features WHERE event_key IN ({q})',part):
                    rows.append({'event_key':key,**json.loads(raw),'deep_status':status})
    frame=pd.DataFrame(rows) if rows else pd.DataFrame(columns=['event_key'])
    try:
        from reaction_lab_telemetry_link import load_telemetry_frame
        linked=load_telemetry_frame(project,keys)
    except ImportError:
        return frame  # older installations work unchanged
    if linked.empty:return frame
    if frame.empty:return linked
    merged=frame.merge(linked,on='event_key',how='outer',suffixes=('','__link'))
    for col in list(merged.columns):
        if col.endswith('__link'):
            original=col[:-6]
            if original in merged:
                # Prefer strictly per-source as-of sidecar values when present.
                merged[original]=merged[col].where(merged[col].notna(),merged[original])
            else:merged[original]=merged[col]
            merged=merged.drop(columns=[col])
    return merged


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--project',default='.')
    p.add_argument('--db')
    p.add_argument('--once',action='store_true')
    p.add_argument('--loop',action='store_true')
    p.add_argument('--status',action='store_true')
    p.add_argument('--check',action='store_true')
    p.add_argument('--limit',type=int,default=100)
    p.add_argument('--poll-seconds',type=int,default=60)
    a=p.parse_args()
    if a.status:print(json.dumps(status(a.project,a.db),ensure_ascii=False));return
    if a.check:
        paths={x:(Path(a.project)/'reports/reaction_lab'/x).is_file() for x in
          ('scanner_events_v1.sqlite3','causal_context_v1.sqlite3','auto_paths_v1.sqlite3')}
        print(json.dumps({'sources':paths,'db_exists':(Path(a.db) if a.db else output_db(a.project)).is_file()}));return
    if not (a.once or a.loop):p.error('Specify --check, --status, --once or --loop')
    if a.limit<1 or a.poll_seconds<30:p.error('Bad limit or polling interval')
    while True:
        try:print(json.dumps(run_once(a.project,limit=a.limit,db=a.db),ensure_ascii=False),flush=True)
        except Exception as e:
            print(json.dumps({'ERROR':repr(e),'traceback':traceback.format_exc()[-1600:]}),flush=True)
            if not a.loop:raise
        if not a.loop:break
        time.sleep(a.poll_seconds)



# Extension note: liquidation not on public snapshot endpoint, so it remains
# NULL until a time-stamped liquidation data provider is explicitly connected.


def load_deep_outcomes(project,keys,horizon):
    """Outcome fields are post-signal labels, never eligible as predictors."""
    import pandas as pd
    path=output_db(project)
    if not path.is_file():return pd.DataFrame(columns=['event_key'])
    rows=[]
    with closing(db_ro(path)) as c:
        for i in range(0,len(keys),350):
            part=list(keys[i:i+350]);q=','.join('?'*len(part))
            if not q:continue
            for key,raw in c.execute(f'SELECT event_key,metrics_json FROM event_outcomes WHERE event_key IN ({q})',part):
                m=json.loads(raw)
                if m.get(f'outcome_{horizon}m_status')!='COMPLETE':continue
                selection={k:v for k,v in m.items() if k in ('entry_next_open','entry_next_open_ts','valid_path_minutes') or
                           k.endswith(f'_{horizon}m')}
                rows.append({'event_key':key,**selection})
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=['event_key'])

if __name__=='__main__':main()
