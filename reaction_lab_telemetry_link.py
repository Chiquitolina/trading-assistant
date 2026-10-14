#!/usr/bin/env python3
"""Prospective immutable per-REACTION microstructure sidecar.

Reads existing scanner and telemetry SQLite in read-only mode. Stores one frozen
as-of record per Event ID in its own SQLite. No trades, no edits to upstream
SQLite, no future observations, and no fabricated zero liquidations.
"""
from __future__ import annotations
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time

VERSION=1
SCHEMA='''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS event_telemetry(
 event_key TEXT PRIMARY KEY,known_ts INTEGER NOT NULL,
 linked_at_ms INTEGER NOT NULL,status TEXT NOT NULL,
 features_json TEXT NOT NULL,quality_json TEXT NOT NULL,
 evidence_json TEXT NOT NULL,evidence_sha256 TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_link_known ON event_telemetry(known_ts);
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY AUTOINCREMENT,
 at_ms INTEGER NOT NULL,inserted INTEGER NOT NULL,errors_json TEXT NOT NULL);
'''
SOURCES={
 'oi': (('oi_contracts',),15*60_000),
 'funding': (('funding_rate',),60*60_000),
 'depth': (('orderbook_spread_bps','orderbook_imbalance_5','orderbook_bid_depth_usdt',
            'orderbook_ask_depth_usdt','orderbook_top5_source'),90_000),
 'taker': (('taker_buy_volume_5m','taker_sell_volume_5m','taker_delta_5m',
            'taker_buy_ratio_5m','taker_buy_volume_15m','taker_sell_volume_15m',
            'taker_delta_15m','taker_buy_ratio_15m','cvd_15m','taker_source'),180_000),
}

def clock():return int(time.time()*1000)

def ro(p):return sqlite3.connect(f'file:{Path(p).resolve().as_posix()}?mode=ro',uri=True,timeout=20)

def output(project):return Path(project)/'reports/reaction_lab/telemetry_link_v1.sqlite3'

def open_writer(p,activation=None):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(p,timeout=20)
    c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA busy_timeout=20000')
    c.executescript(SCHEMA)
    with c:
        c.execute('INSERT OR IGNORE INTO meta VALUES (?,?)',('version',str(VERSION)))
        if c.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0]!=str(VERSION):
            raise ValueError('Schema mismatch')
        c.execute('INSERT OR IGNORE INTO meta VALUES (?,?)',('feature_start_ms',str(clock() if activation is None else activation)))
    return c

def table_exists(con, name):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone() is not None

def market_asof(con,symbol,known):
    """Each metric source is independently time-qualified (no row-level shortcut)."""
    features={};quality={};evidence={'snapshots':{}}
    if con is None or not table_exists(con,'snapshots'):
        return features, {src:{'available':False,'reason':'no_telemetry'} for src in SOURCES},evidence
    rows=con.execute('''SELECT observed_ms,received_ms,features_json,quality_json
       FROM snapshots WHERE symbol=? AND received_ms<=? AND observed_ms<=?
       ORDER BY received_ms DESC LIMIT 600''',(symbol,known,known)).fetchall()
    decoded=[]
    for observed,received,features_raw,quality_raw in rows:
        try:decoded.append((int(observed),int(received),json.loads(features_raw),json.loads(quality_raw)))
        except (ValueError,TypeError):continue
    for source,(keys,max_age) in SOURCES.items():
        match=None
        for obs,rec,f,q in decoded:
            source_quality=q.get(source,{})
            source_ms=source_quality.get('observed_ms')
            if source_quality.get('available') is not True or source_ms is None:continue
            try:source_ms=int(source_ms)
            except (TypeError,ValueError):continue
            if not (0<=source_ms<=rec<=known and known-source_ms<=max_age):continue
            # A partially available source is not a valid snapshot.
            required={'oi':('oi_contracts',),'funding':('funding_rate',),
                      'depth':('orderbook_spread_bps','orderbook_imbalance_5'),
                      'taker':('taker_buy_ratio_5m','taker_buy_ratio_15m')}[source]
            if not all(f.get(k) is not None for k in required):continue
            match=(obs,rec,f,source_ms)
            break
        if match is None:
            quality[source]={'available':False,'reason':'no_fresh_pre_signal_observation',
                             'max_age_ms':max_age}
        else:
            obs,rec,f,source_ms=match
            for k in keys:
                if f.get(k) is not None:features[k]=f[k]
            quality[source]={'available':True,'source_observed_ms':source_ms,
                             'received_ms':rec,'age_ms':known-source_ms}
            evidence['snapshots'][source]={'snapshot_observed_ms':obs,'snapshot_received_ms':rec,
                'source_observed_ms':source_ms,'source_features':{k:f.get(k) for k in keys}}
    if quality['oi']['available']:
        current=features['oi_contracts']
        base_ms=quality['oi']['source_observed_ms']
        for minutes in (5,15):
            target=base_ms-minutes*60_000
            # OI comparison needs a genuinely old observation, not last snapshot
            # with a newer OI carried through an unrelated endpoint.
            for obs,rec,f,q in decoded:
                raw=q.get('oi',{});t=raw.get('observed_ms')
                if not raw.get('available') or t is None:continue
                try:t=int(t)
                except (ValueError,TypeError):continue
                if t<=target and 0<=target-t<=2*60_000 and rec<=known:
                    prev=f.get('oi_contracts')
                    if isinstance(prev,(int,float)) and prev>0 and current>0:
                        features[f'oi_change_{minutes}m_pct']=100*(current/prev-1)
                        evidence['snapshots'][f'oi_before_{minutes}m']={
                            'source_observed_ms':t,'received_ms':rec,'oi_contracts':prev}
                    break
    return features,quality,evidence

def liquidation_asof(con,symbol,known):
    """Observe only snapshot stream if a continuous prior 15m capture is proven."""
    unknown={'available':False,'reason':'no_contiguous_15m_stream'}
    if con is None or not(table_exists(con,'liquidations') and table_exists(con,'liquidation_heartbeats')):
        return {},unknown,{'liquidations':[],'heartbeats':[]}
    first=known-15*60_000
    heartbeat=[r[0] for r in con.execute('''SELECT received_ms FROM liquidation_heartbeats
        WHERE received_ms BETWEEN ? AND ? ORDER BY received_ms''',(first,known))]
    evidence={'liquidations':[],'heartbeats':heartbeat}
    if not heartbeat or heartbeat[0]-first>90_000 or known-heartbeat[-1]>90_000 or any(
            b-a>90_000 for a,b in zip(heartbeat,heartbeat[1:])):
        return {},unknown,evidence
    rows=con.execute('''SELECT event_ms,received_ms,side,notional_usdt,trade_id
        FROM liquidations WHERE symbol=? AND received_ms>? AND received_ms<=?
        AND event_ms<=? ORDER BY received_ms''',(symbol,first,known,known)).fetchall()
    evidence['liquidations']=[list(r) for r in rows]
    amount=sum(float(r[3]) for r in rows)
    feature={'liquidations_15m_usdt':amount,
             'liquidation_data_kind':'binance_forceorder_snapshots_not_total',
             'liquidations_snapshot_count_15m':len(rows)}
    return feature,{'available':True,'window_start_ms':first,'window_end_ms':known,
                    'heartbeats':len(heartbeat),'snapshots':len(rows)},evidence

def asof_context(con,symbol,known):
    features,q,e=market_asof(con,symbol,known)
    liq,lq,le=liquidation_asof(con,symbol,known)
    features.update(liq);q['liquidations']=lq;e['liquidation_evidence']=le
    features['telemetry_link_schema_version']=VERSION
    features['telemetry_link_known_ts']=known
    features['microstructure_status']=('OBSERVED' if all(x['available'] for x in q.values())
          else 'PARTIAL' if any(x['available'] for x in q.values()) else 'NOT_CAPTURED_AT_SIGNAL')
    return features,q,e

def run_once(project,db=None,limit=100,activation=None,settle_seconds=120):
    root=Path(project)
    scanner=root/'reports/reaction_lab/scanner_events_v1.sqlite3'
    if not scanner.exists():raise FileNotFoundError(scanner)
    tele=root/'reports/reaction_lab/telemetry_v1.sqlite3'
    out=Path(db) if db else output(root)
    inserted=0;errors=[]
    with closing(open_writer(out,activation)) as dest,closing(ro(scanner)) as scan:
        start=int(dest.execute("SELECT value FROM meta WHERE key='feature_start_ms'").fetchone()[0])
        existing={r[0] for r in dest.execute('SELECT event_key FROM event_telemetry')}
        pending=[r for r in scan.execute('''SELECT event_key,symbol,known_ts
           FROM events WHERE known_ts>=? AND known_ts<=? ORDER BY known_ts,event_key''',
           (start,clock()-settle_seconds*1000)) if r[0] not in existing][:limit]
        tcon=ro(tele) if tele.exists() else None
        try:
            for key,symbol,known in pending:
                try:
                    features,quality,evidence=asof_context(tcon,symbol,int(known))
                    payload=json.dumps({'features':features,'quality':quality,'evidence':evidence},
                                       sort_keys=True,separators=(',',':'),allow_nan=False)
                    digest=hashlib.sha256(payload.encode()).hexdigest()
                    with dest:
                        dest.execute('''INSERT OR IGNORE INTO event_telemetry VALUES (?,?,?,?,?,?,?,?)''',
                            (key,int(known),clock(),features['microstructure_status'],
                             json.dumps(features,allow_nan=False),json.dumps(quality,allow_nan=False),
                             json.dumps(evidence,allow_nan=False),digest))
                    inserted+=1
                except Exception as exc:errors.append((key,repr(exc)[:180]))
        finally:
            if tcon:tcon.close()
        with dest:dest.execute('INSERT INTO runs(at_ms,inserted,errors_json) VALUES (?,?,?)',
                               (clock(),inserted,json.dumps(errors[:20])))
        total=dest.execute('SELECT COUNT(*) FROM event_telemetry').fetchone()[0]
    return {'feature_start_ms':start,'inserted':inserted,'total':total,'errors':errors[:5]}

def status(project,db=None):
    p=Path(db) if db else output(project)
    if not p.exists():return {'exists':False}
    with closing(ro(p)) as c:
        return {'exists':True,'start_ms':c.execute("SELECT value FROM meta WHERE key='feature_start_ms'").fetchone()[0],
            'total':c.execute('SELECT COUNT(*) FROM event_telemetry').fetchone()[0],
            'states':dict(c.execute('SELECT status,COUNT(*) FROM event_telemetry GROUP BY status')),
            'last_run':c.execute('SELECT at_ms,inserted,errors_json FROM runs ORDER BY id DESC LIMIT 1').fetchone()}

def load_telemetry_frame(project,keys):
    """Pandas helper for dashboard/Runner Analyzer: only causal features."""
    import pandas as pd
    p=output(project)
    if not p.exists():return pd.DataFrame(columns=['event_key'])
    rows=[]
    with closing(ro(p)) as c:
        for i in range(0,len(keys),350):
            part=list(keys[i:i+350]);q=','.join('?'*len(part))
            if not part:continue
            for k,raw,status in c.execute(f'SELECT event_key,features_json,status FROM event_telemetry WHERE event_key IN ({q})',part):
                rows.append({'event_key':k,**json.loads(raw),'telemetry_link_status':status})
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=['event_key'])

def audit(project,db=None,limit=25):
    p=Path(db) if db else output(project)
    if not p.exists():return {'events':0,'passed':0,'different':0,'missing_db':True}
    passed=0;different=0;total=0;sample=[]
    with closing(ro(p)) as c:
        for key,known,features_raw,quality_raw,evidence_raw,digest in c.execute(
             'SELECT event_key,known_ts,features_json,quality_json,evidence_json,evidence_sha256 '
             'FROM event_telemetry ORDER BY known_ts DESC LIMIT ?',(limit,)):
            total+=1
            features=json.loads(features_raw);quality=json.loads(quality_raw);ev=json.loads(evidence_raw)
            canonical=json.dumps({'features':features,'quality':quality,'evidence':ev},sort_keys=True,
                                 separators=(',',':'),allow_nan=False)
            ok=hashlib.sha256(canonical.encode()).hexdigest()==digest
            ok=ok and features.get('telemetry_link_known_ts')==known
            ok=ok and all(not q.get('available') or q.get('source_observed_ms',0)<=known
                          for k,q in quality.items() if k!='liquidations')
            if ok:passed+=1
            else:different+=1
            sample.append({'event_key':key,'result':'PASS' if ok else 'DIFFERENT'})
    return {'events':total,'passed':passed,'different':different,'details':sample}

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--project',default='.')
    p.add_argument('--db')
    p.add_argument('--check',action='store_true')
    p.add_argument('--once',action='store_true')
    p.add_argument('--loop',action='store_true')
    p.add_argument('--status',action='store_true')
    p.add_argument('--audit',action='store_true')
    p.add_argument('--limit',type=int,default=100)
    p.add_argument('--poll-seconds',type=int,default=60)
    p.add_argument('--settle-seconds',type=int,default=120)
    a=p.parse_args()
    if a.check:
        print(json.dumps({'scanner_exists':(Path(a.project)/'reports/reaction_lab/scanner_events_v1.sqlite3').exists(),
           'telemetry_exists':(Path(a.project)/'reports/reaction_lab/telemetry_v1.sqlite3').exists(),
           'network':'OFF','separate_sidecar':str(output(a.project))}));return
    if a.status:print(json.dumps(status(a.project,a.db),ensure_ascii=False));return
    if a.audit:print(json.dumps(audit(a.project,a.db,a.limit),ensure_ascii=False));return
    if not(a.once or a.loop):p.error('Specify --check, --status, --audit, --once or --loop')
    if a.limit<1 or a.poll_seconds<30 or a.settle_seconds<0:p.error('Invalid polling options')
    while True:
        print(json.dumps(run_once(a.project,a.db,a.limit,settle_seconds=a.settle_seconds),ensure_ascii=False),flush=True)
        if not a.loop:break
        time.sleep(a.poll_seconds)
if __name__=='__main__':main()
