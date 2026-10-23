#!/usr/bin/env python3
"""Offline V3 PRE horizontal-level lifecycle: closed HTF candles, no outcomes in labels.

Supports a sampled pilot, resumable per-symbol processing, an immutable-input audit,
and a separate optional First Touch report. No Binance or LIVE write paths.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import tempfile

import numpy as np
import pandas as pd

import audit_persistent_pattern_roles as v2

VERSION='persistent-level-lifecycle-pre-v3.0'
TF_MS={'15m':900000,'30m':1800000}
NEAR_ATR=.50
CONFIRM_CLOSES=2
FOLDERS='level_lifecycle_v3'
GROUPS=['FLIP_CONFIRMED_SUSTAINED_PRE','DEFENDED_CONTINUOUS_PRE',
        'RECOVERED_ORIGINAL_PRE','NEAR_NO_CONFIRMED_PATTERN_PRE',
        'NO_PERSISTENT_LEVEL_NEAR_PRE','UNKNOWN_HISTORY','UNKNOWN_PATH']
STATES=['INTACT_ORIGINAL','RECOVERED_ORIGINAL','OPPOSITE_FLIP_SUSTAINED',
        'OPPOSITE_CONFIRMED_NO_RETEST','PENDING_BREAK','PENDING_RECOVERY',
        'INDETERMINATE_PRE','UNKNOWN_PATH']
FIELDS=['event_key','symbol','side','date_utc','retest_ts','group_pre','v3_group_pre',
        'support_zone_id','support_state_pre','support_tf','support_age_hours','support_visits',
        'flip_zone_id','flip_state_pre','flip_tf','flip_age_hours','flip_visits',
        'support_wrong_confirmations','flip_wrong_confirmations',
        'support_retest_closed_ts','flip_retest_closed_ts','reason']


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1<<20),b''):h.update(b)
    return h.hexdigest()


def _atomic_csv(path, frame):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.lifecycle_',suffix='.csv',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='') as f:
            frame.to_csv(f,index=False)
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def _atomic_json(path, obj):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.lifecycle_',suffix='.json',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(obj,f,indent=2,sort_keys=True)
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def _state_summary(state, confirmations, retest_ts=None):
    return dict(state=state,wrong_confirmations=int(confirmations),
                flip_retest_closed_ts=retest_ts)


def evaluate_zone(frame, z, event_ts):
    """Use strictly *completed* HTF bars, close-confirmed loss/recovery.

    An opposite-side flip must include: 2 HTF closes through the level,
    a later HTF wick intersection with the zone, and 2 later opposite-side
    closes. This is an offline structural label, not a trading signal.
    """
    tf=str(z['timeframe']); step=TF_MS[tf]
    event_ts=int(event_ts); formation=int(z['formation_closed_ts'])
    lo=float(z['zone_low']);hi=float(z['zone_high']);kind=str(z['origin_kind'])
    if kind not in ('LOW','HIGH') or not (0<lo<=hi) or formation>=event_ts:
        return _state_summary('UNKNOWN_PATH',0)
    if frame is None or frame.empty:return _state_summary('UNKNOWN_PATH',0)
    df=frame.copy()
    needed={'timestamp','high','low','close'}
    if not needed.issubset(df):return _state_summary('UNKNOWN_PATH',0)
    # Requirement is strict earlier-than: even a bar that closes exactly on
    # the first REACTION minute is NOT evidence for a pre-existing transition.
    df=df[(df.timestamp+step>=formation)&(df.timestamp+step<event_ts)].sort_values('timestamp')
    if df.empty or int(df.timestamp.iloc[0])+step>formation:
        return _state_summary('UNKNOWN_PATH',0)
    nums=df[['timestamp','high','low','close']].to_numpy(dtype='float64')
    if not np.isfinite(nums).all() or (nums[:,1]<nums[:,2]).any():
        return _state_summary('UNKNOWN_PATH',0)
    if len(df)>1 and not np.all(np.diff(df.timestamp.to_numpy(dtype='int64'))==step):
        return _state_summary('UNKNOWN_PATH',0)
    # LOW starts as support (good side=above); HIGH starts as resistance.
    orig=lambda x: x>hi if kind=='LOW' else x<lo
    opp=lambda x: x<lo if kind=='LOW' else x>hi
    side='ORIGINAL'; o_run=0;b_run=0; confirmed_wrong=0
    retest=None; post_retest_wrong_run=0; flip_proved=False
    last_close_side='INSIDE'
    for r in df.itertuples(index=False):
        ts=int(r.timestamp)+step;c=float(r.close)
        is_orig=bool(orig(c));is_opp=bool(opp(c))
        last_close_side=('ORIGINAL' if is_orig else 'OPPOSITE' if is_opp else 'INSIDE')
        o_run=(o_run+1 if is_orig else 0)
        b_run=(b_run+1 if is_opp else 0)
        if side=='ORIGINAL':
            if b_run>=CONFIRM_CLOSES:
                side='OPPOSITE';confirmed_wrong+=1
                retest=None;post_retest_wrong_run=0;flip_proved=False
                # If bar itself overlaps the level, it is a breakout bar,
                # not a *later* independent retest.
                broken_at=ts
        else:
            if o_run>=CONFIRM_CLOSES:
                side='ORIGINAL';retest=None;post_retest_wrong_run=0;flip_proved=False
                continue
            if retest is None and ts>broken_at and r.high>=lo and r.low<=hi:
                retest=ts;post_retest_wrong_run=0
            elif retest is not None and ts>retest:
                post_retest_wrong_run=(post_retest_wrong_run+1 if is_opp else 0)
                if post_retest_wrong_run>=CONFIRM_CLOSES:
                    flip_proved=True
    if side=='OPPOSITE':
        if flip_proved and last_close_side=='OPPOSITE':
            state='OPPOSITE_FLIP_SUSTAINED'
        elif last_close_side=='ORIGINAL':state='PENDING_RECOVERY'
        else:state='OPPOSITE_CONFIRMED_NO_RETEST'
    elif last_close_side=='OPPOSITE':state='PENDING_BREAK'
    elif confirmed_wrong>0:state='RECOVERED_ORIGINAL'
    elif last_close_side in ('ORIGINAL','INSIDE'):state='INTACT_ORIGINAL'
    else:state='INDETERMINATE_PRE'
    return _state_summary(state,confirmed_wrong,retest)


def deterministic_zones(events,zones):
    """Select 1 historic support candidate and 1 resistance candidate by PRE rank.

    Candidate choice is frozen BEFORE lifecycle states or outcome inspection.
    Avoid selecting levels based on the most favorable lifecycle result.
    """
    x=zones.copy()
    x['distance_pre_atr']=pd.to_numeric(x.distance_pre_atr,errors='raise')
    x=x[x.distance_pre_atr<=NEAR_ATR+1e-9].copy()
    x['age_hours']=pd.to_numeric(x.age_bars,errors='raise') * x.timeframe.map(v2.TF_HOURS)
    x=x.sort_values(['event_key','origin_kind','distance_pre_atr','confirmed_responses',
                     'age_hours','timeframe','zone_id'],
                    ascending=[True,True,True,False,False,True,True],kind='stable')
    x=x.drop_duplicates(['event_key','origin_kind'])
    return x


def label_one(event,zones_for_event,frames):
    key=event['event_key'];s=event['side'];group=event['group_pre']
    row=dict(event_key=key,symbol=event['symbol'],side=s,date_utc=event['date_utc'],
        retest_ts=int(event['retest_ts']),group_pre=group,v3_group_pre='UNKNOWN_PATH',
        support_zone_id='',support_state_pre='',support_tf='',support_age_hours='',support_visits='',
        flip_zone_id='',flip_state_pre='',flip_tf='',flip_age_hours='',flip_visits='',
        support_wrong_confirmations='',flip_wrong_confirmations='',
        support_retest_closed_ts='',flip_retest_closed_ts='',reason='')
    if group==v2.UNKNOWN:
        row.update(v3_group_pre='UNKNOWN_HISTORY',reason='ORIGINAL_HTF_COVERAGE_INCOMPLETE');return row
    if group==v2.FAR:
        row.update(v3_group_pre='NO_PERSISTENT_LEVEL_NEAR_PRE',reason='NO_NEAR_PERSISTENT_LEVEL');return row
    if group!=v2.NEAR:raise ValueError('Unexpected PRE group')
    needed={'support':('LOW' if s=='LONG' else 'HIGH'),
            'flip':('HIGH' if s=='LONG' else 'LOW')}
    states={}
    for role,kind in needed.items():
        matches=[z for z in zones_for_event if z['origin_kind']==kind]
        if not matches:continue
        z=matches[0];out=evaluate_zone(frames.get(z['timeframe']),z,event['retest_ts'])
        states[role]=out['state']
        row.update({f'{role}_zone_id':z['zone_id'],f'{role}_state_pre':out['state'],
                    f'{role}_tf':z['timeframe'],f'{role}_age_hours':z['age_hours'],
                    f'{role}_visits':int(z['independent_visits']),
                    f'{role}_wrong_confirmations':out['wrong_confirmations'],
                    f'{role}_retest_closed_ts':out['flip_retest_closed_ts'] or ''})
    if 'UNKNOWN_PATH' in states.values():
        row.update(v3_group_pre='UNKNOWN_PATH',reason='SELECTED_ZONE_PATH_INCOMPLETE');return row
    if states.get('flip')=='OPPOSITE_FLIP_SUSTAINED':
        row.update(v3_group_pre='FLIP_CONFIRMED_SUSTAINED_PRE',reason='TWO_WRONG_CLOSES_THEN_RETEST_THEN_TWO_WRONG_CLOSES')
    elif states.get('support')=='INTACT_ORIGINAL':
        row.update(v3_group_pre='DEFENDED_CONTINUOUS_PRE',reason='NO_CONFIRMED_WRONG_SIDE_CLOSE_PAIR')
    elif states.get('support')=='RECOVERED_ORIGINAL':
        row.update(v3_group_pre='RECOVERED_ORIGINAL_PRE',reason='LOST_THEN_REGAINED_BEFORE_REACTION')
    else:
        row.update(v3_group_pre='NEAR_NO_CONFIRMED_PATTERN_PRE',reason='NEAR_BUT_NO_QUALIFIED_PRE_LIFECYCLE')
    return row


def prepare(runfolder,max_events=0):
    events,zones,audit=v2.read_pre(runfolder)
    # Deterministic selected subset for bounded pilot, independent of outcomes.
    events=events.sort_values(['retest_ts','event_key'],kind='stable')
    if max_events>0:events=events.head(max_events)
    zones=zones[zones.event_key.isin(events.event_key)]
    shortlist=deterministic_zones(events,zones)
    return events,shortlist,audit


def _configuration(audit,max_events):
    return dict(version=VERSION,strict_close_before_event=True,break_confirm_closes=2,
                flip_retest_confirm_closes=2,candidate_near_atr=.5,max_events=max_events,
                input_event_sha=audit['outputs_sha256']['event_labels.csv'],
                input_zone_sha=audit['outputs_sha256']['candidate_levels.csv'],
                note='No outcome data accessed for classifications; exploratory static study')


def run(root,runfolder,max_events=0):
    import reaction_structural_level_atlas as atlas
    import reaction_compression_breakout_retest_v4 as v4
    events,zones,audit=prepare(runfolder,max_events)
    conf=_configuration(audit,max_events);out=Path(runfolder)/FOLDERS/('pilot_'+str(max_events) if max_events else 'all_live')
    out.mkdir(parents=True,exist_ok=True)
    cfgpath=out/'config.json'
    if cfgpath.is_file() and json.loads(cfgpath.read_text())!=conf:
        raise RuntimeError('Refusing to mix V3 runs with different inputs or rules')
    _atomic_json(cfgpath,conf)
    events_by_symbol={s:group for s,group in events.groupby('symbol',sort=True)}
    zone_records={key:grp.to_dict('records') for key,grp in zones.groupby('event_key',sort=False)}
    src=root/atlas.lineage.ROOT_REL/atlas.lineage.CTX_NAME
    histpath=root/atlas.lineage.ROOT_REL/atlas.lineage.HISTORY_NAME
    hist=atlas.lineage.ro(histpath) if histpath.is_file() else None
    done=0;computed=0;resumed=0
    try:
        with closing(atlas.lineage.ro(src)) as ctx:
            for symbol,part in events_by_symbol.items():
                fn=out/'shards'/f'{symbol}.csv';stamp=out/'shards'/f'{symbol}.json'
                keys=part.event_key.astype(str).tolist()
                if fn.is_file() and stamp.is_file():
                    prior=json.loads(stamp.read_text())
                    if prior.get('keys')==keys and prior.get('sha256')==sha(fn):
                        resumed+=1;done+=1;continue
                selected=part[part.group_pre==v2.NEAR]
                frames={}
                if not selected.empty:
                    df15,df30,_=v4._merge_tf(ctx,hist,symbol)
                    frames={'15m':df15.reset_index(),'30m':df30.reset_index()}
                rows=[label_one(e,zone_records.get(e['event_key'],[]),frames)
                      for e in part.to_dict('records')]
                _atomic_csv(fn,pd.DataFrame(rows,columns=FIELDS))
                _atomic_json(stamp,{'keys':keys,'sha256':sha(fn)})
                computed+=1;done+=1
                if done%20==0 or done==len(events_by_symbol):
                    print(f'V3_PROGRESS {done}/{len(events_by_symbol)} symbols new={computed} resumed={resumed}',flush=True)
    finally:
        if hist is not None:hist.close()
    result=verify(runfolder,max_events)
    print('V3_PRE_DONE',json.dumps(result,ensure_ascii=False),flush=True)
    return result


def verify(runfolder,max_events=0):
    events,zones,audit=prepare(runfolder,max_events)
    out=Path(runfolder)/FOLDERS/('pilot_'+str(max_events) if max_events else 'all_live')
    config=json.loads((out/'config.json').read_text())
    if config!=_configuration(audit,max_events):raise ValueError('V3 config/input hash mismatch')
    allrows=[]
    for symbol,part in events.groupby('symbol',sort=True):
        fn=out/'shards'/f'{symbol}.csv';stamp=out/'shards'/f'{symbol}.json'
        meta=json.loads(stamp.read_text())
        if sha(fn)!=meta['sha256'] or meta['keys']!=part.event_key.tolist():
            raise ValueError(f'V3 shard invalid: {symbol}')
        rows=pd.read_csv(fn,dtype={'event_key':str},keep_default_na=False)
        if rows.event_key.tolist()!=meta['keys'] or len(rows)!=len(part):
            raise ValueError(f'V3 shard keys mismatch: {symbol}')
        if not rows.v3_group_pre.isin(GROUPS).all():raise ValueError('Unexpected V3 group')
        if (rows.loc[rows.v3_group_pre=='UNKNOWN_HISTORY','group_pre']!=v2.UNKNOWN).any():
            raise ValueError('V3 unknown-history provenance mismatch')
        allrows.append(rows)
    joined=pd.concat(allrows,ignore_index=True) if allrows else pd.DataFrame(columns=FIELDS)
    _atomic_csv(out/'lifecycle_labels_pre.csv',joined[FIELDS])
    result=dict(success=True,version=VERSION,events=len(joined),symbols=len(allrows),
        groups={g:int((joined.v3_group_pre==g).sum()) for g in GROUPS},
        no_outcomes_used=True,input_event_sha=audit['outputs_sha256']['event_labels.csv'],
        input_zone_sha=audit['outputs_sha256']['candidate_levels.csv'],
        output_labels_sha=sha(out/'lifecycle_labels_pre.csv'),folder=str(out))
    _atomic_json(out/'audit.json',result)
    return result


def first_touch_report(runfolder,max_events=0,ft_path=None):
    from audit_persistent_first_touch import summarize
    out=Path(runfolder)/FOLDERS/('pilot_'+str(max_events) if max_events else 'all_live')
    audit=json.loads((out/'audit.json').read_text())
    if not audit.get('success') or audit['output_labels_sha']!=sha(out/'lifecycle_labels_pre.csv'):
        raise ValueError('V3 PRE labels not audited or changed')
    labels=pd.read_csv(out/'lifecycle_labels_pre.csv',dtype={'event_key':str},low_memory=False)
    root=Path('/home/multiservices-1m') if ft_path is None else None
    if ft_path is None:ft_path=root/'reports/reaction_lab/reaction_first_touch_lab_v1/first_touch_scenarios.csv'
    merged=v2.join_first_touch(labels,ft_path)
    matched=merged[merged._merge.eq('both')]
    payload=dict(total=len(labels),matched=len(matched),missing=len(labels)-len(matched),
        missing_by_group={g:int(((merged.v3_group_pre==g)&(merged._merge!='both')).sum()) for g in GROUPS},
        summary={str(h):{g:summarize(matched[matched.v3_group_pre==g],h) for g in GROUPS}
                 for h in (180,360)},
        by_side={str(h):{side:{g:summarize(matched[(matched.side_pre==side)&(matched.v3_group_pre==g)],h)
                               for g in GROUPS} for side in ('LONG','SHORT')} for h in (180,360)},
        note='Retrospective correlated sample; no placebo/holdout; TP-first is not P&L')
    # Extra exploratory stratification, so we don't lose the original visits
    # hypothesis while studying structural lifecycle. Not used for selection.
    payload['by_visit_bucket']={}
    for h in (180,360):
        payload['by_visit_bucket'][str(h)]={}
        for side in ('LONG','SHORT'):
            payload['by_visit_bucket'][str(h)][side]={}
            for group,field in (('DEFENDED_CONTINUOUS_PRE','support_visits'),
                                ('FLIP_CONFIRMED_SUSTAINED_PRE','flip_visits')):
                frame=matched[(matched.side_pre==side)&(matched.v3_group_pre==group)].copy()
                visits=pd.to_numeric(frame[field],errors='coerce')
                payload['by_visit_bucket'][str(h)][side][group]={
                    bucket:summarize(frame[(visits==2) if bucket=='2' else (visits>=3)],h)
                    for bucket in ('2','3+')
                }
    _atomic_json(out/'first_touch_report.json',payload)
    print('V3_FIRST_TOUCH_JOIN',json.dumps({k:payload[k] for k in ('total','matched','missing','missing_by_group')},ensure_ascii=False))
    for h in ('180','360'):
        print(f'V3 {h}m TP1/SL1')
        for side in ('LONG','SHORT'):
            for g in GROUPS:
                d=payload['by_side'][h][side][g]
                if d['events']:
                    print(f"{side:5} | {g:37} | events={d['events']:5} TP={d['TP']:4} SL={d['SL']:4} NO_HIT={d['NO_HIT']:4} TP-first={d['tp_rate_resolved']}")
    print('V3_REPORT_PATH',out/'first_touch_report.json')
    return payload


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path('/home/multiservices-1m'))
    p.add_argument('--run-folder',type=Path)
    p.add_argument('--first-touch',type=Path)
    p.add_argument('--max-events',type=int,default=0)
    act=p.add_mutually_exclusive_group(required=True)
    for name in ('plan','run','audit','report'):act.add_argument('--'+name,action='store_true')
    a=p.parse_args()
    if a.max_events<0:raise SystemExit('max-events must be >= 0')
    runfolder=a.run_folder or a.root/'reports/reaction_lab/reaction_persistent_horizontal_v1/runs/all-live_0_bbab543f0ffe'
    if a.plan:
        events,zones,_=prepare(runfolder,a.max_events)
        print(json.dumps({'version':VERSION,'events':len(events),'symbols':events.symbol.nunique(),
           'near_events':int(events.group_pre.eq(v2.NEAR).sum()),
           'selected_candidate_zones':len(zones),'near_atr':NEAR_ATR,'max_events':a.max_events,
           'origin_readonly':str(runfolder),'no_first_touch_for_classification':True},indent=2))
    elif a.run:run(a.root,runfolder,a.max_events)
    elif a.audit:print(json.dumps(verify(runfolder,a.max_events),indent=2))
    elif a.report:first_touch_report(runfolder,a.max_events,a.first_touch)
if __name__=='__main__':main()
