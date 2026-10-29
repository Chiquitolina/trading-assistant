#!/usr/bin/env python3
"""HLL BTC strength research; closed 30m candles only; offline/no LIVE writes.

Reuses the BTC Correlation Analyzer mathematics (Pearson rho, beta, R²,
residual = symbol move - beta * BTC move) on historical 30m candles aligned
strictly to each frozen HLL entry timestamp. Does NOT import Redis snapshots.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen, Request

import numpy as np
import pandas as pd

BASE = Path('reports/horizontal_level_full290_v301')
CANDLES = Path('reports/horizontal_level_290_history_v298/candles_30m_aug20_oct04_v298')
INTERVAL = 1_800_000
START = 1787184000000  # UTC 2026-08-20
END = 1791244800000    # UTC 2026-10-06
BTC_LOCAL = BASE / 'btc_strength_sources' / 'BTCUSDT_30m.csv'
CASES = {(720, 2, 2): '2_2_12h', (2880, 2, 2): '2_2_48h',
         (720, 5, 5): '5_5_12h', (2880, 5, 5): '5_5_48h'}


def root(scope):
    return BASE / f'btc_strength_{scope}'


def btc_path():
    src = CANDLES / 'BTCUSDT_30m.csv'
    return src if src.is_file() else BTC_LOCAL


def sha(path):
    d=hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1<<20), b''):
            d.update(chunk)
    return d.hexdigest()


def fetch_btc():
    """Explicit public fetch only. Never modify the frozen source directory."""
    target=BTC_LOCAL
    if target.is_file():
        print('HLL_BTC_SOURCE_ALREADY_EXISTS',str(target),flush=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    records=[]
    next_ts=START
    while next_ts<END:
        params=urlencode({'symbol':'BTCUSDT','interval':'30m','startTime':next_ts,
                          'endTime':END-1,'limit':1000})
        url='https://fapi.binance.com/fapi/v1/klines?'+params
        for attempt in range(4):
            try:
                with urlopen(Request(url,headers={'User-Agent':'HLL-offline-research/1'}),timeout=30) as r:
                    page=json.load(r)
                if not isinstance(page,list) or not page:raise RuntimeError('Empty Binance BTCUSDT response')
                break
            except Exception:
                if attempt==3:raise
                time.sleep(1.5*(attempt+1))
        for raw in page:
            ts=int(raw[0])
            if ts!=next_ts:raise ValueError(f'Noncontiguous BTC data: expected {next_ts}, got {ts}')
            records.append({'timestamp':ts,'close':float(raw[4])})
            next_ts+=INTERVAL
        print('HLL_BTC_FETCH_PROGRESS',json.dumps({'bars':len(records),'last_ts':next_ts}),flush=True)
        time.sleep(.3)
    if next_ts!=END:raise ValueError('BTC data incomplete')
    f, tmp=tempfile.mkstemp(dir=target.parent,prefix='.hll-btc-',suffix='.csv')
    try:
        with os.fdopen(f,'w',encoding='utf-8') as h:
            pd.DataFrame(records).to_csv(h,index=False)
        if target.exists():raise FileExistsError(target)
        os.rename(tmp,target)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    print('HLL_BTC_SOURCE_SAVED',json.dumps({'path':str(target),'bars':len(records)}),flush=True)


def read_prices(path):
    p=Path(path)
    if not p.is_file():raise FileNotFoundError(p)
    frame=pd.read_csv(p,usecols=['timestamp','close'],low_memory=False)
    for col in ['timestamp','close']:
        frame[col]=pd.to_numeric(frame[col],errors='coerce')
    if frame[['timestamp','close']].isna().any().any():raise ValueError(f'Invalid prices {p}')
    if (frame.close<=0).any():raise ValueError(f'Nonpositive prices {p}')
    frame['timestamp']=frame.timestamp.astype('int64')
    if frame.timestamp.duplicated().any() or not frame.timestamp.is_monotonic_increasing:
        raise ValueError(f'Duplicate/out-of-order candles {p}')
    if (frame.timestamp%INTERVAL!=0).any():raise ValueError(f'Non-aligned candle timestamp {p}')
    return frame


def preentry_metrics(aligned, entry_ts):
    """96 *closed* half-hour returns; never include candle open >= entry_ts.

    At an entry 03:30, the latest legal 30m bar is 03:00--03:30.
    At an entry 03:31, that same 03:00 bar remains the latest legal one.
    Missing 30m bars and gaps are explicitly unavailable, never filled.
    """
    entry_ts=int(entry_ts)
    expected_last_open=(entry_ts//INTERVAL-1)*INTERVAL
    t=aligned['timestamp'].to_numpy(dtype=np.int64)
    j=int(np.searchsorted(t,expected_last_open,side='right'))
    if j<97:return {'btc_context_status':'NOT_ENOUGH_CLOSED_30M'}
    if int(t[j-1])!=expected_last_open:return {'btc_context_status':'MISSING_LAST_CLOSED_30M'}
    window=t[j-97:j]
    if not np.all(np.diff(window)==INTERVAL):return {'btc_context_status':'GAP_IN_30M_LOOKBACK'}
    x=aligned['close_symbol'].to_numpy(dtype=np.float64)[j-97:j]
    b=aligned['close_btc'].to_numpy(dtype=np.float64)[j-97:j]
    if not (np.isfinite(x).all() and np.isfinite(b).all()) or (x<=0).any() or (b<=0).any():
        return {'btc_context_status':'INVALID_SOURCE_PRICE'}
    rx=np.diff(x)/x[:-1]
    rb=np.diff(b)/b[:-1]
    var=np.var(rb,ddof=1)
    if not np.isfinite(var) or var<=0:return {'btc_context_status':'INVALID_BTC_VARIANCE'}
    corr=float(np.corrcoef(rx,rb)[0,1])
    beta=float(np.cov(rx,rb,ddof=1)[0,1]/var)
    if not (math.isfinite(corr) and math.isfinite(beta)):
        return {'btc_context_status':'INVALID_CORRELATION'}
    result={'btc_context_status':'AVAILABLE','context_last_close_ts':int(window[-1]+INTERVAL),
            'btc_corr_30m_48h':corr,'btc_beta_30m_48h':beta,'btc_r2_30m_48h':corr*corr}
    for hours,bars in [(2,4),(4,8),(24,48)]:
        sm=(x[-1]/x[-1-bars]-1)*100
        bm=(b[-1]/b[-1-bars]-1)*100
        result[f'symbol_move_{hours}h_pct']=sm
        result[f'btc_move_{hours}h_pct']=bm
        result[f'btc_residual_{hours}h_pct']=sm-beta*bm
    return result


def classify(features):
    f=features.copy()
    valid=f.btc_context_status.eq('AVAILABLE')
    rho=pd.to_numeric(f.get('btc_corr_30m_48h'),errors='coerce')
    f['btc_corr_bucket']='MISSING'
    f.loc[valid&(rho<0),'btc_corr_bucket']='NEGATIVE'
    f.loc[valid&(rho>=0)&(rho<.30),'btc_corr_bucket']='LOW_POS_0_03'
    f.loc[valid&(rho>=.30)&(rho<.70),'btc_corr_bucket']='MID_POS_03_07'
    f.loc[valid&(rho>=.70),'btc_corr_bucket']='HIGH_POS_07_PLUS'
    residual=pd.to_numeric(f.get('btc_residual_4h_pct'),errors='coerce')
    move=pd.to_numeric(f.get('symbol_move_4h_pct'),errors='coerce')
    btc=pd.to_numeric(f.get('btc_move_4h_pct'),errors='coerce')
    f['btc_residual_bucket_4h']='MISSING'
    for label,mask in [
        ('LE_0',residual<=0),('0_TO_05',(residual>0)&(residual<=.5)),
        ('05_TO_15',(residual>.5)&(residual<=1.5)),('GT_15',residual>1.5)]:
        f.loc[valid&mask,'btc_residual_bucket_4h']=label
    f['btc_strength_state_4h']='MISSING'
    f.loc[valid,'btc_strength_state_4h']='OTHER'
    f.loc[valid&(move>0)&(btc<=0)&(residual>0),'btc_strength_state_4h']='ALT_UP_BTC_DOWN'
    f.loc[valid&(move>0)&(btc>0)&(residual>0),'btc_strength_state_4h']='ALT_STRONG_BTC_UP'
    f.loc[valid&(move>0)&(btc>0)&(residual<=0),'btc_strength_state_4h']='ALT_UP_REL_WEAK'
    f.loc[valid&(move<=0)&(btc<=0)&(residual>0),'btc_strength_state_4h']='ALT_DOWN_LESS_BTC'
    return f


def features_for_entries(entries,candle_dir,btc):
    cols=['entry_id','attempt_id','symbol','route','entry_ts','entry_utc','entry_month','universe_group','pre_pattern_48']
    missing=set(cols)-set(entries)
    if missing:raise ValueError(f'Missing entry columns: {sorted(missing)}')
    if entries.entry_id.duplicated().any():raise ValueError('Duplicate frozen entry_id')
    output=[]
    for i,(symbol,items) in enumerate(entries.groupby('symbol',sort=True),start=1):
        sym=read_prices(Path(candle_dir)/f'{symbol}_30m.csv')
        aligned=sym.merge(btc,on='timestamp',how='inner',suffixes=('_symbol','_btc'),validate='one_to_one')
        for e in items.itertuples(index=False):
            row={c:getattr(e,c) for c in cols}
            row.update(preentry_metrics(aligned,int(e.entry_ts)))
            output.append(row)
        if i%25==0 or i==entries.symbol.nunique():
            print('HLL_BTC_CONTEXT_PROGRESS',json.dumps({'symbols':i,'of':entries.symbol.nunique(),
                       'entries':len(output)}),flush=True)
    f=classify(pd.DataFrame(output))
    if len(f)!=len(entries) or f.entry_id.duplicated().any():raise ValueError('Feature output cardinality')
    known=f.btc_context_status.eq('AVAILABLE')
    if known.any() and not (pd.to_numeric(f.loc[known,'context_last_close_ts'])<=f.loc[known,'entry_ts']).all():
        raise ValueError('LOOKAHEAD: snapshot after entry')
    return f


def read_cases(path):
    needed=['entry_id','horizon_min','tp_pct','sl_pct','result','net_pct']
    parts=[]
    for chunk in pd.read_csv(path,usecols=needed,chunksize=80_000):
        mask=pd.Series(False,index=chunk.index)
        for (h,tp,sl) in CASES:
            mask|=(chunk.horizon_min==h)&(chunk.tp_pct==tp)&(chunk.sl_pct==sl)
        subset=chunk.loc[mask].copy()
        if subset.empty:continue
        subset['case']=[CASES[(int(h),int(tp),int(sl))] for h,tp,sl in
                        zip(subset.horizon_min,subset.tp_pct,subset.sl_pct)]
        parts.append(subset[['entry_id','case','result','net_pct']])
    if not parts:raise ValueError('No requested scenarios')
    cases=pd.concat(parts,ignore_index=True)
    if cases.duplicated(['entry_id','case']).any():raise ValueError('Duplicate scenario per entry/case')
    return cases


def grouped_summary(frame):
    keys=['universe_group','entry_month','route','btc_strength_state_4h','btc_corr_bucket','case']
    src=frame.copy()
    src['known']=src.result.isin(['TP_FIRST','SL_FIRST','TIMEOUT'])
    src['net_known']=pd.to_numeric(src.net_pct,errors='coerce').where(src.known)
    g=src.groupby(keys,dropna=False,sort=True).agg(
      entries=('entry_id','size'),known=('net_known','count'),net_sum=('net_known','sum'),
      tp=('result',lambda x:int(x.eq('TP_FIRST').sum())),
      sl=('result',lambda x:int(x.eq('SL_FIRST').sum())),
      timeout=('result',lambda x:int(x.eq('TIMEOUT').sum()))).reset_index()
    g['net_mean_pct']=g.net_sum/g.known.replace(0,np.nan)
    return g


def atomic_csv(df,dest):
    dest=Path(dest);dest.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=dest.parent,prefix='.hll-btc-',suffix='.part')
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as h:df.to_csv(h,index=False)
        if dest.exists():raise FileExistsError(f'Existing research output: {dest}')
        os.rename(tmp,dest)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def run(scope):
    source=BASE/f'entries_{scope}'
    manifest=source/'manifest.json'
    events=source/'entry_events.csv'; scenarios=source/'entry_scenarios_25.csv'
    for p in [manifest,events,scenarios,btc_path()]:
        if not p.is_file():raise FileNotFoundError(f'Missing {p}')
    if root(scope).exists():raise FileExistsError(f'{root(scope)} already exists; no overwrite')
    m=json.loads(manifest.read_text())
    if m.get('scope')!=scope:raise ValueError('Wrong scope manifest')
    e=pd.read_csv(events,low_memory=False)
    if len(e)!=m.get('entries'):raise ValueError('Entry manifest count mismatch')
    btc=read_prices(btc_path())
    f=features_for_entries(e,CANDLES,btc)
    s=read_cases(scenarios)
    expected=set(CASES.values())
    count=s.groupby('entry_id').case.agg(lambda x:set(x))
    if set(count.index)!=set(f.entry_id) or not count.map(lambda x:x==expected).all():
        raise ValueError('Missing scenario cases for some entries')
    paired=f.merge(s,on='entry_id',how='inner',validate='one_to_many')
    if len(paired)!=len(f)*len(CASES):raise ValueError('Paired outputs cardinality mismatch')
    # Data are historic simulations; no automated thresholds or trading.
    out=root(scope);out.mkdir(parents=True,exist_ok=False)
    try:
        atomic_csv(f,out/'entry_features.csv')
        atomic_csv(paired,out/'paired_results.csv')
        atomic_csv(grouped_summary(paired),out/'segmented_summary.csv')
        meta={'scope':scope,'entries':len(f),'scenarios_compared':len(paired),
              'context_available':int(f.btc_context_status.eq('AVAILABLE').sum()),
              'unavailable_reasons':f.btc_context_status.value_counts().to_dict(),
              'btc_source_path':str(btc_path()),'btc_source_sha256':sha(btc_path()),
              'entry_events_sha256':sha(events),
              'calculation':'historical closed 30m Pearson 96-return corr, beta=cov/var, residual=move-beta*BTC move',
              'causal_rule':'Latest included 30m candle open + 30min <= entry_ts. No backfills / no imputation.',
              'cases':sorted(expected),'scope_entries_source':str(source),
              'limitations':['Exploratory bucket comparisons, not prospective validation',
                             'Concurrent/overlapping entries not independent',
                             '30m source starts Aug20; first 48h can have unavailable metrics',
                             'No funding, live fills or portfolio simulation','LONG routes only']}
        (out/'manifest.json').write_text(json.dumps(meta,indent=2,ensure_ascii=False),encoding='utf-8')
    except Exception:
        import shutil
        shutil.rmtree(out)
        raise
    print('HLL_BTC_STRENGTH_DONE',json.dumps({'scope':scope,'entries':len(f),'paired_rows':len(paired),
          'context_available':meta['context_available'],'unavailable_reasons':meta['unavailable_reasons'],
          'output':str(out)},ensure_ascii=False),flush=True)
    focus=paired.loc[(paired.universe_group=='ADDITIONAL_275')&
       (paired.case=='5_5_48h')&paired.btc_context_status.eq('AVAILABLE')&
       paired.result.isin(['TP_FIRST','SL_FIRST','TIMEOUT'])].copy()
    if not focus.empty:
        baseline=focus.groupby(['entry_month','route'],dropna=False).net_pct.agg(
            base_n='size',base_net='mean').reset_index()
        for dimension,label in [('btc_strength_state_4h','HLL_BTC_STATE_SUMMARY_5_5_48H'),
                                ('btc_corr_bucket','HLL_BTC_CORR_SUMMARY_5_5_48H'),
                                ('btc_residual_bucket_4h','HLL_BTC_RESIDUAL_SUMMARY_5_5_48H')]:
            summary=focus.groupby(['entry_month','route',dimension],dropna=False).agg(
              n=('net_pct','count'),mean_net=('net_pct','mean'),
              tp=('result',lambda x:int(x.eq('TP_FIRST').sum())),
              sl=('result',lambda x:int(x.eq('SL_FIRST').sum()))).reset_index()
            summary=summary.merge(baseline,on=['entry_month','route'],validate='many_to_one')
            summary['delta_vs_route_month_pp']=summary.mean_net-summary.base_net
            print(label,flush=True)
            print(summary[summary.n>=20].round(4).to_string(index=False),flush=True)


def report(scope):
    path=root(scope)/'manifest.json'
    print('HLL_BTC_STRENGTH_REPORT',json.dumps({'scope':scope,
      'btc_source':str(btc_path()),'btc_available':btc_path().exists(),
      'entries_ready':(BASE/f'entries_{scope}'/'entry_events.csv').exists(),
      'research_ready':path.exists(),
      'manifest':json.loads(path.read_text()) if path.exists() else None},ensure_ascii=False),flush=True)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    x=p.add_mutually_exclusive_group(required=True)
    x.add_argument('--plan',action='store_true');x.add_argument('--fetch-btc',action='store_true')
    x.add_argument('--run',action='store_true');x.add_argument('--report',action='store_true')
    p.add_argument('--scope',choices=['FIRST_PER_LEVEL','ALL'],default='FIRST_PER_LEVEL')
    a=p.parse_args(argv)
    if a.fetch_btc:fetch_btc()
    elif a.run:run(a.scope)
    else:report(a.scope)
    return 0

if __name__=='__main__':raise SystemExit(main())
