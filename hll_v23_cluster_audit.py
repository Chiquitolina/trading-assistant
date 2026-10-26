#!/usr/bin/env python3
"""Independent V2.3 audit of a frozen HLC V2.2 evaluation.

No downloads, model fitting, indicators recomputation, broker actions or writes to V2.2 runs.
One observation per symbol+30m signal-close, only after an outcome consistency check.
Bootstrap clusters by UTC calendar day; exploratory intervals, NOT independent-trade CIs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
import pandas as pd

STAGES = ('SUPPORT_CROSS_DOWN', 'RESISTANCE_CROSS_UP')
ALLOWED = {'TP_FIRST', 'SL_FIRST', 'TIME_EXIT', 'BOTH_SAME_1M'}


def sha256(path):
    h=hashlib.sha256()
    with open(path, 'rb') as fh:
        for b in iter(lambda: fh.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def read_inputs(run_dir, eval_tag, horizon, tp):
    run_dir=Path(run_dir)
    signals_file=run_dir/'signals.csv'
    freeze_file=run_dir/'freeze.json'
    audit_file=run_dir/'evaluations'/eval_tag/'audit.json'
    outcomes_file=run_dir/'evaluations'/eval_tag/'outcomes.csv'
    for f in (signals_file, freeze_file, audit_file, outcomes_file):
        if not f.is_file(): raise FileNotFoundError(f)
    freeze=json.loads(freeze_file.read_text(encoding='utf-8'))
    eval_audit=json.loads(audit_file.read_text(encoding='utf-8'))
    sh=sha256(signals_file)
    if sh!=freeze.get('signals_sha256') or sh!=eval_audit.get('signals_sha256'):
        raise ValueError('Frozen V2.2 signals checksum mismatch')
    if eval_audit.get('n_complete_1m') != eval_audit.get('outcomes'):
        raise ValueError('Outcome coverage is NOT fully COMPLETE_1M; abort to avoid denominator bias')
    signals=pd.read_csv(signals_file, low_memory=False)
    outcomes=pd.read_csv(outcomes_file, low_memory=False)
    mask=(outcomes['horizon_min'].eq(horizon) &
          pd.to_numeric(outcomes['tp_pct'],errors='coerce').eq(tp) &
          outcomes['stage'].isin(STAGES))
    x=outcomes.loc[mask].copy()
    if x.empty: raise ValueError('No outcomes for requested stage, horizon and TP')
    if not x['path_state'].eq('COMPLETE_1M').all():
        raise ValueError('Selected outcomes include paths without COMPLETE_1M')
    if not set(x.outcome.dropna()).issubset(ALLOWED):
        raise ValueError('Unexpected outcome labels')
    if x.event_id.duplicated().any(): raise ValueError('Duplicate event_id outcomes')
    feature_cols=['event_id','symbol','stage','known_closed_ts','level_id','vol_ratio',
                  'range_atr','prior_tests','level_age_bars']
    if not set(feature_cols).issubset(signals.columns):
        raise ValueError('Missing frozen causal features')
    sig=signals[feature_cols].copy()
    if sig.event_id.duplicated().any(): raise ValueError('Duplicate frozen signals')
    x=x.merge(sig, on='event_id', how='left', validate='one_to_one',
              suffixes=('', '_signal'), indicator=True)
    if not x['_merge'].eq('both').all(): raise ValueError('Orphan outcome')
    for c in ('symbol','stage','known_closed_ts'):
        if not x[c].astype(str).equals(x[c+'_signal'].astype(str)):
            raise ValueError('Outcome/signal discrepancy for '+c)
    x=x.drop(columns=['_merge']+[c+'_signal' for c in ('symbol','stage','known_closed_ts')])
    x['vol_ratio']=pd.to_numeric(x.vol_ratio, errors='coerce')
    x['range_atr']=pd.to_numeric(x.range_atr, errors='coerce')
    x['prior_tests']=pd.to_numeric(x.prior_tests, errors='coerce')
    x['return_after_fee_pct']=pd.to_numeric(x.return_after_fee_pct,errors='coerce')
    x['entry_price']=pd.to_numeric(x.entry_price,errors='coerce')
    x['day_utc']=pd.to_datetime(x.known_closed_ts,unit='ms',utc=True).dt.strftime('%Y-%m-%d')
    x['vol_group']=np.select([x.vol_ratio>=2,x.vol_ratio<2], ['GE_2','LT_2'],default='UNKNOWN')
    x['range_group']=pd.cut(x.range_atr,bins=[-np.inf,0.75,1.5,np.inf],
                    labels=['LT_075','075_TO_150','GE_150'],right=False).astype(object).fillna('UNKNOWN')
    x['test_group']=x.prior_tests.map(lambda n:'UNKNOWN' if pd.isna(n) else '0' if n<1 else '1' if n<2 else '2+')
    return x, dict(eval_tag=eval_tag,signals_sha256=sh,eval_audit=eval_audit,horizon_min=horizon,tp_pct=tp)


def check_and_dedup(x):
    """Require duplicates of one symbol/30m close to be the SAME hypothetical trade."""
    keys=['stage','symbol','known_closed_ts']
    for key,g in x.groupby(keys):
        if len(g)<2: continue
        for c in ('entry_ts','entry_price','direction','outcome','return_after_fee_pct','vol_group'):
            nums=pd.to_numeric(g[c],errors='coerce') if c in ('entry_ts','entry_price','return_after_fee_pct') else None
            if nums is not None:
                vals=nums.dropna()
                if len(vals)>0 and (vals.max()-vals.min()>1e-7):
                    raise ValueError(f'Same-candle different {c}: {key}')
                if g[c].isna().sum() not in (0,len(g)):
                    raise ValueError(f'Same-candle partial {c}: {key}')
            elif g[c].nunique(dropna=False)!=1:
                raise ValueError(f'Same-candle different {c}: {key}')
    # Lexical level_id is deliberately NOT chosen using outcome or a tuned signal feature.
    return x.sort_values(keys+['level_id','event_id']).drop_duplicates(keys).copy()


def describe(g):
    n=len(g)
    if not n: return dict(N=0)
    counts=g.outcome.value_counts()
    tp=int(counts.get('TP_FIRST',0)); sl=int(counts.get('SL_FIRST',0))
    returns=g.return_after_fee_pct.dropna()
    return dict(N=n, symbols=int(g.symbol.nunique()), days=int(g.day_utc.nunique()),
        symbol_days=int(g.groupby(['symbol','day_utc']).ngroups),
        levels=int(g.groupby(['symbol','level_id']).ngroups),
        TP=tp, SL=sl, TIME=int(counts.get('TIME_EXIT',0)),AMB=int(counts.get('BOTH_SAME_1M',0)),
        TP_all_pct=100*tp/n, TP_resolved_pct=(100*tp/(tp+sl) if tp+sl else None),
        mean_net_pct=(float(returns.mean()) if len(returns) else None),
        median_net_pct=(float(returns.median()) if len(returns) else None))


def strata(x, cols):
    return pd.DataFrame([{**dict(zip(cols,keys if isinstance(keys,tuple) else (keys,))),**describe(g)}
            for keys,g in x.groupby(cols,dropna=False)]).sort_values(cols).reset_index(drop=True)


def differences(g):
    a=g[g.vol_group=='GE_2'];b=g[g.vol_group=='LT_2']
    if not (len(a) and len(b)):return None
    net_a=a.return_after_fee_pct.dropna();net_b=b.return_after_fee_pct.dropna()
    return dict(N_high=len(a),N_low=len(b),
       d_TP_percentage_points=100*((a.outcome=='TP_FIRST').mean()-(b.outcome=='TP_FIRST').mean()),
       d_mean_net_pct=(float(net_a.mean()-net_b.mean()) if len(net_a) and len(net_b) else np.nan))


def remove_one(x, col):
    arr=[]
    for value in sorted(x[col].dropna().unique()):
        d=differences(x[x[col]!=value]);
        if d:arr.append({col+'_removed':value,**d})
    return pd.DataFrame(arr)


def day_bootstrap(x, draws=2000, seed=20261026):
    """Resample entire UTC *days*, preserving all symbols and dependence within each day."""
    rng=np.random.default_rng(seed)
    grouped={key:val for key,val in x.groupby('day_utc')}
    days=sorted(grouped)
    if len(days)<4:return dict(note='too few independent calendar days for day bootstrap')
    tp_delta=[];net_delta=[]
    for _ in range(draws):
        draw=rng.choice(days,size=len(days),replace=True)
        z=pd.concat([grouped[d] for d in draw],ignore_index=True)
        d=differences(z)
        if d is None:continue
        tp_delta.append(d['d_TP_percentage_points'])
        net_delta.append(d['d_mean_net_pct'])
    def interval(a):
        a=np.array(a,dtype=float);a=a[np.isfinite(a)]
        return dict(n=len(a),p2_5=float(np.quantile(a,.025)),median=float(np.median(a)),
                    p97_5=float(np.quantile(a,.975)),fraction_positive=float(np.mean(a>0))) if len(a) else None
    return dict(days=len(days),draws=draws,seed=seed,
                tp_difference_pp=interval(tp_delta),mean_net_difference_pct=interval(net_delta),
                warning='Exploratory 16-day regime sensitivity, not a valid independent-trade significance test')


def analyze(raw, meta, focus='SUPPORT_CROSS_DOWN'):
    dedup=check_and_dedup(raw)
    x=dedup[dedup.stage==focus].copy()
    x=x[x.vol_group.isin(('GE_2','LT_2'))].copy()
    if x.empty:raise ValueError('No eligible observations for '+focus)
    paired=[]
    for key,g in x.groupby(['symbol','day_utc']):
        d=differences(g)
        if d:paired.append(dict(symbol=key[0],day_utc=key[1],**d))
    matched=pd.DataFrame(paired)
    outputs={
        'raw_overview':strata(raw,['stage','vol_group']),
        'deduplicated_overview':strata(dedup,['stage','vol_group']),
        'focus_by_symbol':strata(x,['symbol','vol_group']),
        'focus_by_day':strata(x,['day_utc','vol_group']),
        'focus_by_range':strata(x,['range_group','vol_group']),
        'focus_by_tests':strata(x,['test_group','vol_group']),
        'matched_symbol_days':matched,
        'leave_one_symbol_out':remove_one(x,'symbol'),
        'leave_one_day_out':remove_one(x,'day_utc')}
    dif=differences(x)
    summary=dict(**meta, focus=focus,raw_events=len(raw),deduplicated_events=len(dedup),
                focus_events=len(x), removed_duplicate_records=len(raw)-len(dedup),
                focus_difference=dif,focus_by_volume=strata(x,['vol_group']).to_dict(orient='records'),
                matched_symbol_days=int(len(matched)),
                matched_equal_weight=dict(
                    mean_TP_difference_pp=(float(matched.d_TP_percentage_points.mean()) if len(matched) else None),
                    mean_net_difference_pct=(float(matched.d_mean_net_pct.mean()) if len(matched) else None)),
                day_bootstrap=day_bootstrap(x))
    return summary,outputs


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir',type=Path,required=True)
    p.add_argument('--eval-tag',required=True)
    p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--horizon-min',type=int,default=180)
    p.add_argument('--tp-pct',type=float,default=2.0)
    p.add_argument('--focus',choices=STAGES,default='SUPPORT_CROSS_DOWN')
    p.add_argument('--plan',action='store_true')
    a=p.parse_args(argv)
    x,meta=read_inputs(a.run_dir,a.eval_tag,a.horizon_min,a.tp_pct)
    summary,tables=analyze(x,meta,a.focus)
    print('HLC_V23_CLUSTER_AUDIT',json.dumps(summary,default=str,ensure_ascii=False),flush=True)
    for name in ('deduplicated_overview','focus_by_range','matched_symbol_days','leave_one_day_out'):
        print('\n=== '+name+' ===\n'+tables[name].round(3).to_string(index=False),flush=True)
    if a.plan:
        print('HLC_V23_PLAN_ONLY no files written')
        return
    dst=a.output_dir
    if dst.exists():raise SystemExit('OUTPUT_DIR_EXISTS: no overwrite')
    dst.mkdir(parents=True,exist_ok=False)
    (dst/'audit_summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False,default=str),encoding='utf-8')
    for name,df in tables.items():
        df.to_csv(dst/(name+'.csv'),index=False)
    print('HLC_V23_DONE',json.dumps(dict(output_dir=str(dst),tables=len(tables))),flush=True)

if __name__=='__main__':main()
