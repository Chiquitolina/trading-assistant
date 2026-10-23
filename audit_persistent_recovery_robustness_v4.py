#!/usr/bin/env python3
"""Offline V4 robustness for LONG on PRE-confirmed recovered supports.

Does NOT redefine V1 or V3 labels, alter stored inputs, touch LIVE, or
fetch exchange data. PRE recovery timestamps are reconstructed separately
from closed HTF history. First Touch is joined only by --report.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

import numpy as np
import pandas as pd

import audit_persistent_level_lifecycle_v3 as v3
import audit_persistent_pattern_roles as v2
from audit_persistent_first_touch import summarize, require_cols

VERSION = 'persistent-recovery-robustness-v4.0'
RECOVERED = 'RECOVERED_ORIGINAL_PRE'
DEFENDED = 'DEFENDED_CONTINUOUS_PRE'
NO_LEVEL = 'NO_PERSISTENT_LEVEL_NEAR_PRE'
TARGET_SIDE = 'LONG'
FOLDER = 'recovery_robustness_v4'
FIELDS = ['event_key','symbol','retest_ts','support_zone_id','support_tf',
          'last_recovery_close_ts','hours_since_recovery_pre','recency_pre_status']
RECENCY_BUCKETS = ('<=6h','6-24h','>24h','UNKNOWN')
GROUPS = ('RECOVERED_3PLUS','RECOVERED_2','DEFENDED_3PLUS','DEFENDED_2','NO_LEVEL')


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1<<20),b''):h.update(block)
    return h.hexdigest()


def write_csv(path,df):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.recovery_v4_',suffix='.csv',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf8',newline='') as f:
            df.to_csv(f,index=False);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.remove(tmp)


def write_json(path,x):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.recovery_v4_',suffix='.json',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf8') as f:
            json.dump(x,f,indent=2,sort_keys=True,allow_nan=False)
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.remove(tmp)


def read_inputs(run):
    """Fail closed for changed V1/V3 files; no First Touch read."""
    run=Path(run)
    base_events, base_zones, base_audit=v2.read_pre(run)
    output=run/'level_lifecycle_v3/all_live'
    audit_path=output/'audit.json'
    audit=json.loads(audit_path.read_text())
    labels_path=output/'lifecycle_labels_pre.csv'
    if not audit.get('success') or audit.get('version')!=v3.VERSION:
        raise ValueError('V3 audit missing or wrong version')
    if audit.get('events')!=len(base_events) or audit.get('input_event_sha')!=base_audit['outputs_sha256']['event_labels.csv'] or audit.get('input_zone_sha')!=base_audit['outputs_sha256']['candidate_levels.csv']:
        raise ValueError('V3 inputs do not match V1 audited dataset')
    if sha(labels_path)!=audit.get('output_labels_sha'):
        raise ValueError('V3 lifecycle PRE labels SHA mismatch')
    labels=pd.read_csv(labels_path,dtype={'event_key':str},low_memory=False)
    require_cols(labels,['event_key','symbol','side','retest_ts','v3_group_pre','support_zone_id','support_tf','support_visits','support_state_pre','date_utc'], 'V3 labels')
    if len(labels)!=len(base_events) or labels.event_key.duplicated().any() or labels.event_key.isna().any():
        raise ValueError('Invalid V3 label count/keys')
    if set(labels.event_key)!=set(base_events.event_key):
        raise ValueError('V3 and V1 event keys differ')
    main=labels[(labels.side==TARGET_SIDE)&(labels.v3_group_pre==RECOVERED)].copy()
    if main.empty:
        raise ValueError('No LONG recovered levels in V3')
    if main.support_zone_id.isna().any() or main.support_zone_id.astype(str).str.len().eq(0).any():
        raise ValueError('Recovered LONG without a support zone ID')
    chosen=main[['event_key','support_zone_id']].merge(
        base_zones,left_on=['event_key','support_zone_id'],right_on=['event_key','zone_id'],
        how='left',validate='one_to_one',indicator=True)
    if not chosen._merge.eq('both').all():
        raise ValueError('A selected V3 recovered support cannot be matched to frozen V1 candidate')
    if not chosen.origin_kind.eq('LOW').all():
        raise ValueError('Recovered LONG support must originate from LOW')
    chk=main[['event_key','support_tf']].merge(chosen[['event_key','timeframe']],
        on='event_key',validate='one_to_one')
    if not chk.support_tf.eq(chk.timeframe).all():
        raise ValueError('Frozen level timeframe differs from V3 support TF')
    return labels,chosen,{'v1_event':base_audit['outputs_sha256']['event_labels.csv'],
                          'v1_zone':base_audit['outputs_sha256']['candidate_levels.csv'],
                          'v3_labels':audit['output_labels_sha']}


def last_recovery_closed(frame, zone, event_ts):
    """Find last confirmed recovery of original LOW support using only closed PRE HTF bars.

    Reproduce V3's ORIGINAL/OPPOSITE 2-close transitions. Check the V3 state
    independently before reporting a timestamp. A missing/mutated path fails closed.
    """
    if zone['origin_kind']!='LOW':raise ValueError('Recovery clock only for LONG LOW supports')
    state=v3.evaluate_zone(frame,zone,event_ts)['state']
    if state!='RECOVERED_ORIGINAL':
        raise ValueError(f'PRE path mismatch vs frozen V3: {state}')
    step=v3.TF_MS[str(zone['timeframe'])]
    formation=int(zone['formation_closed_ts'])
    bars=frame[(frame.timestamp+step>=formation)&(frame.timestamp+step<int(event_ts))].sort_values('timestamp')
    if len(bars)<4:raise ValueError('Too few PRE HTF bars for recovered support')
    side='ORIGINAL'; b_run=o_run=0;last=None
    for r in bars.itertuples(index=False):
        ts=int(r.timestamp)+step
        c=float(r.close)
        original=c>float(zone['zone_high']); opposite=c<float(zone['zone_low'])
        o_run=(o_run+1 if original else 0)
        b_run=(b_run+1 if opposite else 0)
        if side=='ORIGINAL' and b_run>=2:
            side='OPPOSITE'
        elif side=='OPPOSITE' and o_run>=2:
            side='ORIGINAL';last=ts
    if last is None or not formation<=last<int(event_ts):
        raise ValueError('V3 RECOVERED without strictly-PRE recovery timestamp')
    return int(last)


def build_recency_rows(part, zone_lookup, frames):
    rows=[]
    for e in part.to_dict('records'):
        key=e['event_key'];zone=zone_lookup[key]
        timeframe=str(zone['timeframe']);fr=frames.get(timeframe)
        stamp=last_recovery_closed(fr,zone,int(e['retest_ts']))
        hours=(int(e['retest_ts'])-stamp)/3_600_000.
        if not math.isfinite(hours) or hours<=0:raise ValueError('Invalid recovery recency')
        rows.append(dict(event_key=key,symbol=e['symbol'],retest_ts=int(e['retest_ts']),
                         support_zone_id=e['support_zone_id'],support_tf=timeframe,
                         last_recovery_close_ts=stamp,hours_since_recovery_pre=round(hours,6),
                         recency_pre_status='COMPLETE'))
    return pd.DataFrame(rows,columns=FIELDS)


def config(inputs):
    return dict(version=VERSION,source_hashes=inputs,strict_recovery_before_retest=True,
                no_outcomes_used=True,scope='ALL_LONG_RECOVERED_IN_FROZEN_V3',
                recency_buckets_hours=[6,24],bootstrap_seed=20261023)


def path_for(run):return Path(run)/'level_lifecycle_v3/all_live'/FOLDER


def run_pre(root,run):
    """Read-only source histories; write only new V4 shards. Restart-safe."""
    import reaction_structural_level_atlas as atlas
    import reaction_compression_breakout_retest_v4 as v4
    labels,zones,hashes=read_inputs(run)
    out=path_for(run);out.mkdir(parents=True,exist_ok=True)
    c=config(hashes)
    if (out/'config.json').exists() and json.loads((out/'config.json').read_text())!=c:
        raise ValueError('Different V4 input/config already exists in this folder')
    write_json(out/'config.json',c)
    target=labels[(labels.side==TARGET_SIDE)&(labels.v3_group_pre==RECOVERED)].copy()
    lookup=dict(zip(zones.event_key,zones.to_dict('records')))
    ctxpath=Path(root)/atlas.lineage.ROOT_REL/atlas.lineage.CTX_NAME
    histpath=Path(root)/atlas.lineage.ROOT_REL/atlas.lineage.HISTORY_NAME
    hist=atlas.lineage.ro(histpath) if histpath.is_file() else None
    symbols=list(target.groupby('symbol',sort=True))
    new=resumed=0
    try:
        with closing(atlas.lineage.ro(ctxpath)) as ctx:
            for idx,(symbol,part) in enumerate(symbols,1):
                keys=part.event_key.tolist()
                fn=out/'shards'/f'{symbol}.csv';meta=out/'shards'/f'{symbol}.json'
                if fn.is_file() and meta.is_file():
                    x=json.loads(meta.read_text())
                    if x.get('keys')==keys and x.get('sha256')==sha(fn):
                        resumed+=1;continue
                df15,df30,_=v4._merge_tf(ctx,hist,symbol)
                frames={'15m':df15.reset_index(),'30m':df30.reset_index()}
                shard=build_recency_rows(part,lookup,frames)
                write_csv(fn,shard)
                write_json(meta,dict(keys=keys,sha256=sha(fn)))
                new+=1
                if idx%20==0 or idx==len(symbols):
                    print(f'ROBUST_V4_PROGRESS {idx}/{len(symbols)} symbols new={new} resumed={resumed}',flush=True)
    finally:
        if hist is not None:hist.close()
    result=audit_pre(run)
    print('ROBUST_V4_PRE_DONE',json.dumps(result,ensure_ascii=False),flush=True)
    return result


def audit_pre(run):
    labels,zones,hashes=read_inputs(run)
    out=path_for(run)
    if json.loads((out/'config.json').read_text())!=config(hashes):
        raise ValueError('V4 config mismatch')
    target=labels[(labels.side==TARGET_SIDE)&(labels.v3_group_pre==RECOVERED)].copy()
    rows=[]
    for symbol,part in target.groupby('symbol',sort=True):
        fn=out/'shards'/f'{symbol}.csv';meta=out/'shards'/f'{symbol}.json'
        stamp=json.loads(meta.read_text())
        if stamp.get('keys')!=part.event_key.tolist() or stamp.get('sha256')!=sha(fn):
            raise ValueError(f'Stale V4 shard for {symbol}')
        one=pd.read_csv(fn,dtype={'event_key':str})
        if len(one)!=len(part) or one.event_key.tolist()!=part.event_key.tolist():
            raise ValueError(f'V4 shard event membership mismatch: {symbol}')
        if not one.recency_pre_status.eq('COMPLETE').all():raise ValueError('Unconfirmed recovery clock')
        m=one.merge(part[['event_key','retest_ts','support_zone_id','support_tf']],on='event_key',
                    suffixes=('', '_frozen'),validate='one_to_one')
        for field in ('retest_ts','support_zone_id','support_tf'):
            if not m[field].astype(str).eq(m[f'{field}_frozen'].astype(str)).all():
                raise ValueError(f'Modified V4 {field}')
        if not ((one.last_recovery_close_ts.astype('int64')<one.retest_ts.astype('int64'))&
                (one.hours_since_recovery_pre>0)).all():
            raise ValueError('Non-causal V4 recovery clock')
        rows.append(one)
    rec=pd.concat(rows,ignore_index=True) if rows else pd.DataFrame(columns=FIELDS)
    if rec.event_key.duplicated().any() or len(rec)!=len(target):raise ValueError('V4 recovery row count incorrect')
    write_csv(out/'recovery_recency_pre.csv',rec[FIELDS])
    r=dict(success=True,version=VERSION,events_v3=len(labels),n_long_recovered=len(target),
           recency_complete=len(rec),no_outcomes_used=True,
           source_hashes=hashes,recovery_recency_sha=sha(out/'recovery_recency_pre.csv'),folder=str(out))
    write_json(out/'audit.json',r)
    return r


def add_group(labels):
    x=labels.copy()
    x['visits']=pd.to_numeric(x.support_visits,errors='coerce')
    x['analysis_group']='EXCLUDED'
    eligible=x.side.eq(TARGET_SIDE)
    rec=eligible & x.v3_group_pre.eq(RECOVERED)
    defended=eligible & x.v3_group_pre.eq(DEFENDED)
    x.loc[rec&x.visits.ge(3),'analysis_group']='RECOVERED_3PLUS'
    x.loc[rec&x.visits.eq(2),'analysis_group']='RECOVERED_2'
    x.loc[defended&x.visits.ge(3),'analysis_group']='DEFENDED_3PLUS'
    x.loc[defended&x.visits.eq(2),'analysis_group']='DEFENDED_2'
    x.loc[eligible&x.v3_group_pre.eq(NO_LEVEL),'analysis_group']='NO_LEVEL'
    if x.loc[rec|defended,'analysis_group'].eq('EXCLUDED').any():
        raise ValueError('LONG recovered/defended without valid 2+ visits')
    return x


def recency_bin(hours):
    if pd.isna(hours):return 'UNKNOWN'
    v=float(hours)
    return '<=6h' if v<=6 else '6-24h' if v<=24 else '>24h'


def _metrics(rows,h):
    return summarize(rows,h)


def _delta(a,b,h):
    x=_metrics(a,h);y=_metrics(b,h)
    if not x['resolved'] or not y['resolved']:return None
    return round(x['TP']/x['resolved']*100-y['TP']/y['resolved']*100,4)


def _bootstrap(data, grouping, a='RECOVERED_3PLUS', b='DEFENDED_3PLUS', h=180, rounds=1000, seed=20261023):
    """Cluster resampling, clusters across BOTH arms, preserving dependence within cluster."""
    x=data[data.analysis_group.isin([a,b])].copy()
    x=x[x[f'result_{h}m'].isin(['TP_FIRST','SL_FIRST'])].copy()
    ids=x[grouping].astype(str).unique()
    if len(ids)<2:return dict(cluster=grouping,clusters=len(ids),replicates=0,ci95_pp=None)
    pt=x.pivot_table(index=grouping,columns=['analysis_group',f'result_{h}m'],values='event_key',aggfunc='count',fill_value=0)
    pt=pt.reindex(ids).fillna(0)
    matrix=np.array([[pt.get((arm,result),pd.Series(0,index=pt.index)).get(k,0)
        for arm in (a,b) for result in ('TP_FIRST','SL_FIRST')] for k in ids],dtype=np.float64)
    rng=np.random.default_rng(seed)
    diffs=[]
    for _ in range(rounds):
        choice=rng.integers(0,len(ids),len(ids))
        t=matrix[choice].sum(axis=0)
        na=t[0]+t[1];nb=t[2]+t[3]
        if na>0 and nb>0:diffs.append(100*(t[0]/na-t[2]/nb))
    return dict(cluster=grouping,clusters=len(ids),replicates=len(diffs),
        ci95_pp=[round(float(v),3) for v in np.quantile(diffs,[.025,.975])] if len(diffs)>=max(50,rounds//2) else None)


def _leave_one(data, column, a, b, h):
    main=data[data.analysis_group.isin([a,b])]
    base=_delta(main[main.analysis_group==a],main[main.analysis_group==b],h)
    changes=[]
    for key in sorted(main[column].dropna().astype(str).unique()):
        filtered=main[main[column].astype(str)!=key]
        d=_delta(filtered[filtered.analysis_group==a],filtered[filtered.analysis_group==b],h)
        if d is not None:changes.append(dict(excluded=key,delta_pp=d,
            events_removed=int((main[column].astype(str)==key).sum())))
    return dict(full_delta_pp=base,observations=len(main),
                min_delta_pp=min((r['delta_pp'] for r in changes),default=None),
                max_delta_pp=max((r['delta_pp'] for r in changes),default=None),
                top_influential=sorted(changes,key=lambda q:abs(q['delta_pp']-(base or 0)),reverse=True)[:8])


def within_strata(data, column, h=180, a='RECOVERED_3PLUS', b='DEFENDED_3PLUS', min_resolved=3):
    """Descriptive matched-period/asset risk difference, not causal adjustment.

    Include only strata with >= min_resolved observed TP-or-SL for BOTH arms.
    Weight each contrast by harmonic effective resolved sample size, so one
    crowded stratum cannot silently dominate without appearing in diagnostics.
    """
    frame=data[(data.analysis_group.isin([a,b])) &
            (data[f'result_{h}m'].isin(('TP_FIRST','SL_FIRST')))]
    summary=[]
    for key,chunk in frame.groupby(column,sort=True):
        ra=chunk[chunk.analysis_group==a];rb=chunk[chunk.analysis_group==b]
        na=len(ra);nb=len(rb)
        if min(na,nb)<min_resolved:continue
        wa=float(na*nb/(na+nb))
        delta=100*(ra[f'result_{h}m'].eq('TP_FIRST').mean()-rb[f'result_{h}m'].eq('TP_FIRST').mean())
        summary.append(dict(stratum=str(key),recovered_resolved=na,control_resolved=nb,
                            difference_pp=round(delta,3),weight=wa))
    if not summary:
        return dict(strata_used=0,within_strata_delta_pp=None,
                    min_resolved_each=min_resolved,details=[])
    avg=sum(s['difference_pp']*s['weight'] for s in summary)/sum(s['weight'] for s in summary)
    return dict(strata_used=len(summary),within_strata_delta_pp=round(avg,3),
                min_resolved_each=min_resolved,
                positive_strata=sum(s['difference_pp']>0 for s in summary),
                negative_strata=sum(s['difference_pp']<0 for s in summary),
                details=summary)


def report(run,ft_path,rounds=1000):
    if not 100<=rounds<=10000:raise ValueError('Use 100 to 10000 bootstrap replicates')
    labels,_,hashes=read_inputs(run)
    out=path_for(run);audit=json.loads((out/'audit.json').read_text())
    recpath=out/'recovery_recency_pre.csv'
    if not audit.get('success') or audit.get('version')!=VERSION or audit.get('source_hashes')!=hashes or audit.get('recovery_recency_sha')!=sha(recpath):
        raise ValueError('V4 RECENCY PRE audit/hash mismatch')
    rec=pd.read_csv(recpath,dtype={'event_key':str})
    if len(rec)!=audit['n_long_recovered'] or rec.event_key.duplicated().any():raise ValueError('Invalid PRE recovery recency rows')
    x=add_group(labels)
    x=x.merge(rec[['event_key','hours_since_recovery_pre','last_recovery_close_ts']],on='event_key',how='left',validate='one_to_one')
    expected=x.v3_group_pre.eq(RECOVERED)&x.side.eq(TARGET_SIDE)
    if x.loc[expected,'hours_since_recovery_pre'].isna().any() or x.loc[~expected,'hours_since_recovery_pre'].notna().any():
        raise ValueError('V4 recovery recency coverage mismatch')
    # Outcomes join is strictly downstream of PRE feature generation.
    merged=v2.join_first_touch(x,ft_path)
    merged['day_utc']=pd.to_datetime(merged.retest_ts,unit='ms',utc=True).dt.strftime('%Y-%m-%d')
    merged['recency_bucket_pre']=merged.hours_since_recovery_pre.map(recency_bin)
    eligible=merged[merged.analysis_group.isin(GROUPS)].copy()
    known=eligible[eligible._merge.eq('both')].copy()
    missing={g:int(((eligible.analysis_group==g)&(eligible._merge!='both')).sum()) for g in GROUPS}
    r=dict(version=VERSION,scope='LIVE_OBSERVED/LONG/fixed V3 PRE groups',
           source_hashes=hashes,recency_pre_sha=audit['recovery_recency_sha'],
           scenario='OBSERVED_OPEN TP1 SL1 First Touch 180/360m',
           first_touch_sha=sha(ft_path),
           joined_total=int(merged._merge.eq('both').sum()),
           included_long_pre={g:int((eligible.analysis_group==g).sum()) for g in GROUPS},
           included_long_with_first_touch={g:int((known.analysis_group==g).sum()) for g in GROUPS},
           missing_first_touch_by_group=missing,
           summary={},daily={},by_symbol={},time_halves={},
           recovery_recency={},comparison={},cautions=[
           'Exploratory retrospective hypothesis selected after seeing these outcomes: no independent holdout.',
           'TP-first percentages are conditional on resolved trades and are NOT portfolio profitability.',
           'Cluster bootstraps by symbol and UTC day separately are descriptive, not two-way dependence adjustment.',
           '6h/24h recency buckets are descriptive only; do not optimize a threshold on this cohort.'])
    for h in (180,360):
        key=str(h);r['summary'][key]={g:_metrics(known[known.analysis_group==g],h) for g in GROUPS}
        r['daily'][key]=[];r['by_symbol'][key]=[]
        for day in sorted(known.day_utc.dropna().unique()):
            record=dict(day_utc=str(day))
            for g in ('RECOVERED_3PLUS','DEFENDED_3PLUS','NO_LEVEL'):
                record[g]=_metrics(known[(known.day_utc==day)&(known.analysis_group==g)],h)
            r['daily'][key].append(record)
        for symbol in sorted(known.symbol_pre.dropna().unique()):
            record=dict(symbol=symbol)
            for g in ('RECOVERED_3PLUS','DEFENDED_3PLUS','NO_LEVEL'):
                record[g]=_metrics(known[(known.symbol_pre==symbol)&(known.analysis_group==g)],h)
            if any(record[g]['events'] for g in ('RECOVERED_3PLUS','DEFENDED_3PLUS')):
                r['by_symbol'][key].append(record)
        days=sorted(known.day_utc.dropna().unique());mid=len(days)//2
        halves=[('early',days[:mid]),('late',days[mid:])]
        r['time_halves'][key]={}
        for name,daylist in halves:
            part=known[known.day_utc.isin(daylist)]
            r['time_halves'][key][name]={
                'days':[daylist[0],daylist[-1]] if daylist else [],
                'groups':{g:_metrics(part[part.analysis_group==g],h) for g in ('RECOVERED_3PLUS','DEFENDED_3PLUS','NO_LEVEL')},
                'delta_vs_defended_pp':_delta(part[part.analysis_group=='RECOVERED_3PLUS'],part[part.analysis_group=='DEFENDED_3PLUS'],h)}
        r['recovery_recency'][key]={}
        for bucket in RECENCY_BUCKETS:
            group=known[(known.analysis_group=='RECOVERED_3PLUS')&(known.recency_bucket_pre==bucket)]
            r['recovery_recency'][key][bucket]=_metrics(group,h)
        r['comparison'][key]={}
        for cmp in ('DEFENDED_3PLUS','NO_LEVEL'):
            a=known[known.analysis_group=='RECOVERED_3PLUS'];b=known[known.analysis_group==cmp]
            core=dict(delta_tp_first_pp=_delta(a,b,h),
                source_a_events=len(a),source_b_events=len(b),
                cluster_bootstrap_symbol=_bootstrap(known,'symbol_pre',b=cmp,h=h,rounds=rounds),
                cluster_bootstrap_day=_bootstrap(known,'day_utc',b=cmp,h=h,rounds=rounds),
                leave_one_symbol_out=_leave_one(known,'symbol_pre','RECOVERED_3PLUS',cmp,h),
                leave_one_day_out=_leave_one(known,'day_utc','RECOVERED_3PLUS',cmp,h),
                within_day=within_strata(known,'day_utc',h,b=cmp),
                within_symbol=within_strata(known,'symbol_pre',h,b=cmp))
            r['comparison'][key][f'RECOVERED_3PLUS_vs_{cmp}']=core
    report_path=out/'robustness_report.json';write_json(report_path,r)
    daily_rows=[]
    for h,items in r['daily'].items():
        for item in items:
            for g in ('RECOVERED_3PLUS','DEFENDED_3PLUS','NO_LEVEL'):
                daily_rows.append(dict(horizon_min=int(h),day_utc=item['day_utc'],group=g,**item[g]))
    write_csv(out/'daily_breakdown.csv',pd.DataFrame(daily_rows))
    print('V4_INPUTS',json.dumps({'version':VERSION,'joined_first_touch':r['joined_total'],'missing_by_group':missing},ensure_ascii=False))
    for h in ('180','360'):
        print(f'=== LONG ROBUSTNESS {h}m TP1 / SL1 ===')
        for g in GROUPS:
            x=r['summary'][h][g]
            print(f"{g:18} events={x['events']:4} TP={x['TP']:4} SL={x['SL']:4} NO_HIT={x['NO_HIT']:4} TP-first={x['tp_rate_resolved']}")
        for name in (f'RECOVERED_3PLUS_vs_DEFENDED_3PLUS',f'RECOVERED_3PLUS_vs_NO_LEVEL'):
            x=r['comparison'][h][name]
            print('COMPARE',name,'delta_pp',x['delta_tp_first_pp'],
                  'cluster_CI_symbol',x['cluster_bootstrap_symbol']['ci95_pp'],
                  'cluster_CI_day',x['cluster_bootstrap_day']['ci95_pp'],
                  'leave1_symbol',x['leave_one_symbol_out']['min_delta_pp'],x['leave_one_symbol_out']['max_delta_pp'],
                  'within_day_pp',x['within_day']['within_strata_delta_pp'],
                  'within_day_N',x['within_day']['strata_used'])
        print('HALVES',json.dumps(r['time_halves'][h],ensure_ascii=False))
        print('RECENCY',json.dumps(r['recovery_recency'][h],ensure_ascii=False))
    print('V4_REPORT',str(report_path))
    print('CAUTION: No new period/holdout. Descriptive robustness only; not a validated trading edge.')
    return r


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path('/home/multiservices-1m'))
    p.add_argument('--run-folder',type=Path)
    p.add_argument('--first-touch',type=Path)
    p.add_argument('--bootstrap',type=int,default=1000)
    actions=p.add_mutually_exclusive_group(required=True)
    for a in ('plan','run','audit','report'):actions.add_argument('--'+a,action='store_true')
    args=p.parse_args()
    runfolder=args.run_folder or args.root/'reports/reaction_lab/reaction_persistent_horizontal_v1/runs/all-live_0_bbab543f0ffe'
    if args.plan:
        labels,zones,hashes=read_inputs(runfolder)
        x=add_group(labels)
        print(json.dumps(dict(version=VERSION,total_events=len(labels),
           long_recovered=int((labels.side.eq('LONG')&labels.v3_group_pre.eq(RECOVERED)).sum()),
           symbols_to_read=labels.loc[labels.side.eq('LONG')&labels.v3_group_pre.eq(RECOVERED),'symbol'].nunique(),
           frozen_groups=x.analysis_group.value_counts().to_dict(),
           input_sha=hashes,destination=str(path_for(runfolder)),
           source_readonly=True,no_outcomes_for_recency=True),indent=2))
    elif args.run:run_pre(args.root,runfolder)
    elif args.audit:print(json.dumps(audit_pre(runfolder),indent=2))
    elif args.report:report(runfolder,args.first_touch or args.root/'reports/reaction_lab/reaction_first_touch_lab_v1/first_touch_scenarios.csv',args.bootstrap)
if __name__=='__main__':main()
