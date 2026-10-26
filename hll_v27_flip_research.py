#!/usr/bin/env python3
"""HLL V2.7: offline post-confirmation LONG flip-entry research.

Read-only on V2.6 and V2.2 inputs. No orders, Redis, exchange access,
market-flow, or parameter optimization. The two entry clocks are V2.6's
actual CLOSED 30m timestamps. Entry at next 1m OPEN after a 1m delay.
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

M=60000; TF=1800000
VERSION='hll-v2.7-flip-entry-causal-v1'
DEFAULT_V26=Path('reports/horizontal_level_structure_lab_v26/runs/core15_extended_v26')
DEFAULT_1M=Path('reports/horizontal_level_cross_research_v22/extended_1m_core15_v25')
DEFAULT_30M=Path('reports/horizontal_level_lifecycle_v21/candles_aug20_oct25_core15_btc')
DEFAULT_OUT=Path('reports/horizontal_level_flip_research_v27/runs/core15_v27')
ENTRY_TYPES=('FLIP_CONFIRMED','FLIP_SUSTAINED_4B')
TP_VALUES=(1.0,2.0,3.0,4.0)
FIXED_STOPS=(1.5,2.0,3.0)
STRUCT_STOPS=('ZONE_ATR_0P1','RETEST_ATR_0P1')
HORIZONS=(30,60,180,360,720)
FEE=0.1


def _num(x):
    try:
        v=float(x)
        return v if math.isfinite(v) else None
    except (TypeError,ValueError):return None

def _ts(x):
    n=_num(x)
    return int(n) if n is not None and n>0 else None

def _sha(p):
    d=hashlib.sha256()
    with open(p,'rb') as f:
        for chunk in iter(lambda:f.read(1<<20),b''):d.update(chunk)
    return d.hexdigest()

def _save_csv(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.v27-',suffix='.csv',dir=path.parent)
    try:
        with os.fdopen(fd,'w',newline='',encoding='utf-8') as f:
            data.to_csv(f,index=False);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def _save_json(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.v27-',suffix='.json',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(data,f,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def load_minute(path):
    if not path.exists():return pd.DataFrame(columns=['timestamp','open','high','low','close'])
    d=pd.read_csv(path,usecols=['timestamp','open','high','low','close'])
    d['timestamp']=pd.to_numeric(d.timestamp,errors='raise').astype('int64')
    d=d.sort_values('timestamp',kind='stable')
    # Duplicate timestamp with different values is not allowed.
    dup=d[d.timestamp.duplicated(keep=False)]
    if len(dup) and (dup.groupby('timestamp')[['open','high','low','close']].nunique(dropna=False)>1).any().any():
        raise ValueError(f'Conflicting 1m duplicates: {path}')
    d=d.drop_duplicates('timestamp')
    for k in ('open','high','low','close'):
        d[k]=pd.to_numeric(d[k],errors='raise')
    if not np.isfinite(d[['open','high','low','close']].to_numpy(dtype=float)).all():
        raise ValueError(f'Nonfinite OHLCV: {path}')
    return d.reset_index(drop=True)


def _clock_candidates(attempts, outcomes):
    a=attempts.merge(outcomes[['attempt_id','status','defense_closed_ts','flip_sustained_closed_ts',
                               'retest_closed_ts']],on='attempt_id',validate='one_to_one')
    if len(a)!=len(attempts):raise ValueError('Missing V2.6 outcome joins')
    if a.attempt_id.duplicated().any():raise ValueError('Duplicate attempts')
    res=[]
    for r in a.itertuples(index=False):
        # Entry candidates are enabled by a causally known confirmation,
        # NOT by selecting later success or eventual loss status.
        retest=_ts(r.retest_closed_ts)
        confirmed=_ts(r.defense_closed_ts)
        sustained=_ts(r.flip_sustained_closed_ts)
        if confirmed is None:continue
        if retest is None or not (_ts(r.known_closed_ts)<retest<confirmed):
            raise ValueError(f'{r.attempt_id}: invalid causal retest/flip clocks')
        clocks=[('FLIP_CONFIRMED',confirmed)]
        if sustained is not None:
            if sustained<confirmed+4*TF:
                raise ValueError(f'{r.attempt_id}: sustained not 4 bars after flip')
            clocks.append(('FLIP_SUSTAINED_4B',sustained))
        for typ,ts in clocks:
            res.append(dict(attempt_id=r.attempt_id,symbol=r.symbol,level_id=r.level_id,
                breakout_closed_ts=int(r.known_closed_ts),retest_closed_ts=retest,
                flip_confirmed_closed_ts=confirmed,confirmed_sustained_closed_ts=sustained,
                known_closed_ts=ts,entry_type=typ,entry_ts=ts+M,
                v26_status=r.status,zone_low=float(r.zone_low),zone_high=float(r.zone_high),
                atr_pre=float(r.atr_pre),prior_independent_touches=int(r.prior_independent_touches),
                prior_confirmed_rejections=int(r.prior_confirmed_rejections),
                prior_failed_breakouts=int(r.prior_failed_breakouts),
                pressure_build_up=int(r.pressure_build_up),
                volume_ratio_pre20=_num(r.volume_ratio_pre20),
                next_resistance_distance_atr=_num(r.next_resistance_distance_atr)))
    return pd.DataFrame.from_records(res)


def structural_stops(entry, bars30):
    """Only closes at/before this variant's event-known timestamp are permitted."""
    known=int(entry.known_closed_ts)
    past=bars30[(bars30.timestamp+TF<=known) & (bars30.timestamp+TF>int(entry.retest_closed_ts)-TF)]
    # first selected bar must be the actual retest candle (its CLOSE is retest clock)
    if past.empty or int(past.timestamp.iloc[0])+TF!=int(entry.retest_closed_ts):
        return (None,None,'NO_RETEST_30M')
    if int(past.timestamp.iloc[-1])+TF!=known or np.any(np.diff(past.timestamp.to_numpy())!=TF):
        return (None,None,'GAP_30M')
    if float(entry.atr_pre)<=0 or float(entry.zone_low)<=0:return (None,None,'INVALID_ATR_OR_ZONE')
    buffer=0.1*float(entry.atr_pre)
    return (float(entry.zone_low)-buffer,float(past.low.min())-buffer,'OK')


def coverage_and_path(minute, known, horizon):
    """One full contiguous 1m path; no retroactive fill and no implicit gaps."""
    start=int(known)+M
    if minute.empty:return 'NO_1M_ARCHIVE',None
    times=minute.timestamp.to_numpy(dtype='int64')
    ix=int(np.searchsorted(times,start))
    if ix==len(times) or int(times[ix])!=start:return 'NO_ENTRY_1M',None
    end=ix+int(horizon)
    if end>len(minute):return 'CENSORED_1M_TAIL',None
    sub=minute.iloc[ix:end]
    if len(sub)>1 and np.any(np.diff(sub.timestamp.to_numpy(dtype='int64'))!=M):
        return 'GAP_1M',None
    return 'COMPLETE_1M',sub


def path_summary(path,entry_price):
    return {'mfe_full_pct':100*(float(path.high.max())/entry_price-1),
            'mae_full_pct':100*(1-float(path.low.min())/entry_price),
            'time_end_return_pct':100*(float(path.close.iloc[-1])/entry_price-1)}


def simulate_path(path,tp_pct,stop_price,fee=FEE):
    """Long-first touch using actual 1m high/low; same-minute race is worst-case.

    The entire path MUST be contiguous and known by the caller. MFE/MAE are
    computed to exit inclusive, not on candles occurring after a trade closed.
    """
    entry=float(path.open.iloc[0]); risk_pct=100*(1-stop_price/entry)
    if not (math.isfinite(stop_price) and 0<stop_price<entry and risk_pct>0):
        return dict(outcome='INVALID_STOP',risk_pct=risk_pct if math.isfinite(risk_pct) else None)
    goal=entry*(1+float(tp_pct)/100)
    out='TIME_EXIT';exit_idx=len(path)-1;exit_price=float(path.close.iloc[-1]);gross=100*(exit_price/entry-1)
    for i,b in enumerate(path.itertuples(index=False)):
        # Price gaps against the position execute at the worse available OPEN,
        # never at an unreachable stop price. Favorable gaps are conservatively
        # capped at the original TP level.
        if float(b.open)<=stop_price:
            out='SL_FIRST';exit_idx=i;exit_price=float(b.open)
            gross=100*(exit_price/entry-1);break
        if float(b.open)>=goal:
            out='TP_FIRST';exit_idx=i;exit_price=goal;gross=float(tp_pct);break
        hit_tp=float(b.high)>=goal;hit_sl=float(b.low)<=stop_price
        if hit_tp and hit_sl:
            out='BOTH_SAME_1M_SL_ASSUMED';exit_idx=i;exit_price=stop_price;gross=-risk_pct;break
        if hit_tp or hit_sl:
            out='TP_FIRST' if hit_tp else 'SL_FIRST';exit_idx=i
            exit_price=goal if hit_tp else stop_price;gross=100*(exit_price/entry-1);break
    used=path.iloc[:exit_idx+1]
    return dict(outcome=out,stop_risk_pct=risk_pct,return_gross_pct=gross,
        return_after_fee_pct=gross-fee,exit_ts=int(used.timestamp.iloc[-1]),
        exit_price=exit_price,bars_to_exit=exit_idx+1,
        mfe_to_exit_pct=100*(float(used.high.max())/entry-1),
        mae_to_exit_pct=100*(1-float(used.low.min())/entry))


def analyze_symbol(entries,bar30,minute,horizons=HORIZONS,tps=TP_VALUES,fixed_stops=FIXED_STOPS):
    bars=[];trades=[];paths=[]
    for row in entries.itertuples(index=False):
        zstop,rstop,state=structural_stops(row,bar30)
        # CLOCK + structural stops only use 30m CLOSED as-of entry type.
        first_state,one=coverage_and_path(minute,row.known_closed_ts,1)
        if first_state=='COMPLETE_1M':
            eprice=float(one.open.iloc[0])
            if eprice<=0:raise ValueError('Nonpositive entry price')
        else:eprice=None
        last_closed=bar30[bar30.timestamp.eq(int(row.known_closed_ts)-TF)]
        retest_candle=bar30[bar30.timestamp.eq(int(row.retest_closed_ts)-TF)]
        if len(last_closed)!=1 or len(retest_candle)!=1:
            raise ValueError(f'{row.symbol} {row.attempt_id}: missing causal 30m clock')
        retest_depth=(float(row.zone_high)-float(retest_candle.low.iloc[0]))/float(row.atr_pre)
        defense_above=(float(last_closed.close.iloc[0])-float(row.zone_high))/float(row.atr_pre)
        base=dict(attempt_id=row.attempt_id,level_id=row.level_id,symbol=row.symbol,
            entry_type=row.entry_type,v26_status=row.v26_status,
            breakout_closed_ts=row.breakout_closed_ts,known_closed_ts=row.known_closed_ts,
            entry_ts=row.entry_ts,entry_price=eprice,zone_low=row.zone_low,
            retest_depth_atr=retest_depth,
            entry_close_above_zone_atr=defense_above,
            bars_since_breakout=int((int(row.known_closed_ts)-int(row.breakout_closed_ts))//TF),
            bars_since_retest=int((int(row.known_closed_ts)-int(row.retest_closed_ts))//TF),
            zone_high=row.zone_high,atr_pre=row.atr_pre,structural_state=state,
            zone_stop_price=zstop,retest_stop_price=rstop,
            prior_independent_touches=row.prior_independent_touches,
            prior_confirmed_rejections=row.prior_confirmed_rejections,
            prior_failed_breakouts=row.prior_failed_breakouts,
            pressure_build_up=row.pressure_build_up,
            volume_ratio_pre20=row.volume_ratio_pre20,
            next_resistance_distance_atr=row.next_resistance_distance_atr)
        stops=[(f'FIXED_{str(s).replace(".","P")}',eprice*(1-s/100) if eprice else None) for s in fixed_stops]
        stops.extend([('ZONE_ATR_0P1',zstop),('RETEST_ATR_0P1',rstop)])
        for h in horizons:
            avail,sub=coverage_and_path(minute,row.known_closed_ts,h)
            p=dict(**base,horizon_min=int(h),path_state=avail)
            if sub is not None:
                p.update(path_summary(sub,eprice))
            paths.append(p)
            for stop_name,stop_px in stops:
                for tp in tps:
                    item=dict(**base,horizon_min=int(h),tp_pct=float(tp),
                              stop_type=stop_name,stop_price=stop_px,
                              fee_roundtrip_pct=FEE,path_state=avail,
                              return_after_fee_pct=None,stop_risk_pct=None)
                    if sub is not None:
                        if stop_px is None:
                            item.update(outcome='INVALID_STRUCTURAL_STOP')
                        else:item.update(simulate_path(sub,tp,stop_px))
                    else:item.update(outcome='CENSORED')
                    trades.append(item)
        bars.append(base)
    return bars,paths,trades


def _summarize(trades):
    t=trades.copy()
    t['net']=pd.to_numeric(t.return_after_fee_pct,errors='coerce')
    valid=t[t.path_state.eq('COMPLETE_1M') & t.outcome.isin(
        ['TP_FIRST','SL_FIRST','TIME_EXIT','BOTH_SAME_1M_SL_ASSUMED'])].copy()
    if valid.empty:return pd.DataFrame(columns=['entry_type','horizon_min','tp_pct','stop_type','n','mean_net_pct'])
    groups=['entry_type','horizon_min','tp_pct','stop_type']
    s=valid.groupby(groups,dropna=False).agg(n=('attempt_id','size'),symbols=('symbol','nunique'),
        tp=('outcome',lambda x:int(x.eq('TP_FIRST').sum())),
        sl=('outcome',lambda x:int(x.eq('SL_FIRST').sum())),
        time=('outcome',lambda x:int(x.eq('TIME_EXIT').sum())),
        ambiguous_worst_sl=('outcome',lambda x:int(x.eq('BOTH_SAME_1M_SL_ASSUMED').sum())),
        mean_net_pct=('net','mean'),median_net_pct=('net','median'),
        avg_stop_risk_pct=('stop_risk_pct','mean')).reset_index()
    equal=valid.groupby(groups+['symbol'])['net'].mean().groupby(groups).mean().rename('equal_symbol_mean_net_pct').reset_index()
    s=s.merge(equal,on=groups,validate='one_to_one')
    s['tp_pct_all']=100*s.tp/s.n
    return s.sort_values(['entry_type','horizon_min','tp_pct','stop_type'])


def main(argv=None):
    p=argparse.ArgumentParser(description='HLL V2.7 offline post-flip entry and structural stop audit')
    m=p.add_mutually_exclusive_group(required=True);m.add_argument('--plan',action='store_true');m.add_argument('--run',action='store_true')
    p.add_argument('--v26-dir',type=Path,default=DEFAULT_V26)
    p.add_argument('--minute-dir',type=Path,default=DEFAULT_1M)
    p.add_argument('--candles-30m-dir',type=Path,default=DEFAULT_30M)
    p.add_argument('--output-dir',type=Path,default=DEFAULT_OUT)
    p.add_argument('--max-symbols',type=int,default=0)
    p.add_argument('--horizons',default=','.join(str(x) for x in HORIZONS))
    p.add_argument('--min-free-gb',type=float,default=.5)
    args=p.parse_args(argv)
    horizons=sorted({int(s) for s in args.horizons.split(',')})
    if not horizons or any(h<1 or h>2880 for h in horizons):raise SystemExit('Invalid horizons; allowed 1..2880 minutes')
    required=[args.v26_dir/'breakout_attempts.csv',args.v26_dir/'attempt_outcomes.csv']
    for f in required:
        if not f.is_file():raise SystemExit(f'Missing {f}')
    for src in [args.v26_dir,args.minute_dir,args.candles_30m_dir]:
        if args.output_dir.resolve()==src.resolve() or args.output_dir.resolve().is_relative_to(src.resolve()):
            raise SystemExit('Output directory cannot write into source directory')
    attempts=pd.read_csv(required[0]);outcomes=pd.read_csv(required[1])
    entries=_clock_candidates(attempts,outcomes)
    symbols=sorted(entries.symbol.unique())
    if args.max_symbols>0:symbols=symbols[:args.max_symbols]
    entries=entries[entries.symbol.isin(symbols)].copy()
    # Never hide absent sustained transitions; count eligible denominators.
    flip_count=outcomes.defense_closed_ts.notna().sum()
    print('HLL_V27_PLAN',json.dumps(dict(version=VERSION,source_hashes={k.name:_sha(k) for k in required},
       attempts=len(attempts),flip_confirmed=int(flip_count),entry_candidates=len(entries),
       entry_types=Counter(entries.entry_type),symbols=len(symbols),horizons_min=horizons,
       tps=TP_VALUES,fixed_sls=FIXED_STOPS,structural_sls=STRUCT_STOPS,
       expected_evaluations=len(entries)*len(horizons)*len(TP_VALUES)*(len(FIXED_STOPS)+len(STRUCT_STOPS)),
       outputs=str(args.output_dir),mode='READ_ONLY_OFFLINE'),default=dict,sort_keys=True),flush=True)
    if args.plan:return 0
    if args.output_dir.exists() and any(args.output_dir.iterdir()):raise SystemExit('OUTPUT_EXISTS: no overwrite')
    if shutil.disk_usage(Path.cwd()).free < args.min_free_gb*1024**3:raise SystemExit('Insufficient disk')
    combined_b=[];combined_p=[];combined_t=[]
    for n,sym in enumerate(symbols,1):
        cfile=args.candles_30m_dir/f'{sym}_30m.csv'
        if not cfile.exists():raise SystemExit(f'Missing {cfile}')
        candles=pd.read_csv(cfile,usecols=['timestamp','open','high','low','close']).sort_values('timestamp').reset_index(drop=True)
        if candles.timestamp.duplicated().any():raise SystemExit(f'Duplicate 30m bars for {sym}')
        minutes=load_minute(args.minute_dir/f'{sym}_1m.csv')
        b,p,t=analyze_symbol(entries[entries.symbol.eq(sym)],candles,minutes,horizons)
        combined_b.extend(b);combined_p.extend(p);combined_t.extend(t)
        print('HLL_V27_SYMBOL',json.dumps(dict(index=n,symbol=sym,entries=len(b),
            status=Counter(x['path_state'] for x in p))),flush=True)
    ent=pd.DataFrame(combined_b);paths=pd.DataFrame(combined_p);trades=pd.DataFrame(combined_t)
    # Every retained post-clock trade must correspond to exactly one frozen V2.6 flip.
    assert len(ent)==len(entries)
    assert len(trades)==len(entries)*len(horizons)*len(TP_VALUES)*(len(FIXED_STOPS)+len(STRUCT_STOPS))
    # For paired comparison, one episode may have both A and B; both remain.
    paths['duplicate_symbol_clock']=paths.duplicated(['symbol','entry_type','entry_ts'],keep='first')
    trades['duplicate_symbol_clock']=trades.duplicated(['symbol','entry_type','entry_ts','horizon_min','tp_pct','stop_type'],keep='first')
    summary=_summarize(trades[~trades.duplicate_symbol_clock])
    # Paired sensitivity analysis only for episodes with BOTH clocks. B exists
    # only after surviving its extra four bars: this is not a randomized A/B test.
    a=trades[trades.entry_type.eq('FLIP_CONFIRMED')]
    b=trades[trades.entry_type.eq('FLIP_SUSTAINED_4B')]
    pair_keys=['attempt_id','symbol','horizon_min','tp_pct','stop_type']
    pair_columns=pair_keys+['known_closed_ts','entry_ts','entry_price','path_state','outcome',
                              'return_after_fee_pct','stop_risk_pct','mfe_to_exit_pct','mae_to_exit_pct']
    paired=a[pair_columns].merge(b[pair_columns],on=pair_keys,validate='one_to_one',
                                  suffixes=('_at_confirmed','_at_sustained'))
    paired['both_paths_complete']=paired.path_state_at_confirmed.eq('COMPLETE_1M') & paired.path_state_at_sustained.eq('COMPLETE_1M')
    paired['paired_net_delta_B_minus_A']=pd.to_numeric(paired.return_after_fee_pct_at_sustained,errors='coerce')-pd.to_numeric(paired.return_after_fee_pct_at_confirmed,errors='coerce')
    coverage=paths.groupby(['entry_type','horizon_min','path_state']).size().rename('n').reset_index()
    hashes={k.name:_sha(k) for k in required}
    manifest=dict(version=VERSION,source_hashes=hashes,minute_dir=str(args.minute_dir),
        candles_30m_dir=str(args.candles_30m_dir),horizons_min=horizons,
        entry_rule='1m OPEN at knowledge_close + 1 minute, never same candle',
        input_entry_types='FLIP_CONFIRMED or FLIP_SUSTAINED_4B observed by clock; not selected by final status',
        stop_rule='Fixed percentage, zone_low-0.1 ATR prior, or retest-low-0.1 ATR prior',
        fee_roundtrip_pct=FEE,assumption='SL wins if both hit same 1m bar; no funding/slippage/portfolio overlaps',
        source_flips=int(flip_count),entries=len(ent),trades=len(trades),paired_scenarios=len(paired),
        full_1m_cases=int(paths.path_state.eq('COMPLETE_1M').sum()),
        gap_1m_cases=int(paths.path_state.ne('COMPLETE_1M').sum()))
    for key,d in [('entry_candidates.csv',ent),('paths.csv',paths),('trades.csv',trades),
                  ('summary.csv',summary),('coverage.csv',coverage),('paired_comparison.csv',paired)]:
        _save_csv(args.output_dir/key,d)
    _save_json(args.output_dir/'manifest.json',manifest)
    print('HLL_V27_DONE',json.dumps({k:manifest[k] for k in ['entries','trades','full_1m_cases','gap_1m_cases']}|{'output':str(args.output_dir)}),flush=True)
    return 0

if __name__=='__main__':raise SystemExit(main())
