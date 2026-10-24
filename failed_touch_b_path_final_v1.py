#!/usr/bin/env python3
"""Final read-only 1m path audit of the *frozen* B_RETURN5_POS_T15 holdout.

No network, no LIVE, no retrospective optimization. Reproduces the 88 deduped B
signals from all 50 frozen validation manifests and reads only verified 1m OHLC.
Measures potential favorable/adverse excursions in 180m, plus which TP was
reached BEFORE the original SL1.5% (including same-candle ambiguity).

This is a diagnostic on an inspected validation cohort, NOT a new out-of-sample
strategy validation. The 207 reserved symbols remain untouched.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import csv
import json
import math
from pathlib import Path
from shutil import disk_usage
import statistics
import sys

import failed_touch_causal_2pct_v1 as core
import failed_touch_causal_validation_50_v1 as validation

VERSION = 'failed-touch-b-final-path-audit-v1.0'
RULE_NAME = 'B_RETURN5_POS_T15'
HORIZON = 180
POST_RECLAIM_HORIZON = 60
LEVELS = (0.5, 1.0, 1.5, 2.0)
STOP = 1.5
EXPECTED_COUNT = 88
EXPECTED_SYMBOLS = 40
EXPECTED_TRADES = {'TP_FIRST': 31, 'SL_FIRST': 39, 'TIMEOUT': 18}

FIELDS = (
    'event_key','symbol','side','cluster_key','first_touch_ts','depth2_delay_min',
    'signal_ts','entry_ts','entry_price','pivot_price','prior_trade_outcome',
    'prior_structural_outcome','prior_trade_gross_pct','path_bars',
    'mfe_180_pct','mae_180_pct','mfe_until_original_exit_pct',
    'mae_until_original_exit_pct','first_favorable_0_5_bar',
    'first_favorable_1_bar','first_favorable_1_5_bar','first_favorable_2_bar',
    'first_adverse_1_bar','first_adverse_1_5_bar',
    'tp0_5_vs_sl1_5','tp0_5_sl1_5_gross_pct',
    'tp1_vs_sl1_5','tp1_sl1_5_gross_pct',
    'tp1_5_vs_sl1_5','tp1_5_sl1_5_gross_pct',
    'tp2_vs_sl1_5','tp2_sl1_5_gross_pct',
    'pivot_reclaim_bar','pivot_reclaim_entry_to_pivot_pct',
    'pivot_relost_within_entry_180','first_pivot_reloss_bar',
    'sl_first_after_reclaim','post_reclaim_entry_ts',
    'post_reclaim_next_open','post_reclaim_60m_mfe_pct',
    'post_reclaim_60m_mae_pct','post_reclaim_60m_reloss',
    'post_reclaim_60m_complete',
)


def fmt(value):
    if value is None:return ''
    if isinstance(value,float):
        if not math.isfinite(value):raise ValueError('Nonfinite calculated value')
        return round(value,7)
    return value


def tsint(value):
    return int(float(value))


def numeric(value):
    return core.fnum(value)


def label_0_1(x):
    return ('%g' % x).replace('.','_')


def exact_select(root):
    """Read all 50 source tables to preserve dedup BEFORE applying rule B."""
    spec,complete,issues=validation.verified_completion(root)
    if issues or len(complete)!=50:
        raise RuntimeError(f'Validation 50/50 must be intact: complete={len(complete)} issues={issues[:3]}')
    _,cache,oldout,dev,study,_=validation.get_paths(root)
    if len(spec['validation_symbols'])!=50 or len(spec['reserved_unselected_symbols'])!=207:
        raise RuntimeError('Frozen validation cohort changed')
    snapshots=[]
    for sym in complete:
        records=core.read_csv(study/sym/'snapshot_features_outcomes.csv')
        snapshots.extend(x for x in records if x['checkpoint_status']=='ACTIVE' and
                         int(x['checkpoint_min'])==15)
    dedup=validation.deduplicate(snapshots)
    rule=dict(validation.RULES[RULE_NAME])
    selected=sorted((r for r in dedup if validation.eligible(r,rule)),
                    key=lambda r:(r['symbol'],tsint(r['entry_ts']),r['event_key']))
    if len(selected)!=EXPECTED_COUNT or len(set(r['event_key'] for r in selected))!=EXPECTED_COUNT:
        raise RuntimeError(f'Selection no longer reproduces original 88: {len(selected)}')
    if len(set(r['cluster_key'] for r in selected))!=EXPECTED_COUNT:
        raise RuntimeError('Duplicate touch cluster in B selection')
    if len(set(r['symbol'] for r in selected))!=EXPECTED_SYMBOLS:
        raise RuntimeError('Selected symbol count differs from frozen original')
    if Counter(r['trade_outcome'] for r in selected)!=Counter(EXPECTED_TRADES):
        raise RuntimeError('Frozen trade labels do not reproduce the audited 31/39/18')
    if set(r['symbol'] for r in selected)-set(spec['validation_symbols']):
        raise RuntimeError('Development or reserved symbol contaminated the holdout')
    return spec,study,cache,oldout,selected


def first_hit_level(favorable,adverse,goal,sl=STOP):
    """First TP versus SL in bar-index order. Same bar is intrabar ambiguous."""
    fi=next((i for i,v in enumerate(favorable) if v>=goal-1e-9),None)
    si=next((i for i,v in enumerate(adverse) if v>=sl-1e-9),None)
    if fi is not None and (si is None or fi<si):return 'TP_FIRST',fi
    if si is not None and (fi is None or si<fi):return 'SL_FIRST',si
    if si is not None and fi==si:return 'SL_AMBIGUOUS',si
    return 'TIMEOUT',None


def evaluate_path(frame,source):
    """Compute only from next-open entry through 180 consecutive candles."""
    import numpy as np
    ts=frame.timestamp.astype('int64').to_numpy()
    if len(ts)<2 or not np.all(np.diff(ts)==60000):
        raise RuntimeError('1m source has gaps; stop instead of filling')
    sym=source['symbol'];side=source['side']
    if side not in ('LONG','SHORT'):raise RuntimeError('Unexpected side')
    entry_ts=tsint(source['entry_ts']); signal_ts=tsint(source['signal_ts'])
    if entry_ts-signal_ts!=60000:raise RuntimeError('Noncausal entry timing '+source['event_key'])
    j=int(np.searchsorted(ts,entry_ts))
    if j>=len(ts) or int(ts[j])!=entry_ts:raise RuntimeError('Entry 1m missing '+sym)
    p=float(source['entry_price']);pivot=float(source['pivot_price'])
    if p<=0 or pivot<=0 or abs(float(frame.iloc[j].open)-p)>max(1e-12,p*1e-7):
        raise RuntimeError('Stored entry does not match verified 1m OPEN '+sym)
    end=j+HORIZON
    if end>len(frame):raise RuntimeError('Incomplete 180m future path '+sym)
    highs=frame.high.iloc[j:end].to_numpy(dtype=float)
    lows=frame.low.iloc[j:end].to_numpy(dtype=float)
    if side=='LONG':
        fav=np.maximum(0.,(highs/p-1)*100)
        adv=np.maximum(0.,(1-lows/p)*100)
    else:
        fav=np.maximum(0.,(1-lows/p)*100)
        adv=np.maximum(0.,(highs/p-1)*100)
    if np.any(~np.isfinite(fav)) or np.any(~np.isfinite(adv)):
        raise RuntimeError('Nonfinite OHLC path '+sym)

    outcome,trade_bars,gross=core.trade_path(frame,j,side,p,HORIZON,2.,STOP)
    struct,struct_bars=core.structural_path(frame,j,side,pivot,HORIZON)
    if outcome!=source['trade_outcome'] or struct!=source['structural_outcome']:
        raise RuntimeError('Baseline trade or structure differs from frozen validation '+source['event_key'])
    if abs(trade_bars-float(source['trade_bars']))>1e-8 or abs(struct_bars-float(source['structural_bars']))>1e-8:
        raise RuntimeError('Baseline 1m first-hit delay mismatch '+source['event_key'])
    if gross is not None and (numeric(source['trade_gross_pct']) is None or
                              abs(gross-float(source['trade_gross_pct']))>2e-6):
        raise RuntimeError('Baseline trade gross return mismatch '+source['event_key'])

    m={'event_key':source['event_key'],'symbol':sym,'side':side,'cluster_key':source['cluster_key'],
        'first_touch_ts':tsint(source['first_touch_ts']),
        'depth2_delay_min':int(source['depth2_delay_min']),
        'signal_ts':signal_ts,'entry_ts':entry_ts,'entry_price':p,'pivot_price':pivot,
        'prior_trade_outcome':outcome,'prior_structural_outcome':struct,
        'prior_trade_gross_pct':gross,'path_bars':HORIZON,
        'mfe_180_pct':float(np.max(fav)),'mae_180_pct':float(np.max(adv)),
        'mfe_until_original_exit_pct':float(np.max(fav[:trade_bars])),
        'mae_until_original_exit_pct':float(np.max(adv[:trade_bars])),
    }
    for goal in LEVELS:
        ix=next((i for i,v in enumerate(fav) if v>=goal-1e-9),None)
        m['first_favorable_'+label_0_1(goal)+'_bar']=(ix+1 if ix is not None else '')
        hit,_=first_hit_level(fav,adv,goal,STOP)
        if hit=='TP_FIRST':ret=goal
        elif hit=='SL_FIRST':ret=-STOP
        elif hit=='TIMEOUT':
            close=float(frame.iloc[end-1].close)
            ret=(close/p-1)*100*(1 if side=='LONG' else -1)
        else:ret=None
        m['tp'+label_0_1(goal)+'_vs_sl1_5']=hit
        m['tp'+label_0_1(goal)+'_sl1_5_gross_pct']=ret
    for goal in (1.,1.5):
        ix=next((i for i,v in enumerate(adv) if v>=goal-1e-9),None)
        m['first_adverse_'+label_0_1(goal)+'_bar']=ix+1 if ix is not None else ''
    if m['tp2_vs_sl1_5']!=outcome:
        raise RuntimeError('TP2/SL1.5 path does not match frozen replay '+source['event_key'])

    rec_idx=None
    for pos in range(j,end):
        close=float(frame.iloc[pos].close)
        if core.recovered(side,pivot,close):
            rec_idx=pos;break
    m['pivot_reclaim_bar']=(rec_idx-j+1) if rec_idx is not None else ''
    m['pivot_reclaim_entry_to_pivot_pct']=(pivot/p-1)*100*(1 if side=='LONG' else -1)
    m['pivot_relost_within_entry_180']=''
    m['first_pivot_reloss_bar']=''
    m['sl_first_after_reclaim']=''
    m['post_reclaim_entry_ts']=''
    m['post_reclaim_next_open']=''
    m['post_reclaim_60m_mfe_pct']=''
    m['post_reclaim_60m_mae_pct']=''
    m['post_reclaim_60m_reloss']=''
    m['post_reclaim_60m_complete']=''
    if rec_idx is not None:
        lost=next((i for i in range(rec_idx+1,end)
                   if not core.recovered(side,pivot,float(frame.iloc[i].close))),None)
        m['pivot_relost_within_entry_180']=lost is not None
        m['first_pivot_reloss_bar']=lost-j+1 if lost is not None else ''
        sl_idx=next((i for i,v in enumerate(adv) if v>=STOP-1e-9),None)
        m['sl_first_after_reclaim']=(outcome=='SL_FIRST' and sl_idx is not None
                                     and j+sl_idx>rec_idx)
        # Reclaim is only confirmed at candle CLOSE, so a hypothetical fresh
        # post-reclaim entry is at the NEXT candle OPEN, never in reclaim bar.
        post_idx=rec_idx+1
        if post_idx<len(frame) and int(ts[post_idx])-int(ts[rec_idx])==60000:
            price=float(frame.iloc[post_idx].open)
            finish=min(len(frame),post_idx+POST_RECLAIM_HORIZON)
            ph=frame.high.iloc[post_idx:finish].to_numpy(dtype=float)
            pl=frame.low.iloc[post_idx:finish].to_numpy(dtype=float)
            if side=='LONG':
                pf=np.maximum(0.,(ph/price-1)*100)
                pa=np.maximum(0.,(1-pl/price)*100)
            else:
                pf=np.maximum(0.,(1-pl/price)*100)
                pa=np.maximum(0.,(ph/price-1)*100)
            m['post_reclaim_entry_ts']=int(ts[post_idx]);m['post_reclaim_next_open']=price
            m['post_reclaim_60m_mfe_pct']=float(np.max(pf))
            m['post_reclaim_60m_mae_pct']=float(np.max(pa))
            m['post_reclaim_60m_reloss']=any(not core.recovered(side,pivot,float(c))
                                             for c in frame.close.iloc[post_idx:finish])
            m['post_reclaim_60m_complete']=(finish-post_idx==POST_RECLAIM_HORIZON)
    return {k:fmt(m.get(k,'')) for k in FIELDS}


def compute_symbol(root,study,out,sym,inputs,cache,oldout):
    import legacy_first_touch_replay_14d_v1 as replay
    a,b=replay.bounds(replay.iso_ms(replay.DEFAULT_START),replay.iso_ms(replay.DEFAULT_END))
    prior_sha=core.sha(study/sym/'manifest.json')
    orig_sha=core.sha(oldout/sym/'manifest.json')
    dest=out/sym
    manifest=dest/'manifest.json'
    if manifest.is_file():
        old=json.loads(manifest.read_text(encoding='utf-8'))
        if old.get('version')!=VERSION or old.get('input_manifest_sha256')!=prior_sha or old.get('old_sha')!=orig_sha:
            raise RuntimeError('Existing output/input mismatch '+sym)
        if core.sha(dest/'path_metrics.csv')!=old['hashes']['path_metrics.csv']:
            raise RuntimeError('Output hash mismatch '+sym)
        if old['event_keys']!=sorted(x['event_key'] for x in inputs):
            raise RuntimeError('Source selection changed '+sym)
        return 'EXISTING_VERIFIED',len(inputs)
    if dest.exists():raise RuntimeError('Unfinished output dir, manual inspection needed: '+str(dest))
    core.verify_output(study,sym,orig_sha)
    info,frame,_=replay.source_info(root,replay.DEFAULT_ORIGINAL,cache,sym,a,b,read=True)
    if frame is None:raise RuntimeError('Missing or nonverified 1m for '+sym)
    from reaction_compare_swing_detectors_v1 import assert_full_minute_coverage
    assert_full_minute_coverage(frame,a,b,sym)
    rows=[evaluate_path(frame,r) for r in inputs]
    dest.mkdir(parents=True)
    core.atomic_csv(dest/'path_metrics.csv',rows,FIELDS)
    hashes={'path_metrics.csv':core.sha(dest/'path_metrics.csv')}
    core.atomic_json(dest/'manifest.json',dict(version=VERSION,symbol=sym,
        input_manifest_sha256=prior_sha,old_sha=orig_sha,
        source=info,event_keys=sorted(r['event_key'] for r in inputs),count=len(rows),hashes=hashes))
    return 'COMPLETE',len(rows)


def verify_symbol(out,study,oldout,sym,inputs):
    dest=out/sym
    meta=json.loads((dest/'manifest.json').read_text(encoding='utf-8'))
    if meta['version']!=VERSION or meta['symbol']!=sym or meta['count']!=len(inputs):
        raise RuntimeError('Output manifest mismatch '+sym)
    if meta['input_manifest_sha256']!=core.sha(study/sym/'manifest.json') or meta['old_sha']!=core.sha(oldout/sym/'manifest.json'):
        raise RuntimeError('Source manifests have changed '+sym)
    if meta['event_keys']!=sorted(r['event_key'] for r in inputs):
        raise RuntimeError('Frozen B selection has changed '+sym)
    for name,digest in meta['hashes'].items():
        if core.sha(dest/name)!=digest:raise RuntimeError('Output hash differs from manifest '+sym+'/'+name)
    actual=core.read_csv(dest/'path_metrics.csv')
    if len(actual)!=len(inputs):raise RuntimeError('Output row count changed '+sym)
    return actual


def summarize(rows,side,group,value):
    s=[r for r in rows if (side=='ALL' or r['side']==side) and
       (group=='ALL' or r[group]==value)]
    vals=lambda k:[x for r in s if (x:=numeric(r[k])) is not None]
    def quant(data,p):
        if not data:return None
        return float(__import__('numpy').percentile(data,p))
    def metric(k):
        vv=vals(k)
        return dict(mean=fmt(statistics.mean(vv)) if vv else '',
                    median=fmt(statistics.median(vv)) if vv else '',
                    p25=fmt(quant(vv,25)) if vv else '',p75=fmt(quant(vv,75)) if vv else '')
    if not s:return None
    hits={}
    for goal in LEVELS:
        k='tp'+label_0_1(goal)+'_vs_sl1_5'
        h=Counter(r[k] for r in s)
        amount=[numeric(r['tp'+label_0_1(goal)+'_sl1_5_gross_pct']) for r in s
                if r[k]!='SL_AMBIGUOUS']
        amount=[x for x in amount if x is not None]
        hits[str(goal)]=dict(n=len(s),first_tp=h['TP_FIRST'],first_sl=h['SL_FIRST'],
                same_bar_ambiguous=h['SL_AMBIGUOUS'],timeouts=h['TIMEOUT'],
                gross_mean_excluding_ambiguous_pct=fmt(statistics.mean(amount)) if amount else '',
                net_cost_0_10_mean_pct=fmt(statistics.mean(amount)-0.1) if amount else '',
                net_cost_0_20_mean_pct=fmt(statistics.mean(amount)-0.2) if amount else '')
    return dict(side=side,group=group,value=value,n=len(s),symbols=len(set(r['symbol'] for r in s)),
        original=Counter(r['prior_trade_outcome'] for r in s),
        structural=Counter(r['prior_structural_outcome'] for r in s),
        mfe180=metric('mfe_180_pct'),mae180=metric('mae_180_pct'),
        mfe_until_exit=metric('mfe_until_original_exit_pct'),
        mae_until_exit=metric('mae_until_original_exit_pct'),
        original_gross=metric('prior_trade_gross_pct'),
        time_to_reclaim=metric('pivot_reclaim_bar'),
        time_to_favorable_0_5=metric('first_favorable_0_5_bar'),
        time_to_favorable_1=metric('first_favorable_1_bar'),
        time_to_favorable_1_5=metric('first_favorable_1_5_bar'),
        time_to_favorable_2=metric('first_favorable_2_bar'),
        reclaimed=sum(str(r['pivot_reclaim_bar'])!='' for r in s),
        recl_then_relost=sum(str(r['pivot_relost_within_entry_180']).lower()=='true' for r in s),
        stopped_after_reclaim=sum(str(r['sl_first_after_reclaim']).lower()=='true' for r in s),
        post_reclaim60_mfe=metric('post_reclaim_60m_mfe_pct'),
        post_reclaim60_mae=metric('post_reclaim_60m_mae_pct'),
        competing_tp_sl15=hits)


def report(root,spec,study,cache,oldout,selected):
    out=study/'last_path_diagnostics_v1'
    bysym=defaultdict(list)
    for r in selected:bysym[r['symbol']].append(r)
    expected=set(bysym)
    complete={s for s in expected if (out/s/'manifest.json').is_file()}
    if complete!=expected:raise RuntimeError(f'No final report until all selected symbols complete: {len(complete)}/{len(expected)}')
    rows=[]
    for sym in sorted(expected):rows.extend(verify_symbol(out,study,oldout,sym,bysym[sym]))
    if len(rows)!=EXPECTED_COUNT or Counter(r['prior_trade_outcome'] for r in rows)!=Counter(EXPECTED_TRADES):
        raise RuntimeError('Final report inconsistent with frozen 88 original labels')
    report_rows=[]
    for side in ('ALL','LONG','SHORT'):
        for group,value in [('ALL','ALL'),('prior_trade_outcome','TP_FIRST'),
                            ('prior_trade_outcome','SL_FIRST'),('prior_trade_outcome','TIMEOUT'),
                            ('prior_structural_outcome','RECOVERY_FIRST'),
                            ('prior_structural_outcome','CONTINUATION_FIRST'),
                            ('prior_structural_outcome','NO_DECISION')]:
            row=summarize(rows,side,group,value)
            if row:report_rows.append(row)
    core.atomic_csv(out/'all_B_88_path_metrics.csv',rows,FIELDS)
    summary=dict(version=VERSION,scope='FROZEN_50_HOLDOUT_B_SELECTED_FINAL_DIAGNOSTIC',
        sample=EXPECTED_COUNT,selected_symbols=EXPECTED_SYMBOLS,
        frozen_validation_plan_sha256=core.sha(study/'frozen_validation_plan.json'),
        experiment=dict(checkpoint=15,depth_pct=2.0,recovery_rule='favorable_return_5m_pct > 0',
            original_benchmark_tp=2.0,original_benchmark_sl=1.5,horizon_min=180,
            post_reclaim_horizon_min=60),
        caveats=['Already-inspected validation sample: any alternative TP or exit is exploratory only',
                 'MFE/MAE full 180m continues even after original trade stop/exit; counterfactual, not earned PnL',
                 'Both thresholds in same 1m candle are SL_AMBIGUOUS; cannot know intra-candle order',
                 'Reclaim requires CLOSED 1m; post-reclaim 60m measured from next minute OPEN',
                 'Multiple symbols/touches correlated; not 1-slot portfolio or net realized returns',
                 'The 207 reserved symbols were not accessed'],
        results=report_rows)
    core.atomic_json(out/'path_summary.json',summary)
    core.atomic_json(out/'report_manifest.json',dict(version=VERSION,
        summary_sha256=core.sha(out/'path_summary.json'),csv_sha256=core.sha(out/'all_B_88_path_metrics.csv'),
        source_plan_sha256=core.sha(study/'frozen_validation_plan.json')))
    concise=[r for r in report_rows if r['side'] in ('ALL','LONG','SHORT') and r['group']=='ALL']
    print('FAILED_TOUCH_B_PATH_FINAL_REPORT',json.dumps(dict(
        success=True,events=len(rows),symbols=len(expected),output=str(out),overview=concise),
        ensure_ascii=False),flush=True)
    return summary


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group(required=True)
    for a in ('plan','run','audit','report'):group.add_argument('--'+a,action='store_true')
    parser.add_argument('--project',type=Path,default=Path.cwd())
    parser.add_argument('--batch-size',type=int,default=10)
    parser.add_argument('--min-free-gb',type=float,default=4.0)
    args=parser.parse_args(argv)
    if args.batch_size<1:raise ValueError('--batch-size must be positive')
    root=args.project.resolve()
    spec,study,cache,oldout,selected=exact_select(root)
    out=study/'last_path_diagnostics_v1'
    bysym=defaultdict(list)
    for r in selected:bysym[r['symbol']].append(r)
    complete=sorted(s for s in bysym if (out/s/'manifest.json').is_file())
    pending=sorted(set(bysym)-set(complete))
    if args.plan:
        print('FAILED_TOUCH_B_PATH_PLAN',json.dumps(dict(
            selection=RULE_NAME,selected=len(selected),selected_symbols=len(bysym),
            completed_symbols=len(complete),pending_symbols=len(pending),
            next_pending=pending[:10],frozen_reserved_symbols=207,
            input_mode='OFFLINE_VERIFIED_1M_NO_DOWNLOAD',
            horizon_1m=HORIZON,post_reclaim_1m=POST_RECLAIM_HORIZON,
            disk_free_gb=round(disk_usage(root).free/1024**3,2),output=str(out)),ensure_ascii=False,indent=2))
        return
    if args.audit:
        issues=[];valid=0
        for sym in complete:
            try:verify_symbol(out,study,oldout,sym,bysym[sym]);valid+=1
            except Exception as exc:issues.append(dict(symbol=sym,error=str(exc)))
        done=valid==len(bysym)
        man=out/'report_manifest.json'
        if done and man.is_file():
            hashes=json.loads(man.read_text(encoding='utf-8'))
            for target,key in [('path_summary.json','summary_sha256'),('all_B_88_path_metrics.csv','csv_sha256')]:
                if core.sha(out/target)!=hashes[key]:issues.append(dict(file=target,error='Aggregate hash mismatch'))
        result=dict(success=not issues,selected=EXPECTED_COUNT,
                    valid_symbols=valid,selected_symbols=len(bysym),pending=len(bysym)-valid,
                    issues=issues[:20],full_audit_complete=done and not issues)
        print('FAILED_TOUCH_B_PATH_AUDIT',json.dumps(result,ensure_ascii=False))
        if issues:raise RuntimeError('Audit failed')
        return
    if args.run:
        count=0
        for sym in pending[:args.batch_size]:
            if disk_usage(root).free<args.min_free_gb*(1024**3):
                raise RuntimeError('Below minimum free disk; stopping before analysis')
            mode,n=compute_symbol(root,study,out,sym,bysym[sym],cache,oldout)
            count+=1
            print('FAILED_TOUCH_B_PATH_SYMBOL',json.dumps(dict(symbol=sym,status=mode,
                  signal_count=n),ensure_ascii=False),flush=True)
        print('FAILED_TOUCH_B_PATH_PROGRESS',json.dumps(dict(
            newly_done=count,complete_symbols=len(complete)+count,
            total_symbols=len(bysym),remaining=len(bysym)-len(complete)-count),ensure_ascii=False),flush=True)
        if len(complete)+count==len(bysym):report(root,spec,study,cache,oldout,selected)
        return
    report(root,spec,study,cache,oldout,selected)


if __name__=='__main__':main()
