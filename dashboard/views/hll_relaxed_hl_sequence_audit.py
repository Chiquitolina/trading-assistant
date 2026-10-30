#!/usr/bin/env python3
"""HLL offline research: FIRST rising line -> breach -> NEW confirmed higher low.

Causal PRE-only sequence audit. No detector mutations, no orders, no LIVE.
The first eligible 3-pivot line, NOT V291's latest broken line, is studied.
An attempt-only cohort CANNOT estimate P(breakout | structure) without a
pre-breakout opportunity denominator. Keep all labels exploratory.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

TF=1_800_000
WINDOWS=(24,48,96)
ATR_STEP=.10
ATR_BREACH=.35
MIN_SPAN_BARS=8
COHORT=Path('reports/horizontal_level_structure_quality_v282/runs/core15_pre_all_v282/pre_cohort.csv')
V291=Path('reports/horizontal_level_structure_integrity_v291/runs/core15_trendline_v291')
CANDLES=Path('reports/horizontal_level_lifecycle_v21/candles_aug20_oct25_core15_btc')
OUTPUT=Path('reports/horizontal_level_relaxed_hl_sequence/core15_initial_line_new_hl')


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1<<20),b''):h.update(b)
    return h.hexdigest()


def load_bars(path):
    c=pd.read_csv(path,usecols=['timestamp','open','high','low','close'])
    if c.empty:raise ValueError(f'Empty candles {path}')
    for col in c.columns:c[col]=pd.to_numeric(c[col],errors='raise')
    c['timestamp']=c.timestamp.astype('int64')
    c=c.sort_values('timestamp',kind='stable').reset_index(drop=True)
    if c.timestamp.duplicated().any():raise ValueError(f'Duplicate 30m timestamps {path}')
    a=c[['open','high','low','close']].to_numpy(float)
    if (not np.isfinite(a).all() or (a<=0).any() or (c.low>c.high).any()
        or (c.low>c.open).any() or (c.low>c.close).any()
        or (c.high<c.open).any() or (c.high<c.close).any()):
        raise ValueError(f'Invalid OHLC {path}')
    if (c.timestamp%TF!=0).any():raise ValueError(f'Unaligned timestamps {path}')
    return c


def parse_pivots(raw,pre,from_ts):
    if pd.isna(raw) or not str(raw).strip():return []
    p=json.loads(raw)
    out=[]
    for x in p:
        v={'open_ts':int(x['open_ts']),'known_closed_ts':int(x['known_closed_ts']),
           'low':float(x['low'])}
        if not np.isfinite(v['low']) or v['low']<=0:raise ValueError('Bad pivot low')
        if v['known_closed_ts']<=pre and from_ts<=v['open_ts']<pre:
            out.append(v)
    out.sort(key=lambda x:x['open_ts'])
    if len({v['open_ts'] for v in out})!=len(out):raise ValueError('Duplicate pivots')
    return out


def initial_line_and_break(bars,pivots,atr,pre):
    """Choose earliest chronologically confirmed valid 3-pivot rising line.

    No selection on whether line later breaks, no selection on future PnL.
    A break needs two consecutive CLOSED 30m candles below by .35 ATR.
    """
    empty={'line_state':'SIN_DIRECTRIZ_INICIAL', 'initial_slope_atr_per_bar':np.nan,
           'anchor_first_ts':None,'anchor_third_ts':None,'anchor_confirmed_ts':None,
           'anchor_last_low':np.nan,'anchor_prior_low':np.nan,
           'initial_break_closed_ts':None,'initial_break_depth_atr':np.nan}
    if len(pivots)<3:return empty
    ts=bars.timestamp.to_numpy('int64')
    closes=bars.close.to_numpy(float)
    for i in range(len(pivots)-2):
        tri=pivots[i:i+3]
        xx=np.array([(v['open_ts']-tri[0]['open_ts'])/TF for v in tri],float)
        yy=np.array([v['low'] for v in tri],float)
        if xx[-1]<MIN_SPAN_BARS or (np.diff(yy)<ATR_STEP*atr).any():continue
        slope,intercept=np.polyfit(xx,yy,1)
        if not np.isfinite(slope) or slope<=0:continue
        known=int(tri[-1]['known_closed_ts'])
        if known>pre:continue
        line=intercept+slope*((ts.astype(float)-tri[0]['open_ts'])/TF)
        result=dict(empty,line_state='INICIAL_INTACTA',
                    initial_slope_atr_per_bar=float(slope/atr),
                    anchor_first_ts=int(tri[0]['open_ts']),
                    anchor_third_ts=int(tri[-1]['open_ts']),
                    anchor_confirmed_ts=known,
                    anchor_last_low=float(yy[-1]),anchor_prior_low=float(yy[-2]))
        below=closes < line-ATR_BREACH*atr
        # Each bar's close is known at its OPEN timestamp + TF.
        start=int(np.searchsorted(ts,known))
        for j in range(start+1,len(ts)):
            if int(ts[j])+TF>pre:break
            if below[j-1] and below[j]:
                result.update(line_state='INICIAL_ROTA',
                    initial_break_closed_ts=int(ts[j])+TF,
                    initial_break_depth_atr=float((line[j]-closes[j])/atr))
                break
        return result
    return empty


def sequence_at_pre(bars,pivots,line,atr,pre,zone_low):
    """First confirmed new pivot AFTER the initial-line break. No POST data.

    STRICT: new pivot above third original anchor; RELAXED: above penultimate
    anchor but not last (structural hierarchy ambiguous, inspect visually).
    """
    empty=dict(sequence='NO_ROTURA_DE_LINEA_INICIAL',new_hl_open_ts=None,
      new_hl_known_closed_ts=None,new_hl_low=np.nan,
      low_vs_last_anchor_atr=np.nan,low_vs_prior_anchor_atr=np.nan,
      relaxed_slope_atr_per_bar=np.nan,slope_ratio_to_initial=np.nan,
      slope_reduced=None,zone_reapproach_before_breakout=None,
      reapproach_first_known_ts=None,post_break_pivots_confirmed=0,
      min_low_since_anchor_atr_vs_last=np.nan,anchor_low_was_undercut=None)
    if line['line_state']!='INICIAL_ROTA':return empty
    broken=int(line['initial_break_closed_ts'])
    later=[v for v in pivots if v['open_ts']>=broken and v['known_closed_ts']<=pre]
    out=dict(empty,sequence='ROTA_SIN_NUEVO_PIVOTE_CONFIRMADO',post_break_pivots_confirmed=len(later))
    if not later:return out
    # The FIRST pivot is mandatory; do not cherry-pick a later favorable pivot.
    x=later[0]
    diff_last=(x['low']-line['anchor_last_low'])/atr
    diff_prior=(x['low']-line['anchor_prior_low'])/atr
    # A pivot that LOOKS higher must not hide an earlier wick under the floor.
    # Inspect the complete causal path from anchor confirmation through new pivot
    # confirmation; a wick undercut is flagged for review, never silently valid.
    formation=bars.loc[(bars.timestamp>=line['anchor_confirmed_ts']) &
                        (bars.timestamp+TF<=x['known_closed_ts'])]
    floor_min=float(formation.low.min()) if not formation.empty else np.nan
    undercut=bool(np.isfinite(floor_min) and floor_min < line['anchor_last_low'])
    if diff_last>=ATR_STEP and not undercut:
        state='ROTA_NUEVO_HL_ESTRICTO'
    elif diff_last>=ATR_STEP and undercut:
        state='ROTA_NUEVO_HL_CON_BARRIDA_PREVIA_REVISAR'
    elif diff_prior>=ATR_STEP:
        state='ROTA_CORRECCION_SOBRE_HL_ANTERIOR_REVISAR'
    else:
        state='ROTA_NUEVO_MINIMO_NO_HL'
    out.update(sequence=state,new_hl_open_ts=int(x['open_ts']),
        new_hl_known_closed_ts=int(x['known_closed_ts']),new_hl_low=float(x['low']),
        low_vs_last_anchor_atr=float(diff_last),low_vs_prior_anchor_atr=float(diff_prior),
        min_low_since_anchor_atr_vs_last=float((floor_min-line['anchor_last_low'])/atr),
        anchor_low_was_undercut=undercut)
    # Rate connecting old anchor -> confirmed new low, not a retrofitted line.
    steps=(x['open_ts']-line['anchor_third_ts'])/TF
    if steps>0:
        slope=(x['low']-line['anchor_last_low'])/(steps*atr)
        ratio=slope/line['initial_slope_atr_per_bar']
        out.update(relaxed_slope_atr_per_bar=float(slope),
                   slope_ratio_to_initial=float(ratio),
                   slope_reduced=bool(0<=slope<line['initial_slope_atr_per_bar']))
    # Reapproach is separate from the break itself. This is PRE ONLY.
    future_pre=bars[(bars.timestamp>=x['known_closed_ts'])&(bars.timestamp+TF<=pre)]
    hits=future_pre.loc[future_pre.high >= zone_low-0.25*atr]
    out['zone_reapproach_before_breakout']=bool(len(hits))
    if len(hits):out['reapproach_first_known_ts']=int(hits.iloc[0].timestamp)+TF
    return out


def evaluate(row,bars,w):
    pre=int(row['pre_closed_ts']);atr=float(row['atr_pre'])
    lo=float(row['zone_low'])
    if not (np.isfinite(atr) and atr>0 and np.isfinite(lo) and lo>0):
        raise ValueError('Invalid PRE ATR/zone')
    start=pre-w*TF
    sub=bars.loc[(bars.timestamp>=start)&(bars.timestamp<pre)].copy()
    key=dict(attempt_id=str(row['attempt_id']),symbol=str(row['symbol']),
             level_id=str(row.get('level_id','')),window_30m=int(w),
             pre_closed_ts=pre,breakout_closed_ts=int(row['breakout_closed_ts']),
             original_pre_pattern=str(row[f'pre_pattern_{w}']))
    if len(sub)!=w or sub.timestamp.astype('int64').tolist()!=list(range(start,pre,TF)):
        return dict(key,coverage='GAP_30M',sequence='INDETERMINADO_GAP_30M',
                    line_state='INDETERMINADO_GAP_30M')
    pivots=parse_pivots(row[f'pre{w}_pivot_lows_json'],pre,start)
    line=initial_line_and_break(sub,pivots,atr,pre)
    progression=sequence_at_pre(sub,pivots,line,atr,pre,lo)
    return dict(key,coverage='COMPLETE',confirmed_pivots=len(pivots),**line,**progression)


def sources(cohort_path,v291_dir):
    if not cohort_path.is_file():raise FileNotFoundError(cohort_path)
    audit_path=v291_dir/'integrity_audit.csv';manifest_path=v291_dir/'manifest.json'
    if not audit_path.is_file() or not manifest_path.is_file():
        raise FileNotFoundError(f'V291 missing {v291_dir}')
    meta=json.loads(manifest_path.read_text(encoding='utf-8'))
    if meta.get('original_sha256')!=sha(cohort_path):
        raise ValueError('V291 mismatch PRE hash')
    c=pd.read_csv(cohort_path,low_memory=False)
    a=pd.read_csv(audit_path,usecols=['attempt_id'])
    if len(c)!=len(a) or c.attempt_id.isna().any() or c.attempt_id.duplicated().any() or a.attempt_id.duplicated().any() or set(c.attempt_id.astype(str))!=set(a.attempt_id.astype(str)):
        raise ValueError('V282/V291 cohort mismatch')
    required={'attempt_id','symbol','level_id','pre_closed_ts','breakout_closed_ts','atr_pre','zone_low'}
    for w in WINDOWS:required.update({f'pre_pattern_{w}',f'pre{w}_pivot_lows_json'})
    if required-set(c):raise ValueError('Missing PRE fields: '+str(sorted(required-set(c))))
    return c


def run(c,candles,max_symbols):
    syms=sorted(c.symbol.astype(str).unique())
    if max_symbols:syms=syms[:max_symbols]
    results=[]
    for k,sym in enumerate(syms,1):
        path=candles/f'{sym}_30m.csv'
        if not path.is_file():raise FileNotFoundError(path)
        bars=load_bars(path)
        for r in c.loc[c.symbol.eq(sym)].to_dict('records'):
            for w in WINDOWS:results.append(evaluate(r,bars,w))
        print('HLL_RELAXED_HL_PROGRESS',json.dumps(dict(symbol=k,total=len(syms),name=sym)),flush=True)
    d=pd.DataFrame(results)
    if len(d)!=len(c[c.symbol.isin(syms)])*len(WINDOWS) or d[['attempt_id','window_30m']].duplicated().any():
        raise ValueError('Invalid output cardinality')
    return d


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    g=p.add_mutually_exclusive_group(required=True)
    g.add_argument('--plan',action='store_true');g.add_argument('--run',action='store_true')
    p.add_argument('--cohort',type=Path,default=COHORT)
    p.add_argument('--v291-dir',type=Path,default=V291)
    p.add_argument('--candles-dir',type=Path,default=CANDLES)
    p.add_argument('--output-dir',type=Path,default=OUTPUT)
    p.add_argument('--max-symbols',type=int,default=0)
    args=p.parse_args(argv)
    if args.max_symbols<0:raise SystemExit('--max-symbols must be >= 0')
    try:
        c=sources(args.cohort,args.v291_dir)
        syms=sorted(c.symbol.astype(str).unique());syms=syms[:args.max_symbols] if args.max_symbols else syms
        missing=[s for s in syms if not (args.candles_dir/f'{s}_30m.csv').is_file()]
        plan=dict(scope='CORE15_PRE_CAUSAL',attempts=int(c.symbol.isin(syms).sum()),symbols=len(syms),
            windows=WINDOWS,missing_candles=missing[:20],output=str(args.output_dir),
            pre_cohort=str(args.cohort),reference_v291=str(args.v291_dir),
            logic='FIRST_RISING_LINE_BROKEN_THEN_NEW_CONFIRMED_HL',
            no_live=True,unmodified=['PRE_V282','V291','dashboard','entry_routes'])
        print('HLL_RELAXED_HL_PLAN',json.dumps(plan,ensure_ascii=False),flush=True)
        if args.plan:return 0
        if missing:raise ValueError('Missing OHLC: '+str(missing[:10]))
        dest=args.output_dir.resolve()
        for src in [args.cohort.resolve(),args.v291_dir.resolve(),args.candles_dir.resolve()]:
            if dest==src or src in dest.parents:raise ValueError('Output nested in input')
        if dest.exists():raise ValueError(f'OUTPUT_EXISTS: {dest}')
        if shutil.disk_usage(Path.cwd()).free<150*1024**2:raise ValueError('Disk free <150MB')
        d=run(c,args.candles_dir,args.max_symbols)
        summary=d.groupby(['window_30m','original_pre_pattern','sequence'],dropna=False).agg(
            attempts=('attempt_id','size'),symbols=('symbol','nunique')).reset_index()
        review=d[d.coverage.eq('COMPLETE')].copy()
        review['review_group']=review.sequence.astype(str)+' / '+review.original_pre_pattern.astype(str)
        review['draw_key']=review.apply(lambda r:hashlib.sha256(
            (str(r.attempt_id)+'|'+str(r.window_30m)).encode()).hexdigest(),axis=1)
        review=(review.sort_values('draw_key').groupby(['window_30m','review_group'],group_keys=False)
            .head(12).sort_values(['window_30m','review_group','symbol']))
        keep=['attempt_id','symbol','level_id','window_30m','pre_closed_ts',
          'original_pre_pattern','sequence','initial_slope_atr_per_bar',
          'initial_break_closed_ts','new_hl_open_ts','new_hl_known_closed_ts',
          'new_hl_low','slope_ratio_to_initial','slope_reduced',
          'zone_reapproach_before_breakout']
        review=review[keep].copy();review['revision_visual']='PENDIENTE';review['observaciones']=''
        dest.parent.mkdir(parents=True,exist_ok=True)
        tmp=Path(tempfile.mkdtemp(prefix='.relaxed-hl-',dir=dest.parent))
        try:
            d.to_csv(tmp/'sequence_pre.csv',index=False)
            summary.to_csv(tmp/'summary_pre.csv',index=False)
            review.to_csv(tmp/'revision_visual_ciega.csv',index=False)
            (tmp/'manifest.json').write_text(json.dumps(dict(**plan,
                cohort_sha256=sha(args.cohort),
                v291_audit_sha256=sha(args.v291_dir/'integrity_audit.csv'),
                observations=len(d),review_rows=len(review),
                warning='Attempt-only sample; cannot infer probability of breakout from structure without opportunities including no breakout. No post outcomes used.'),
                ensure_ascii=False,indent=2),encoding='utf-8')
            if dest.exists():raise ValueError(f'OUTPUT_EXISTS: {dest}')
            os.rename(tmp,dest)
        finally:
            if tmp.exists():shutil.rmtree(tmp)
        print('HLL_RELAXED_HL_DONE',json.dumps(dict(rows=len(d),
            sequence_counts={str(k):int(v) for k,v in d.sequence.value_counts().items()},
            output=str(dest)),ensure_ascii=False),flush=True)
        return 0
    except (FileNotFoundError,ValueError,KeyError,TypeError) as ex:
        print('HLL_RELAXED_HL_ERROR',str(ex),flush=True)
        return 2

if __name__=='__main__':raise SystemExit(main())
