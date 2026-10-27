#!/usr/bin/env python3
"""V2.8.1: causal, *descriptive* price-structure taxonomy for V2.8 flips.

Run from repository root. Completely offline; does not change V2.6/V2.7/V2.8,
Trading LIVE, or any historical inputs. Never uses TP/SL to assign a pattern.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

TF = 1_800_000
V28 = Path('reports/horizontal_level_structure_quality_v28/runs/core15_v28')
CANDLES = Path('reports/horizontal_level_lifecycle_v21/candles_aug20_oct25_core15_btc')
OUTPUT = Path('reports/horizontal_level_structure_quality_v281/runs/core15_v281')
WINDOWS = (24, 48, 96)
PATTERNS = (
    'ASCENDING_CLEAN', 'ASCENDING_CHOPPY', 'HORIZONTAL_BASE',
    'MULTI_CROSS_RANGE', 'IMPULSIVE_APPROACH', 'DESCENDING_LOW_SEQUENCE',
    'OTHER', 'UNKNOWN',
)
PATTERN_ES = {
    'ASCENDING_CLEAN': 'A · Mínimos ascendentes limpios',
    'ASCENDING_CHOPPY': 'B · Mínimos ascendentes, pero desordenados',
    'HORIZONTAL_BASE': 'C · Base horizontal bajo el techo',
    'MULTI_CROSS_RANGE': 'D · Cruces desordenados de la zona',
    'IMPULSIVE_APPROACH': 'E · Aproximación impulsiva',
    'DESCENDING_LOW_SEQUENCE': 'F · Mínimos descendentes',
    'OTHER': 'G · Otra / mixta',
    'UNKNOWN': 'Sin contexto suficiente',
}
DEFENSE_ES = {
    'CLEAN_DEFENSE': 'Retest y defensa limpios',
    'CHOPPY_DEFENSE': 'Defensa con reiterados cruces',
    'DEEP_RETEST': 'Retest profundo',
    'EXTENDED_CONFIRMATION': 'Confirmación alejada del nivel',
    'OTHER_DEFENSE': 'Defensa mixta',
    'UNKNOWN': 'Sin datos completos',
}


def _finite(x):
    try:
        v = float(x)
        return v if np.isfinite(v) else np.nan
    except (ValueError, TypeError):
        return np.nan


def _save_csv(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.v281-', suffix='.csv', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', newline='', encoding='utf-8') as file:
            data.to_csv(file, index=False)
            file.flush(); os.fsync(file.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.v281-', suffix='.json', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(data, file, indent=2, ensure_ascii=False)
            file.flush(); os.fsync(file.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def load_candles(path):
    c = pd.read_csv(path, usecols=['timestamp','open','high','low','close'])
    if c.empty: raise ValueError('No hay velas: ' + str(path))
    c = c.sort_values('timestamp', kind='stable').reset_index(drop=True)
    c['timestamp'] = pd.to_numeric(c.timestamp, errors='raise').astype('int64')
    if c.timestamp.duplicated().any(): raise ValueError('Timestamp repetido: ' + str(path))
    for col in ('open','high','low','close'):
        c[col] = pd.to_numeric(c[col],errors='raise')
    ohlc = c[['open','high','low','close']].to_numpy(dtype=float)
    if not np.isfinite(ohlc).all() or (ohlc <= 0).any() or (c.low > c.high).any():
        raise ValueError('OHLC invalido: ' + str(path))
    return c


def pivot_lows_known(c):
    low=c.low.to_numpy(dtype=float); ts=c.timestamp.to_numpy(dtype=np.int64)
    result=[]
    for j in range(2,len(c)-2):
        if low[j]<min(low[j-2:j]) and low[j]<min(low[j+1:j+3]):
            result.append((j, int(ts[j+2])+TF, float(low[j])))
    return result


def window_features(c, pivots, end_closed_ts, bars, lo, hi, atr):
    """Features from exactly `bars` contiguous CLOSED candles ending at end_closed_ts.

    A 2x2 pivot only counts if its two right-hand confirming bars closed by
    end_closed_ts. No access to later prices is possible in this function.
    """
    if not np.isfinite([lo, hi, atr]).all() or not (0 < lo <= hi and atr > 0):
        raise ValueError('Zona o ATR invalidos')
    ts=c.timestamp.to_numpy(dtype=np.int64)
    expected_open=int(end_closed_ts)-TF
    end=int(np.searchsorted(ts, expected_open))
    unknown = dict(coverage=False, window_bars=bars, pivots=np.nan, higher_low_steps=np.nan,
                   last3_rising=np.nan, last3_falling=np.nan, low_progress_atr=np.nan,
                   zone_center_crosses=np.nan, full_zone_crosses=np.nan,
                   zone_overlap_bars=np.nan, zone_close_bars=np.nan,
                   net_move_atr=np.nan, directional_efficiency=np.nan,
                   pivot_lows_json='[]')
    if end >= len(c) or end < bars-1 or ts[end]!=expected_open:
        return unknown
    start=end-bars+1
    if np.any(np.diff(ts[start:end+1])!=TF): return unknown
    sub=c.iloc[start:end+1]
    known=[p for p in pivots if start<=p[0]<=end and p[1]<=end_closed_ts]
    # Zone-aware: traversals count only after a fully established opposite-side
    # close. Touches/near-level oscillations alone do not count as full crosses.
    close=sub.close.to_numpy(dtype=float)
    state=np.where(close>hi,1,np.where(close<lo,-1,0))
    nonzero=state[state!=0]
    full_cross=int(np.count_nonzero(nonzero[1:]!=nonzero[:-1]))
    centre=(lo+hi)/2
    sign=np.sign(close-centre)
    nonzero_sign=sign[sign!=0]
    centre_cross=int(np.count_nonzero(nonzero_sign[1:]!=nonzero_sign[:-1]))
    sum_steps=float(np.abs(np.diff(close)).sum())
    lo_values=[p[2] for p in known]
    rising=np.nan;falling=np.nan;progress=np.nan
    if len(known)>=3:
        last3=np.array(lo_values[-3:],dtype=float)
        steps=np.diff(last3)
        rising=int(bool(np.all(steps >= .1*atr)))
        falling=int(bool(np.all(steps <= -.1*atr)))
        progress=float((last3[-1]-last3[0])/atr)
    return dict(coverage=True, window_bars=bars, pivots=len(known),
        higher_low_steps=sum(b>a+.1*atr for a,b in zip(lo_values[:-1],lo_values[1:])),
        last3_rising=rising,last3_falling=falling,low_progress_atr=progress,
        zone_center_crosses=centre_cross,full_zone_crosses=full_cross,
        zone_overlap_bars=int(((sub.low<=hi)&(sub.high>=lo)).sum()),
        zone_close_bars=int(((sub.close>=lo)&(sub.close<=hi)).sum()),
        net_move_atr=float((close[-1]-close[0])/atr),
        directional_efficiency=abs(float(close[-1]-close[0]))/sum_steps if sum_steps else 0.,
        pivot_lows_json=json.dumps([dict(open_ts=int(ts[j]),known_closed_ts=t,low=v)
                                     for j,t,v in known],separators=(',',':')))


def classify_structure(f):
    """Fixed geometry rules, independently of PnL or eventual flip status."""
    if not f['coverage']:return 'UNKNOWN'
    crosses=int(f['zone_center_crosses'])
    full=int(f['full_zone_crosses'])
    overlaps=int(f['zone_overlap_bars'])
    n=int(f['pivots'])
    rising=n>=3 and f['last3_rising']==1
    if rising:
        # Key improvement: higher lows ALONE no longer imply a CLEAN build.
        return 'ASCENDING_CHOPPY' if (crosses>=3 or full>=1) else 'ASCENDING_CLEAN'
    if full>=2 or crosses>=4:return 'MULTI_CROSS_RANGE'
    # Price stays near the ceiling, without clear rising pivot progression.
    if overlaps>=max(4,int(np.ceil(.18*f['window_bars']))) and full==0 and f['directional_efficiency']<=.36:
        return 'HORIZONTAL_BASE'
    if (f['net_move_atr']>=.9 and f['directional_efficiency']>=.45
            and n<3 and overlaps<=max(5,int(.25*f['window_bars']))):
        return 'IMPULSIVE_APPROACH'
    if n>=3 and f['last3_falling']==1:return 'DESCENDING_LOW_SEQUENCE'
    return 'OTHER'


def classify_defense(row):
    depth=_finite(row.get('retest_low_below_zone_high_atr'))
    dist=_finite(row.get('signal_distance_from_zone_low_atr'))
    inside=_finite(row.get('retest_to_signal_closes_inside_zone'))
    below=_finite(row.get('retest_to_signal_closes_below_zone'))
    overlap=_finite(row.get('retest_to_signal_zone_overlaps'))
    if not np.isfinite([depth,dist,inside,below,overlap]).all():return 'UNKNOWN'
    if below>0 or overlap>=3 or inside>=2:return 'CHOPPY_DEFENSE'
    if depth>.5:return 'DEEP_RETEST'
    if dist>2:return 'EXTENDED_CONFIRMATION'
    if overlap<=2 and below==0 and depth<=.5:return 'CLEAN_DEFENSE'
    return 'OTHER_DEFENSE'


def compute_taxonomy(entries,candles_dir,max_symbols=0):
    data=entries[entries.entry_type.eq('FLIP_CONFIRMED')].copy()
    if data.duplicated(['attempt_id','entry_type']).any():
        raise ValueError('Entradas duplicadas')
    result=[]
    symbols=sorted(data.symbol.dropna().unique())
    if max_symbols>0:symbols=symbols[:max_symbols]
    for n,symbol in enumerate(symbols,1):
        file=candles_dir/f'{symbol}_30m.csv'
        if not file.is_file():raise ValueError('Faltan velas: '+str(file))
        candles=load_candles(file)
        pivots=pivot_lows_known(candles)
        for row in data[data.symbol.eq(symbol)].to_dict('records'):
            required=['attempt_id','known_closed_ts','breakout_closed_ts','zone_low','zone_high','atr_pre']
            if any(pd.isna(row.get(key)) for key in required):
                raise ValueError('Faltan datos causales del episodio')
            # PRE freezes on the candle immediately BEFORE breakout. Breakout
            # opens at breakout_closed_ts-TF, so prior bar closes at that open.
            pre_clock=int(row['breakout_closed_ts'])-TF
            flip_clock=int(row['known_closed_ts'])
            if flip_clock<int(row['breakout_closed_ts']):raise ValueError('Relojes invertidos')
            lo,hi,atr=map(float,(row['zone_low'],row['zone_high'],row['atr_pre']))
            new=dict(attempt_id=row['attempt_id'],symbol=symbol,
                     entry_type='FLIP_CONFIRMED',known_closed_ts=flip_clock,
                     breakout_closed_ts=int(row['breakout_closed_ts']),
                     source_clock='FLIP_CONFIRMED')
            for phase,cutoff in [('pre',pre_clock),('flip',flip_clock)]:
                for window in WINDOWS:
                    feat=window_features(candles,pivots,cutoff,window,lo,hi,atr)
                    new[f'{phase}_pattern_{window}']=classify_structure(feat)
                    for field,value in feat.items():
                        new[f'{phase}{window}_{field}']=value
            new['defense_pattern']=classify_defense(row)
            result.append(new)
        print('HLL_V281_SYMBOL',json.dumps(dict(index=n,symbol=symbol,
                 entries=int(data.symbol.eq(symbol).sum()))),flush=True)
    return pd.DataFrame(result)


def summarize(tax, evaluated):
    ref=evaluated[evaluated.entry_type.eq('FLIP_CONFIRMED')].copy()
    ref=ref.merge(tax[['attempt_id','symbol','entry_type']+
             [f'{phase}_pattern_{w}' for phase in ('pre','flip') for w in WINDOWS]+
             ['defense_pattern']],on=['attempt_id','symbol','entry_type'],
             how='inner',validate='many_to_one')
    if len(ref)!=len(evaluated[evaluated.entry_type.eq('FLIP_CONFIRMED')]):
        raise ValueError('Evaluaciones V2.8 no enlazadas con taxonomy')
    ref=ref.drop_duplicates(['symbol','entry_ts','horizon_min'])
    ref=ref[(ref.path_state=='COMPLETE_1M') & ref.outcome.isin(
        ['TP_FIRST','SL_FIRST','TIME_EXIT','BOTH_SAME_1M_SL_ASSUMED'])].copy()
    ref['net']=pd.to_numeric(ref.return_after_fee_pct,errors='coerce')
    ref=ref[ref.net.notna()]
    ref['week']=pd.to_datetime(ref.entry_ts,unit='ms',utc=True).dt.strftime('%G-W%V')
    ref['period']=np.where(pd.to_datetime(ref.entry_ts,unit='ms',utc=True)<
                           pd.Timestamp('2026-10-01',tz='UTC'), 'AUG_SEP','OCT')
    rows=[]
    for factor in [f'{phase}_pattern_{w}' for phase in ('pre','flip') for w in WINDOWS]+['defense_pattern']:
        for (period,horizon,pattern),g in ref.groupby(['period','horizon_min',factor],dropna=False):
            s=g.groupby('symbol').net.mean()
            rows.append(dict(factor=factor,period=period,horizon_min=int(horizon),
                pattern=pattern,n=len(g),symbols=g.symbol.nunique(),
                symbol_weeks=g.groupby(['symbol','week']).ngroups,
                mean_net_pct=g.net.mean(),equal_symbol_net_pct=s.mean(),
                tp_pct=100*g.outcome.eq('TP_FIRST').mean(),
                sl_pct=100*g.outcome.isin(['SL_FIRST','BOTH_SAME_1M_SL_ASSUMED']).mean()))
    return pd.DataFrame(rows)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true');mode.add_argument('--run',action='store_true')
    p.add_argument('--v28-dir',type=Path,default=V28)
    p.add_argument('--candles-dir',type=Path,default=CANDLES)
    p.add_argument('--output-dir',type=Path,default=OUTPUT)
    p.add_argument('--max-symbols',type=int,default=0)
    p.add_argument('--min-free-gb',type=float,default=.15)
    args=p.parse_args(argv)
    source=args.v28_dir/'entry_structures.csv'
    evals=args.v28_dir/'evaluation_enriched.csv'
    for file in (source,evals):
        if not file.is_file():raise SystemExit(f'Missing source file: {file}')
    entries=pd.read_csv(source,low_memory=False)
    base=entries[entries.entry_type=='FLIP_CONFIRMED']
    symbols=sorted(base.symbol.unique())
    if args.max_symbols>0:symbols=symbols[:args.max_symbols]
    plan=dict(entries=int(base.symbol.isin(symbols).sum()),symbols=len(symbols),
        windows=WINDOWS,patterns=PATTERNS,source=str(source),output=str(args.output_dir),
        mode='OFFLINE_CAUSAL_NO_REPLAY',version='HLL_V281')
    print('HLL_V281_PLAN',json.dumps(plan,ensure_ascii=False),flush=True)
    if args.plan:return 0
    dest=args.output_dir.resolve()
    for src in [args.v28_dir.resolve(),args.candles_dir.resolve()]:
        if dest==src or src in dest.parents:raise SystemExit('OUTPUT must not overwrite source')
    if dest.exists() and any(dest.iterdir()):raise SystemExit('OUTPUT_EXISTS, no overwrite')
    if shutil.disk_usage(Path.cwd()).free<args.min_free_gb*1024**3:
        raise SystemExit('Insufficient free disk space')
    tax=compute_taxonomy(entries,args.candles_dir,args.max_symbols)
    eval_df=pd.read_csv(evals,low_memory=False)
    if args.max_symbols>0:eval_df=eval_df[eval_df.symbol.isin(symbols)]
    summary=summarize(tax,eval_df)
    _save_csv(args.output_dir/'taxonomy_entries.csv',tax)
    _save_csv(args.output_dir/'taxonomy_summary.csv',summary)
    _save_json(args.output_dir/'manifest.json',dict(**plan,
        taxonomy_counts_48=dict(Counter(tax.pre_pattern_48)),
        limitations='Exploratory fixed rules; manually inspect; Oct is NOT external holdout; dependent levels and incomplete 1m. Classification never sees outcomes.',
        causal_clock='PRE ends before breakout; FLIP ends at confirmed closed candle; 2x2 pivots require two future-for-pivot but already closed candles'))
    print('HLL_V281_DONE',json.dumps(dict(entries=len(tax),groups=len(summary),
        pre48=dict(Counter(tax.pre_pattern_48)),output=str(args.output_dir))),flush=True)
    return 0

if __name__=='__main__':raise SystemExit(main())
