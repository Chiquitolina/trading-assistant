#!/usr/bin/env python3
"""Offline-only persistent horizontal levels, PRE-REACTION, restartable by symbol.

Zero network calls, zero LIVE writes, zero outcome access. Uses installed frozen
Full Context and optional 15m/30m history cache via existing Atlas providers.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import reaction_structural_level_atlas as atlas_v1
import reaction_compression_breakout_retest_v4 as v4

VERSION = 'persistent-horizontal-pre-v1.0'
ROOT_REL = Path('reports/reaction_lab/reaction_persistent_horizontal_v1')
TF = {'15m': 15*60000, '30m': 30*60000}
WINDOW = {'15m': 160, '30m': 80}  # 40h PRE in each timeframe; frozen pilot
MIN_SPAN_BARS = 20  # minimum between first and last qualified historical visits
MIN_GAP_BARS = 5
MAX_RESPONSE_BARS = 20
MIN_RESPONSE_ATR = 0.75
NEAR_ATR = 0.50
MIN_INDEPENDENT_RESPONSES = 2
EVENT_COLS = ['event_key','symbol','side','date_utc','retest_ts','observation_type',
              'coverage_15m','coverage_30m','coverage_all','near_pre','group_pre',
              'qualified_count','chosen_zone_id','chosen_timeframe','chosen_kind',
              'chosen_zone_low','chosen_zone_high','chosen_age_bars','chosen_distance_atr',
              'chosen_visits','chosen_responses','chosen_status_pre','chosen_role_pre',
              'chosen_80plus_bars','reason']
ZONE_COLS = ['event_key','symbol','side','retest_ts','timeframe','zone_id','origin_kind',
             'zone_low','zone_high','center','formation_closed_ts','first_pivot_ts',
             'age_bars','pivot_matches','independent_visits','confirmed_responses',
             'response_min_atr','distance_pre_atr','relation_pre','level_status_pre',
             'source_coverage','visit_timestamps','response_timestamps']
GROUP_NEAR='PERSISTENT_LEVEL_NEAR_PRE'
GROUP_FAR='NO_PERSISTENT_LEVEL_NEAR_PRE'
GROUP_UNKNOWN='UNKNOWN_HISTORY'

def safe_float(x):
    try:
        v=float(x)
        return v if math.isfinite(v) else None
    except (TypeError,ValueError):return None

def gap(price,lo,hi):
    return max(lo-price,price-hi,0.)

def classify_side(close,lo,hi):
    return 'ABOVE' if close>hi else 'BELOW' if close<lo else 'INSIDE'

def coverage_slice(frame, tf, event_ts):
    """PRE ends strictly before first 1m candle of REACTION."""
    step=TF[tf];n=WINDOW[tf]
    if frame.empty:return None,'INSUFFICIENT'
    # Frame.timestamp is the OPEN timestamp. Only bars with close <= event open.
    closed=frame.loc[frame.timestamp+step<=int(event_ts)].tail(n).copy().reset_index(drop=True)
    if len(closed)<n:return None,'INSUFFICIENT'
    ts=closed.timestamp.to_numpy(dtype='int64')
    if not np.all(np.diff(ts)==step):return None,'GAPPED'
    v=closed[['open','high','low','close','volume']].to_numpy(dtype=float)
    if not np.isfinite(v).all() or (v[:,1]<v[:,2]).any() or (v[:,0:4]<=0).any():
        return None,'INVALID'
    if np.any(v[:,1] < np.maximum(v[:,0],v[:,3])) or np.any(v[:,2] > np.minimum(v[:,0],v[:,3])):
        return None,'INVALID_OHLC'
    return closed,'COMPLETE'

def response_from_pivot(frame, p, nxt_idx, zone, atr):
    """Causal departure after a historical pivot, terminating before next pivot.

    Use highs for support departures and lows for resistance departures; for
    resistance a DOWN departure is meaningful and for support an UP departure.
    The entire measured interval must already be closed PRE REACTION.
    """
    i=int(p['pivot_i']); upper=min(len(frame),i+1+MAX_RESPONSE_BARS,int(nxt_idx))
    if i+1>=upper:return None
    nextbars=frame.iloc[i+1:upper]
    if zone['origin']=='LOW':
        best=float(nextbars.high.max());moved=max(0,best-zone['high'])
        hit=nextbars.index[nextbars.high>=zone['high']+MIN_RESPONSE_ATR*atr]
    else:
        best=float(nextbars.low.min());moved=max(0,zone['low']-best)
        hit=nextbars.index[nextbars.low<=zone['low']-MIN_RESPONSE_ATR*atr]
    return {'response_atr':moved/atr, 'response_closed_ts':(
        int(frame.timestamp.iloc[int(hit[0])])+int(zone['step']) if len(hit) else None)}

def candidate_zones(event,tf,frame):
    closed,cov=coverage_slice(frame,tf,event['retest_ts'])
    if cov!='COMPLETE':return [],cov
    step=TF[tf]
    pivots=atlas_v1.pivots_confirmed(closed)
    pre_close=float(closed.close.iloc[-1]);pre_atr=atlas_v1.true_range_at(closed,len(closed)-1)
    if pre_atr is None:return [],'INVALID_ATR'
    records=[]
    for kind in ('LOW','HIGH'):
        frozen=atlas_v1.frozen_zones(closed,pivots,kind,step)
        matching_kind=[p for p in pivots if p['kind']==kind]
        for z in frozen:
            if z['formation_closed_ts']>=int(event['retest_ts']):continue
            # Must be able to observe a response AFTER the second pivot's confirmation.
            eligible=[p for p in matching_kind if p['price']>=z['low'] and p['price']<=z['high']
                      and p['confirm_ts']+step<=int(event['retest_ts'])
                      and p['pivot_i']<=len(closed)-4]
            eligible.sort(key=lambda p:p['pivot_i'])
            if len(eligible)<2:continue
            qualifying=[]
            for idx,p in enumerate(eligible):
                if qualifying and p['pivot_i']-qualifying[-1][0]['pivot_i']<MIN_GAP_BARS:
                    continue
                # A departure must happen before the next matched pivot, not borrowed
                # from a later unrelated price impulse.
                next_i=eligible[idx+1]['pivot_i'] if idx+1<len(eligible) else len(closed)
                atr=atlas_v1.true_range_at(closed,p['confirm_i'])
                if atr is None:continue
                proof=response_from_pivot(closed,p,next_i,{**z,'step':step},atr)
                if proof and proof['response_closed_ts'] is not None:
                    qualifying.append((p,proof))
            if len(qualifying)<MIN_INDEPENDENT_RESPONSES:continue
            earliest=qualifying[0][0]['pivot_i']
            latest=qualifying[-1][0]['pivot_i']
            # Two bounces close together do not establish a persistent level.
            # The historical visits themselves must span a meaningful interval.
            if latest-earliest<MIN_SPAN_BARS:continue
            age=len(closed)-1-earliest
            if age<MIN_SPAN_BARS:continue
            # Side-aware PRE status is descriptive only; current REACTION and
            # outcome are excluded from all computations.
            relation=classify_side(pre_close,float(z['low']),float(z['high']))
            d=gap(pre_close,float(z['low']),float(z['high']))/pre_atr
            matched=eligible
            zid=hashlib.sha256((f"{event['event_key']}|{tf}|{kind}|{z['formation_closed_ts']}|"
                         f"{z['center']:.14g}").encode()).hexdigest()[:20]
            records.append(dict(event_key=event['event_key'],symbol=event['symbol'],side=event['side'],
                retest_ts=event['retest_ts'],timeframe=tf,zone_id=zid,origin_kind=kind,
                zone_low=z['low'],zone_high=z['high'],center=z['center'],
                formation_closed_ts=z['formation_closed_ts'],first_pivot_ts=qualifying[0][0]['pivot_ts'],
                age_bars=age,pivot_matches=len(matched),independent_visits=len(qualifying),
                confirmed_responses=len(qualifying),
                response_min_atr=min(x['response_atr'] for _,x in qualifying),
                distance_pre_atr=d,relation_pre=relation,
                level_status_pre=('TEST_IN_ZONE' if relation=='INSIDE' else
                    'SUPPORT_BELOW' if relation=='ABOVE' else 'RESISTANCE_ABOVE'),
                source_coverage='COMPLETE',
                visit_timestamps=';'.join(str(int(p['pivot_ts'])) for p,_ in qualifying),
                response_timestamps=';'.join(str(int(x['response_closed_ts'])) for _,x in qualifying)))
    # Keep every qualified zone. Selection is strictly PRE and deterministic.
    records.sort(key=lambda x:(x['distance_pre_atr'],-x['confirmed_responses'],
                               -x['age_bars'],x['timeframe'],x['zone_id']))
    return records,'COMPLETE'

def classify(event,levels_by_tf,coverages):
    all_z=[z for tf in ('15m','30m') for z in levels_by_tf[tf]]
    covered=all(coverages[tf]=='COMPLETE' for tf in TF)
    if not covered: group=GROUP_UNKNOWN;chosen=None
    elif not all_z:group=GROUP_FAR;chosen=None
    else:
        chosen=min(all_z,key=lambda z:(z['distance_pre_atr'],-z['confirmed_responses'],
                                      -z['age_bars'],z['timeframe'],z['zone_id']))
        group=GROUP_NEAR if chosen['distance_pre_atr']<=NEAR_ATR else GROUP_FAR
    # Only one PRE-chosen level, if it is near. A distant zone can be inspected
    # in zone exports but is not advertised as a contact or opportunity.
    qualifying_near=(chosen if chosen and group==GROUP_NEAR else None)
    role='UNCLASSIFIED'
    if qualifying_near:
        z=qualifying_near; side=event['side']
        if z['relation_pre']=='INSIDE':role='INSIDE_ZONE'
        elif ((z['origin_kind']=='LOW' and side=='LONG' and z['relation_pre']=='ABOVE') or
              (z['origin_kind']=='HIGH' and side=='SHORT' and z['relation_pre']=='BELOW')):
            role='REBOTE_COMPATIBLE_PRE'
        else:role='OTRO_ROL_O_RUPTURA_PRE'
    row={k:event.get(k) for k in ('event_key','symbol','side','date_utc','retest_ts','observation_type')}
    row.update(coverage_15m=coverages['15m'],coverage_30m=coverages['30m'],
      coverage_all=int(covered),near_pre=1 if group==GROUP_NEAR else 0 if covered else '',
      group_pre=group,qualified_count=len(all_z),
      chosen_zone_id=qualifying_near['zone_id'] if qualifying_near else '',
      chosen_timeframe=qualifying_near['timeframe'] if qualifying_near else '',
      chosen_kind=qualifying_near['origin_kind'] if qualifying_near else '',
      chosen_zone_low=qualifying_near['zone_low'] if qualifying_near else '',
      chosen_zone_high=qualifying_near['zone_high'] if qualifying_near else '',
      chosen_age_bars=qualifying_near['age_bars'] if qualifying_near else '',
      chosen_distance_atr=qualifying_near['distance_pre_atr'] if qualifying_near else '',
      chosen_visits=qualifying_near['independent_visits'] if qualifying_near else '',
      chosen_responses=qualifying_near['confirmed_responses'] if qualifying_near else '',
      chosen_status_pre=qualifying_near['level_status_pre'] if qualifying_near else '',
      chosen_role_pre=role,
      chosen_80plus_bars=int(qualifying_near['age_bars']>=80) if qualifying_near else '',
      reason='STRICTLY_PRE;OUTCOMES_NOT_READ' if covered else 'HISTORY_COVERAGE_INCOMPLETE')
    return row

def _atomic_csv(path,rows,cols):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.persistent_',suffix='.csv',dir=path.parent)
    try:
        with os.fdopen(fd,'w',newline='',encoding='utf-8') as f:
            w=csv.DictWriter(f,fieldnames=cols,extrasaction='ignore');w.writeheader();w.writerows(rows)
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def _atomic_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.persistent_',suffix='.json',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(value,f,indent=2,sort_keys=True);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for part in iter(lambda:f.read(1024*1024),b''):h.update(part)
    return h.hexdigest()

def _setup(root,cohort,max_events):
    audit=atlas_v1.lineage.integrity(root)  # requires consistent frozen cohort
    path=root/atlas_v1.lineage.ROOT_REL/atlas_v1.lineage.CTX_NAME
    with closing(atlas_v1.lineage.ro(path)) as ctx:
        selected=v4.select_events(ctx,cohort,max_events)
    token={'version':VERSION,'cohort':cohort,'max_events':max_events,
           'source_sha256':audit['frozen_cohort_sha256'],'windows':WINDOW,
           'near_atr':NEAR_ATR,'response_atr':MIN_RESPONSE_ATR,
           'max_response_bars':MAX_RESPONSE_BARS,'min_span':MIN_SPAN_BARS}
    short=hashlib.sha256(json.dumps(token,sort_keys=True).encode()).hexdigest()[:12]
    out=root/ROOT_REL/'runs'/f'{cohort}_{max_events}_{short}'
    return selected,token,out

def plan(root,cohort,max_events):
    events,token,out=_setup(root,cohort,max_events)
    per=Counter(e['symbol'] for e in events)
    ctx=root/atlas_v1.lineage.ROOT_REL/atlas_v1.lineage.CTX_NAME
    hist=root/atlas_v1.lineage.ROOT_REL/atlas_v1.lineage.HISTORY_NAME
    free=shutil.disk_usage(out.parent if out.parent.exists() else root).free/1024**3
    return dict(version=VERSION,events=len(events),symbols=len(per),
         observation_types=dict(Counter(x['observation_type'] for x in events)),
         window_bars=WINDOW,window_hours={'15m':40,'30m':40},
         min_responses=MIN_INDEPENDENT_RESPONSES,min_response_atr=MIN_RESPONSE_ATR,
         near_atr=NEAR_ATR,source_db=str(ctx),history_db_present=hist.is_file(),
         free_disk_gb=round(free,2),run_folder=str(out),
         note='No Binance queries, source SQLite strictly read-only, no outcomes in detector')

def _sym_key(symbol):
    return ''.join(c for c in symbol if c.isalnum() or c in ('_','-'))

def _symbol_paths(out,symbol):
    tag=_sym_key(symbol)
    return out/'shards'/f'{tag}.events.csv',out/'shards'/f'{tag}.levels.csv',out/'shards'/f'{tag}.json'

def run(root,cohort,max_events,min_free_gb=2.0):
    events,token,out=_setup(root,cohort,max_events)
    if shutil.disk_usage(root).free/1024**3<min_free_gb:
        raise RuntimeError(f'Insufficient free disk (<{min_free_gb} GB); nothing written')
    out.mkdir(parents=True,exist_ok=True)
    old=out/'run_config.json'
    if old.exists() and json.loads(old.read_text())!=token:
        raise RuntimeError('Run configuration mismatch; refusing to mix shards')
    _atomic_json(old,token)
    items=defaultdict(list)
    for e in events:items[e['symbol']].append(e)
    src=root/atlas_v1.lineage.ROOT_REL/atlas_v1.lineage.CTX_NAME
    histpath=root/atlas_v1.lineage.ROOT_REL/atlas_v1.lineage.HISTORY_NAME
    hist=atlas_v1.lineage.ro(histpath) if histpath.is_file() else None
    counters=Counter();done=0
    try:
        with closing(atlas_v1.lineage.ro(src)) as ctx:
            for symbol,requests in sorted(items.items()):
                a,b,m=_symbol_paths(out,symbol)
                keys=[x['event_key'] for x in requests]
                if m.is_file() and a.is_file() and b.is_file():
                    prior=json.loads(m.read_text())
                    if prior.get('event_keys')==keys and prior.get('events_sha256')==digest(a) and prior.get('levels_sha256')==digest(b):
                        counters['resumed_symbols']+=1;done+=1;continue
                df15,df30,source30=v4._merge_tf(ctx,hist,symbol)
                frames={'15m':df15.reset_index(),'30m':df30.reset_index()}
                symbol_events=[];symbol_levels=[]
                for e in requests:
                    tf_rows={};cov={}
                    for tf in ('15m','30m'):
                        rows,c=candidate_zones(e,tf,frames[tf]);tf_rows[tf]=rows;cov[tf]=c
                        symbol_levels.extend(rows)
                    symbol_events.append(classify(e,tf_rows,cov))
                _atomic_csv(a,symbol_events,EVENT_COLS)
                _atomic_csv(b,symbol_levels,ZONE_COLS)
                _atomic_json(m,dict(symbol=symbol,event_keys=keys,events_sha256=digest(a),
                                    levels_sha256=digest(b),levels=len(symbol_levels)))
                counters['processed_symbols']+=1;done+=1
                if done%10==0 or done==len(items):
                    print(f'PROGRESS {done}/{len(items)} symbols; new={counters["processed_symbols"]}; resumed={counters["resumed_symbols"]}',flush=True)
    finally:
        if hist is not None:hist.close()
    result=audit(root,cohort,max_events)
    print('PERSISTENT_LEVEL_DONE',json.dumps(result,ensure_ascii=False),flush=True)
    return result

def audit(root,cohort,max_events):
    events,token,out=_setup(root,cohort,max_events)
    if not (out/'run_config.json').is_file():raise RuntimeError('No run_config: execute --run first')
    if json.loads((out/'run_config.json').read_text())!=token:raise RuntimeError('Configuration mismatch')
    groups=defaultdict(list)
    for e in events:groups[e['symbol']].append(e)
    summaries=[];zone_count=0;checked=0
    combined_events=out/'event_labels.csv'
    combined_zones=out/'candidate_levels.csv'
    zone_tmp=combined_zones.with_suffix('.partial.csv')
    zone_tmp.parent.mkdir(parents=True,exist_ok=True)
    zone_handle=zone_tmp.open('w',newline='',encoding='utf-8')
    zone_writer=csv.DictWriter(zone_handle,fieldnames=ZONE_COLS)
    zone_writer.writeheader()
    for symbol,items in sorted(groups.items()):
        a,b,m=_symbol_paths(out,symbol)
        if not all(p.is_file() for p in (a,b,m)):
            raise RuntimeError(f'Incomplete symbol shard: {symbol}')
        meta=json.loads(m.read_text());expected=[x['event_key'] for x in items]
        if meta['event_keys']!=expected or meta['events_sha256']!=digest(a) or meta['levels_sha256']!=digest(b):
            raise RuntimeError(f'Shard mismatch/hash failure: {symbol}')
        with a.open(newline='') as f:rs=list(csv.DictReader(f))
        if len(rs)!=len(items) or [r['event_key'] for r in rs]!=expected:
            raise RuntimeError(f'Shard events missing/duplicated: {symbol}')
        with b.open(newline='') as f:zs=list(csv.DictReader(f))
        if any(z['event_key'] not in set(expected) or int(z['formation_closed_ts'])>=int(z['retest_ts']) or
               int(z['confirmed_responses'])<MIN_INDEPENDENT_RESPONSES or
               any(int(t)>=int(z['retest_ts']) for t in z['response_timestamps'].split(';') if t) or
               z['source_coverage']!='COMPLETE' for z in zs):
            raise RuntimeError(f'Invalid causal persistent zones: {symbol}')
        for r in rs:
            if (r['group_pre']==GROUP_NEAR) != (r['near_pre']=='1'):
                if r['group_pre']!=GROUP_UNKNOWN:raise RuntimeError(f'Group mismatch: {symbol}')
            if r['group_pre']==GROUP_UNKNOWN and r['coverage_all']!='0':raise RuntimeError('UNKNOWN must have incomplete coverage')
        zone_writer.writerows(zs)
        summaries.extend(rs);zone_count+=len(zs);checked+=1
    if len(summaries)!=len(events) or len(set(x['event_key'] for x in summaries))!=len(events):
        raise RuntimeError('Duplicate or missing event')
    zone_handle.flush();os.fsync(zone_handle.fileno());zone_handle.close()
    os.replace(zone_tmp,combined_zones)
    _atomic_csv(combined_events,summaries,EVENT_COLS)
    counts=dict(Counter(r['group_pre'] for r in summaries))
    cov=dict(Counter(r['coverage_15m']+'|'+r['coverage_30m'] for r in summaries))
    # Joined outcomes are intentionally forbidden here. The first release
    # provides ground truth PRE labels; statistical outcome join is next stage.
    result=dict(success=True,version=VERSION,events=len(events),symbols=checked,
                persistent_candidate_zones=zone_count,groups=counts,coverage=cov,
                no_outcome_access=True,run_folder=str(out),
                outputs_sha256={'event_labels.csv':digest(combined_events),
                                'candidate_levels.csv':digest(combined_zones)})
    _atomic_json(out/'audit.json',result)
    return result

def inspect(root,cohort,max_events,symbol):
    _,_,out=_setup(root,cohort,max_events)
    a,b,_=_symbol_paths(out,symbol)
    if not a.is_file():raise FileNotFoundError(f'No processed symbol: {symbol} (run --run)')
    x=pd.read_csv(a,keep_default_na=False)
    y=pd.read_csv(b,keep_default_na=False)
    return {'events':x.to_dict('records'),'zones':y.to_dict('records')}

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    op=parser.add_mutually_exclusive_group(required=True)
    for name in ('plan','run','audit','inspect'):op.add_argument('--'+name,action='store_true')
    parser.add_argument('--cohort',choices=['live-stratified','all-live','all-frozen'],default='live-stratified')
    parser.add_argument('--max-events',type=int,default=40,help='0 = all cohort')
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parent)
    parser.add_argument('--symbol',help='For --inspect, e.g. TRUTHUSDT')
    parser.add_argument('--min-free-gb',type=float,default=2.)
    a=parser.parse_args(argv)
    if a.max_events<0:parser.error('--max-events cannot be negative')
    root=a.root.resolve()
    if a.plan:result=plan(root,a.cohort,a.max_events)
    elif a.run:result=run(root,a.cohort,a.max_events,a.min_free_gb)
    elif a.audit:result=audit(root,a.cohort,a.max_events)
    else:
        if not a.symbol:parser.error('--inspect needs --symbol')
        result=inspect(root,a.cohort,a.max_events,a.symbol.upper())
    print(json.dumps(result,ensure_ascii=False,indent=2,default=str))

if __name__=='__main__':main()
