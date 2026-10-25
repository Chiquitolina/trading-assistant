#!/usr/bin/env python3
"""Horizontal First Touch Research V1: read-only, outcome-blind PRE labels and separate analysis.

Reuses reaction_persistent_horizontal_levels.py exactly (no edits to the detector).

--plan       inspect counts/source availability without opening Redis or writing files
--run        freeze PRE labels for all first touches, or newest N per status
--analyze    separately join frozen PRE labels to First Touch Paths V2 outcomes

All inputs are read-only. Redis is optional and only ever accessed with LRANGE.
Missing history is UNKNOWN_HISTORY, *never* negative evidence of no levels.
Results are exploratory, not a backtest or investment recommendation.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time

import pandas as pd

import reaction_persistent_horizontal_levels as persistent

VERSION = 'horizontal-first-touch-research-v1.0'
REPORT_REL = Path('reports/reaction_lab/horizontal_first_touch_research_v1')
SOURCE_REL = Path('reports/reaction_lab/scanner_first_touches_v1.sqlite3')
PATHS_REL = Path('reports/reaction_lab/first_touch_paths_v2.sqlite3')
ARCHIVE_REL = Path('reports/reaction_lab/reaction_full_context_v1.sqlite3')
HISTORY_REL = Path('reports/reaction_lab/reaction_compression_history_v1.sqlite3')
TIMES = {'15m': 900_000, '30m': 1_800_000}
COLUMNS = ('timestamp','open','high','low','close','volume','quoteVolume')
LABEL_COLUMNS = ('touch_key','legacy_event_key','symbol','side','status','observation_type',
                 'retest_ts','known_ts','date_utc','coverage_15m','coverage_30m',
                 'coverage_all','group_pre','near_pre','qualified_count',
                 'chosen_zone_id','structural_level_id','chosen_timeframe',
                 'chosen_kind','chosen_zone_low','chosen_zone_high',
                 'chosen_age_bars','chosen_distance_atr','chosen_visits',
                 'chosen_responses','chosen_status_pre','chosen_role_pre',
                 'history_source_15m','history_source_30m','reason')
LEVEL_COLUMNS = ('touch_key','structural_level_id','symbol','side','status','retest_ts',
                 'timeframe','origin_kind','zone_low','zone_high','center',
                 'formation_closed_ts','distance_pre_atr','confirmed_responses',
                 'age_bars','relation_pre','level_status_pre')
ANALYSIS_COLS = ('status','side','group_pre','perspective','horizon',
                 'N_labeled','N_metric','N_outcome','coverage_metric_pct',
                 'N_tp_first','N_sl_first','N_ambiguous_sl','N_time_exit',
                 'tp_first_pct','median_mfe_pct','median_mae_pct','median_return_pct')


def readonly(path: Path):
    """Never create or alter a database, including journaling modes."""
    path=path.resolve()
    if not path.is_file():raise FileNotFoundError(str(path))
    con=sqlite3.connect(f'file:{path}?mode=ro',uri=True,timeout=20)
    con.execute('PRAGMA query_only=ON')
    return con


def load_touches(project,limit=0):
    with closing(readonly(project / SOURCE_REL)) as con:
        rows=con.execute('''SELECT touch_key,legacy_event_key,symbol,side,status,
            observation_type,retest_ts,known_ts FROM first_touches
            WHERE status IN ('TOUCH_FAILED','TOUCH_INDECISIVE','REACTION')
            ORDER BY retest_ts DESC,touch_key ASC''').fetchall()
    keys=('touch_key','legacy_event_key','symbol','side','status','observation_type','retest_ts','known_ts')
    events=[dict(zip(keys,r)) for r in rows]
    for e in events:
        if e['known_ts']!=e['retest_ts']+60_000:
            raise RuntimeError('Non-causal first-touch known_ts: '+e['touch_key'])
        if e['side'] not in ('LONG','SHORT'):
            raise RuntimeError('Unrecognized first-touch direction: '+e['touch_key'])
    if limit:
        selected=[];counts=Counter()
        for e in events:
            if counts[e['status']]<limit:
                selected.append(e);counts[e['status']]+=1
        events=selected
    return sorted(events,key=lambda e:(e['symbol'],e['retest_ts'],e['touch_key']))


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1<<20),b''):h.update(b)
    return h.hexdigest()


def iso(now_ms):return datetime.fromtimestamp(now_ms/1000,timezone.utc).isoformat()


def mk_id(e):
    """Event-independent identity for the exact frozen-zone construction.

    Comparable when formation and center match. This ID does not assert that
    near-but-distinct levels have been clustered as the same market structure.
    """
    token='|'.join((str(e['symbol']),e['timeframe'],e['origin_kind'],
                    str(int(e['formation_closed_ts'])),f"{float(e['center']):.14g}"))
    return hashlib.sha256(token.encode()).hexdigest()[:20]


def read_archive(con,symbol,tf):
    if con is None:return []
    try:
        return list(con.execute('''SELECT open_ts,open,high,low,close,volume,quote_volume
            FROM candles WHERE symbol=? AND timeframe=? ORDER BY open_ts''',(symbol,tf)))
    except sqlite3.OperationalError as err:
        raise RuntimeError(f'Archive schema error: {err}') from err


def redis_candles(client,symbol,tf,now_ms):
    if client is None:return []
    raws=client.lrange(f'market-data:v1:history:{symbol}:{tf}',0,-1)
    rows=[]
    for raw in raws:
        try:
            d=json.loads(raw)
            timestamp=int(d['timestamp'])
            close_timestamp=int(d.get('close_timestamp',timestamp+TIMES[tf]-1))
            if close_timestamp>now_ms:continue
            rows.append((timestamp,*[float(d[k]) for k in ('open','high','low','close','volume')],
                         float(d.get('quoteVolume',d.get('quote_volume',0.)))))
        except (KeyError,TypeError,ValueError,OverflowError):
            # Invalid source rows cannot be interpreted as no-level.
            raise RuntimeError(f'Bad Redis candle JSON for {symbol} {tf}')
    return rows


def merge_rows(parts,symbol,tf):
    """Keep exact data provenance and fail closed on archival/Redis conflicts."""
    combined={};sources=set()
    for name,rows in parts:
        if rows:sources.add(name)
        for row in rows:
            ts=int(row[0]);v=tuple(float(x) for x in row[1:])
            if ts%TIMES[tf] or len(v)!=6 or any(not math.isfinite(x) for x in v):
                raise RuntimeError(f'Invalid {tf} OHLCV {symbol} {ts}')
            prior=combined.get(ts)
            if prior is not None:
                # quoteVolume can be absent in Redis, and is not used by the
                # level detector. Require strict agreement on the five relevant
                # OHLCV fields only; preserve a known quoteVolume if available.
                if any(not math.isclose(a,b,rel_tol=1e-8,abs_tol=1e-10) for a,b in zip(prior[:5],v[:5])):
                    raise RuntimeError(f'OHLCV conflict {symbol} {tf} {ts}')
                if prior[5]==0 and v[5]!=0:combined[ts]=v
            else:combined[ts]=v
    frame=pd.DataFrame([(ts,*v) for ts,v in sorted(combined.items())],columns=COLUMNS)
    return frame, '+'.join(sorted(sources)) if sources else 'MISSING'


def load_history(symbol,ctx,hist,redis_client,now_ms):
    frames={};sources={}
    for tf in TIMES:
        parts=[('ARCHIVE',read_archive(ctx,symbol,tf)),
               ('HISTORY_CACHE',read_archive(hist,symbol,tf))]
        if redis_client is not None:parts.append(('REDIS',redis_candles(redis_client,symbol,tf,now_ms)))
        frames[tf],sources[tf]=merge_rows(parts,symbol,tf)
    return frames,sources


def write_csv(path,rows,cols):
    temp=path.with_suffix(path.suffix+'.tmp')
    with temp.open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=cols,extrasaction='ignore')
        writer.writeheader();writer.writerows(rows);f.flush();os.fsync(f.fileno())
    os.replace(temp,path)


def write_json(path,value):
    p=path.with_suffix(path.suffix+'.tmp')
    p.write_text(json.dumps(value,indent=2,ensure_ascii=False,allow_nan=False),encoding='utf-8')
    os.replace(p,path)


def check_input_files(project,use_redis):
    paths=[p for p in (project / ARCHIVE_REL,project / HISTORY_REL) if p.is_file()]
    if not paths and not use_redis:
        raise RuntimeError('No 15m/30m archival source. Add --redis (read-only local history), or provide a history DB.')
    return paths


def plan(project,limit,use_redis):
    touches=load_touches(project,limit)
    return {'version':VERSION,'mode':'PLAN_READ_ONLY','first_touches':len(touches),
       'statuses':dict(Counter(e['status'] for e in touches)),
       'observation_type':dict(Counter(e['observation_type'] for e in touches)),
       'symbols':len(set(e['symbol'] for e in touches)),
       'event_range_utc':[iso(min(e['retest_ts'] for e in touches)),iso(max(e['retest_ts'] for e in touches))] if touches else [],
       'archive_exists':(project/ARCHIVE_REL).is_file(),
       'history_cache_exists':(project/HISTORY_REL).is_file(),
       'redis_requested':use_redis,'events_per_status_limit':limit,
       'outcomes_accessed':False,'any_writes':False}


def run(project,limit,use_redis,output_dir=None,redis_client=None):
    events=load_touches(project,limit)
    if not events:raise RuntimeError('No first touches in source SQLite')
    check_input_files(project,use_redis)
    if use_redis and redis_client is None:raise ValueError('Redis client required')
    now=int(time.time()*1000)
    name=datetime.fromtimestamp(now/1000,timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out=output_dir or (project/REPORT_REL/'runs'/name)
    # Do not overwrite/falsely fold new events into previous frozen cohorts.
    out.mkdir(parents=True,exist_ok=False)
    labels=[];levels=[];errors=[]
    ctx=readonly(project/ARCHIVE_REL) if (project/ARCHIVE_REL).is_file() else None
    hist=readonly(project/HISTORY_REL) if (project/HISTORY_REL).is_file() else None
    try:
        by_symbol=defaultdict(list)
        for e in events:by_symbol[e['symbol']].append(e)
        for nth,(symbol,evs) in enumerate(sorted(by_symbol.items()),1):
            # An error for this symbol is UNKNOWN, not a negative/no-level label.
            try:
                frames,sources=load_history(symbol,ctx,hist,redis_client,now)
            except RuntimeError as err:
                frames={tf:pd.DataFrame(columns=COLUMNS) for tf in TIMES}
                sources={tf:'ERROR_SOURCE' for tf in TIMES}
                errors.append({'symbol':symbol,'error':str(err)})
            for e in evs:
                event={**e,'event_key':e['touch_key'],'date_utc':iso(e['retest_ts'])[:10]}
                all_z={};cov={}
                for tf in TIMES:
                    z,c=persistent.candidate_zones(event,tf,frames[tf])
                    all_z[tf]=z;cov[tf]=c
                label=persistent.classify(event,all_z,cov)
                if sources['15m']=='ERROR_SOURCE' or sources['30m']=='ERROR_SOURCE':
                    label.update(group_pre='UNKNOWN_HISTORY',near_pre='',coverage_all=0,
                                 reason='HISTORY_SOURCE_ERROR')
                level_id=''
                if label['chosen_zone_id']:
                    chosen=next(z for group in all_z.values() for z in group if z['zone_id']==label['chosen_zone_id'])
                    level_id=mk_id(chosen)
                row={**e,**label,'structural_level_id':level_id,
                     'history_source_15m':sources['15m'],
                     'history_source_30m':sources['30m']}
                labels.append(row)
                for tf in TIMES:
                    for z in all_z[tf]:
                        levels.append({**z,'touch_key':e['touch_key'],'structural_level_id':mk_id(z),
                                       'status':e['status']})
            if nth%10==0 or nth==len(by_symbol):
                print('HFT_LEVEL_PROGRESS '+json.dumps({'symbols_done':nth,'total_symbols':len(by_symbol),
                    'events_done':len(labels),'near':sum(x['group_pre']==persistent.GROUP_NEAR for x in labels),
                    'unknown':sum(x['group_pre']==persistent.GROUP_UNKNOWN for x in labels)}),flush=True)
    finally:
        if ctx:ctx.close()
        if hist:hist.close()
    write_csv(out/'event_labels.csv',labels,LABEL_COLUMNS)
    write_csv(out/'candidate_levels.csv',levels,LEVEL_COLUMNS)
    write_json(out/'source_errors.json',errors)
    summary={'version':VERSION,'freeze_utc':iso(now),'events':len(labels),
             'symbols':len(set(e['symbol'] for e in labels)),
             'groups':dict(Counter(x['group_pre'] for x in labels)),
             'status_groups':{s:dict(Counter(e['group_pre'] for e in labels if e['status']==s)) for s in sorted(set(e['status'] for e in labels))},
             'history_coverage_pairs':dict(Counter(f"{e['coverage_15m']}|{e['coverage_30m']}" for e in labels)),
             'source_error_symbols':len(errors),'level_records':len(levels),
             'events_per_status_limit':limit,'no_outcomes_used_for_labels':True,
             'redis_read_only':bool(use_redis),
             'detector_versions':{'persistent':persistent.VERSION,'audit_rule':persistent.AUDIT_RULE},
             'inputs':{'first_touches':str(project/SOURCE_REL),'archives':[str(p) for p in (project/ARCHIVE_REL,project/HISTORY_REL) if p.is_file()]},
             'sha256':{f:sha(out/f) for f in ('event_labels.csv','candidate_levels.csv')},
             'read_only_to_project':True,'run_dir':str(out)}
    write_json(out/'audit.json',summary)
    return summary


def analyze(project,run_dir,analysis_dir=None):
    audit=json.loads((run_dir/'audit.json').read_text(encoding='utf-8'))
    if audit['version']!=VERSION:raise RuntimeError('Analysis version mismatch')
    if sha(run_dir/'event_labels.csv')!=audit['sha256']['event_labels.csv']:
        raise RuntimeError('Frozen labels modified: refusing outcome join')
    if sha(run_dir/'candidate_levels.csv')!=audit['sha256']['candidate_levels.csv']:
        raise RuntimeError('Frozen levels modified: refusing outcome join')
    with (run_dir/'event_labels.csv').open(newline='',encoding='utf-8') as f:
        labels=list(csv.DictReader(f))
    by_key={e['touch_key']:e for e in labels}
    if len(by_key)!=len(labels):raise RuntimeError('Duplicate touch key in labels')
    groups=defaultdict(list)
    for e in labels:
        groups[(e['status'],e['side'],e['group_pre'])].append(e['touch_key'])
    paths=project/PATHS_REL
    db=readonly(paths)
    metrics={};outcomes={}
    try:
        for key,h,p,mfe,mae,ret in db.execute('''SELECT touch_key,horizon,perspective,mfe_pct,mae_pct,return_pct
          FROM metrics WHERE horizon IN (15,30,60,120,180,360) AND perspective IN ('ORIGINAL_OPEN','INVERSE')'''):
            if key in by_key:metrics[(key,int(h),p)]=(mfe,mae,ret)
        for key,h,p,tp,sl,outcome in db.execute('''SELECT touch_key,horizon,perspective,tp,sl,outcome
          FROM outcomes WHERE horizon IN (15,30,60,120,180,360) AND perspective IN ('ORIGINAL_OPEN','INVERSE')'''):
            if key in by_key and math.isclose(float(tp),1.0) and math.isclose(float(sl),1.0):
                outcomes[(key,int(h),p)]=outcome
    finally:db.close()
    horizons=(15,30,60,120,180,360)
    output=[]
    for (status,side,grp),keys in sorted(groups.items()):
        for perspective in ('ORIGINAL_OPEN','INVERSE'):
            for h in horizons:
                m=[metrics[(k,h,perspective)] for k in keys if (k,h,perspective) in metrics]
                o=[outcomes[(k,h,perspective)] for k in keys if (k,h,perspective) in outcomes]
                n=len(keys);known=len(m);no=len(o)
                count=Counter(o)
                output.append(dict(status=status,side=side,group_pre=grp,perspective=perspective,horizon=h,
                    N_labeled=n,N_metric=known,N_outcome=no,
                    coverage_metric_pct=round(100*known/n,3) if n else None,
                    N_tp_first=count['TP_FIRST'],N_sl_first=count['SL_FIRST'],
                    N_ambiguous_sl=count['SL_AMBIGUOUS'],N_time_exit=count['TIME_EXIT'],
                    tp_first_pct=round(100*count['TP_FIRST']/no,3) if no else None,
                    median_mfe_pct=round(float(pd.Series([x[0] for x in m]).median()),4) if m else None,
                    median_mae_pct=round(float(pd.Series([x[1] for x in m]).median()),4) if m else None,
                    median_return_pct=round(float(pd.Series([x[2] for x in m]).median()),4) if m else None))
    at=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    out=analysis_dir or run_dir/f'analysis_{at}'
    out.mkdir(parents=True,exist_ok=False)
    write_csv(out/'comparison_by_status_side.csv',output,ANALYSIS_COLS)
    summary={'version':VERSION,'source_labels':str(run_dir),'label_sha256':audit['sha256']['event_labels.csv'],
       'groups':len(groups),'comparisons':len(output),'metric_records_in_join':len(metrics),
       'outcome_records_in_join':len(outcomes),'analysis_utc':iso(int(time.time()*1000)),
       'denominator_note':'TP-first is divided by 1pct/1pct evaluated outcomes. Unknown/missing coverage is NOT a negative. This is gross without fees.',
       'interpretation':'Exploratory, no selection optimization, no validated trading edge.',
       'file_sha256':sha(out/'comparison_by_status_side.csv'),'dir':str(out)}
    write_json(out/'analysis.json',summary)
    return summary


def connect_redis(project):
    try:
        from dotenv import load_dotenv
        load_dotenv(project/'.env')
    except ImportError:pass
    import redis
    r=redis.Redis(host=os.getenv('REDIS_HOST','127.0.0.1'),port=int(os.getenv('REDIS_PORT','6379')),
                  db=int(os.getenv('REDIS_DB','0')),socket_timeout=15)
    r.ping();return r


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    modes=ap.add_mutually_exclusive_group(required=True)
    modes.add_argument('--plan',action='store_true')
    modes.add_argument('--run',action='store_true')
    modes.add_argument('--analyze',action='store_true')
    ap.add_argument('--root',type=Path,default=Path(__file__).resolve().parent)
    ap.add_argument('--max-per-status',type=int,default=0,help='0 = entire frozen universe; nonzero = newest N of EACH status (biased pilot)')
    ap.add_argument('--redis',action='store_true',help='READ-ONLY Redis histories for recent symbols')
    ap.add_argument('--run-dir',type=Path,help='Existing frozen run for --analyze')
    a=ap.parse_args(argv)
    if a.max_per_status<0:ap.error('--max-per-status must be >=0')
    root=a.root.resolve()
    if a.analyze:
        if a.run_dir is None:ap.error('--analyze needs --run-dir')
        result=analyze(root,a.run_dir.resolve())
    elif a.plan:result=plan(root,a.max_per_status,a.redis)
    else:result=run(root,a.max_per_status,a.redis,redis_client=connect_redis(root) if a.redis else None)
    print('HFT_RESEARCH_RESULT '+json.dumps(result,ensure_ascii=False,allow_nan=False),flush=True)
    return 0

if __name__=='__main__':sys.exit(main())
