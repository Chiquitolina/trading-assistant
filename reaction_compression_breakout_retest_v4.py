#!/usr/bin/env python3
"""Reaction Compression Breakout/Retest V4. Causal, offline, read-only sources.

Qualified historical compression uses the V3 production scanner (separate 2x
baseline). V4 freezes candidate selection BEFORE the retest candle, then labels
its touch/hold with that candle CLOSED. An actual first retest requires gap-free
1m data after the breakout; unavailable data is UNKNOWN, never FIRST.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import math
import os
import sqlite3
import tempfile
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import reaction_compression_lineage as v3

VERSION='compression-breakout-retest-v4.0'
OUTPUT='reaction_compression_breakout_retest_v4'
M=v3.M
COLUMNS=['event_key','symbol','side','date_utc','observation_type','retest_ts',
    'timeframe','window','coverage','history_source','snapshots_total',
    'snapshots_eligible','snapshots_qualified','directional_candidates',
    'state_pre','age_bucket','state_known','first_retest_status',
    'prior_wall_touches','first_touch_coverage','box_start_ts','box_last_close_ts',
    'box_high','box_low','wall','breakout_ts','breakout_closed_ts',
    'age_break_min','breakout_close','breakout_strength_pct',
    'breakout_body_share','breakout_close_strength','breakout_quote_ratio20',
    'pre_wall_distance_pct','pre_wall_distance_atr',
    'known_wick_distance_pct','known_close_side_pct','known_touch','known_hold',
    'readiness_score','range_ratio','atr_ratio','volume_ratio',
    'compression_height_pct','compression_shape','reason']
BEST_COLS=['event_key','symbol','side','date_utc','observation_type','retest_ts',
           'selection_timeframe','selection_window','selection_state_pre',
           'selection_state_known','selection_age_bucket','selection_first_retest_status',
           'selection_pre_wall_distance_pct','selection_age_break_min','selection_coverage',
           'has_qualified_any','coverage_all_5','outcome','entry_mode','entry_delay_min']


def select_events(ctx,cohort='live-stratified',max_events=100):
    """Outcome-blind, deterministic temporal/day/side sampling (no future labels)."""
    records=v3.load_events(ctx,0)
    if cohort!='all-frozen':
        records=[e for e in records if e['observation_type']=='LIVE_OBSERVED']
    if cohort=='live-stratified':
        buckets=defaultdict(list)
        for e in records:buckets[(e['date_utc'],e['side'])].append(e)
        keys=sorted(buckets)
        # Rotate day+side rather than favoring one day. Hash ordering prevents
        # dependence on accidental row ordering; does NOT use TP/SL labels.
        for k in keys:
            buckets[k].sort(key=lambda e:hashlib.sha256(e['event_key'].encode()).hexdigest())
        taken=[];used=Counter()
        while keys and (not max_events or len(taken)<max_events):
            nextkeys=[]
            for k in keys:
                if max_events and len(taken)>=max_events:break
                while buckets[k]:
                    e=buckets[k].pop()
                    key=(e['symbol'],e['date_utc'],e['side'])
                    if used[key]>=2:continue
                    used[key]+=1;taken.append(e);break
                if buckets[k]:nextkeys.append(k)
            keys=nextkeys
        return sorted(taken,key=lambda e:(e['retest_ts'],e['event_key']))
    if max_events:return records[:max_events]
    return records


def full_causal_snapshots(r,step,hours,scan_every=1):
    """Every prior closed timeframe bar by default; older grid optional audit."""
    last=(r//step)*step-2*step
    floor=r-hours*60*M
    return list(range(last,floor-1,-step*scan_every)) if last>=floor else []


def first_break(df,end_open,tf_ms,r,hi,lo,side,margin=.10):
    """First close outside known box, never after retest; opposite first blocks."""
    stage,bt,bc=v3._break_history(df,end_open,tf_ms,r,hi,lo,side,margin_pct=margin)
    return stage,bt,bc


def age_bucket(age):
    if age is None:return 'UNKNOWN'
    if age<=180:return 'FRESH_0_3H'
    if age<=360:return 'FRESH_3_6H'
    if age<=720:return 'INTERMEDIATE_6_12H'
    if age<=1440:return 'OLD_12_24H'
    return 'STALE_OVER_24H'


def _sgn(side):return 1 if side=='LONG' else -1


def _side_pct(price,wall,side):
    return 100*(float(price)/float(wall)-1)*_sgn(side)


def prior_minute_touches(m1,break_closed,r,wall,side,tolerance=.25):
    """Exclude retest candle. Strict exact minute continuity, no false first-touch.

    Prior 1m touches are tagged even if they occurred on a candle whose close
    was not favorable. This tests a genuine first visit to the broken level.
    """
    start=int(break_closed)//M*M
    end=int(r)-M
    if start>end:return 'UNKNOWN_TIME',None,'INVALID_INTERVAL'
    expected=np.arange(start,end+M,M,dtype='int64')
    try:part=m1.loc[expected]
    except KeyError:return 'UNKNOWN_GAP',None,'MISSING_1M_HISTORY'
    if len(part)!=len(expected) or not np.array_equal(part.index.to_numpy(dtype='int64'),expected):
        return 'UNKNOWN_GAP',None,'NONCONTIGUOUS_1M_HISTORY'
    tested=part['low'] if side=='LONG' else part['high']
    # Wick crosses an old resistance/support (even if overshot); first visit
    # is defined using crossing into the +/- tolerance neighborhood.
    if side=='LONG':touched=tested<=wall*(1+tolerance/100)
    else:touched=tested>=wall*(1-tolerance/100)
    n=int(touched.sum())
    return ('PREVIOUS_TOUCH' if n else 'VERIFIED_FIRST'),n,'CONTIGUOUS_1M'


def _break_quality(df,bt,box_end,tf_ms,wall,side,bc):
    b=df.loc[bt]
    spread=float(b['high']-b['low'])
    body=abs(float(b['close'])-float(b['open']))/spread if spread>0 else None
    loc=((float(b['close'])-float(b['low']))/spread if side=='LONG' else
         (float(b['high'])-float(b['close']))/spread) if spread>0 else None
    prior=df.loc[(df.index<bt)&(df.index>=bt-20*tf_ms),'quoteVolume']
    volume=float(b['quoteVolume'])/float(prior.median()) if len(prior)>=10 and float(prior.median())>0 else None
    return body,loc,volume


def snapshot_candidate(event,raw,df,pre,tf_ms):
    hi=float(raw['high']);lo=float(raw['low']);wall=hi if event['side']=='LONG' else lo
    box_end=int(raw['end_ts'])-tf_ms+1
    stage,bt,bc=first_break(df,box_end,tf_ms,event['retest_ts'],hi,lo,event['side'])
    if stage!='DIRECTION_BREAK':return stage,None
    break_closed=bt+tf_ms
    age=(event['retest_ts']-break_closed)/M
    if age<0:return 'INVALID_TIME',None
    pre_dist=_side_pct(pre,wall,event['side']) if pre is not None else None
    body,loc,vol=_break_quality(df,bt,box_end,tf_ms,wall,event['side'],bc)
    return stage,dict(box_start_ts=int(raw['start_ts']),box_last_close_ts=int(raw['end_ts']),
        box_high=hi,box_low=lo,wall=wall,breakout_ts=bt,breakout_closed_ts=break_closed,
        breakout_close=bc,age_break_min=age,age_bucket=age_bucket(age),
        breakout_strength_pct=_side_pct(bc,wall,event['side']),breakout_body_share=body,
        breakout_close_strength=loc,breakout_quote_ratio20=vol,
        pre_wall_distance_pct=pre_dist,readiness_score=float(raw['readiness_score']),
        range_ratio=raw.get('range_ratio'),atr_ratio=raw.get('atr_ratio'),
        volume_ratio=raw.get('volume_ratio'),compression_height_pct=raw.get('compression_height_pct'),
        compression_shape=raw.get('compression_shape'))


def choose_prior_box(options):
    """No current retest candle or future outcomes appear in ranking."""
    if not options:return None
    def key(c):
        delta=c['pre_wall_distance_pct']
        # First favor recently broken levels near current price, then closeness,
        # then shorter age/quality. Deeply distant walls are diagnostic only.
        near=(delta is not None and abs(delta)<=1.0)
        correct=delta is not None and delta>=-.10
        recent=c['age_break_min']<=360
        d=abs(delta) if delta is not None else 1e9
        return (int(near and correct and recent),int(near and correct),
                int(near),-d,-c['age_break_min'],c['readiness_score'])
    return max(options,key=key)


def annotate(event,selected,retest,m1,df):
    side=event['side'];wall=selected['wall'];r=event['retest_ts']
    d=selected['pre_wall_distance_pct']
    state_pre=('PRE_CLOSE_UNAVAILABLE' if d is None else
               'LOST_WALL_PRE' if d<-.10 else
               'PRE_NEAR_WALL' if abs(d)<=.5 else
               'PRE_ABOVE_WALL' if d>.5 else 'PRE_UNDER_TOLERANCE')
    # The first-retetst property is historical, but only as observed before r.
    first,n,source=prior_minute_touches(m1,selected['breakout_closed_ts'],r,wall,side)
    known_touch=None;known_hold=None;wick=None;close_pct=None
    if retest is not None:
        extremum=float(retest[3] if side=='LONG' else retest[2])
        wick=_side_pct(extremum,wall,side)
        close_pct=_side_pct(float(retest[4]),wall,side)
        known_touch=bool(-.25 <= wick <= .25)
        known_hold=bool(close_pct>=0)
    if retest is None:state_known='RETEST_CANDLE_MISSING'
    elif state_pre=='LOST_WALL_PRE':state_known='LOST_WALL_BEFORE_RETEST'
    elif known_touch and known_hold:
        if first=='VERIFIED_FIRST':state_known='FIRST_RETEST_HELD'
        elif first=='PREVIOUS_TOUCH':state_known='REPEATED_RETEST_HELD'
        else:state_known='RETEST_HELD_FIRST_UNKNOWN'
    elif known_touch:state_known='RETEST_FAILED_HOLD'
    elif wick is not None and wick<-.25 and known_hold:state_known='DEEP_SWEEP_RECLAIMED'
    elif wick is not None and wick<-.25:state_known='DEEP_SWEEP_FAILED'
    elif known_hold:state_known='NO_TOUCH_HELD_SIDE'
    else:state_known='NO_TOUCH_LOST_SIDE'
    pre_atr=None
    # True range of the last 14 fully closed TF bars, never the retest bar.
    before=df.loc[df.index+int(df.index[1]-df.index[0])<=r].tail(15) if len(df)>1 else pd.DataFrame()
    if len(before)==15 and d is not None:
        x=before[['high','low','close']].astype(float)
        prev=x['close'].shift(1)
        tr=pd.concat([x.high-x.low,(x.high-prev).abs(),(x.low-prev).abs()],axis=1).max(axis=1).iloc[1:]
        denom=float(tr.mean())
        if denom>0:pre_atr=(d/100*wall)/denom
    return dict(state_pre=state_pre,state_known=state_known,
        first_retest_status=first,prior_wall_touches=n,first_touch_coverage=source,
        known_wick_distance_pct=wick,known_close_side_pct=close_pct,
        known_touch=known_touch,known_hold=known_hold,pre_wall_distance_atr=pre_atr)


def event_tf_window(event,tf,n,df,m1,pre,retest,cache,search_hours,scan_every,source):
    times=full_causal_snapshots(event['retest_ts'],v3.TF[tf],search_hours,scan_every)
    eligible=0;qualified=0;stages=Counter();options=[]
    for end in times:
        key=(tf,n,end)
        if key not in cache:
            cut=v3.validate_slice(df,end,3*n,v3.TF[tf])
            cache[key]=('MISSING',None) if cut is None else ('AVAILABLE',v3.scan_snapshot(cut,event['symbol'],tf,n,v3.TF[tf]))
        status,raw=cache[key]
        if status!='AVAILABLE':continue
        eligible+=1
        if raw is None:continue
        qualified+=1
        stage,candidate=snapshot_candidate(event,raw,df,pre,v3.TF[tf])
        stages[stage]+=1
        if candidate is not None:options.append(candidate)
    coverage=('COMPLETE_SEARCH' if eligible==len(times) and len(times)>0 else
              'PARTIAL_SEARCH' if eligible>0 else 'INSUFFICIENT_HISTORY')
    d={x:None for x in COLUMNS}
    d.update({k:event.get(k) for k in ('event_key','symbol','side','date_utc','observation_type','retest_ts')})
    d.update(timeframe=tf,window=n,coverage=coverage,history_source=source,
        snapshots_total=len(times),snapshots_eligible=eligible,
        snapshots_qualified=qualified,directional_candidates=len(options))
    best=choose_prior_box(options)
    if best is not None:
        d.update(best)
        d.update(annotate(event,best,retest,m1,df))
        d['reason']='BOX_AND_FIRST_DIRECTIONAL_BREAK_FROZEN_BEFORE_RETEST'
    elif eligible==0:d.update(state_pre='INSUFFICIENT_HISTORY',state_known='NOT_EVALUABLE',reason='NO_VALID_CLOSED_HISTORY')
    elif qualified==0:d.update(state_pre='NO_QUALIFIED_BOX' if coverage=='COMPLETE_SEARCH' else 'INCOMPLETE_NO_BOX',
        state_known='NOT_EVALUABLE',reason='NO_BOX_IN_SEARCH')
    else:
        state='OPPOSITE_FIRST' if stages['OPPOSITE_FIRST'] else 'NO_DIRECTIONAL_BREAK'
        d.update(state_pre=state if coverage=='COMPLETE_SEARCH' else 'INCOMPLETE_'+state,
                 state_known='NOT_EVALUABLE',reason=json.dumps(dict(stages)))
    return d


def _merge_tf(ctx,hist,symbol):
    df15,_=v3._merge_bar_data(ctx,hist,symbol,'15m')
    native30,_=v3._merge_bar_data(ctx,hist,symbol,'30m')
    aggregate=v3.derive_30m(df15)
    if native30.empty:df30=aggregate;src='AGGREGATED_15M_PROXY'
    else:
        missing=aggregate[~aggregate.timestamp.isin(native30.timestamp)]
        df30=pd.concat([native30,missing],ignore_index=True).sort_values('timestamp')
        src='NATIVE_30M' if missing.empty else 'MIXED_NATIVE_15M_PROXY'
    return df15.set_index('timestamp'),df30.set_index('timestamp'),src


def _all_1m(ctx,symbol):
    rows=ctx.execute("SELECT open_ts,open,high,low,close FROM candles WHERE symbol=? AND timeframe='1m' ORDER BY open_ts",(symbol,)).fetchall()
    return pd.DataFrame(rows,columns=['timestamp','open','high','low','close']).set_index('timestamp')


def _hash(path):return v3.hash_file(path)


def _write_csv(path,rows,cols):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.v4_',suffix='.csv',dir=path.parent)
    try:
        with os.fdopen(fd,'w',newline='',encoding='utf-8') as stream:
            writer=csv.DictWriter(stream,fieldnames=cols,extrasaction='ignore');writer.writeheader();writer.writerows(rows)
            stream.flush();os.fsync(stream.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def _load_outcomes(root):
    folder=root/v3.ROOT_REL/'reaction_first_touch_lab_v1'
    summary=json.loads((folder/'summary.json').read_text())
    path=folder/'first_touch_scenarios.csv'
    if _hash(path)!=summary['file_sha256'][path.name]:raise RuntimeError('First-Touch checksum no coincide')
    frame=pd.read_csv(path,low_memory=False)
    part=frame[(frame.entry_mode=='OBSERVED_OPEN')&(frame.tp_pct==1.0)&(frame.sl_pct==0.5)].copy()
    if part.event_key.duplicated().any():raise RuntimeError('Resultados First-Touch duplicados')
    return part.set_index('event_key'),summary


def _select_global(rows):
    directional=[r for r in rows if r['breakout_ts'] is not None]
    if not directional:return None
    selected=v4_pick(directional)
    return selected


def v4_pick(rows):
    return max(rows,key=lambda x:(
        int(x['coverage']=='COMPLETE_SEARCH'),
        int(x['state_pre'] not in ('LOST_WALL_PRE','PRE_CLOSE_UNAVAILABLE')),
        int(abs(x['pre_wall_distance_pct'])<=.5) if x['pre_wall_distance_pct'] is not None else 0,
        int(x['age_break_min']<=360),
        -abs(x['pre_wall_distance_pct']) if x['pre_wall_distance_pct'] is not None else -1e9,
        -x['age_break_min'],x['readiness_score']))


def run(root,max_events=100,cohort='live-stratified',search_hours=48,scan_every=1,progress=10):
    audit=v3.integrity(root)
    hpath=root/v3.ROOT_REL/v3.HISTORY_NAME
    result=[];events=None
    with closing(v3.ro(root/v3.ROOT_REL/v3.CTX_NAME)) as ctx:
        events=select_events(ctx,cohort,max_events)
        groups=defaultdict(list)
        for e in events:groups[e['symbol']].append(e)
        hist=v3.ro(hpath) if hpath.is_file() else None
        done=0
        try:
            for symbol,event_list in groups.items():
                df15,df30,src30=_merge_tf(ctx,hist,symbol)
                m1=_all_1m(ctx,symbol)
                cache={}
                for e in event_list:
                    pre,retest=v3.one_min_pre_and_retest(ctx,symbol,e['retest_ts'])
                    for tf,ns in v3.WINDOWS.items():
                        df=df15 if tf=='15m' else df30
                        for n in ns:
                            result.append(event_tf_window(e,tf,n,df,m1,pre,retest,cache,search_hours,scan_every,
                                'NATIVE_15M' if tf=='15m' else src30))
                    done+=1
                    if progress and done%progress==0:
                        print('V4_PROGRESS',json.dumps({'done':done,'total':len(events),'rows':len(result)}),flush=True)
        finally:
            if hist is not None:hist.close()
    expected_windows=sum(len(ns) for ns in v3.WINDOWS.values())
    if len(result)!=expected_windows*len(events):raise RuntimeError(f'Expected {expected_windows} rows for every event')
    outcomes,first_summary=_load_outcomes(root)
    if first_summary['frozen_cohort_sha256']!=audit['frozen_cohort_sha256']:
        raise RuntimeError('Cohort mismatch vs First-Touch')
    per_event=defaultdict(list)
    for r in result:per_event[r['event_key']].append(r)
    selected=[]
    for e in events:
        opts=per_event[e['event_key']]
        winner=_select_global(opts)
        label=outcomes.loc[e['event_key']] if e['event_key'] in outcomes.index else None
        d={k:e.get(k) for k in ('event_key','symbol','side','date_utc','observation_type','retest_ts')}
        d.update(selection_timeframe=winner['timeframe'] if winner else None,
                 selection_window=winner['window'] if winner else None,
                 selection_state_pre=winner['state_pre'] if winner else 'NO_DIRECTIONAL_BOX',
                 selection_state_known=winner['state_known'] if winner else 'NOT_EVALUABLE',
                 selection_age_bucket=winner['age_bucket'] if winner else None,
                 selection_first_retest_status=winner['first_retest_status'] if winner else None,
                 selection_pre_wall_distance_pct=winner['pre_wall_distance_pct'] if winner else None,
                 selection_age_break_min=winner['age_break_min'] if winner else None,
                 selection_coverage=winner['coverage'] if winner else ('COMPLETE_SEARCH' if all(x['coverage']=='COMPLETE_SEARCH' for x in opts) else 'INCOMPLETE'),
                 has_qualified_any=int(any(x['snapshots_qualified'] for x in opts)),
                 coverage_all_5=int(all(x['coverage']=='COMPLETE_SEARCH' for x in opts)),
                 outcome=label['result_180m'] if label is not None else 'NO_PATH',
                 entry_mode=label['entry_mode'] if label is not None else None,
                 entry_delay_min=label['entry_delay_min'] if label is not None else None)
        selected.append(d)
    out=root/v3.ROOT_REL/OUTPUT
    _write_csv(out/'structures_v4.csv',result,COLUMNS)
    _write_csv(out/'event_best_v4.csv',selected,BEST_COLS)
    meta=dict(version=VERSION,events=len(events),rows=len(result),
        frozen_cohort_sha256=audit['frozen_cohort_sha256'],cohort=cohort,max_events=max_events,
        search_hours=search_hours,scan_every=scan_every,windows={tf:list(ns) for tf,ns in v3.WINDOWS.items()},
        windows_per_event=expected_windows,source_history=str(hpath) if hpath.exists() else None,
        coverage=dict(Counter(r['coverage'] for r in result)),states_pre=dict(Counter(r['selection_state_pre'] for r in selected)),
        states_known=dict(Counter(r['selection_state_known'] for r in selected)),
        unique_events=len({r['event_key'] for r in result}),
        structures_sha256=_hash(out/'structures_v4.csv'),best_sha256=_hash(out/'event_best_v4.csv'),
        assumptions=['No future prices/TP/SL in box detection, ranking, or event sampling',
           'RETEST_CLOSED labels use the current 1m candle and require later entry',
           'VERIFIED_FIRST needs every 1m candle from breakout close to retest; else UNKNOWN_GAP',
           'FULL_SEARCH means every eligible timeframe close (no 4-bar skip)',
           'Outcome OBSERVED_OPEN remains from original First-Touch replay, not newly simulated after added confirmation',
           'Historical exploratory only; same dates cannot become unbiased holdout'])
    (out/'summary.json').write_text(json.dumps(meta,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print('V4_DONE',json.dumps({'events':len(events),'rows':len(result),'coverage':meta['coverage'],
                               'states_known':meta['states_known']}),flush=True)
    return meta


def plan(root,max_events,cohort,search_hours,scan_every):
    audit=v3.integrity(root)
    with closing(v3.ro(root/v3.ROOT_REL/v3.CTX_NAME)) as ctx:
        events=select_events(ctx,cohort,max_events)
    return dict(version=VERSION,frozen_events=audit['events'],selected_events=len(events),
        cohort=cohort,selection_by_day_side={f'{k[0]} {k[1]}':n for k,n in Counter((e['date_utc'],e['side']) for e in events).items()},
        symbols=len({e['symbol'] for e in events}),first_retest_ts=min((e['retest_ts'] for e in events),default=None),
        last_retest_ts=max((e['retest_ts'] for e in events),default=None),
        native_history_exists=(root/v3.ROOT_REL/v3.HISTORY_NAME).exists(),
        timeframe_windows={k:list(v) for k,v in v3.WINDOWS.items()},
        search_hours=search_hours,scan_every=scan_every,network_calls=False,writes=False,
        note='Backfill selection MUST use reaction_compression_v4_backfill.py, not original chronological --max-events.')


def audit_output(root):
    folder=root/v3.ROOT_REL/OUTPUT
    meta=json.loads((folder/'summary.json').read_text())
    frames=[]
    for f,sha in [('structures_v4.csv','structures_sha256'),('event_best_v4.csv','best_sha256')]:
        path=folder/f
        if _hash(path)!=meta[sha]:return dict(success=False,reason=f+' SHA mismatch')
        with path.open(newline='',encoding='utf-8') as h:frames.append(list(csv.DictReader(h)))
    structs,best=frames
    events=set(x['event_key'] for x in best)
    identities={(x['event_key'],x['timeframe'],x['window']) for x in structs}
    chrono=all(int(x['box_last_close_ts']) < int(x['breakout_ts']) and
               int(x['breakout_closed_ts']) <= int(x['retest_ts']) for x in structs
        if x['breakout_ts'] and x['box_last_close_ts'])
    expected_windows=sum(len(ns) for ns in v3.WINDOWS.values())
    valid=(len(structs)==expected_windows*meta['events'] and len(best)==meta['events']
        and len(events)==meta['events'] and len(identities)==len(structs)
        and len({x['event_key'] for x in structs})==meta['events'] and chrono
        and meta['frozen_cohort_sha256']==v3.integrity(root)['frozen_cohort_sha256'])
    return dict(success=valid,events=meta['events'],rows=len(structs),
                chronology_valid=chrono,unique_best=len(events),
                coverage=meta['coverage'],states_known=meta['states_known'])


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    g=ap.add_mutually_exclusive_group(required=True)
    for mode in ('plan','run','audit'):g.add_argument('--'+mode,action='store_true')
    ap.add_argument('--root',type=Path,default=Path('/home/multiservices-1m'))
    ap.add_argument('--max-events',type=int,default=100,help='0 = all events in selected cohort')
    ap.add_argument('--cohort',choices=['live-stratified','all-live','all-frozen'],default='live-stratified')
    ap.add_argument('--search-hours',type=int,default=48)
    ap.add_argument('--scan-every',type=int,default=1)
    args=ap.parse_args(argv)
    if args.max_events<0 or args.search_hours<6 or args.search_hours>168 or args.scan_every not in (1,2,4):
        ap.error('max-events>=0, search-hours=6..168, scan-every=1,2,4')
    root=args.root.resolve()
    if args.plan:res=plan(root,args.max_events,args.cohort,args.search_hours,args.scan_every)
    elif args.run:res=run(root,args.max_events,args.cohort,args.search_hours,args.scan_every)
    else:res=audit_output(root)
    print(json.dumps(res,indent=2,ensure_ascii=False))

if __name__=='__main__':main()
