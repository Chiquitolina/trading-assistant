#!/usr/bin/env python3
"""Break–Hold–Retest PRE: independent, offline, frozen V1 persistent-level cohort.

For LONG consider only old HIGH/resistance levels, for SHORT only old LOW/support.
A candidate's two documented earlier responses MUST finish before the initial
breakout. Evaluate strictly HTF-closed candles before REACTION's first minute.
All label creation is outcome-blind; First Touch is joined separately by --report.
Never connects to Binance, Redis or an order executor.
"""
from __future__ import annotations
import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import tempfile
import pandas as pd
import numpy as np

import audit_persistent_pattern_roles as v2

VERSION='break-hold-retest-pre-v1.0'
TF_MS={'15m':900000,'30m':1800000}
DISTANCE_ATR=.50
BREAK_CLOSES=2
HOLD_CLOSES=2
RETEST_HOLD_CLOSES=2
GROUPS=(
    'BULLISH_BREAK_HOLD_RETEST_PRE','BEARISH_BREAK_HOLD_RETEST_PRE',
    'BULLISH_BREAK_HOLD_PRE','BEARISH_BREAK_HOLD_PRE',
    'BREAK_CONFIRMED_PENDING_HOLD_PRE','BREAK_INVALIDATED_PRE',
    'HOLD_WITHOUT_CLEAR_BREAK_PRE','NEAR_LEVEL_NO_BREAK_PRE',
    'NO_QUALIFIED_DIRECTIONAL_LEVEL_NEAR_PRE','UNKNOWN_HISTORY','UNKNOWN_PATH'
)
FIELDS=('event_key','symbol','side','date_utc','retest_ts','observation_type',
    'group_pre','bhr_group_pre','zone_id','level_tf','origin_kind','zone_low','zone_high',
    'zone_distance_atr','zone_formed_closed_ts','prior_visits','prior_responses',
    'responses_before_break','first_break_closed_ts','break_confirm_closed_ts',
    'hold_confirm_closed_ts','pre_retest_closed_ts','retest_hold_confirm_closed_ts',
    'last_pre_close_ts','last_pre_close_side','reason')


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1<<20),b''):h.update(b)
    return h.hexdigest()


def atom_json(path,obj):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.bhr_',suffix='.json',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(obj,f,indent=2,sort_keys=True,allow_nan=False)
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def atom_csv(path,frame):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.bhr_',suffix='.csv',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='') as f:
            frame.to_csv(f,index=False)
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def response_ts(zone):
    raw=zone.get('response_timestamps','')
    try:
        ts=[int(x) for x in str(raw).split(';') if x]
    except (ValueError,TypeError):return []
    return ts


def _side(close,lo,hi,side):
    """OLD is resistance-below for LONG, support-above for SHORT."""
    good=(close>hi if side=='LONG' else close<lo)
    old=(close<lo if side=='LONG' else close>hi)
    return 'GOOD' if good else 'OLD' if old else 'INSIDE'


def path_for_zone(frame,zone,event_ts,side):
    """Evaluate candidate ONLY from formation to strictly pre-event HTF closes.

    Proof order: 2 historical responses -> (old-side close) -> two good closes
    -> two additional good hold closes -> optional distinct later OHLC retest
    -> two subsequent good closes. Two old-side closes invalidate a breakout.
    If the REACTION is itself the first retest, it is NOT PRE retest evidence.
    """
    tf=str(zone['timeframe'])
    if tf not in TF_MS or side not in ('LONG','SHORT'):return {'state':'UNKNOWN_PATH','reason':'BAD_TF_OR_SIDE'}
    step=TF_MS[tf];lo=float(zone['zone_low']);hi=float(zone['zone_high'])
    formed=int(zone['formation_closed_ts']);event_ts=int(event_ts)
    if not(0<lo<=hi) or formed>=event_ts:return {'state':'UNKNOWN_PATH','reason':'BAD_ZONE_FORMATION'}
    if frame is None or frame.empty or not {'timestamp','open','high','low','close'}.issubset(frame):
        return {'state':'UNKNOWN_PATH','reason':'NO_HTF_PATH'}
    df=frame[(frame.timestamp+step>=formed)&(frame.timestamp+step<event_ts)].sort_values('timestamp').copy()
    if df.empty or int(df.timestamp.iloc[0])+step>formed:
        return {'state':'UNKNOWN_PATH','reason':'GAP_FROM_FORMATION'}
    ts=df.timestamp.to_numpy(dtype='int64')
    if (len(ts)>1 and np.any(np.diff(ts)!=step)) or np.any(np.diff(ts)<=0):
        return {'state':'UNKNOWN_PATH','reason':'GAPPED_HTF_PATH'}
    vals=df[['open','high','low','close']].to_numpy(dtype=float)
    if (not np.isfinite(vals).all() or (vals<=0).any() or
        (vals[:,1]<np.maximum(vals[:,0],vals[:,3])).any() or
        (vals[:,2]>np.minimum(vals[:,0],vals[:,3])).any()):
        return {'state':'UNKNOWN_PATH','reason':'INVALID_OHLC'}
    proofs=response_ts(zone)
    if len(proofs)<2 or len(proofs)!=int(zone['confirmed_responses']):
        return {'state':'UNKNOWN_PATH','reason':'INVALID_PRIOR_RESPONSES'}
    out=dict(state='NEAR_LEVEL_NO_BREAK_PRE',reason='NO_CONFIRMED_BREAK',
        responses_before_break=0,first_break_closed_ts='',break_confirm_closed_ts='',
        hold_confirm_closed_ts='',pre_retest_closed_ts='',retest_hold_confirm_closed_ts='',
        last_pre_close_ts=int(ts[-1])+step,
        last_pre_close_side=_side(float(df.close.iloc[-1]),lo,hi,side))
    # Maintain the latest active break, not a conveniently selected historical one.
    prior_old=False;old_count=0;pre_good_run=0;current=None;had_break=False;invalidated=False
    for r in df.itertuples(index=False):
        closed=int(r.timestamp)+step
        cside=_side(float(r.close),lo,hi,side)
        old_count=old_count+1 if cside=='OLD' else 0
        if current is None:
            if cside=='OLD':prior_old=True
            if cside=='GOOD' and prior_old:
                pre_good_run+=1
                if pre_good_run==1:started=closed
                if pre_good_run>=BREAK_CLOSES:
                    nproof=sum(p<started for p in proofs)
                    if formed<started and nproof>=2:
                        current=dict(start=started,confirmed=closed,hold_run=0,hold='',
                                     retest='',post_retest_run=0,retest_hold='',nproof=nproof)
                        had_break=True
                    else:
                        # The historical level was not fully established before this break.
                        # Do not invent a pre-break level after the fact.
                        pre_good_run=0;prior_old=False
            elif cside!='GOOD':pre_good_run=0
            continue
        # A confirmed reverse passage invalidates the latest break.
        if old_count>=2:
            current=None;invalidated=True;prior_old=True;pre_good_run=0
            continue
        if closed<=current['confirmed']:continue
        if cside=='GOOD':
            current['hold_run']+=1
            if current['hold_run']>=HOLD_CLOSES and not current['hold']:
                current['hold']=closed
        else:current['hold_run']=0
        # A retest is a later, distinct CLOSED candle overlapping the frozen band.
        # Count the first such retest; a second close doesn't retroactively label it.
        if not current['retest'] and float(r.low)<=hi and float(r.high)>=lo:
            current['retest']=closed
            current['post_retest_run']=0
        elif current['retest'] and closed>current['retest']:
            current['post_retest_run']=current['post_retest_run']+1 if cside=='GOOD' else 0
            if current['post_retest_run']>=RETEST_HOLD_CLOSES and not current['retest_hold']:
                current['retest_hold']=closed
    if current is not None:
        out.update(responses_before_break=current['nproof'],first_break_closed_ts=current['start'],
            break_confirm_closed_ts=current['confirmed'],hold_confirm_closed_ts=current['hold'],
            pre_retest_closed_ts=current['retest'],retest_hold_confirm_closed_ts=current['retest_hold'])
        if out['last_pre_close_side']!='GOOD':
            out.update(state='BREAK_CONFIRMED_PENDING_HOLD_PRE',reason='FINAL_PRE_CLOSE_NOT_ACCEPTED')
        elif current['retest_hold'] and current['hold']:
            out.update(state='BULLISH_BREAK_HOLD_RETEST_PRE' if side=='LONG' else 'BEARISH_BREAK_HOLD_RETEST_PRE',reason='CONFIRMED_BREAK_RETEST_AND_HOLD_BEFORE_REACTION')
        elif current['hold']:
            out.update(state='BULLISH_BREAK_HOLD_PRE' if side=='LONG' else 'BEARISH_BREAK_HOLD_PRE',reason='CONFIRMED_BREAK_HOLD_NO_FULL_PRE_RETEST')
        else:out.update(state='BREAK_CONFIRMED_PENDING_HOLD_PRE',reason='TWO_BREAK_CLOSES_NO_POST_BREAK_HOLD')
    elif invalidated:
        out.update(state='BREAK_INVALIDATED_PRE',reason='TWO_ORIGINAL_SIDE_CLOSES_AFTER_BREAK')
    elif not prior_old and out['last_pre_close_side']=='GOOD':
        out.update(state='HOLD_WITHOUT_CLEAR_BREAK_PRE',reason='NO_CAUSAL_ORIGINAL_TO_NEW_SIDE_SEQUENCE')
    return out


def choose_zone(event,zones):
    """Deterministic PRE selection, never inspect path state or outcome to pick."""
    kind='HIGH' if event['side']=='LONG' else 'LOW'
    options=[z for z in zones if str(z['origin_kind'])==kind and
             float(z['distance_pre_atr'])<=DISTANCE_ATR+1e-9]
    options.sort(key=lambda z:(float(z['distance_pre_atr']),
              -int(z['confirmed_responses']),-int(z['age_bars']),str(z['timeframe']),str(z['zone_id'])))
    return options[0] if options else None


def classify(event,zones,frames):
    r={field:'' for field in FIELDS}
    r.update({k:event.get(k,'') for k in ('event_key','symbol','side','date_utc','retest_ts','observation_type','group_pre')})
    if event['group_pre']==v2.UNKNOWN:
        r.update(bhr_group_pre='UNKNOWN_HISTORY',reason='V1_HISTORY_INSUFFICIENT');return r
    if event['group_pre'] not in (v2.NEAR,v2.FAR):raise ValueError('Unexpected v1 group')
    z=choose_zone(event,zones)
    if z is None:
        r.update(bhr_group_pre='NO_QUALIFIED_DIRECTIONAL_LEVEL_NEAR_PRE',reason='NO_NEAR_OPPOSITE_ORIGIN_LEVEL');return r
    r.update(zone_id=z['zone_id'],level_tf=z['timeframe'],origin_kind=z['origin_kind'],
             zone_low=float(z['zone_low']),zone_high=float(z['zone_high']),
             zone_distance_atr=float(z['distance_pre_atr']),zone_formed_closed_ts=int(z['formation_closed_ts']),
             prior_visits=int(z['independent_visits']),prior_responses=int(z['confirmed_responses']))
    proof=path_for_zone(frames.get(z['timeframe']),z,int(event['retest_ts']),str(event['side']))
    r['bhr_group_pre']=proof['state']
    r.update({k:v for k,v in proof.items() if k in FIELDS and k!='state'})
    return r


def config(audit,max_events):
    return dict(version=VERSION,max_events=max_events,
        event_sha=audit['outputs_sha256']['event_labels.csv'],
        zone_sha=audit['outputs_sha256']['candidate_levels.csv'],
        candidate_distance_atr=DISTANCE_ATR,break_confirm_closes=BREAK_CLOSES,
        hold_after_break_closes=HOLD_CLOSES,retest_hold_closes=RETEST_HOLD_CLOSES,
        strict_pre_close=True,old_level_proofs_before_break=2,
        selection='nearest_matching_origin_kind_before_state',
        note='Research only, no outcomes/network/order execution')


def prepare(runfolder,max_events=0):
    events,zones,audit=v2.read_pre(runfolder)
    events=events.sort_values(['retest_ts','event_key'],kind='stable')
    if max_events>0:events=events.head(max_events).copy()
    zones=zones[zones.event_key.isin(events.event_key)]
    return events,zones,audit


def target(root):
    return root/'reports/reaction_lab/reaction_persistent_horizontal_v1/runs/all-live_0_bbab543f0ffe'


def outdir(runfolder,max_events):
    return Path(runfolder)/'break_hold_retest_pre_v1'/('pilot_'+str(max_events) if max_events else 'all_live')


def run(root,runfolder,max_events=0):
    import reaction_structural_level_atlas as atlas
    import reaction_compression_breakout_retest_v4 as v4
    ev,zones,audit=prepare(runfolder,max_events)
    out=outdir(runfolder,max_events);cfg=config(audit,max_events)
    out.mkdir(parents=True,exist_ok=True)
    existing=out/'config.json'
    if existing.is_file() and json.loads(existing.read_text())!=cfg:
        raise RuntimeError('Refusing to mix configuration/input hashes')
    atom_json(existing,cfg)
    zone_by_event={key:g.to_dict('records') for key,g in zones.groupby('event_key',sort=False)}
    src=root/atlas.lineage.ROOT_REL/atlas.lineage.CTX_NAME
    histpath=root/atlas.lineage.ROOT_REL/atlas.lineage.HISTORY_NAME
    hist=atlas.lineage.ro(histpath) if histpath.is_file() else None
    done=new=resumed=0
    try:
        with closing(atlas.lineage.ro(src)) as ctx:
            for symbol,part in ev.groupby('symbol',sort=True):
                dest=out/'shards'/f'{symbol}.csv';meta=out/'shards'/f'{symbol}.json'
                keys=part.event_key.tolist()
                if dest.is_file() and meta.is_file():
                    prior=json.loads(meta.read_text())
                    if prior.get('keys')==keys and prior.get('sha256')==sha(dest):
                        resumed+=1;done+=1;continue
                relevant=part[part.group_pre!=v2.UNKNOWN]
                frames={}
                if not relevant.empty and any(choose_zone(e,zone_by_event.get(e['event_key'],[]))
                                              is not None for e in relevant.to_dict('records')):
                    df15,df30,_=v4._merge_tf(ctx,hist,symbol)
                    frames={'15m':df15.reset_index(),'30m':df30.reset_index()}
                rows=[classify(e,zone_by_event.get(e['event_key'],[]),frames) for e in part.to_dict('records')]
                atom_csv(dest,pd.DataFrame(rows,columns=FIELDS))
                atom_json(meta,dict(keys=keys,sha256=sha(dest)))
                done+=1;new+=1
                if done%20==0 or done==ev.symbol.nunique():
                    print(f'BHR_PROGRESS {done}/{ev.symbol.nunique()} symbols new={new} resumed={resumed}',flush=True)
    finally:
        if hist is not None:hist.close()
    result=audit_run(runfolder,max_events)
    print('BHR_PRE_DONE',json.dumps(result,ensure_ascii=False),flush=True)
    return result


def audit_run(runfolder,max_events=0):
    ev,zones,audit=prepare(runfolder,max_events)
    out=outdir(runfolder,max_events)
    if json.loads((out/'config.json').read_text())!=config(audit,max_events):
        raise ValueError('BHR config/input hash mismatch')
    frames=[]
    for symbol,part in ev.groupby('symbol',sort=True):
        dest=out/'shards'/f'{symbol}.csv';meta=out/'shards'/f'{symbol}.json'
        st=json.loads(meta.read_text())
        if st.get('keys')!=part.event_key.tolist() or st.get('sha256')!=sha(dest):
            raise ValueError(f'Invalid BHR shard: {symbol}')
        df=pd.read_csv(dest,dtype={'event_key':str},keep_default_na=False)
        if df.event_key.tolist()!=part.event_key.tolist() or len(df)!=len(part):
            raise ValueError(f'Invalid BHR event identity: {symbol}')
        if not df.bhr_group_pre.isin(GROUPS).all():raise ValueError('Unexpected BHR labels')
        for identity in ('symbol','side','retest_ts','group_pre'):
            if not df[identity].astype(str).reset_index(drop=True).eq(
                    part[identity].astype(str).reset_index(drop=True)).all():
                raise ValueError(f'BHR {identity} identity mismatch: {symbol}')
        if ((df.bhr_group_pre.str.startswith('BULLISH_')) & (df.side!='LONG')).any() or \
           ((df.bhr_group_pre.str.startswith('BEARISH_')) & (df.side!='SHORT')).any():
            raise ValueError('BHR directional label mismatch')
        if ((df.group_pre==v2.UNKNOWN)&(df.bhr_group_pre!='UNKNOWN_HISTORY')).any():
            raise ValueError('BHR unknown history provenance mismatch')
        has=df.zone_id.ne('')
        if ((df.bhr_group_pre.isin(('BULLISH_BREAK_HOLD_RETEST_PRE',
            'BEARISH_BREAK_HOLD_RETEST_PRE','BULLISH_BREAK_HOLD_PRE',
            'BEARISH_BREAK_HOLD_PRE')))&~has).any():
            raise ValueError('BHR positive state without selected zone')
        for _,r in df[has].iterrows():
            event_ts=int(r.retest_ts);tf=str(r.level_tf)
            if tf not in TF_MS or int(r.zone_formed_closed_ts)>=event_ts:raise ValueError('Noncausal BHR level')
            stamps=[int(r[k]) for k in ('first_break_closed_ts','break_confirm_closed_ts',
                'hold_confirm_closed_ts','pre_retest_closed_ts','retest_hold_confirm_closed_ts') if str(r[k])!='']
            if any(t>=event_ts for t in stamps):raise ValueError('Noncausal BHR sequence')
            if r.first_break_closed_ts!='':
                if not (int(r.zone_formed_closed_ts)<int(r.first_break_closed_ts)<int(r.break_confirm_closed_ts)<event_ts):
                    raise ValueError('Invalid BHR breakout order')
                if int(r.responses_before_break)<2:raise ValueError('BHR missing pre-break antecedents')
            if r.bhr_group_pre in ('BULLISH_BREAK_HOLD_PRE','BEARISH_BREAK_HOLD_PRE',
                'BULLISH_BREAK_HOLD_RETEST_PRE','BEARISH_BREAK_HOLD_RETEST_PRE'):
                if str(r.hold_confirm_closed_ts)=='' or r.last_pre_close_side!='GOOD':
                    raise ValueError('BHR hold missing or not currently accepted')
                if not(int(r.break_confirm_closed_ts)<int(r.hold_confirm_closed_ts)<event_ts):
                    raise ValueError('BHR hold timestamp order wrong')
            if r.bhr_group_pre in ('BULLISH_BREAK_HOLD_RETEST_PRE','BEARISH_BREAK_HOLD_RETEST_PRE'):
                if not all(str(r[k])!='' for k in ('hold_confirm_closed_ts','pre_retest_closed_ts','retest_hold_confirm_closed_ts')):
                    raise ValueError('BHR advertised without full proof')
                if not(int(r.break_confirm_closed_ts)<int(r.pre_retest_closed_ts)<int(r.retest_hold_confirm_closed_ts)<event_ts):
                    raise ValueError('Invalid BHR retest/hold ordering')
        frames.append(df)
    allrows=pd.concat(frames,ignore_index=True) if frames else pd.DataFrame(columns=FIELDS)
    atom_csv(out/'bhr_labels_pre.csv',allrows[list(FIELDS)])
    result=dict(success=True,version=VERSION,events=len(allrows),symbols=len(frames),
        groups={g:int((allrows.bhr_group_pre==g).sum()) for g in GROUPS},
        by_side={s:{g:int(((allrows.side==s)&(allrows.bhr_group_pre==g)).sum())
            for g in GROUPS if ((allrows.side==s)&(allrows.bhr_group_pre==g)).any()} for s in ('LONG','SHORT')},
        no_outcomes_used=True,input_event_sha=audit['outputs_sha256']['event_labels.csv'],
        input_zone_sha=audit['outputs_sha256']['candidate_levels.csv'],
        labels_sha=sha(out/'bhr_labels_pre.csv'),folder=str(out))
    atom_json(out/'audit.json',result)
    return result


def report(runfolder,max_events,first_touch):
    from audit_persistent_first_touch import summarize
    out=outdir(runfolder,max_events)
    audited=json.loads((out/'audit.json').read_text())
    if not audited.get('success') or audited.get('labels_sha')!=sha(out/'bhr_labels_pre.csv'):
        raise ValueError('BHR labels require successful frozen audit')
    labels=pd.read_csv(out/'bhr_labels_pre.csv',dtype={'event_key':str},keep_default_na=False)
    joined=v2.join_first_touch(labels,first_touch)
    matched=joined[joined._merge.eq('both')]
    payload=dict(version=VERSION,total_pre=len(labels),first_touch_matched=len(matched),
        first_touch_missing=len(labels)-len(matched),
        missing_by_group={g:int(((joined.bhr_group_pre==g)&joined._merge.ne('both')).sum()) for g in GROUPS},
        by_side={str(h):{side:{g:summarize(matched[(matched.side_pre==side)&(matched.bhr_group_pre==g)],h)
            for g in GROUPS} for side in ('LONG','SHORT')} for h in (180,360)},
        note='Exploratory retrospective TP-first, not net P&L or a holdout')
    atom_json(out/'first_touch_report.json',payload)
    print('BHR_FIRST_TOUCH_JOIN',json.dumps({k:payload[k] for k in ('total_pre','first_touch_matched','first_touch_missing','missing_by_group')}))
    for h in ('180','360'):
        print(f'=== BHR {h}m TP1% / SL1% ===')
        for side in ('LONG','SHORT'):
            for g in GROUPS:
                d=payload['by_side'][h][side][g]
                if d['events']:
                    print(f"{side:5} | {g:40} n={d['events']:5} TP={d['TP']:4} SL={d['SL']:4} NO_HIT={d['NO_HIT']:4} TP-first={d['tp_rate_resolved']}")
    print('BHR_REPORT_PATH',out/'first_touch_report.json')
    return payload


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path('/home/multiservices-1m'))
    p.add_argument('--run-folder',type=Path)
    p.add_argument('--max-events',type=int,default=0)
    p.add_argument('--first-touch',type=Path)
    acts=p.add_mutually_exclusive_group(required=True)
    for act in ('plan','run','audit','report'):acts.add_argument('--'+act,action='store_true')
    args=p.parse_args()
    if args.max_events<0:raise SystemExit('--max-events must be >=0')
    rf=args.run_folder or target(args.root)
    if args.plan:
        e,z,a=prepare(rf,args.max_events)
        near_z=z[pd.to_numeric(z.distance_pre_atr,errors='coerce').le(DISTANCE_ATR+1e-9)]
        near_high=set(near_z.loc[near_z.origin_kind.eq('HIGH'),'event_key'])
        near_low=set(near_z.loc[near_z.origin_kind.eq('LOW'),'event_key'])
        target_kind_near=sum((ev['event_key'] in near_high if ev['side']=='LONG' else ev['event_key'] in near_low)
             for ev in e[['event_key','side']].to_dict('records'))
        print(json.dumps(dict(version=VERSION,events=len(e),symbols=e.symbol.nunique(),
            near_directional_candidates=target_kind_near,min_responses_before_break=2,
            distance_atr=DISTANCE_ATR,HTF=['15m','30m'],
            output=str(outdir(rf,args.max_events)),no_outcomes_read=True,no_network_calls=True),indent=2))
    elif args.run:run(args.root,rf,args.max_events)
    elif args.audit:print(json.dumps(audit_run(rf,args.max_events),indent=2))
    elif args.report:report(rf,args.max_events,args.first_touch or
        args.root/'reports/reaction_lab/reaction_first_touch_lab_v1/first_touch_scenarios.csv')
if __name__=='__main__':main()
