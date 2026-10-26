#!/usr/bin/env python3
"""HLL V2.8 -- causal structure quality at FLIP_CONFIRMED.

Read-only structural features based on CLOSED 30m candles. V2.7 outcome rows
are joined afterwards, and never passed to feature engineering. No LIVE, Redis,
orders, network access or re-optimization of strategy parameters.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import tempfile
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

TF = 1_800_000
VERSION = 'hll-v2.8-structure-quality-causal-v1'
V26 = Path('reports/horizontal_level_structure_lab_v26/runs/core15_extended_v26')
V27 = Path('reports/horizontal_level_flip_research_v27/runs/core15_v27')
CANDLES = Path('reports/horizontal_level_lifecycle_v21/candles_aug20_oct25_core15_btc')
OUTPUT = Path('reports/horizontal_level_structure_quality_v28/runs/core15_v28')
TEST_HORIZONS = (60, 180)
VALID_OUTCOMES = {'TP_FIRST','SL_FIRST','TIME_EXIT','BOTH_SAME_1M_SL_ASSUMED'}


def number(v):
    try:
        v=float(v)
        return v if math.isfinite(v) else np.nan
    except (ValueError,TypeError):
        return np.nan


def filehash(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for b in iter(lambda:f.read(1<<20),b''):
            h.update(b)
    return h.hexdigest()


def save_csv(path, frame):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.v28-',suffix='.csv',dir=path.parent)
    try:
        with os.fdopen(fd,'w',newline='',encoding='utf8') as f:
            frame.to_csv(f,index=False);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def save_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.v28-',suffix='.json',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf8') as f:
            json.dump(value,f,indent=2,ensure_ascii=False,sort_keys=True,allow_nan=False)
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def load_30m(path):
    c=pd.read_csv(path,usecols=['timestamp','open','high','low','close'])
    if c.empty:raise ValueError(f'Empty candles {path}')
    c=c.sort_values('timestamp',kind='stable').reset_index(drop=True)
    c['timestamp']=pd.to_numeric(c.timestamp,errors='raise').astype('int64')
    if c.timestamp.duplicated().any():raise ValueError(f'Duplicate candles {path}')
    for field in ('open','high','low','close'):
        c[field]=pd.to_numeric(c[field],errors='raise')
    values=c[['open','high','low','close']].to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values<=0).any():raise ValueError(f'Invalid OHLC {path}')
    if (c.low>c.high).any() or (c.high<c[['open','close']].max(axis=1)).any() or (c.low>c[['open','close']].min(axis=1)).any():
        raise ValueError(f'Inconsistent OHLC {path}')
    return c


def confirmed_pivot_lows(lows,ts):
    """Only return strict 2-left/2-right lows, with their TRUE availability clock."""
    l=np.asarray(lows,dtype=float);t=np.asarray(ts,dtype=np.int64)
    out=[]
    for j in range(2,len(l)-2):
        if l[j]<min(l[j-2:j]) and l[j]<min(l[j+1:j+3]):
            out.append((j,int(t[j+2])+TF,float(l[j])))
    return out


def _slice_at(c, idx, count, include_end=True):
    end=idx+1 if include_end else idx
    start=end-count
    if start<0:return None
    sub=c.iloc[start:end]
    if len(sub)!=count or (len(sub)>1 and not np.all(np.diff(sub.timestamp.to_numpy())==TF)):
        return None
    return sub


def _pivots_known(pivots, start_idx, end_idx, max_closed_ts):
    return [(j,ts,price) for j,ts,price in pivots if start_idx<=j<=end_idx and ts<=max_closed_ts]


def _pivot_factors(c,pivots,idx,known_ts,atr,window,prefix):
    result={f'{prefix}_pivots':np.nan,f'{prefix}_higher_low_steps':np.nan,
            f'{prefix}_lower_low_steps':np.nan,f'{prefix}_hl_last3':np.nan,
            f'{prefix}_low_progression_atr':np.nan,
            f'{prefix}_pullback_depth_last_over_first':np.nan}
    segment=_slice_at(c,idx,window)
    if segment is None:return result,[],False
    pi=_pivots_known(pivots,idx-window+1,idx,known_ts)
    result[f'{prefix}_pivots']=len(pi)
    result[f'{prefix}_higher_low_steps']=sum(pi[j][2]>pi[j-1][2] for j in range(1,len(pi)))
    result[f'{prefix}_lower_low_steps']=sum(pi[j][2]<pi[j-1][2] for j in range(1,len(pi)))
    if len(pi)>=3:
        recent=pi[-3:]
        result[f'{prefix}_hl_last3']=int(recent[0][2]<recent[1][2]<recent[2][2])
        result[f'{prefix}_low_progression_atr']=(recent[-1][2]-recent[0][2])/atr
        depths=[]
        for prior,cur in zip(recent[:-1],recent[1:]):
            segmenthigh=float(c.iloc[prior[0]:cur[0]+1].high.max())
            depths.append((segmenthigh-cur[2])/atr)
        if depths[0]>0:result[f'{prefix}_pullback_depth_last_over_first']=depths[-1]/depths[0]
    return result,pi,True


def structure_at_clock(c,pivots,attempt,entry):
    """Construct features at breakout and at the chosen knowledge clock ONLY.

    No eventual outcome labels, forward highs/lows, or unconfirmed pivots used.
    Missing/gapped windows are NaN (not silently treated as no pressure).
    """
    ts=c.timestamp.to_numpy(dtype=np.int64)
    start_clock=int(attempt.known_closed_ts)
    known_clock=int(entry.known_closed_ts)
    breakout_open=start_clock-TF;known_open=known_clock-TF
    idx=int(np.searchsorted(ts,breakout_open))
    end=int(np.searchsorted(ts,known_open))
    if idx>=len(c) or end>=len(c) or ts[idx]!=breakout_open or ts[end]!=known_open:
        raise ValueError(f'Missing signal/breakout candle: {attempt.attempt_id}')
    if end<idx or np.any(np.diff(ts[idx:end+1])!=TF):
        raise ValueError(f'Gap between breakout and signal: {attempt.attempt_id}')
    atr=float(attempt.atr_pre);lo=float(attempt.zone_low);hi=float(attempt.zone_high)
    if not (0<atr and 0<lo<=hi):raise ValueError(f'Invalid ATR or zone: {attempt.attempt_id}')
    # PRE: cutoff at candle BEFORE breakout. Signal: cutoff at the actual
    # confirmed 30m close. Thus a pivot 2x2 confirmed after entry is excluded.
    pre_idx=idx-1
    pre_clock=breakout_open
    pres={};sig={};piv_display=[]
    for prefix,p_index,p_clock,window in [('pre24',pre_idx,pre_clock,24),
                                         ('pre48',pre_idx,pre_clock,48),
                                         ('signal24',end,known_clock,24),
                                         ('signal48',end,known_clock,48)]:
        vals,pi,ok=_pivot_factors(c,pivots,p_index,p_clock,atr,window,prefix)
        (pres if prefix.startswith('pre') else sig).update(vals)
        if prefix=='signal48':piv_display=pi
        (pres if prefix.startswith('pre') else sig)[f'{prefix}_coverage']=int(ok)
    pre=_slice_at(c,pre_idx,24)
    pre_cover=int(pre is not None)
    if pre is not None:
        closes=pre.close.to_numpy(dtype=float)
        signs=np.sign(closes-(hi+lo)/2)
        sign_changes=int(np.count_nonzero(signs[1:]*signs[:-1]<0))
        overlap=int(np.count_nonzero((pre.low.to_numpy()<=hi)&(pre.high.to_numpy()>=lo)))
        efficiency=abs(closes[-1]-closes[0])/np.abs(np.diff(closes)).sum() if np.abs(np.diff(closes)).sum()>0 else 0
        r=(pre.high-pre.low).to_numpy(dtype=float)
        contraction=(float(np.mean(r[-12:]))/float(np.mean(r[:12]))) if np.mean(r[:12])>0 else np.nan
    else:sign_changes=overlap=efficiency=contraction=np.nan
    between=c.iloc[idx:end+1]
    # Window [breakout ... flip] is entirely known at flip close and contiguous.
    bclose=between.close.to_numpy(dtype=float)
    center=(lo+hi)/2
    zone_cross=int(np.count_nonzero(np.sign(bclose[1:]-center)*np.sign(bclose[:-1]-center)<0))
    touches=int(np.count_nonzero((between.low.to_numpy()<=hi)&(between.high.to_numpy()>=lo)))
    retest_idx=int(np.searchsorted(ts,int(entry.retest_closed_ts)-TF))
    if retest_idx>=len(c) or ts[retest_idx]!=int(entry.retest_closed_ts)-TF or not idx<retest_idx<=end:
        raise ValueError(f'Invalid retest clock for {attempt.attempt_id}')
    retest=c.iloc[retest_idx:end+1]
    retest_overlap=int(np.count_nonzero((retest.low.to_numpy()<=hi)&(retest.high.to_numpy()>=lo)))
    retest_inside=int(np.count_nonzero((retest.close.to_numpy()>=lo)&(retest.close.to_numpy()<=hi)))
    retest_below=int(np.count_nonzero(retest.close.to_numpy()<lo))
    close=float(c.close.iloc[end]);break_close=float(c.close.iloc[idx])
    first_close=float(c.close.iloc[retest_idx]);retest_low=float(retest.low.min())
    base={
        'attempt_id':attempt.attempt_id,'symbol':attempt.symbol,'level_id':attempt.level_id,
        'entry_type':entry.entry_type,'breakout_closed_ts':start_clock,
        'known_closed_ts':known_clock,'entry_ts':int(entry.entry_ts),
        'zone_low':lo,'zone_high':hi,'atr_pre':atr,
        'prior_independent_touches':int(attempt.prior_independent_touches),
        'pressure_build_up_v26':int(attempt.pressure_build_up),
        'breakout_volume_ratio':number(attempt.volume_ratio_pre20),
        'next_resistance_at_breakout_atr':number(attempt.next_resistance_distance_atr),
        'breakout_body_atr':number(attempt.breakout_body_atr),
        'breakout_close_beyond_high_atr':number(attempt.breakout_close_beyond_high_atr),
        'pre24_close_center_crossings':sign_changes,
        'pre24_zone_overlap_bars':overlap,
        'pre24_directional_efficiency':efficiency,
        'pre24_range_recent_earlier_ratio':contraction,
        'pre24_coverage':pre_cover,
        'breakout_to_signal_center_crossings':zone_cross,
        'breakout_to_signal_zone_overlaps':touches,
        'retest_to_signal_zone_overlaps':retest_overlap,
        'retest_to_signal_closes_inside_zone':retest_inside,
        'retest_to_signal_closes_below_zone':retest_below,
        'retest_low_below_zone_high_atr':(hi-retest_low)/atr,
        'retest_candle_close_above_zone_high_atr':(first_close-hi)/atr,
        'defense_close_above_zone_high_atr':(close-hi)/atr,
        'breakout_to_signal_extension_atr':(close-break_close)/atr,
        'signal_distance_from_zone_low_atr':(close-lo)/atr,
        'bars_breakout_to_signal':end-idx,
        'bars_retest_to_signal':end-retest_idx,
        'known_pivots_signal48_json':json.dumps([
            {'open_ts':int(ts[j]),'confirmed_close_ts':int(p_ts),'low':float(v)}
            for j,p_ts,v in piv_display],separators=(',',':')),
    }
    base.update(pres);base.update(sig)
    return base


def read_selected_trades(path,horizons):
    use=['attempt_id','symbol','entry_type','known_closed_ts','entry_ts',
         'horizon_min','tp_pct','stop_type','path_state','outcome',
         'return_after_fee_pct','mfe_to_exit_pct','mae_to_exit_pct','duplicate_symbol_clock']
    selected=[]
    for chunk in pd.read_csv(path,usecols=use,chunksize=45000,low_memory=False):
        x=chunk[(chunk.entry_type=='FLIP_CONFIRMED') & (chunk.tp_pct==2.0) &
                (chunk.stop_type=='FIXED_2P0') & chunk.horizon_min.isin(horizons)]
        if not x.empty:selected.append(x.copy())
    if not selected:return pd.DataFrame(columns=use)
    t=pd.concat(selected,ignore_index=True)
    if t.duplicated(['attempt_id','entry_type','horizon_min']).any():
        raise ValueError('Duplicate V2.7 scenario per attempt/horizon')
    return t


def cohort(ts):
    return 'AUG_SEP_DISCOVERY' if pd.Timestamp(ts) < pd.Timestamp('2026-10-01',tz='UTC') else 'OCT_REVIEW'


def factor_bucket(df,factor):
    x=pd.to_numeric(df[factor],errors='coerce')
    if factor.endswith('hl_last3'):
        return x.map({0:'NO',1:'YES'}).fillna('UNKNOWN')
    if factor in ('pre24_close_center_crossings','breakout_to_signal_center_crossings'):
        return pd.cut(x,bins=[-1,0,2,np.inf],labels=['0','1-2','3+']).astype('object').fillna('UNKNOWN')
    if factor.endswith('low_progression_atr'):
        return pd.cut(x,bins=[-np.inf,0,.5,np.inf],labels=['NON_RISING','0-0.5 ATR','>0.5 ATR']).astype('object').fillna('UNKNOWN')
    if factor=='pre24_directional_efficiency':
        return pd.cut(x,bins=[-np.inf,.2,.4,np.inf],labels=['<=0.2','0.2-0.4','>0.4']).astype('object').fillna('UNKNOWN')
    if factor.endswith('pullback_depth_last_over_first') or factor=='pre24_range_recent_earlier_ratio':
        return pd.cut(x,bins=[-np.inf,.8,1.2,np.inf],labels=['CONTRACTING','STABLE','EXPANDING']).astype('object').fillna('UNKNOWN')
    if factor in ('pre24_zone_overlap_bars','pre24_higher_low_steps','retest_to_signal_closes_inside_zone'):
        return pd.cut(x,bins=[-1,0,2,np.inf],labels=['0','1-2','3+']).astype('object').fillna('UNKNOWN')
    if factor=='retest_to_signal_zone_overlaps':
        return pd.cut(x,bins=[-1,1,2,np.inf],labels=['1','2','3+']).astype('object').fillna('UNKNOWN')
    if factor=='retest_low_below_zone_high_atr':
        return pd.cut(x,bins=[-np.inf,.2,.5,np.inf],labels=['<=0.2','0.2-0.5','>0.5']).astype('object').fillna('UNKNOWN')
    if factor=='breakout_volume_ratio':
        return pd.cut(x,bins=[-np.inf,1,2,np.inf],labels=['<=1x','1-2x','>2x']).astype('object').fillna('UNKNOWN')
    if factor=='signal_distance_from_zone_low_atr':
        return pd.cut(x,bins=[-np.inf,1,2,np.inf],labels=['<=1 ATR','1-2 ATR','>2 ATR']).astype('object').fillna('UNKNOWN')
    raise KeyError(factor)

FACTORS=(
    'pre24_hl_last3','signal24_hl_last3',
    'pre24_low_progression_atr','signal24_low_progression_atr',
    'pre24_close_center_crossings','breakout_to_signal_center_crossings',
    'pre24_directional_efficiency','pre24_range_recent_earlier_ratio',
    'pre24_pullback_depth_last_over_first','signal24_pullback_depth_last_over_first',
    'pre24_zone_overlap_bars','pre24_higher_low_steps',
    'retest_to_signal_zone_overlaps','retest_to_signal_closes_inside_zone',
    'retest_low_below_zone_high_atr','breakout_volume_ratio',
    'signal_distance_from_zone_low_atr',
)


def factor_tables(enriched):
    allrows=[];clusters=[]
    valid=enriched[(enriched.path_state=='COMPLETE_1M') & enriched.outcome.isin(VALID_OUTCOMES)].copy()
    valid['net']=pd.to_numeric(valid.return_after_fee_pct,errors='coerce')
    valid=valid[valid.net.notna()].copy()
    # A common series of levels on one symbol+clock is only one cluster for
    # the descriptive sensitivity view (but episodes remain distinct upstream).
    valid['week']=pd.to_datetime(valid.entry_ts,unit='ms',utc=True).dt.strftime('%G-W%V')
    valid['period']=pd.to_datetime(valid.entry_ts,unit='ms',utc=True).map(cohort)
    for factor in FACTORS:
        valid['bucket']=factor_bucket(valid,factor)
        for (period,h,bucket),g in valid.groupby(['period','horizon_min','bucket'],dropna=False):
            sb=g.groupby('symbol').net.mean()
            allrows.append(dict(factor=factor,period=period,horizon_min=int(h),bucket=str(bucket),
                n=len(g),symbols=g.symbol.nunique(),clusters=g.groupby(['symbol','week']).ngroups,
                mean_net_pct=g.net.mean(),equal_symbol_net_pct=sb.mean(),
                positive_symbols=int((sb>0).sum()),
                tp_pct=100*g.outcome.eq('TP_FIRST').mean(),
                sl_pct=100*g.outcome.isin(['SL_FIRST','BOTH_SAME_1M_SL_ASSUMED']).mean()))
        cw=(valid.groupby(['period','horizon_min','symbol','week','bucket'],dropna=False)
             .agg(n=('attempt_id','size'),mean_net_pct=('net','mean')).reset_index())
        cw.insert(0,'factor',factor)
        clusters.append(cw)
    cols=['factor','period','horizon_min','bucket','n','symbols','clusters',
          'mean_net_pct','equal_symbol_net_pct','positive_symbols','tp_pct','sl_pct']
    return pd.DataFrame(allrows,columns=cols),pd.concat(clusters,ignore_index=True)


def run(args):
    a_file=args.v26_dir/'breakout_attempts.csv'
    e_file=args.v27_dir/'entry_candidates.csv'
    t_file=args.v27_dir/'trades.csv'
    o_file=args.v26_dir/'attempt_outcomes.csv'
    files=[a_file,e_file,t_file,o_file]
    for p in files:
        if not p.is_file():raise SystemExit(f'Missing source file: {p}')
    for p in (args.v26_dir,args.v27_dir,args.candles_dir):
        if args.output_dir.resolve()==p.resolve() or args.output_dir.resolve().is_relative_to(p.resolve()):
            raise SystemExit('Refusing to write inside source directory')
    attempts=pd.read_csv(a_file,low_memory=False)
    entries=pd.read_csv(e_file,low_memory=False)
    confirmed=entries[entries.entry_type=='FLIP_CONFIRMED'].copy()
    if attempts.attempt_id.duplicated().any() or entries.duplicated(['attempt_id','entry_type']).any():
        raise SystemExit('Duplicate source IDs')
    if not set(confirmed.attempt_id).issubset(set(attempts.attempt_id)):
        raise SystemExit('V2.7 entries missing corresponding V2.6 attempts')
    selected=entries[entries.entry_type.isin(['FLIP_CONFIRMED','FLIP_SUSTAINED_4B'])].copy()
    # Some precursor V2.7 exports carry this field; V2.6 remains authority.
    selected=selected.drop(columns=['retest_closed_ts'],errors='ignore')
    symbols=sorted(selected.symbol.unique())
    if args.max_symbols>0:symbols=symbols[:args.max_symbols]
    selected=selected[selected.symbol.isin(symbols)]
    counts=Counter(selected.entry_type)
    plan=dict(version=VERSION,entries=len(selected),entry_types=dict(counts),symbols=len(symbols),
              test_horizons=TEST_HORIZONS,baseline='TP2/SL2 V2.7 no re-simulation',
              source_hashes={p.name:filehash(p) for p in files},
              output=str(args.output_dir),mode='READ_ONLY_OFFLINE')
    print('HLL_V28_PLAN',json.dumps(plan,sort_keys=True),flush=True)
    if args.plan:return 0
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        raise SystemExit('OUTPUT_EXISTS (refusing to overwrite)')
    if shutil.disk_usage(Path.cwd()).free<args.min_free_gb*1024**3:
        raise SystemExit('Insufficient free disk space')
    outcomes=pd.read_csv(o_file,usecols=['attempt_id','retest_closed_ts'])
    if outcomes.attempt_id.duplicated().any():raise ValueError('Duplicate V2.6 outcomes')
    joined=selected.merge(attempts,on=['attempt_id','symbol','level_id'],how='left',
                          validate='many_to_one',suffixes=('_entry',''))
    joined=joined.merge(outcomes,on='attempt_id',how='left',validate='many_to_one')
    if joined.known_closed_ts.isna().any() or joined.retest_closed_ts.isna().any():
        raise ValueError('Missing V2.6 breakout or retest join')
    results=[]
    for n,sym in enumerate(symbols,1):
        p=args.candles_dir/f'{sym}_30m.csv'
        if not p.is_file():raise SystemExit(f'Missing {p}')
        c=load_30m(p);pivots=confirmed_pivot_lows(c.low.to_numpy(),c.timestamp.to_numpy())
        r=joined[joined.symbol==sym]
        for item in r.itertuples(index=False):
            # V2.6 breakout known_closed_ts collides with V2.7 entry known_closed_ts.
            # Explicitly read V2.7 clock from suffixed column.
            attempt=type('Attempt',(),dict(attempt_id=item.attempt_id,symbol=item.symbol,
                level_id=item.level_id,known_closed_ts=int(item.known_closed_ts),
                atr_pre=item.atr_pre,zone_low=item.zone_low,zone_high=item.zone_high,
                prior_independent_touches=item.prior_independent_touches,
                pressure_build_up=item.pressure_build_up,
                volume_ratio_pre20=item.volume_ratio_pre20,
                next_resistance_distance_atr=item.next_resistance_distance_atr,
                breakout_body_atr=item.breakout_body_atr,
                breakout_close_beyond_high_atr=item.breakout_close_beyond_high_atr))
            entry=type('Entry',(),dict(known_closed_ts=int(item.known_closed_ts_entry),
                entry_ts=int(item.entry_ts),entry_type=item.entry_type,
                retest_closed_ts=int(item.retest_closed_ts) if hasattr(item,'retest_closed_ts') else None,
                v26_status=item.v26_status))
            results.append(structure_at_clock(c,pivots,attempt,entry))
        print('HLL_V28_SYMBOL',json.dumps(dict(index=n,symbol=sym,entries=len(r))),flush=True)
    features=pd.DataFrame(results)
    assert len(features)==len(selected)
    historical=read_selected_trades(t_file,TEST_HORIZONS)
    historical=historical[historical.symbol.isin(symbols)].copy()
    enriched=features[features.entry_type=='FLIP_CONFIRMED'].merge(
        historical,on=['attempt_id','symbol','entry_type','known_closed_ts','entry_ts'],
        how='left',validate='one_to_many',suffixes=('','_v27'))
    if len(enriched)!=len(features[features.entry_type=='FLIP_CONFIRMED'])*len(TEST_HORIZONS):
        raise ValueError('V2.7 TP2 SL2 comparison join incomplete; refuse inferred results')
    enriched['duplicate_symbol_clock']=enriched.duplicated(['symbol','entry_ts','horizon_min'])
    # Separate exact event analysis from sensitivity removing simultaneous duplicated clocks.
    analysis=enriched[~enriched.duplicate_symbol_clock].copy()
    factors,clusters=factor_tables(analysis)
    manifest={**plan,'entries_written':len(features),'evaluations_written':len(enriched),
        'deduplicated_eval_count':len(analysis),'coverage_state_counts':dict(Counter(enriched.path_state)),
        'factor_definitions':{
            'pre_*':'Only CLOSED bars BEFORE the CROSS_UP candle; 2x2 low pivots confirmed at that time',
            'signal_*':'Closed bars through FLIP_CONFIRMED; confirmed 2x2 pivots only',
            'retest_to_signal_*':'Retest candle through FLIP_CONFIRMED, both inclusive and already closed',
            'zone_center_crossings':'Sign changes of adjacent 30m CLOSES around zone midpoint',
            'pullback_depth_last_over_first':'Two last retracement depths ending at three CONFIRMED swing lows',
            'periods':'Aug-Sep versus Oct descriptive only; exploratory dataset already inspected; NOT true holdout'},
        'safety':'No lookahead; no future outcome used for factors; missing context is NaN; read-only source paths',
        'limitations':'Research sample 15 symbols, dependent levels; time_exit and incomplete 1m coverage; no slippage/funding/portfolio',
        'v27_outcome_scenario':'FLIP_CONFIRMED TP2/SL2 60/180m with fee as configured by V2.7'}
    save_csv(args.output_dir/'entry_structures.csv',features)
    save_csv(args.output_dir/'evaluation_enriched.csv',enriched)
    save_csv(args.output_dir/'factor_tables.csv',factors)
    save_csv(args.output_dir/'symbol_week_groups.csv',clusters)
    save_json(args.output_dir/'manifest.json',manifest)
    print('HLL_V28_DONE',json.dumps(dict(entries=len(features),evaluations=len(enriched),
            rows_factor_tables=len(factors),output=str(args.output_dir))),flush=True)
    return 0


def report_existing(folder,horizon=180):
    """Present pre-specified, non-optimized groups from a completed audit."""
    p=folder/'factor_tables.csv';e=folder/'evaluation_enriched.csv';f=folder/'entry_structures.csv'
    for path in [p,e,f]:
        if not path.is_file():raise SystemExit(f'Missing report file: {path}')
    factors=pd.read_csv(p);evals=pd.read_csv(e,low_memory=False)
    features=pd.read_csv(f,low_memory=False)
    selected=evals[evals.horizon_min.eq(horizon)]
    print('HLL_V28_REPORT',json.dumps(dict(horizon_min=horizon,
        flips_confirmed=int(features.entry_type.eq('FLIP_CONFIRMED').sum()),
        sustained4b=int(features.entry_type.eq('FLIP_SUSTAINED_4B').sum()),
        eval_scenarios=len(selected),
        coverage=dict(Counter(selected.path_state))),sort_keys=True),flush=True)
    important=('pre24_hl_last3','signal24_hl_last3',
        'pre24_close_center_crossings','pre24_pullback_depth_last_over_first',
        'retest_to_signal_zone_overlaps','retest_low_below_zone_high_atr',
        'breakout_volume_ratio','signal_distance_from_zone_low_atr')
    display=factors[factors.horizon_min.eq(horizon)&factors.factor.isin(important)].copy()
    if display.empty:
        print('No complete 1m evaluations: do not interpret missing coverage as SL.')
        return 0
    print('HLL_V28_FACTOR_REPORT')
    cols=['factor','period','bucket','n','symbols','mean_net_pct',
          'equal_symbol_net_pct','tp_pct','sl_pct','positive_symbols']
    print(display[cols].round(4).sort_values(['factor','period','bucket']).to_string(index=False))
    print('HLL_V28_REPORT_NOTE Discovery/October groups are exploratory, not a genuine holdout.')
    return 0


def main(argv=None):
    p=argparse.ArgumentParser(description='V2.8 offline structural quality audit, 30m')
    m=p.add_mutually_exclusive_group(required=True)
    m.add_argument('--plan',action='store_true');m.add_argument('--run',action='store_true');m.add_argument('--report',action='store_true')
    p.add_argument('--v26-dir',type=Path,default=V26)
    p.add_argument('--v27-dir',type=Path,default=V27)
    p.add_argument('--candles-dir',type=Path,default=CANDLES)
    p.add_argument('--output-dir',type=Path,default=OUTPUT)
    p.add_argument('--max-symbols',type=int,default=0)
    p.add_argument('--min-free-gb',type=float,default=.5)
    p.add_argument('--report-horizon',type=int,choices=TEST_HORIZONS,default=180)
    args=p.parse_args(argv)
    return report_existing(args.output_dir,args.report_horizon) if args.report else run(args)

if __name__=='__main__':raise SystemExit(main())
