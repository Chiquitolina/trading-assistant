#!/usr/bin/env python3
"""V2.8.2: freeze PRE geometry of *every* V2.6 breakout attempt; attach outcomes later.

Research only. Does not change original V2.6/V2.7/V2.8/V2.8.1 data, LIVE or orders.
CLI is intended to be called from /home/multiservices-1m.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import shutil
from collections import Counter
from pathlib import Path

import pandas as pd
import numpy as np

try:
    from dashboard.views.hll_v281_structure_patterns import (
        WINDOWS, TF, pivot_lows_known, load_candles, window_features,
        classify_structure, _save_csv, _save_json,
    )
except ModuleNotFoundError:
    from hll_v281_structure_patterns import (
        WINDOWS, TF, pivot_lows_known, load_candles, window_features,
        classify_structure, _save_csv, _save_json,
    )

V26 = Path('reports/horizontal_level_structure_lab_v26/runs/core15_extended_v26')
CANDLES = Path('reports/horizontal_level_lifecycle_v21/candles_aug20_oct25_core15_btc')
OUTPUT = Path('reports/horizontal_level_structure_quality_v282/runs/core15_pre_all_v282')
# No observed flip != failed flip. Non-observable endpoints are explicitly censored.
UNRESOLVED = {
    'UNRESOLVED_FIRST4', 'RETEST_UNRESOLVED',
    'CENSORED_ACCEPTANCE', 'CENSORED_POST_ACCEPTANCE', 'CENSORED_RETEST',
}


def parse_ts(value):
    if pd.isna(value) or str(value).strip() in ('', 'None', 'nan', 'NaN'):
        return None
    n = int(float(value))
    return n if n > 0 else None


def outcome_class(status, flip_ts):
    if flip_ts is not None:
        return 'FLIP_CONFIRMADO'
    if status in UNRESOLVED or status.startswith('CENSORED_') or status.endswith('_CENSORED'):
        return 'INDETERMINADO'
    if status in {
        'FAILED_BREAKOUT', 'RETEST_FAILED', 'ACCEPTED_THEN_FAILED_NO_CONTACT',
        'DIRECT_NO_RETEST_24B',
    }:
        return 'SIN_FLIP_EN_VENTANA'
    # Fail closed for unexpected labels: never misrepresent as failure.
    return 'INDETERMINADO'


def join_attempts(attempts, outcomes):
    """One original PRE candidate per unique V2.6 attempt_id. No filtering by outcome."""
    need_a = {'attempt_id','symbol','known_closed_ts','zone_low','zone_high','atr_pre'}
    need_o = {'attempt_id','status','defense_closed_ts','retest_closed_ts','failed_closed_ts'}
    if need_a - set(attempts) or need_o - set(outcomes):
        raise ValueError(f'Missing columns A={need_a-set(attempts)}, O={need_o-set(outcomes)}')
    if attempts.attempt_id.isna().any() or outcomes.attempt_id.isna().any():
        raise ValueError('Blank attempt_id')
    if attempts.attempt_id.duplicated().any() or outcomes.attempt_id.duplicated().any():
        raise ValueError('Duplicate V2.6 attempt_id')
    x = attempts.merge(outcomes[['attempt_id','status','defense_closed_ts','retest_closed_ts','failed_closed_ts']].copy(),
                       on='attempt_id',how='left',indicator=True,validate='one_to_one')
    if x._merge.ne('both').any() or len(x)!=len(attempts):
        raise ValueError('V2.6 outcomes incomplete for attempts')
    x=x.drop(columns='_merge')
    return x


def create_cohort(joined, candles_dir, max_symbols=0):
    data=joined.copy()
    symbols=sorted(data.symbol.dropna().unique().tolist())
    if max_symbols>0: symbols=symbols[:max_symbols]
    result=[]
    for num,sym in enumerate(symbols,1):
        fp=candles_dir/f'{sym}_30m.csv'
        if not fp.is_file(): raise FileNotFoundError(f'Faltan velas originales 30m: {fp}')
        bars=load_candles(fp)
        pivots=pivot_lows_known(bars)
        for r in data.loc[data.symbol.eq(sym)].to_dict('records'):
            attempt_clock=parse_ts(r['known_closed_ts'])
            if attempt_clock is None: raise ValueError(f'{r["attempt_id"]}: missing breakout known timestamp')
            pre_clock=attempt_clock-TF
            flip_ts=parse_ts(r['defense_closed_ts'])
            retest_ts=parse_ts(r['retest_closed_ts'])
            failed_ts=parse_ts(r['failed_closed_ts'])
            if flip_ts is not None and (flip_ts<=attempt_clock or (retest_ts is not None and flip_ts<=retest_ts)):
                raise ValueError(f'{r["attempt_id"]}: inconsistent flip clock')
            status=str(r['status'])
            lo,hi,atr=(float(r['zone_low']),float(r['zone_high']),float(r['atr_pre']))
            if not np.isfinite([lo,hi,atr]).all() or lo<=0 or hi<lo or atr<=0:
                raise ValueError(f'{r["attempt_id"]}: invalid zone/ATR')
            o=dict(attempt_id=r['attempt_id'],symbol=sym,level_id=r.get('level_id'),
                   breakout_closed_ts=attempt_clock,pre_closed_ts=pre_clock,
                   zone_low=lo,zone_high=hi,atr_pre=atr,
                   v26_status=status,flip_confirmed_closed_ts=flip_ts,
                   retest_closed_ts=retest_ts,failed_closed_ts=failed_ts,
                   post_outcome=outcome_class(status,flip_ts))
            for win in WINDOWS:
                pre=window_features(bars,pivots,pre_clock,win,lo,hi,atr)
                o[f'pre_pattern_{win}']=classify_structure(pre)
                o[f'pre_{win}_coverage']=bool(pre['coverage'])
                o[f'pre{win}_pivot_lows_json']=pre['pivot_lows_json']
                o[f'pre{win}_pivots']=pre['pivots']
                if flip_ts is not None:
                    flip=window_features(bars,pivots,flip_ts,win,lo,hi,atr)
                    o[f'flip_pattern_{win}']=classify_structure(flip)
                    o[f'flip{win}_pivot_lows_json']=flip['pivot_lows_json']
                else:
                    o[f'flip_pattern_{win}']='NO_APLICA'
                    o[f'flip{win}_pivot_lows_json']='[]'
            result.append(o)
        print('HLL_V282_SYMBOL',json.dumps({'index':num,'symbol':sym,
            'attempts':int(data.symbol.eq(sym).sum())}),flush=True)
    return pd.DataFrame(result)


def sha(path):
    obj=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024*1024),b''):obj.update(block)
    return obj.hexdigest()


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true');mode.add_argument('--run',action='store_true')
    p.add_argument('--v26-dir',type=Path,default=V26)
    p.add_argument('--candles-dir',type=Path,default=CANDLES)
    p.add_argument('--output-dir',type=Path,default=OUTPUT)
    p.add_argument('--max-symbols',type=int,default=0)
    p.add_argument('--min-free-gb',type=float,default=.15)
    args=p.parse_args(argv)
    if args.max_symbols<0:raise SystemExit('--max-symbols must be >= 0')
    sources=[args.v26_dir/'breakout_attempts.csv',args.v26_dir/'attempt_outcomes.csv']
    for src in sources:
        if not src.is_file():raise SystemExit('Falta archivo V2.6: '+str(src))
    a=pd.read_csv(sources[0],low_memory=False);o=pd.read_csv(sources[1],low_memory=False)
    merged=join_attempts(a,o)
    if merged.symbol.isna().any():raise SystemExit('Blank symbol in V2.6')
    syms=sorted(merged.symbol.unique().tolist())
    if args.max_symbols:syms=syms[:args.max_symbols]
    scope=merged[merged.symbol.isin(syms)]
    plan=dict(version='HLL_V282_PRE_COHORT',mode='OFFLINE_READ_ONLY',
              original_attempts=len(scope),symbols=len(syms),windows=list(WINDOWS),
              outcome_counts=dict(Counter(outcome_class(str(r.status),parse_ts(r.defense_closed_ts))
                                          for r in scope.itertuples(index=False))),
              source=str(args.v26_dir),output=str(args.output_dir))
    print('HLL_V282_PLAN',json.dumps(plan,ensure_ascii=False),flush=True)
    if args.plan:return 0
    dest=args.output_dir.resolve()
    for src in [args.v26_dir.resolve(),args.candles_dir.resolve()]:
        if dest==src or src in dest.parents:raise SystemExit('Output must not nest inside source')
    if dest.exists() and any(dest.iterdir()):raise SystemExit('OUTPUT_EXISTS: no overwriting historical run')
    if shutil.disk_usage(Path.cwd()).free < args.min_free_gb*1024**3:
        raise SystemExit('Insufficient disk space')
    tax=create_cohort(scope,args.candles_dir)
    if len(tax)!=len(scope) or tax.attempt_id.duplicated().any():
        raise ValueError('Cohort cardinality mismatch')
    _save_csv(args.output_dir/'pre_cohort.csv',tax)
    _save_json(args.output_dir/'manifest.json',dict(**plan,
        source_hashes={src.name:sha(src) for src in sources},
        pattern_counts_48=dict(Counter(tax.pre_pattern_48)),
        after_known_clock='POST is attached later by attempt_id, never used to assign PRE',
        limitations='V2.6 horizon bounds. INDETERMINADO != FAILED. No profit statistics, October already inspected; repeated symbols/levels are dependent.'))
    print('HLL_V282_DONE',json.dumps(dict(attempts=len(tax),outcomes=dict(Counter(tax.post_outcome)),
             output=str(args.output_dir)),ensure_ascii=False),flush=True)
    return 0

if __name__=='__main__':raise SystemExit(main())
