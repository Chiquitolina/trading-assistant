#!/usr/bin/env python3
"""Offline TP/SL grid over a frozen V2.2 signal census and existing 1m windows.

Runs the EXISTING V2.2 simulator separately for each SL; does not rescan
levels or alter LIVE. Audits 100% minute coverage, retains all raw events,
and produces a second, deterministic symbol/stage/30m-bar deduplication.
This is discovery, not an out-of-sample strategy validation or a portfolio.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_TPS = '0.75,1,1.5,2,3,4'
DEFAULT_SLS = '0.5,0.75,1,1.5,2,2.5'
DEFAULT_HORIZONS = '60,180,360'
STAGES_PRIMARY = ('SUPPORT_CROSS_DOWN','RESISTANCE_CROSS_UP',
                  'RESISTANCE_ATTACK_BELOW','SUPPORT_ATTACK_ABOVE')
ALLOWED = {'TP_FIRST','SL_FIRST','TIME_EXIT','BOTH_SAME_1M'}


def sha(path):
    h = hashlib.sha256()
    with open(path,'rb') as f:
        for b in iter(lambda: f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()


def grid(text, lower=0, upper=1e6):
    xs = sorted({float(s.strip()) for s in text.split(',')})
    if not xs or any(not np.isfinite(x) or not lower < x <= upper for x in xs):
        raise ValueError('Invalid nonempty parameter list: '+text)
    return xs


def fmt_num(n):
    return f'{n:g}'.replace('.','p')


def eval_tag(prefix, sl):
    return prefix+'_sl'+fmt_num(sl)


def require_inputs(run, minute):
    freeze_path=run/'freeze.json'; sig_path=run/'signals.csv'
    if not all(p.is_file() for p in (freeze_path,sig_path)):
        raise FileNotFoundError('Missing frozen V2.2 run: '+str(run))
    freeze=json.loads(freeze_path.read_text(encoding='utf-8'))
    digest=sha(sig_path)
    if freeze.get('signals_sha256')!=digest:
        raise ValueError('FROZEN_SIGNALS_HASH_CHANGED')
    if not minute.is_dir():
        raise FileNotFoundError('Missing 1m backfill folder: '+str(minute))
    sig=pd.read_csv(sig_path)
    if sig.empty or sig.event_id.duplicated().any():
        raise ValueError('Invalid/duplicate frozen signals')
    for key in ('event_id','stage','symbol','known_closed_ts','level_id'):
        if key not in sig: raise ValueError('Missing signal feature: '+key)
    return sig, digest


def plan(args):
    sig, digest=require_inputs(args.run_dir,args.minute_dir)
    horizons=grid(args.horizons,0,360)
    if any(int(x)!=x for x in horizons): raise ValueError('Horizons must be integer minutes')
    tps=grid(args.tps,0,25); sls=grid(args.sls,0,25)
    if not args.tag or not all(c.isalnum() or c in '._-' for c in args.tag):
        raise ValueError('Unsafe tag')
    if args.fee < 0 or not np.isfinite(args.fee): raise ValueError('Invalid fee')
    output={'tag':args.tag, 'signals_sha256':digest,'symbols':int(sig.symbol.nunique()),
            'events':len(sig),'tp_values':tps,'sl_values':sls,
            'horizons_min':[int(x) for x in horizons], 'fee_roundtrip_pct':args.fee,
            'scenarios_per_event':len(tps)*len(sls)*len(horizons),
            'total_scenarios':len(sig)*len(tps)*len(sls)*len(horizons),
            'backfill_dir':str(args.minute_dir),'source_run':str(args.run_dir)}
    print('HLC_V24_PLAN',json.dumps(output,ensure_ascii=False),flush=True)
    return sig,output


def validated_outcomes(args, sl, meta):
    tag=eval_tag(args.tag,sl)
    path=args.run_dir/'evaluations'/tag
    audit_file=path/'audit.json'; outcome_file=path/'outcomes.csv'
    if not audit_file.is_file() or not outcome_file.is_file():
        raise FileNotFoundError('Missing finished evaluation: '+str(path))
    audit=json.loads(audit_file.read_text(encoding='utf-8'))
    exp=meta['events']*len(meta['tp_values'])*len(meta['horizons_min'])
    if not (audit.get('signals_sha256')==meta['signals_sha256'] and
            audit.get('n_events')==meta['events'] and
            audit.get('n_complete_1m')==exp and audit.get('outcomes')==exp and
            audit.get('path_states')=={'COMPLETE_1M':exp} and
            sorted(float(t) for t in audit.get('tps',[]))==meta['tp_values'] and
            sorted(int(h) for h in audit.get('horizons',[]))==meta['horizons_min'] and
            abs(float(audit.get('sl_pct',-99))-sl)<1e-9 and
            abs(float(audit.get('fee_roundtrip_pct',-99))-meta['fee_roundtrip_pct'])<1e-9):
        raise ValueError('Incorrect or incomplete evaluation: '+tag)
    return path


def run_evals(args, meta):
    exe=Path(__file__).with_name('horizontal_level_cross_research_v22.py')
    if not exe.is_file():
        # Code is deliberately not copied or modified. Use installed project script.
        exe=Path.cwd()/'horizontal_level_cross_research_v22.py'
    if not exe.is_file():raise FileNotFoundError('Cannot locate frozen V2.2 evaluator')
    for sl in meta['sl_values']:
        tag=eval_tag(args.tag,sl)
        path=args.run_dir/'evaluations'/tag
        if path.exists():
            validated_outcomes(args,sl,meta)
            print('HLC_V24_SKIP_VALID',json.dumps({'sl':sl,'eval_tag':tag}),flush=True)
            continue
        cmd=[sys.executable,'-u',str(exe),'--evaluate',
             '--run-dir',str(args.run_dir),'--minute-dir',str(args.minute_dir),
             '--eval-tag',tag,'--tps',','.join(f'{v:g}' for v in meta['tp_values']),
             '--horizons',','.join(str(v) for v in meta['horizons_min']),
             '--sl',f'{sl:g}','--fee',f"{meta['fee_roundtrip_pct']:g}"]
        print('HLC_V24_EVALUATING',json.dumps({'sl':sl,'tag':tag}),flush=True)
        subprocess.run(cmd,check=True)
        validated_outcomes(args,sl,meta)
    print('HLC_V24_ALL_EVALUATIONS_VALID',len(meta['sl_values']),flush=True)


def check_dedup_consistency(raw):
    keys=['stage','symbol','known_closed_ts','horizon_min','tp_pct','sl_pct']
    # Same 30m symbol/stage bar must represent one identical hypothetical trade.
    cols=['direction','entry_ts','entry_price','path_state','outcome','return_after_fee_pct']
    for c in cols:
        z=raw.groupby(keys,dropna=False)[c].nunique(dropna=False)
        if (z>1).any():
            raise ValueError('Conflicting outcomes for simultaneous levels: '+c)


def summarized(raw, basis, extra=()):
    raw=raw.copy()
    raw['net_worst']=pd.to_numeric(raw.return_after_fee_pct,errors='coerce')
    ambiguous=raw.outcome.eq('BOTH_SAME_1M')
    raw.loc[ambiguous,'net_worst']=-raw.loc[ambiguous,'sl_pct']-raw.loc[ambiguous,'fee_roundtrip_pct']
    if raw.net_worst.isna().any():
        raise ValueError('Non-numeric return for an outcome not flagged ambiguous')
    raw['day_utc']=pd.to_datetime(raw.known_closed_ts,unit='ms',utc=True).dt.strftime('%Y-%m-%d')
    keys=['stage','horizon_min','tp_pct','sl_pct']+list(extra)
    rows=[]
    for key,g in raw.groupby(keys,sort=True,dropna=False):
        counts=g.outcome.value_counts()
        tp=int(counts.get('TP_FIRST',0)); sl=int(counts.get('SL_FIRST',0))
        by_sym=g.groupby('symbol').net_worst.mean()
        rows.append(dict(basis=basis,stage=key[0],horizon_min=int(key[1]),tp_pct=float(key[2]),sl_pct=float(key[3]),
            **dict(zip(extra,key[4:])),
            n=len(g),n_symbols=g.symbol.nunique(),n_days=g.day_utc.nunique(),
            n_symbol_days=g.groupby(['symbol','day_utc']).ngroups,
            TP=tp,SL=sl,TIME=int(counts.get('TIME_EXIT',0)),AMB=int(counts.get('BOTH_SAME_1M',0)),
            TP_pct_all=100*tp/len(g), TP_pct_resolved=100*tp/(tp+sl) if tp+sl else np.nan,
            mean_net_worst_pct=g.net_worst.mean(),median_net_worst_pct=g.net_worst.median(),
            equal_symbol_mean_net_pct=by_sym.mean(),positive_symbols=int((by_sym>0).sum()),
            profit_factor=np.nan if g.loc[g.net_worst<0,'net_worst'].empty else (
                g.loc[g.net_worst>0,'net_worst'].sum() / -g.loc[g.net_worst<0,'net_worst'].sum()),
            breakeven_TP_resolved_pct=100*(key[3]+g.fee_roundtrip_pct.iloc[0])/(key[2]+key[3])))
    return pd.DataFrame(rows)


def verify_baseline(args, meta):
    """Verify new V2.4-run output reproduces the old frozen V2.2 settings exactly."""
    baseline=args.run_dir/'evaluations'/'pilot15_binance_backfill_v1'
    if not (baseline/'outcomes.csv').is_file():
        print('HLC_V24_BASELINE_NOT_FOUND (comparison skipped)',flush=True)
        return
    if 1.5 not in meta['sl_values']:
        print('HLC_V24_BASELINE_SL_NOT_IN_GRID',flush=True)
        return
    old_a=json.loads((baseline/'audit.json').read_text(encoding='utf-8'))
    if old_a.get('signals_sha256')!=meta['signals_sha256'] or abs(float(old_a['sl_pct'])-1.5)>1e-9:
        raise ValueError('Frozen baseline not compatible')
    key=['event_id','horizon_min','tp_pct']
    cols=key+['path_state','outcome','entry_ts','entry_price','return_after_fee_pct']
    old=pd.read_csv(baseline/'outcomes.csv',usecols=cols)
    new=pd.read_csv(args.run_dir/'evaluations'/eval_tag(args.tag,1.5)/'outcomes.csv',usecols=cols)
    common=old.merge(new,on=key,how='left',validate='one_to_one',suffixes=('_old','_new'),indicator=True)
    if not common._merge.eq('both').all():
        raise ValueError('Baseline outcomes missing from new risk grid')
    for col in ('path_state','outcome'):
        a=common[col+'_old'].fillna('NA').astype(str)
        b=common[col+'_new'].fillna('NA').astype(str)
        if not a.eq(b).all():raise ValueError('BASELINE_PARITY_FAIL '+col)
    for col in ('entry_ts','entry_price','return_after_fee_pct'):
        a=pd.to_numeric(common[col+'_old'],errors='coerce')
        b=pd.to_numeric(common[col+'_new'],errors='coerce')
        if not (np.isclose(a,b,rtol=1e-9,atol=1e-9,equal_nan=True)).all():
            raise ValueError('BASELINE_PARITY_FAIL '+col)
    print('HLC_V24_BASELINE_PARITY_OK',json.dumps({'old_scenarios_verified':len(common)}),flush=True)


def report(args, sig, meta):
    out_files=[]
    for sl in meta['sl_values']:
        path=validated_outcomes(args,sl,meta)
        x=pd.read_csv(path/'outcomes.csv',low_memory=False)
        if len(x)!=meta['events']*len(meta['tp_values'])*len(meta['horizons_min']):
            raise ValueError('Corrupt outcomes length')
        if not x.path_state.eq('COMPLETE_1M').all() or not set(x.outcome.unique()).issubset(ALLOWED):
            raise ValueError('Path state or outcome invalid')
        if x.duplicated(['event_id','horizon_min','tp_pct']).any():
            raise ValueError('Duplicate scenario outcomes')
        if not set(x.event_id)==set(sig.event_id):
            raise ValueError('Event IDs mismatch frozen signals')
        out_files.append(x)
        print('HLC_V24_READ',json.dumps({'sl':sl,'outcomes':len(x)}),flush=True)
    verify_baseline(args,meta)
    joined=pd.concat(out_files,ignore_index=True)
    joined['sl_pct']=pd.to_numeric(joined.sl_pct,errors='raise')
    joined['tp_pct']=pd.to_numeric(joined.tp_pct,errors='raise')
    joined['fee_roundtrip_pct']=pd.to_numeric(joined.fee_roundtrip_pct,errors='raise')
    check_dedup_consistency(joined)
    # Choose level deterministically using frozen metadata, before reading outcomes.
    selected=sig.sort_values(['stage','symbol','known_closed_ts','level_id','event_id'])\
        .drop_duplicates(['stage','symbol','known_closed_ts'])['event_id']
    dedup=joined[joined.event_id.isin(set(selected))].copy()
    factors=sig[['event_id','vol_ratio','prior_tests']].copy()
    dedup=dedup.merge(factors,on='event_id',how='left',validate='many_to_one')
    dedup['vol_ratio']=pd.to_numeric(dedup.vol_ratio,errors='coerce')
    dedup['vol_group']=np.select([dedup.vol_ratio>=2,dedup.vol_ratio<2],['GE_2','LT_2'],default='UNKNOWN')
    dedup['prior_tests']=pd.to_numeric(dedup.prior_tests,errors='coerce')
    dedup['tests_group']=np.select([dedup.prior_tests.eq(0),dedup.prior_tests.eq(1),dedup.prior_tests.ge(2)],['0','1','2+'],default='UNKNOWN')
    summary=pd.concat([summarized(joined,'ALL_LEVELS'),summarized(dedup,'ONE_PER_STAGE_SYMBOL_BAR')],ignore_index=True)
    priority=summary[(summary.basis=='ONE_PER_STAGE_SYMBOL_BAR') &
                     (summary.stage.isin(STAGES_PRIMARY)) & (summary.n_symbols>=8)]
    top=(priority.sort_values(['stage','equal_symbol_mean_net_pct','mean_net_worst_pct'],ascending=[True,False,False])
         .groupby('stage',sort=True).head(15))
    volume=summarized(dedup,'ONE_PER_STAGE_SYMBOL_BAR',('vol_group',))
    tests=summarized(dedup,'ONE_PER_STAGE_SYMBOL_BAR',('tests_group',))
    args.output_dir.mkdir(parents=True,exist_ok=True)
    for name,data in [('risk_grid_full.csv',summary),('risk_grid_top15_per_stage.csv',top),
                      ('risk_grid_by_volume.csv',volume),('risk_grid_by_prior_tests.csv',tests)]:
        target=args.output_dir/name
        if target.exists():raise FileExistsError('Refusing to overwrite '+str(target))
        data.to_csv(target,index=False)
    (args.output_dir/'run_manifest.json').write_text(json.dumps({**meta,
        'method':'V2.2 real 1m simulator, same frozen events; 1 per stage/symbol/30m bar',
        'ranking':'Exploratory equal-symbol mean, ambiguous=SL (worst-case)',
        'warning':'Repeated/time-overlapping trades, no funding/slippage/portfolio; sweep is in-sample discovery'},indent=2),encoding='utf-8')
    print('\n=== HLC_V24_RESULTS: DEDUP TOP 8 PER STAGE (by equal-symbol mean) ===')
    cols=['stage','horizon_min','tp_pct','sl_pct','n','TP','SL','TIME','AMB',
          'TP_pct_all','mean_net_worst_pct','equal_symbol_mean_net_pct','positive_symbols']
    print(top.groupby('stage',sort=True).head(8)[cols].round(3).to_string(index=False))
    print('HLC_V24_DONE',json.dumps({'scenario_rows':len(joined),'unique_stage_bar_scenarios':len(dedup),
        'all_scenarios':len(summary),'path':str(args.output_dir),'source_hash':meta['signals_sha256']}),flush=True)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    group=p.add_mutually_exclusive_group(required=True)
    group.add_argument('--plan',action='store_true')
    group.add_argument('--run',action='store_true')
    group.add_argument('--report',action='store_true')
    p.add_argument('--run-dir',type=Path,required=True)
    p.add_argument('--minute-dir',type=Path,required=True)
    p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--tag',default='pilot15_risk_v24')
    p.add_argument('--tps',default=DEFAULT_TPS)
    p.add_argument('--sls',default=DEFAULT_SLS)
    p.add_argument('--horizons',default=DEFAULT_HORIZONS)
    p.add_argument('--fee',type=float,default=.1)
    args=p.parse_args(argv)
    args.run_dir=args.run_dir.resolve()
    args.minute_dir=args.minute_dir.resolve()
    args.output_dir=args.output_dir.resolve()
    sig,meta=plan(args)
    if args.run: run_evals(args,meta);report(args,sig,meta)
    if args.report: report(args,sig,meta)
    return 0

if __name__=='__main__': raise SystemExit(main())
