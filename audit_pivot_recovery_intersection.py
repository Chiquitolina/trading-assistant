#!/usr/bin/env python3
"""Read-only, audited intersection: LONG recovered support (3+ PRE visits) x pivot mixed S/R PRE.

Three separate stages: --run freezes 2x2 PRE membership without outcomes;
--audit verifies its hashes and membership; --report separately joins First Touch.
Never recalculates pivots, horizontal levels, or outcomes and never touches LIVE.
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import numpy as np
import pandas as pd

VERSION = 'pivot-recovery-intersection-pre-v1.0'
BASE = Path('reports/reaction_lab/reaction_persistent_horizontal_v1/runs/all-live_0_bbab543f0ffe')
V3 = BASE/'level_lifecycle_v3/all_live'
V4 = V3/'recovery_robustness_v4'
PIVOT = Path('reports/reaction_lab/pivot_historical_memory_pre_v1/all_live')
FT = Path('reports/reaction_lab/reaction_first_touch_lab_v1/first_touch_scenarios.csv')
OUT = Path('reports/reaction_lab/pivot_recovery_intersection_v1/all_live')
GROUPS = ('BOTH','RECOVERED_ONLY','MIXED_ONLY','NEITHER')
UNKNOWN_V3 = {'UNKNOWN_HISTORY','UNKNOWN_PATH'}
UNKNOWN_PIVOT = {'UNKNOWN_HISTORY','UNKNOWN_PIVOT'}
STATUS = {'TP_FIRST','SL_FIRST','NO_HIT','AMBIGUOUS','NOT_ENTERED'}
COMPARISONS = (
    ('BOTH','RECOVERED_ONLY'),  # does pivot memory add among recovered supports?
    ('MIXED_ONLY','NEITHER'),  # does pivot memory add without recovered-support feature?
    ('BOTH','MIXED_ONLY'),  # does recovered support add among mixed pivots?
    ('RECOVERED_ONLY','NEITHER'),
    ('BOTH','NEITHER'),
)


def digest(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()


def require(df,cols,name):
    missing=set(cols)-set(df.columns)
    if missing:raise ValueError(f'{name}: missing {sorted(missing)}')


def read_audit(p,version,events=6423):
    a=json.loads(Path(p).read_text(encoding='utf8'))
    if a.get('success') is not True or a.get('version')!=version or a.get('events',a.get('events_v3'))!=events:
        raise ValueError(f'Invalid successful {version} audit')
    return a


def json_write(path,obj):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.intersection_',suffix='.json',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf8') as f:
            json.dump(obj,f,ensure_ascii=False,indent=2,sort_keys=True,allow_nan=False)
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def csv_write(path,df):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.intersection_',suffix='.csv',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf8',newline='') as f:
            df.to_csv(f,index=False);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def load_sources(root):
    """All inputs independently verified against original PRE audits, no outcome access."""
    v1=root/BASE;v3=root/V3;v4=root/V4;pivot=root/PIVOT
    a1=read_audit(v1/'audit.json','persistent-horizontal-pre-v1.0')
    if a1.get('audit_rule')!='strict_response_close_before_retest_v1.1':
        raise ValueError('V1 is not strict causal v1.1')
    p_events=v1/'event_labels.csv';p_zones=v1/'candidate_levels.csv'
    eh=digest(p_events);zh=digest(p_zones)
    if a1.get('outputs_sha256')!={'event_labels.csv':eh,'candidate_levels.csv':zh}:
        raise ValueError('V1 frozen SHA mismatch')
    a3=read_audit(v3/'audit.json','persistent-level-lifecycle-pre-v3.0')
    p3=v3/'lifecycle_labels_pre.csv';h3=digest(p3)
    if a3.get('input_event_sha')!=eh or a3.get('input_zone_sha')!=zh or a3.get('output_labels_sha')!=h3 or a3.get('no_outcomes_used') is not True:
        raise ValueError('V3 provenance SHA mismatch')
    ap=read_audit(pivot/'audit.json','pivot-historical-memory-pre-v1.0')
    pp=pivot/'pivot_memory_labels_pre.csv';hp=digest(pp)
    provenance=ap.get('source_hashes',{})
    if ap.get('labels_sha')!=hp or ap.get('no_outcomes_used') is not True or provenance.get('v1_events_sha')!=eh or provenance.get('v1_zones_sha')!=zh or provenance.get('selected_events')!=6423:
        raise ValueError('Pivot PRE provenance SHA mismatch')
    a4=read_audit(v4/'audit.json','persistent-recovery-robustness-v4.0')
    p4=v4/'recovery_recency_pre.csv';h4=digest(p4)
    if a4.get('no_outcomes_used') is not True or a4.get('recovery_recency_sha')!=h4 or a4.get('source_hashes')!={'v1_event':eh,'v1_zone':zh,'v3_labels':h3} or a4.get('recency_complete')!=a4.get('n_long_recovered'):
        raise ValueError('V4 frozen recovery audit mismatch')
    event=pd.read_csv(p_events,dtype={'event_key':str},low_memory=False)
    v=pd.read_csv(p3,dtype={'event_key':str},low_memory=False)
    p=pd.read_csv(pp,dtype={'event_key':str},low_memory=False)
    for df,name,columns in ((event,'V1',['event_key','symbol','side','retest_ts','date_utc','observation_type']),
                            (v,'V3',['event_key','symbol','side','retest_ts','date_utc','v3_group_pre','support_visits']),
                            (p,'Pivot',['event_key','symbol','side','retest_ts','date_utc','memory_group_pre','observation_type'])):
        require(df,columns,name)
        if len(df)!=6423 or df.event_key.isna().any() or df.event_key.duplicated().any():
            raise ValueError(f'{name} missing or duplicate event keys')
    if not event.observation_type.eq('LIVE_OBSERVED').all() or not p.observation_type.eq('LIVE_OBSERVED').all():
        raise ValueError('Not all source events LIVE_OBSERVED')
    if set(event.event_key)!=set(v.event_key) or set(event.event_key)!=set(p.event_key):
        raise ValueError('V1/V3/Pivot cohorts differ')
    for df, au, field, name in ((v,a3,'v3_group_pre','V3'),(p,ap,'memory_group_pre','Pivot')):
        got={k:int(x) for k,x in df[field].value_counts().items()}
        expect={k:int(x) for k,x in au.get('groups',{}).items() if x}
        if got!=expect:raise ValueError(f'{name} category counts differ from audit')
    merged=event[['event_key','symbol','side','date_utc','retest_ts']].merge(
        v[['event_key','symbol','side','date_utc','retest_ts','v3_group_pre','support_visits']],
        on='event_key',how='left',validate='one_to_one',suffixes=('', '_v3'))
    merged=merged.merge(p[['event_key','symbol','side','date_utc','retest_ts','memory_group_pre']],
        on='event_key',how='left',validate='one_to_one',suffixes=('', '_pivot'))
    for name in ('v3','pivot'):
        for field in ('symbol','side','date_utc','retest_ts'):
            left=merged[field].astype(str)
            right=merged[f'{field}_{name}'].astype(str)
            if not left.eq(right).all():raise ValueError(f'{name} {field} identity mismatch')
    hashes={'v1_events':eh,'v1_zones':zh,'v3_labels':h3,'v4_recency':h4,'pivot_labels':hp}
    return merged,hashes


def classify_pre(merged):
    """Four mutually exclusive groups; unknowns excluded. No results or return values read."""
    df=merged.copy()
    require(df,['event_key','symbol','side','date_utc','retest_ts','v3_group_pre','support_visits','memory_group_pre'],'PRE')
    visits=pd.to_numeric(df.support_visits,errors='coerce')
    recovered=df.v3_group_pre.eq('RECOVERED_ORIGINAL_PRE') & df.side.eq('LONG')
    if visits[recovered].isna().any() or (~visits[recovered].ge(2)).any():
        raise ValueError('Recovered LONG must have two or more visits')
    rec3=recovered & visits.ge(3)
    mixed=df.memory_group_pre.eq('PRIOR_MIXED_SR')
    known=(~df.v3_group_pre.isin(UNKNOWN_V3)) & (~df.memory_group_pre.isin(UNKNOWN_PIVOT))
    if df.v3_group_pre.isna().any() or df.memory_group_pre.isna().any():raise ValueError('Missing PRE group')
    df['intersection_group_pre']='EXCLUDED_OTHER_SIDE'
    df.loc[df.side.eq('LONG')&~known,'intersection_group_pre']='EXCLUDED_UNKNOWN_PRE'
    eligible=df.side.eq('LONG')&known
    df.loc[eligible & rec3 & mixed,'intersection_group_pre']='BOTH'
    df.loc[eligible & rec3 & ~mixed,'intersection_group_pre']='RECOVERED_ONLY'
    df.loc[eligible & ~rec3 & mixed,'intersection_group_pre']='MIXED_ONLY'
    df.loc[eligible & ~rec3 & ~mixed,'intersection_group_pre']='NEITHER'
    df['recovered_3plus_pre']=rec3
    df['pivot_mixed_pre']=mixed
    cols=['event_key','symbol','side','date_utc','retest_ts','v3_group_pre','support_visits',
          'memory_group_pre','recovered_3plus_pre','pivot_mixed_pre','intersection_group_pre']
    return df[cols].copy()


def plan(root):
    merged,hashes=load_sources(root);labels=classify_pre(merged)
    counts={k:int(v) for k,v in labels.intersection_group_pre.value_counts().items()}
    return dict(version=VERSION,events=len(labels),long_events=int(labels.side.eq('LONG').sum()),
                group_counts=counts,pre_hashes=hashes,output=str(root/OUT),
                note='No First Touch read; PRE intersection only; 4 disjoint eligible LONG groups')


def run(root):
    merged,hashes=load_sources(root);df=classify_pre(merged)
    dest=root/OUT;file=dest/'intersection_labels_pre.csv';csv_write(file,df)
    result=dict(success=True,version=VERSION,events=len(df),long_events=int(df.side.eq('LONG').sum()),
                source_hashes=hashes,labels_sha=digest(file),no_outcomes_used=True,
                group_counts={k:int(v) for k,v in df.intersection_group_pre.value_counts().items()},folder=str(dest))
    json_write(dest/'audit.json',result)
    return result


def audit(root):
    merged,hashes=load_sources(root);dest=root/OUT
    a=read_audit(dest/'audit.json',VERSION)
    path=dest/'intersection_labels_pre.csv'
    if not a.get('no_outcomes_used') or a.get('source_hashes')!=hashes or a.get('labels_sha')!=digest(path):
        raise ValueError('Changed/not-audited intersection PRE labels')
    expected=classify_pre(merged)
    actual=pd.read_csv(path,dtype={'event_key':str},low_memory=False)
    if len(actual)!=6423 or actual.event_key.duplicated().any() or set(actual.event_key)!=set(expected.event_key):
        raise ValueError('Invalid intersection membership')
    for col in ('recovered_3plus_pre','pivot_mixed_pre'):
        # CSV bool roundtrip must not be coerced into strings other than True/False.
        if actual[col].dtype!='bool' or not expected.set_index('event_key')[col].equals(actual.set_index('event_key').reindex(expected.event_key)[col].set_axis(expected.event_key)):
            raise ValueError(f'Bad {col} PRE flag')
    ex=expected.sort_values('event_key').reset_index(drop=True)
    ac=actual.sort_values('event_key').reset_index(drop=True)
    if not ex.intersection_group_pre.equals(ac.intersection_group_pre):
        raise ValueError('Changed PRE intersection groups')
    counts={k:int(v) for k,v in actual.intersection_group_pre.value_counts().items()}
    if counts!=a.get('group_counts'):raise ValueError('Invalid intersection group counts')
    return a,actual


def ft_join(pre,ftpath):
    required=['event_key','symbol','side','observation_type','entry_mode','tp_pct','sl_pct','result_180m','result_360m']
    header=pd.read_csv(ftpath,nrows=0);require(header,required,'First Touch')
    ft=pd.read_csv(ftpath,usecols=required,dtype={'event_key':str},low_memory=False)
    ft=ft[(ft.observation_type=='LIVE_OBSERVED') & (ft.entry_mode=='OBSERVED_OPEN') &
          pd.to_numeric(ft.tp_pct,errors='coerce').sub(1.0).abs().lt(1e-9) &
          pd.to_numeric(ft.sl_pct,errors='coerce').sub(1.0).abs().lt(1e-9)].copy()
    if ft.event_key.isna().any() or ft.event_key.duplicated().any():raise ValueError('Duplicate First Touch scenarios')
    for col in ('result_180m','result_360m'):
        invalid=set(ft[col].dropna().astype(str))-STATUS
        if invalid:raise ValueError(f'Unexpected First Touch statuses {col}: {sorted(invalid)}')
    m=pre.merge(ft,on='event_key',how='left',indicator=True,validate='one_to_one',suffixes=('_pre','_ft'))
    ok=m._merge.eq('both')
    for col in ('side','symbol'):
        if not m.loc[ok,f'{col}_pre'].eq(m.loc[ok,f'{col}_ft']).all():
            raise ValueError(f'First Touch {col} mismatch')
    return m,ft


def stats(df,h):
    n=int(len(df));k=f'result_{h}m'
    counts=Counter(df[k].dropna().astype(str)) if n else Counter()
    tp=int(counts.get('TP_FIRST',0));sl=int(counts.get('SL_FIRST',0));res=tp+sl
    nh=int(counts.get('NO_HIT',0));amb=int(counts.get('AMBIGUOUS',0));not_entered=int(counts.get('NOT_ENTERED',0))
    other=n-tp-sl-nh-amb-not_entered
    return dict(events=n,TP=tp,SL=sl,NO_HIT=nh,AMBIGUOUS=amb,NOT_ENTERED=not_entered,OTHER=other,
                resolved=res,tp_first_resolved_pct=round(tp/res*100,3) if res else None,
                tp_all_pct=round(tp/n*100,3) if n else None)


def delta(df,a,b,h):
    sa=stats(df[df.intersection_group_pre.eq(a)],h)
    sb=stats(df[df.intersection_group_pre.eq(b)],h)
    if not sa['resolved'] or not sb['resolved']:return None
    return round(sa['TP']/sa['resolved']*100 - sb['TP']/sb['resolved']*100,4)


def cluster_bootstrap(df,col,contrast,h,rounds=1000,seed=20261023):
    """Pooled clusters across all four groups; preserves shared-day or shared-symbol shocks."""
    x=df[df[f'result_{h}m'].isin(('TP_FIRST','SL_FIRST'))].copy()
    ids=sorted(x[col].dropna().astype(str).unique().tolist())
    if len(ids)<5:return {'clusters':len(ids),'replicates':0,'ci95_pp':None}
    key=col;cache=x.groupby([key,'intersection_group_pre',f'result_{h}m']).size()
    matrix=np.zeros((len(ids),len(GROUPS),2),dtype=np.int64)
    for i,idx in enumerate(ids):
        for j,g in enumerate(GROUPS):
            matrix[i,j,0]=int(cache.get((idx,g,'TP_FIRST'),0))
            matrix[i,j,1]=int(cache.get((idx,g,'SL_FIRST'),0))
    rng=np.random.default_rng(seed)
    picks=rng.integers(0,len(ids),size=(rounds,len(ids)))
    sampled=matrix[picks].sum(axis=1)
    groupidx={v:i for i,v in enumerate(GROUPS)}
    def rate(g):
        z=sampled[:,groupidx[g],:];den=z.sum(axis=1)
        return np.where(den>0,z[:,0]/np.where(den>0,den,1),np.nan)
    a,b=contrast
    if a=='INTERACTION':
        values=100*(rate('BOTH')-rate('RECOVERED_ONLY')-rate('MIXED_ONLY')+rate('NEITHER'))
    else:values=100*(rate(a)-rate(b))
    values=values[np.isfinite(values)]
    if len(values)<max(50,rounds//2):ci=None
    else:ci=[round(float(t),3) for t in np.quantile(values,[.025,.975])]
    return dict(clusters=int(len(ids)),replicates=int(len(values)),ci95_pp=ci)


def within_strata(df,column,a,b,h,min_resolved_each=3):
    records=[]
    for value,part in df[df.intersection_group_pre.isin((a,b))].groupby(column,sort=True):
        sa=stats(part[part.intersection_group_pre.eq(a)],h)
        sb=stats(part[part.intersection_group_pre.eq(b)],h)
        na,nb=sa['resolved'],sb['resolved']
        if min(na,nb)<min_resolved_each:continue
        weight=na*nb/(na+nb)
        diff=100*(sa['TP']/na-sb['TP']/nb)
        records.append((weight,diff))
    return dict(strata_used=len(records),min_resolved_each=min_resolved_each,
                positive_strata=sum(d>0 for w,d in records),negative_strata=sum(d<0 for w,d in records),
                weighted_delta_pp=round(sum(w*d for w,d in records)/sum(w for w,d in records),3) if records else None)


def interaction(df,h):
    rates={g:stats(df[df.intersection_group_pre.eq(g)],h) for g in GROUPS}
    if any(z['resolved']==0 for z in rates.values()):return None
    return round((rates['BOTH']['TP']/rates['BOTH']['resolved']-
                  rates['RECOVERED_ONLY']['TP']/rates['RECOVERED_ONLY']['resolved']-
                  rates['MIXED_ONLY']['TP']/rates['MIXED_ONLY']['resolved']+
                  rates['NEITHER']['TP']/rates['NEITHER']['resolved'])*100,4)


def leave_one_out(df,col,a,b,h):
    subset=df[df.intersection_group_pre.isin((a,b))].copy()
    if subset.empty:return dict(clusters=0,min_pp=None,max_pp=None)
    ds=[]
    for name in subset[col].dropna().astype(str).unique():
        part=subset[~subset[col].astype(str).eq(name)]
        d=delta(part,a,b,h)
        if d is not None:ds.append(d)
    return dict(clusters=int(subset[col].nunique()),min_pp=round(min(ds),3) if ds else None,
                max_pp=round(max(ds),3) if ds else None)


def analyze(pre,ftpath,bootstrap):
    if not 100<=bootstrap<=5000:raise ValueError('bootstrap between 100 and 5000')
    merged,ft=ft_join(pre,ftpath)
    pre_eligible=merged[merged.intersection_group_pre.isin(GROUPS)].copy()
    known=pre_eligible[pre_eligible._merge.eq('both')].copy()
    known['day_utc']=pd.to_datetime(known.retest_ts,unit='ms',utc=True).dt.strftime('%Y-%m-%d')
    if not known.day_utc.eq(known.date_utc).all():raise ValueError('PRE date and retest_ts day differ')
    r=dict(version=VERSION,first_touch_sha=digest(ftpath),scenario='LIVE_OBSERVED OBSERVED_OPEN TP1 SL1',
           first_touch_filtered_rows=int(len(ft)),first_touch_matched_total=int(merged._merge.eq('both').sum()),
           first_touch_missing_total=int(merged._merge.ne('both').sum()),
           total_pre=int(len(merged)),long_pre=int(merged.side_pre.eq('LONG').sum()),
           excluded_unknown_pre=int(merged.intersection_group_pre.eq('EXCLUDED_UNKNOWN_PRE').sum()),
           out_of_scope_short=int(merged.intersection_group_pre.eq('EXCLUDED_OTHER_SIDE').sum()),
           pre_eligible=int(len(pre_eligible)),matched_eligible=int(len(known)),
           missing_eligible=int(len(pre_eligible)-len(known)),
           groups_pre={g:int(pre_eligible.intersection_group_pre.eq(g).sum()) for g in GROUPS},
           groups_matched={g:int(known.intersection_group_pre.eq(g).sum()) for g in GROUPS},
           missing_scenario_by_group={g:int((pre_eligible.intersection_group_pre.eq(g)&pre_eligible._merge.ne('both')).sum()) for g in GROUPS},
           overlap_pre={'recovered3':int(pre_eligible.recovered_3plus_pre.sum()),
                        'mixed':int(pre_eligible.pivot_mixed_pre.sum()),
                        'both':int(pre_eligible.intersection_group_pre.eq('BOTH').sum())},
           horizons={},cautions=[
              'Four mutually exclusive groups; unknown PRE history excluded rather than counted as NEITHER.',
              'Exploratory selected hypotheses from same October 2026 period; no independent holdout.',
              'TP-first among resolved is not TP probability over all signals, nor P&L.',
              'Cluster-by-symbol and cluster-by-day resampling done separately; not two-way inference.',
              'First Touch missing scenarios are excluded and shown, not classified as SL/NO_HIT.'])
    days=sorted(known.day_utc.unique());half=len(days)//2
    for h in (180,360):
        hh=str(h)
        bygroup={g:stats(known[known.intersection_group_pre.eq(g)],h) for g in GROUPS}
        compare={}
        for a,b in COMPARISONS:
            compare[f'{a}_vs_{b}']={
                'delta_tp_first_pp':delta(known,a,b,h),
                'cluster_symbol':cluster_bootstrap(known,'symbol_pre',(a,b),h,bootstrap),
                'cluster_day':cluster_bootstrap(known,'day_utc',(a,b),h,bootstrap),
                'leave_one_symbol_out':leave_one_out(known,'symbol_pre',a,b,h),
                'leave_one_day_out':leave_one_out(known,'day_utc',a,b,h),
                'within_day':within_strata(known,'day_utc',a,b,h),
                'within_symbol':within_strata(known,'symbol_pre',a,b,h)}
        interaction_value=interaction(known,h)
        r['horizons'][hh]={
            'groups':bygroup,'comparisons':compare,
            'interaction_difference_in_differences_pp':interaction_value,
            'interaction_cluster_symbol':cluster_bootstrap(known,'symbol_pre',('INTERACTION',''),h,bootstrap),
            'interaction_cluster_day':cluster_bootstrap(known,'day_utc',('INTERACTION',''),h,bootstrap),
            'early_late':{p:{'days':[datevals[0],datevals[-1]] if datevals else [],
                             'groups':{g:stats(known[known.day_utc.isin(datevals)&known.intersection_group_pre.eq(g)],h) for g in GROUPS}}
                          for p,datevals in (('early',days[:half]),('late',days[half:]))},
            'by_day':[{ 'day_utc':day,'groups':{g:stats(part[part.intersection_group_pre.eq(g)],h) for g in GROUPS}}
                      for day,part in known.groupby('day_utc',sort=True)],
            'by_symbol':[{ 'symbol':symbol,'groups':{g:stats(part[part.intersection_group_pre.eq(g)],h) for g in GROUPS}}
                         for symbol,part in known.groupby('symbol_pre',sort=True) if part.intersection_group_pre.isin(('BOTH','RECOVERED_ONLY','MIXED_ONLY')).any()],
        }
    return r,known


def report(root,ftpath,bootstrap):
    audit_data,pre=audit(root)
    result,known=analyze(pre,ftpath,bootstrap)
    result['input_pre_sha']=audit_data['labels_sha'];result['source_hashes']=audit_data['source_hashes']
    out=root/OUT
    json_write(out/'intersection_report.json',result)
    # Separate outcome-attached file explicitly named POST; PRE remains untouched.
    cols=['event_key','symbol_pre','side_pre','date_utc','retest_ts','intersection_group_pre',
          'recovered_3plus_pre','pivot_mixed_pre','result_180m','result_360m']
    csv_write(out/'intersection_first_touch_POST.csv',known[cols].sort_values('event_key'))
    daily=[];symbol_rows=[]
    for h,block in result['horizons'].items():
        for item in block['by_day']:
            for g,ss in item['groups'].items():daily.append(dict(horizon_min=int(h),day_utc=item['day_utc'],group=g,**ss))
        for item in block['by_symbol']:
            for g,ss in item['groups'].items():symbol_rows.append(dict(horizon_min=int(h),symbol=item['symbol'],group=g,**ss))
    csv_write(out/'daily_breakdown.csv',pd.DataFrame(daily))
    csv_write(out/'symbol_breakdown.csv',pd.DataFrame(symbol_rows))
    print('INTERSECTION_COVERAGE',json.dumps({k:result[k] for k in ('first_touch_filtered_rows','first_touch_matched_total','first_touch_missing_total','total_pre','long_pre','excluded_unknown_pre','out_of_scope_short','pre_eligible','matched_eligible','missing_eligible','groups_pre','groups_matched','missing_scenario_by_group','overlap_pre')},ensure_ascii=False))
    for h in ('180','360'):
        print(f'\n=== LONG PIVOT × RECOVERY {h}m / TP1 SL1 ===')
        for g in GROUPS:
            s=result['horizons'][h]['groups'][g]
            print(f'{g:18} n={s["events"]:4} TP={s["TP"]:4} SL={s["SL"]:4} NO_HIT={s["NO_HIT"]:4} TP-first={s["tp_first_resolved_pct"]} all={s["tp_all_pct"]}')
        for name,cmp in result['horizons'][h]['comparisons'].items():
            print('COMPARE',name,'delta_pp',cmp['delta_tp_first_pp'],
                  'CI_symbol',cmp['cluster_symbol']['ci95_pp'],
                  'CI_day',cmp['cluster_day']['ci95_pp'],
                  'leave1_symbol',cmp['leave_one_symbol_out']['min_pp'],cmp['leave_one_symbol_out']['max_pp'])
        print('INTERACTION',result['horizons'][h]['interaction_difference_in_differences_pp'],
              'CI_symbol',result['horizons'][h]['interaction_cluster_symbol']['ci95_pp'],
              'CI_day',result['horizons'][h]['interaction_cluster_day']['ci95_pp'])
        for half,item in result['horizons'][h]['early_late'].items():
            print('HALF',half,item['days'],{g:{'n':s['events'],'tp_first':s['tp_first_resolved_pct']} for g,s in item['groups'].items()})
    print('INTERSECTION_REPORT_PATH',out/'intersection_report.json')
    print('INTERSECTION_POST_PATH',out/'intersection_first_touch_POST.csv')
    print('CAUTION: exploratory same-period analysis, no holdout; no portfolio profitability inference.')
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path('/home/multiservices-1m'))
    p.add_argument('--first-touch',type=Path)
    p.add_argument('--bootstrap',type=int,default=1000)
    actions=p.add_mutually_exclusive_group(required=True)
    actions.add_argument('--plan',action='store_true')
    actions.add_argument('--run',action='store_true')
    actions.add_argument('--audit',action='store_true')
    actions.add_argument('--report',action='store_true')
    a=p.parse_args();root=a.root.resolve()
    if a.plan:print(json.dumps(plan(root),ensure_ascii=False,indent=2))
    elif a.run:print('INTERSECTION_PRE_DONE',json.dumps(run(root),ensure_ascii=False))
    elif a.audit:print('INTERSECTION_PRE_AUDIT',json.dumps(audit(root)[0],ensure_ascii=False))
    else:report(root,a.first_touch or root/FT,a.bootstrap)


if __name__=='__main__':main()
