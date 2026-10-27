#!/usr/bin/env python3
"""HLL V2.9.2: independent PRE contact audit, mechanism tags, and POST survival.

Offline research-only. Never changes V2.6/V2.8.2/V2.9.1 inputs or live trading.
The PRE outputs are computed without future bars or outcomes. POST is joined
solely by frozen attempt_id and remains separate. Thresholds are hypotheses.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import tempfile
from pathlib import Path
from collections import Counter

import numpy as np
import pandas as pd

try:
    from dashboard.views.hll_v281_structure_patterns import load_candles, window_features, pivot_lows_known
    from dashboard.views.hll_v282_pre_cohort import parse_ts
except ModuleNotFoundError:
    from hll_v281_structure_patterns import load_candles, window_features, pivot_lows_known
    from hll_v282_pre_cohort import parse_ts

TF = 1_800_000
MIN = 60_000
WINDOWS = (24, 48, 96)
HORIZONS_MIN = (120,720,2880) # 2h, 12h, 48h
COHORT = Path('reports/horizontal_level_structure_quality_v282/runs/core15_pre_all_v282/pre_cohort.csv')
INTEGRITY = Path('reports/horizontal_level_structure_integrity_v291/runs/core15_trendline_v291')
V26_TOUCHES = Path('reports/horizontal_level_structure_lab_v26/runs/core15_extended_v26/touches.csv')
CANDLES = Path('reports/horizontal_level_lifecycle_v21/candles_aug20_oct25_core15_btc')
MINUTES = Path('reports/horizontal_level_cross_research_v22/extended_1m_core15_v25')
OUTPUT = Path('reports/horizontal_level_mechanisms_v292/runs/core15_mechanisms_v292')


def digest(p):
    h = hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda:f.read(1<<20), b''):h.update(chunk)
    return h.hexdigest()


def require_one_to_one(df, what):
    if 'attempt_id' not in df or df.attempt_id.isna().any() or df.attempt_id.duplicated().any():
        raise ValueError(f'{what}: attempt_id nulo o repetido')


def load_inputs(cohort_path, integrity_dir, touches_path):
    c = pd.read_csv(cohort_path,low_memory=False)
    require_one_to_one(c,'V2.8.2')
    must = {'symbol','level_id','breakout_closed_ts','pre_closed_ts','atr_pre','zone_low','zone_high',
            'post_outcome','flip_confirmed_closed_ts','v26_status'}
    for w in WINDOWS: must.update({f'pre_pattern_{w}',f'pre{w}_pivot_lows_json'})
    if must-set(c): raise ValueError('Faltan columnas PRE: '+str(sorted(must-set(c))))
    integrity = pd.read_csv(integrity_dir/'integrity_audit.csv',low_memory=False)
    require_one_to_one(integrity,'V2.9.1')
    manifest = json.loads((integrity_dir/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('original_sha256') != digest(cohort_path):
        raise ValueError('V2.9.1 no corresponde al CSV V2.8.2')
    if set(integrity.attempt_id)!=set(c.attempt_id):
        raise ValueError('V2.9.1: universo de IDs distinto')
    icols = ['attempt_id']+[f'pre_pattern_v291_{w}' for w in WINDOWS]+[f'trendline{w}_broken' for w in WINDOWS]
    if set(icols)-set(integrity):raise ValueError('Columnas integridad incompletas')
    c = c.merge(integrity[icols], on='attempt_id', validate='one_to_one')
    t = pd.read_csv(touches_path,low_memory=False)
    fields = {'level_id','symbol','touch_closed_ts','side','test_id','depth_atr','rejection_atr','resolver_type','resolver_known_ts'}
    if fields-set(t):raise ValueError('V2.6 touches sin columnas: '+str(sorted(fields-set(t))))
    t['touch_closed_ts']=pd.to_numeric(t.touch_closed_ts,errors='raise').astype('int64')
    return c,t


def pre_touch_audit(row, candles, touches, window):
    """Use canonical independent TEST_FROM_BELOW V2.6, known by PRE cutoff.

    Other visible overlaps are only physical geometric contacts; they must NOT
    be mislabeled as independent canonical contacts.
    """
    pre=int(row['pre_closed_ts']);start=pre-window*TF
    lo=float(row['zone_low']);hi=float(row['zone_high']);atr=float(row['atr_pre'])
    local=candles[(candles.timestamp>=start)&(candles.timestamp<pre)]
    if len(local)!=window or (local.timestamp.diff().dropna()!=TF).any():
        return [],0, 'GAP_30M_PRE'
    canonical=touches[(touches.level_id.astype(str)==str(row['level_id'])) &
                      (touches.symbol.astype(str)==str(row['symbol'])) &
                      (touches.side.astype(str)=='BELOW') &
                      (touches.touch_closed_ts>start) & (touches.touch_closed_ts<=pre)].copy()
    canonical=canonical.sort_values('touch_closed_ts',kind='stable')
    # V2.6 test_id is the dedup key; do not collapse unique resolved test events.
    canonical=canonical.drop_duplicates(subset=['test_id','touch_closed_ts'],keep='first')
    by_ts=local.set_index('timestamp')
    events=[]
    for _,t in canonical.iterrows():
        clk=int(t.touch_closed_ts);bar_open=clk-TF
        if bar_open not in by_ts.index:
            raise ValueError(f'Toque V2.6 sin vela visible: {row["attempt_id"]} {clk}')
        b=by_ts.loc[bar_open]
        resolver=parse_ts(t.resolver_known_ts)
        outcome=t.resolver_type if resolver is not None and resolver<=pre else 'PENDIENTE_AL_PRE'
        events.append(dict(attempt_id=row['attempt_id'],symbol=row['symbol'],window=window,
          contact_num=len(events)+1,level_id=row['level_id'],test_id=t.test_id,
          touch_closed_ts=clk,touch_open_ts=bar_open,touch_price=float(b.high),
          depth_atr=float(t.depth_atr) if pd.notna(t.depth_atr) else np.nan,
          rejection_atr=float(t.rejection_atr) if pd.notna(t.rejection_atr) else np.nan,
          resolver_pre=outcome,resolver_known_ts_pre=resolver if resolver is not None and resolver<=pre else np.nan))
    # Count contiguous bar-overlaps separately (no independent-test claim).
    overlap=((local.low<=hi)&(local.high>=lo)).to_numpy(bool)
    clusters=int(sum(bool(on) and (i==0 or not bool(overlap[i-1])) for i,on in enumerate(overlap)))
    return events,clusters,'COMPLETE_30M_PRE'


def subtype(features, row, window, canon_count, sub):
    """Descriptive mechanisms, not optimized parameters or actionable signals."""
    original=str(row[f'pre_pattern_{window}'])
    revised=str(row[f'pre_pattern_v291_{window}'])
    if not features['coverage']:return 'SIN_COBERTURA'
    if revised=='ASCENDING_TREND_BROKEN':return 'DIRECTRIZ_ROTA'
    if original=='MULTI_CROSS_RANGE' or features['full_zone_crosses']>=2:
        return 'RANGO_CRUCES'
    if original=='DESCENDING_LOW_SEQUENCE':return 'MINIMOS_DESCENDENTES'
    if original=='ASCENDING_CHOPPY':return 'ASCENSO_IRREGULAR'
    close=sub.close.to_numpy(float); lows=sub.low.to_numpy(float)
    atr=float(row['atr_pre']);n=len(sub);ix=int(np.argmin(lows[:max(2,int(n*2/3))]))
    from_start=(float(close[0])-float(lows[ix]))/atr
    recovered=(float(close[-1])-float(lows[ix]))/atr
    # Historical V recovery is different from repeated compression against a ceiling.
    if ix>=2 and ix<int(.65*n) and from_start>=1.0 and recovered>=1.5:
        return 'RECUPERACION_V'
    early=sub.iloc[:max(2,int(n*2/3))]
    early_range=(float(early.high.max())-float(early.low.min()))/atr
    last_third_gain=(float(close[-1])-float(close[max(0,int(n*2/3))]))/atr
    if original=='HORIZONTAL_BASE' or (early_range<=1.25 and last_third_gain>=.5):
        return 'BASE_CON_PRESION_FINAL'
    if canon_count>=2 and original in ('ASCENDING_CLEAN','HORIZONTAL_BASE'):
        return 'PRESION_TESTS_REPETIDOS'
    if original=='ASCENDING_CLEAN':return 'ASCENSO_SOSTENIDO'
    if original=='IMPULSIVE_APPROACH':return 'APROXIMACION_IMPULSIVA'
    return 'MIXTA_OTRA'


def pre_features(row, bars, pivots, touches, window):
    pre=int(row['pre_closed_ts']); lo,hi,atr=(float(row['zone_low']),float(row['zone_high']),float(row['atr_pre']))
    features=window_features(bars,pivots,pre,window,lo,hi,atr)
    ev,overlaps,coverage=pre_touch_audit(row,bars,touches,window)
    selected=bars[(bars.timestamp>=pre-window*TF)&(bars.timestamp<pre)]
    if not features['coverage'] or coverage!='COMPLETE_30M_PRE':
        return dict(mechanism='SIN_COBERTURA',pre_30m_coverage=coverage,
          touch_v26_independent=len(ev),physical_overlap_clusters=overlaps,
          confirmed_pivots=np.nan,pivot_span_bars=np.nan,median_pivot_gap_bars=np.nan,
          pivot_slope_atr_per_bar=np.nan,progress_low_atr=np.nan,close_efficiency=np.nan,
          early_drawdown_atr=np.nan,late_recovery_atr=np.nan,pullback_depth_ratio=np.nan),ev
    points=json.loads(row[f'pre{window}_pivot_lows_json'])
    for point in points:
        if int(point['known_closed_ts'])>pre:raise ValueError('Pivote futuro usado en PRE')
    gaps=np.diff([int(p['open_ts']) for p in points])/TF if len(points)>=2 else []
    slope=((float(points[-1]['low'])-float(points[0]['low']))/(sum(gaps)*atr)
           if len(points)>=2 and sum(gaps)>0 else np.nan)
    first=selected.iloc[:max(2,int(len(selected)*2/3))]
    low_index=int(np.argmin(first.low.to_numpy()))
    drawdown=(float(selected.close.iloc[0])-float(first.low.iloc[low_index]))/atr
    recovery=(float(selected.close.iloc[-1])-float(first.low.iloc[low_index]))/atr
    # Real peak-to-next-low pullbacks between successive confirmed LOW pivots,
    # not merely the pivot's distance from the horizontal ceiling.
    pullbacks=[]
    for previous, current in zip(points[-3:-1],points[-2:]):
        segment=selected[(selected.timestamp>=int(previous['open_ts'])) &
                         (selected.timestamp<=int(current['open_ts']))]
        if segment.empty:continue
        pullbacks.append((float(segment.high.max())-float(current['low']))/atr)
    ratio=(pullbacks[-1]/pullbacks[0] if len(pullbacks)==2 and pullbacks[0]>.001 else np.nan)
    return dict(mechanism=subtype(features,row,window,len(ev),selected),pre_30m_coverage=coverage,
      touch_v26_independent=len(ev),physical_overlap_clusters=overlaps,
      confirmed_pivots=len(points),pivot_span_bars=(int(sum(gaps)) if len(gaps) else np.nan),
      median_pivot_gap_bars=(float(np.median(gaps)) if len(gaps) else np.nan),
      pivot_slope_atr_per_bar=slope,progress_low_atr=features['low_progress_atr'],
      close_efficiency=features['directional_efficiency'],early_drawdown_atr=drawdown,
      late_recovery_atr=recovery,pullback_depth_ratio=ratio),ev


def load_minute(symbol, path):
    p=path/f'{symbol}_1m.csv'
    if not p.is_file():return None
    a=pd.read_csv(p,usecols=['timestamp','open','high','low','close'])
    a=a.sort_values('timestamp',kind='stable').reset_index(drop=True)
    for field in ('timestamp','open','high','low','close'):
        a[field]=pd.to_numeric(a[field],errors='raise')
    a['timestamp']=a.timestamp.astype('int64')
    if a.timestamp.duplicated().any():raise ValueError(f'1m duplicados: {p}')
    vals=a[['open','high','low','close']].to_numpy(float)
    if not np.isfinite(vals).all() or (vals<=0).any() or np.any(a.high<a[['open','close','low']].max(axis=1)) or np.any(a.low>a[['open','close','high']].min(axis=1)):
        raise ValueError(f'1m OHLC invalido: {p}')
    return a


def survival(row, minute, horizon):
    """Independent flip-confirmation 1m path, following V2.7 entry convention.

    Entry = OPEN at flip_closed_ts + 1 minute. Complete contiguous path only;
    MFE/MAE, survival, and reclaim are NOT populated on censored data.
    """
    answer=dict(attempt_id=row['attempt_id'],symbol=row['symbol'],horizon_min=horizon,
                path_state='NO_FLIP',entry_ts=np.nan,entry_open=np.nan,
                mfe_pct=np.nan,mae_pct=np.nan,close_return_pct=np.nan,
                first_low_wick_breach_ts=np.nan,first_close_loss_ts=np.nan,
                first_reclaim_ts=np.nan,time_to_max_min=np.nan,
                survived_without_close_loss=np.nan,final_above_zone=np.nan)
    flip=parse_ts(row['flip_confirmed_closed_ts'])
    if flip is None:return answer
    start=flip+MIN;answer['entry_ts']=start
    if minute is None or minute.empty:
        answer['path_state']='NO_1M_ARCHIVE';return answer
    ts=minute.timestamp.to_numpy(dtype='int64')
    i=int(np.searchsorted(ts,start))
    if i>=len(ts) or int(ts[i])!=start:
        answer['path_state']='NO_ENTRY_1M';return answer
    sub=minute.iloc[i:i+horizon]
    if len(sub)<horizon:
        answer['path_state']='CENSORED_1M_TAIL';return answer
    if len(sub)>1 and np.any(np.diff(sub.timestamp.to_numpy(dtype='int64'))!=MIN):
        answer['path_state']='GAP_1M';return answer
    lo,hi=float(row['zone_low']),float(row['zone_high'])
    entry=float(sub.open.iloc[0]);answer['entry_open']=entry
    answer['path_state']='COMPLETE_1M'
    answer['mfe_pct']=100*(float(sub.high.max())/entry-1)
    answer['mae_pct']=100*(1-float(sub.low.min())/entry)
    answer['close_return_pct']=100*(float(sub.close.iloc[-1])/entry-1)
    hits_wick=np.flatnonzero(sub.low.to_numpy(float)<lo)
    hits_loss=np.flatnonzero(sub.close.to_numpy(float)<lo)
    if len(hits_wick):answer['first_low_wick_breach_ts']=int(sub.timestamp.iloc[hits_wick[0]])+MIN
    if len(hits_loss):
        z=int(hits_loss[0]);answer['first_close_loss_ts']=int(sub.timestamp.iloc[z])+MIN
        reclaim=np.flatnonzero(sub.close.to_numpy(float)[z+1:]>hi)
        if len(reclaim):answer['first_reclaim_ts']=int(sub.timestamp.iloc[z+1+reclaim[0]])+MIN
    answer['survived_without_close_loss']=not len(hits_loss)
    answer['final_above_zone']=float(sub.close.iloc[-1])>hi
    answer['time_to_max_min']=int(np.argmax(sub.high.to_numpy(float)))+1
    return answer


def summarize(audit):
    # Outcomes are attached *after* group assignment, never used for PRE groups.
    rows=[]
    for window in WINDOWS:
        gcol=f'mechanism_{window}'
        for name,part in [('ALL',audit),('FIRST_PER_LEVEL',audit.sort_values('breakout_closed_ts')
                .drop_duplicates(['symbol','level_id']))]:
            for mech,g in part.groupby(gcol,dropna=False):
                counts=g.post_outcome.value_counts()
                flips=int(counts.get('FLIP_CONFIRMADO',0))
                no=int(counts.get('SIN_FLIP_EN_VENTANA',0));un=int(counts.get('INDETERMINADO',0))
                rows.append(dict(window=window,scope=name,mechanism=mech,N=len(g),
                  symbols=g.symbol.nunique(),levels=g[['symbol','level_id']].drop_duplicates().shape[0],
                  symbol_days=g.assign(day=pd.to_datetime(g.breakout_closed_ts,unit='ms',utc=True).dt.strftime('%Y-%m-%d'))
                    [['symbol','day']].drop_duplicates().shape[0],
                  flip_confirmed=flips,no_flip_in_v26_window=no,indeterminate=un,
                  flip_all_pct=100*flips/len(g) if len(g) else np.nan,
                  flip_resolved_pct=100*flips/(flips+no) if flips+no else np.nan))
    return pd.DataFrame(rows)


def monthly_summary(audit, window=48):
    x=audit.sort_values('breakout_closed_ts').drop_duplicates(['symbol','level_id']).copy()
    x['month']=pd.to_datetime(x.breakout_closed_ts,unit='ms',utc=True).dt.strftime('%Y-%m')
    rows=[]
    for (month,mechanism),g in x.groupby(['month',f'mechanism_{window}']):
        c=g.post_outcome.value_counts();yes=int(c.get('FLIP_CONFIRMADO',0))
        no=int(c.get('SIN_FLIP_EN_VENTANA',0));unknown=int(c.get('INDETERMINADO',0))
        rows.append(dict(window=window,month=month,mechanism=mechanism,N=len(g),symbols=g.symbol.nunique(),
          flips=yes,no_flip=no,indeterminate=unknown,
          flip_pct_resolved=100*yes/(yes+no) if yes+no else np.nan))
    return pd.DataFrame(rows,columns=['window','month','mechanism','N','symbols','flips',
                                       'no_flip','indeterminate','flip_pct_resolved'])


def survival_summary(audit, paths, window=48):
    if paths.empty:
        return pd.DataFrame(columns=['mechanism','horizon_min','flip_events','complete_1m',
             'censored_or_gap','mean_mfe_pct','mean_mae_pct','survival_close_pct','mean_minutes_to_max'])
    joined=paths.merge(audit[['attempt_id',f'mechanism_{window}']],on='attempt_id',validate='many_to_one')
    rows=[]
    for (mechanism,horizon),group in joined.groupby([f'mechanism_{window}','horizon_min']):
        good=group[group.path_state.eq('COMPLETE_1M')]
        rows.append(dict(mechanism=mechanism,horizon_min=int(horizon),flip_events=len(group),
          complete_1m=len(good),censored_or_gap=len(group)-len(good),
          mean_mfe_pct=good.mfe_pct.mean() if len(good) else np.nan,
          mean_mae_pct=good.mae_pct.mean() if len(good) else np.nan,
          survival_close_pct=100*good.survived_without_close_loss.astype(float).mean() if len(good) else np.nan,
          mean_minutes_to_max=good.time_to_max_min.mean() if len(good) else np.nan))
    return pd.DataFrame(rows)


def matched_controls(audit, window=48):
    """Exploratory case-control for resolved original attempts; no rate inference.

    First per (symbol, level), same V2.9.1 PRE class and month. Match by
    PRE features only, preferring same symbol, without replacement. Cannot
    be used to estimate population flip rates (case-control ascertainment).
    """
    col=f'pre_pattern_v291_{window}'
    m=f'mechanism_{window}'
    ordered=audit.sort_values('breakout_closed_ts').drop_duplicates(['symbol','level_id']).copy()
    ordered['month']=pd.to_datetime(ordered.breakout_closed_ts,unit='ms',utc=True).dt.strftime('%Y-%m')
    cases=ordered[ordered.post_outcome.eq('FLIP_CONFIRMADO')]
    controls=ordered[ordered.post_outcome.eq('SIN_FLIP_EN_VENTANA')]
    used=set();matches=[];unmatched=[]
    numeric=['touch_v26_independent','confirmed_pivots','pivot_slope_atr_per_bar',
             'close_efficiency','pullback_depth_ratio']
    scales=[1.,2.,.1,.3,1.]
    for case in cases.itertuples(index=False):
        case=case._asdict()
        pool=controls[(controls[col]==case[col]) & (controls.month==case['month']) &
                      (~controls.attempt_id.isin(used))]
        if pool.empty:
            unmatched.append(dict(case_id=case['attempt_id'],reason='SIN_CONTROL_MISMA_CLASE_MES'))
            continue
        options=[]
        for ctrl in pool.to_dict('records'):
            score=0.
            missing=0
            for field,scale in zip(numeric,scales):
                a=case.get(f'{field}_{window}');b=ctrl.get(f'{field}_{window}')
                if not (pd.notna(a) and pd.notna(b)):
                    missing+=1
                    continue
                score+=min(8.,abs(float(a)-float(b))/scale)
            score+=(0. if ctrl[m]==case[m] else 2.)
            score+=(0. if ctrl['symbol']==case['symbol'] else 1.)
            options.append((score,missing,int(ctrl['breakout_closed_ts']),str(ctrl['attempt_id']),ctrl))
        score,missing,_,_,best=min(options,key=lambda x:x[:4])
        used.add(best['attempt_id'])
        matches.append(dict(case_id=case['attempt_id'],control_id=best['attempt_id'],
          case_symbol=case['symbol'],control_symbol=best['symbol'],
          case_month=case['month'],pre_class=case[col],
          case_mechanism=case[m],control_mechanism=best[m],
          same_symbol=case['symbol']==best['symbol'],same_mechanism=case[m]==best[m],
          match_distance=round(score,4),missing_pair_features=missing,
          case_breakout_closed_ts=case['breakout_closed_ts'],
          control_breakout_closed_ts=best['breakout_closed_ts']))
    return (pd.DataFrame(matches,columns=['case_id','control_id','case_symbol','control_symbol',
        'case_month','pre_class','case_mechanism','control_mechanism','same_symbol',
        'same_mechanism','match_distance','missing_pair_features',
        'case_breakout_closed_ts','control_breakout_closed_ts']),
        pd.DataFrame(unmatched,columns=['case_id','reason']))


def blind_sample(audit, seed=20261027, n_each=30):
    # Selection by PRE-only features, NEVER by outcome; reviewer cannot see POST.
    w=48
    source=audit.sort_values('breakout_closed_ts').drop_duplicates(['symbol','level_id'])
    rng=np.random.default_rng(seed)
    ids=[]
    for _,g in source.groupby(f'mechanism_{w}'):
        pos=rng.choice(len(g),size=min(n_each,len(g)),replace=False)
        ids.extend(g.iloc[pos].attempt_id.tolist())
    df=source[source.attempt_id.isin(ids)].copy()
    cols=['attempt_id','symbol','level_id','breakout_closed_ts','pre_closed_ts',
          'pre_pattern_48','pre_pattern_v291_48','mechanism_48','touch_v26_independent_48',
          'confirmed_pivots_48','pivot_slope_atr_per_bar_48']
    df=df[cols].sort_values('breakout_closed_ts')
    df['revision_manual']='PENDIENTE'
    df['notas_revision']=''
    return df


def run_audit(cohort, touches, candle_dir, minute_dir, max_symbols=0):
    symbols=sorted(cohort.symbol.unique())
    if max_symbols:symbols=symbols[:max_symbols]
    attempts=[];contacts=[];paths=[]
    for ix,sym in enumerate(symbols,1):
        candle=load_candles(candle_dir/f'{sym}_30m.csv')
        pv=pivot_lows_known(candle)
        minutely=load_minute(sym,minute_dir)
        t=touches[touches.symbol.astype(str).eq(str(sym))].copy()
        for original in cohort[cohort.symbol.eq(sym)].to_dict('records'):
            r=original.copy();r['attempt_id']=str(r['attempt_id'])
            if not np.isfinite([float(r['zone_low']),float(r['zone_high']),float(r['atr_pre'])]).all() or float(r['atr_pre'])<=0:
                raise ValueError(f'ATR/zona inváli{r["attempt_id"]}')
            for w in WINDOWS:
                feat,contact=pre_features(r,candle,pv,t,w)
                for k,v in feat.items():r[f'{k}_{w}']=v
                contacts.extend(contact)
            attempts.append(r)
            if parse_ts(r['flip_confirmed_closed_ts']) is not None:
                for horizon in HORIZONS_MIN:paths.append(survival(r,minutely,horizon))
        print('HLL_V292_SYMBOL',json.dumps(dict(index=ix,symbol=sym,attempts=int(cohort.symbol.eq(sym).sum()),
            has_1m=minutely is not None)),flush=True)
    a=pd.DataFrame(attempts);require_one_to_one(a,'V2.9.2')
    contact_cols=['attempt_id','symbol','window','contact_num','level_id','test_id',
      'touch_closed_ts','touch_open_ts','touch_price','depth_atr','rejection_atr',
      'resolver_pre','resolver_known_ts_pre']
    path_cols=list(survival(dict(attempt_id='test',symbol='test',flip_confirmed_closed_ts=None),None,120).keys())
    return a,pd.DataFrame(contacts,columns=contact_cols),pd.DataFrame(paths,columns=path_cols)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true');mode.add_argument('--run',action='store_true');mode.add_argument('--report',action='store_true')
    p.add_argument('--cohort',type=Path,default=COHORT)
    p.add_argument('--integrity-dir',type=Path,default=INTEGRITY)
    p.add_argument('--touches',type=Path,default=V26_TOUCHES)
    p.add_argument('--candles-dir',type=Path,default=CANDLES)
    p.add_argument('--minute-dir',type=Path,default=MINUTES)
    p.add_argument('--output-dir',type=Path,default=OUTPUT)
    p.add_argument('--max-symbols',type=int,default=0)
    a=p.parse_args(argv)
    if a.max_symbols<0:raise SystemExit('max_symbols inválido')
    if a.report:
        f=a.output_dir/'mechanism_summary.csv'
        if not f.is_file():raise SystemExit('Falta reporte V2.9.2: '+str(f))
        df=pd.read_csv(f)
        print('HLL_V292_REPORT',json.dumps(dict(output=str(a.output_dir),window=48,
              summary_scope='FIRST_PER_LEVEL'),ensure_ascii=False))
        print(df[(df.window==48)&(df.scope=='FIRST_PER_LEVEL')].to_string(index=False))
        mf=a.output_dir/'manifest.json'
        if mf.is_file():
            m=json.loads(mf.read_text())
            print('HLL_V292_COVERAGE',json.dumps(m.get('survival_coverage',{})))
            print('HLL_V292_MATCHED',json.dumps(dict(matched=m.get('matched_cases'),unmatched=m.get('unmatched_cases'))))
        return 0
    for source in [a.cohort,a.integrity_dir/'integrity_audit.csv',a.integrity_dir/'manifest.json',a.touches]:
        if not source.is_file():raise SystemExit('Falta archivo: '+str(source))
    cohort,touches=load_inputs(a.cohort,a.integrity_dir,a.touches)
    symbols=sorted(cohort.symbol.unique())
    if a.max_symbols:symbols=symbols[:a.max_symbols]
    plan=dict(version='HLL_V292_AUDIT',mode='OFFLINE_READ_ONLY',attempts=int(cohort.symbol.isin(symbols).sum()),
         symbols=len(symbols),windows=WINDOWS,post_horizons_min=HORIZONS_MIN,
         source_prehash=digest(a.cohort),source_v291hash=digest(a.integrity_dir/'integrity_audit.csv'),
         source_touches_hash=digest(a.touches),output=str(a.output_dir),
         entry_clock='FLIP_CONFIRMED_CLOSED_PLUS_1M_OPEN',original_data_unchanged=True)
    print('HLL_V292_PLAN',json.dumps(plan,ensure_ascii=False),flush=True)
    if a.plan:return 0
    out=a.output_dir.resolve()
    for src in [a.cohort.resolve(),a.integrity_dir.resolve(),a.touches.resolve(),a.candles_dir.resolve(),a.minute_dir.resolve()]:
        if out==src or src in out.parents:raise SystemExit('No sobrescribir insumos')
    if out.exists():raise SystemExit('OUTPUT_EXISTS: no sobrescribir una corrida')
    if shutil.disk_usage(Path.cwd()).free<150*1024*1024:
        raise SystemExit('Falta espacio libre en disco')
    out.parent.mkdir(parents=True,exist_ok=True)
    tmp=Path(tempfile.mkdtemp(prefix='.hll-v292-',dir=str(out.parent)))
    try:
        attempt,contact,path=run_audit(cohort,touches,a.candles_dir,a.minute_dir,a.max_symbols)
        if len(attempt)!=plan['attempts']:raise ValueError('Cardinalidad PRE cambió')
        attempt.to_csv(tmp/'mechanism_attempts.csv',index=False)
        contact.to_csv(tmp/'pre_contacts.csv',index=False)
        path.to_csv(tmp/'flip_survival_1m.csv',index=False)
        summ=summarize(attempt);summ.to_csv(tmp/'mechanism_summary.csv',index=False)
        monthly_summary(attempt).to_csv(tmp/'mechanism_monthly_48.csv',index=False)
        survival_summary(attempt,path).to_csv(tmp/'survival_by_mechanism_48.csv',index=False)
        blind_sample(attempt).to_csv(tmp/'blind_review_sample.csv',index=False)
        matched,unmatched=matched_controls(attempt)
        matched.to_csv(tmp/'matched_controls_post.csv',index=False)
        unmatched.to_csv(tmp/'unmatched_cases.csv',index=False)
        coverage={str(k):int(v) for k,v in path.path_state.value_counts().items()} if not path.empty else {}
        (tmp/'manifest.json').write_text(json.dumps(dict(**plan,
          survival_coverage=coverage,touch_count=len(contact),mechanism_summary_rows=len(summ),
          matched_cases=len(matched),unmatched_cases=len(unmatched),
          note='PRE classes causal and descriptive. POST complete 1m only. No PnL inference, no unseen data filled, no LIVE changes.'),
          indent=2,ensure_ascii=False),encoding='utf-8')
        out.parent.mkdir(parents=True,exist_ok=True)
        tmp.rename(out)
    finally:
        if tmp.exists():shutil.rmtree(tmp)
    print('HLL_V292_DONE',json.dumps(dict(attempts=len(attempt),touches=len(contact),
      flip_horizon_rows=len(path),survival_coverage=coverage,matched_cases=len(matched),
      unmatched_cases=len(unmatched),output=str(out)),ensure_ascii=False),flush=True)
    return 0

if __name__=='__main__':raise SystemExit(main())
