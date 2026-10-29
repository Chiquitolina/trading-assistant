#!/usr/bin/env python3
"""Historical BTC dependency for frozen HLL entries. Offline; no LIVE writes.

Exact taxonomy from dashboard/app.py's add_btc_correlation_buckets:
  corr >= 0.80 and R² >= 0.60 -> copied_weak / copied_neutral /
  correlated_with_strength, split by directional residual +/-0.20%.
  Else -> independent_strength / independent_weakness / mixed.

5m,15m are aggregated from actual complete archived 1m OHLC bars;
30m is native, 1h and 4h from complete archived 30m bars.
Only CLOSED data with close_ts <= HLL entry_ts are ever used.
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
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

from dashboard.views.hll_v301_btc_strength import (
    BASE, CANDLES, CASES, btc_path, read_prices, root as prior_root, sha,
)

MINUTE = 60_000
MINUTES = BASE / 'minutes'
BTC_1M = BASE / 'btc_strength_sources' / 'BTCUSDT_1m.csv'
SOURCE_START = 1787184000000
SOURCE_END = 1791244800000
# (source interval, target interval, lookback in returns, movement in bars)
FRAMES = {
    '5m': (MINUTE, 5 * MINUTE, 96, 12),
    '15m': (MINUTE, 15 * MINUTE, 96, 4),
    '30m': (30 * MINUTE, 30 * MINUTE, 96, 4),
    '1h': (30 * MINUTE, 60 * MINUTE, 72, 3),
    '4h': (30 * MINUTE, 240 * MINUTE, 42, 2),
}
LABELS = (
    'btc_copied_weak', 'btc_copied_neutral', 'btc_correlated_with_strength',
    'independent_strength', 'independent_weakness', 'mixed', 'unknown',
)


def output_root(scope):
    return BASE / f'btc_dependency_mtf_{scope}'


def read_minute(path):
    frame = pd.read_csv(path, usecols=['timestamp', 'close'], low_memory=False)
    for col in ('timestamp', 'close'):
        frame[col] = pd.to_numeric(frame[col], errors='coerce')
    if frame[['timestamp', 'close']].isna().any().any():
        raise ValueError(f'Bad minute values {path}')
    frame['timestamp'] = frame.timestamp.astype('int64')
    if (frame.timestamp % MINUTE != 0).any():
        raise ValueError(f'Misaligned minute {path}')
    if frame.timestamp.duplicated().any() or not frame.timestamp.is_monotonic_increasing:
        raise ValueError(f'Unordered/duplicate minute {path}')
    if (frame.close <= 0).any() or not np.isfinite(frame.close).all():
        raise ValueError(f'Nonpositive/nonfinite minute prices {path}')
    return frame


def aggregate_closed(source, source_interval, target_interval):
    """Last close of an entire complete higher-timeframe bar; no gap filling."""
    if target_interval == source_interval:
        return source[['timestamp', 'close']].copy().reset_index(drop=True)
    ratio, remainder = divmod(target_interval, source_interval)
    if remainder or ratio < 2:
        raise ValueError('Target timeframe must be a multiple of source')
    source = source[['timestamp', 'close']].copy()
    source['start'] = source.timestamp // target_interval * target_interval
    g = source.groupby('start', sort=True).agg(
        n=('timestamp', 'size'), first=('timestamp', 'first'),
        last=('timestamp', 'last'), close=('close', 'last'))
    ok = ((g.n == ratio) & (g['first'] == g.index) &
          (g['last'] == g.index + target_interval - source_interval))
    # Input timestamps strictly increasing and unique; complete count + endpoints
    # prove continuity within this group because all lie on the child grid.
    g = g.loc[ok]
    return pd.DataFrame({
        'timestamp': g.index.to_numpy(dtype=np.int64),
        'close': g.close.to_numpy(dtype=float),
    })


def at_entry(aligned, entry_ts, tf):
    """Return metrics for exactly one timeframe, or explicit missing status."""
    _, duration, lookback, move_bars = FRAMES[tf]
    t = aligned.timestamp.to_numpy(dtype=np.int64)
    expected = (int(entry_ts) // duration - 1) * duration
    j = int(np.searchsorted(t, expected, side='right'))
    if j < lookback + 1:
        return {'status': 'NOT_ENOUGH_CLOSED_BARS'}
    if t[j-1] != expected:
        return {'status': 'MISSING_LATEST_CLOSED_BAR'}
    bars = t[j-lookback-1:j]
    if not np.all(np.diff(bars) == duration):
        return {'status': 'GAP_IN_LOOKBACK'}
    a = aligned.close_symbol.to_numpy(dtype=float)[j-lookback-1:j]
    b = aligned.close_btc.to_numpy(dtype=float)[j-lookback-1:j]
    if (not np.isfinite(a).all() or not np.isfinite(b).all() or
            (a <= 0).any() or (b <= 0).any()):
        return {'status': 'INVALID_PRICE'}
    ra = np.diff(a) / a[:-1]
    rb = np.diff(b) / b[:-1]
    var = float(np.var(rb, ddof=1))
    if not math.isfinite(var) or var <= 0:
        return {'status': 'INVALID_BTC_VARIANCE'}
    corr = float(np.corrcoef(ra, rb)[0, 1])
    beta = float(np.cov(ra, rb, ddof=1)[0, 1] / var)
    if not (math.isfinite(corr) and math.isfinite(beta)):
        return {'status': 'INVALID_CORRELATION'}
    # Same move_lookback as existing BTCCorrelationAnalyzer.DEFAULT_CONFIG
    symbol_move = float((a[-1] / a[-1-move_bars] - 1) * 100)
    btc_move = float((b[-1] / b[-1-move_bars] - 1) * 100)
    residual = symbol_move - beta * btc_move  # LONG directional residual = raw residual
    r2 = corr * corr
    copied = corr >= 0.80 and r2 >= 0.60
    if copied:
        dependency = ('btc_copied_weak' if residual <= -0.20 else
                      'btc_copied_neutral' if residual < 0.20 else
                      'btc_correlated_with_strength')
    else:
        dependency = ('independent_strength' if residual >= 0.20 else
                      'independent_weakness' if residual <= -0.20 else 'mixed')
    last_close_ts = int(t[j-1] + duration)
    if last_close_ts > int(entry_ts):
        raise ValueError('LOOKAHEAD: candle closes after the entry')
    return {'status': 'AVAILABLE', 'last_close_ts': last_close_ts,
            'corr': corr, 'beta': beta, 'r2': r2,
            'symbol_move_pct': symbol_move, 'btc_move_pct': btc_move,
            'residual_pct': residual, 'dependency': dependency}


def contextualize(entries, btc_30, btc_1m=None, minute_dir=MINUTES, candle_dir=CANDLES):
    """Calculate all five windows per entry without filtering any HLL entry."""
    if entries.entry_id.duplicated().any():
        raise ValueError('Duplicate HLL entry_id')
    base_cols = ['entry_id','symbol','route','entry_ts','entry_utc',
                 'entry_month','universe_group','pre_pattern_48']
    missing = set(base_cols) - set(entries.columns)
    if missing: raise ValueError(f'Missing HLL columns: {sorted(missing)}')
    btc_30_views = {tf: aggregate_closed(btc_30, FRAMES[tf][0], FRAMES[tf][1])
                    for tf in ('30m','1h','4h')}
    btc_min_views = ({tf: aggregate_closed(btc_1m, FRAMES[tf][0], FRAMES[tf][1])
                     for tf in ('5m','15m')} if btc_1m is not None else {})
    output = []
    groups = entries.groupby('symbol', sort=True)
    total = entries.symbol.nunique()
    for i, (symbol, rows) in enumerate(groups, 1):
        alt30 = read_prices(Path(candle_dir) / f'{symbol}_30m.csv')
        views = {tf: aggregate_closed(alt30, FRAMES[tf][0], FRAMES[tf][1])
                 for tf in ('30m','1h','4h')}
        has_1m = btc_1m is not None and (Path(minute_dir) / f'{symbol}_1m.csv').is_file()
        if has_1m:
            minute = read_minute(Path(minute_dir) / f'{symbol}_1m.csv')
            views.update({tf: aggregate_closed(minute, FRAMES[tf][0], FRAMES[tf][1])
                          for tf in ('5m','15m')})
        aligned = {}
        for tf, alt in views.items():
            btc = btc_min_views[tf] if tf in btc_min_views else btc_30_views[tf]
            aligned[tf] = alt.merge(btc, on='timestamp', how='inner',
                                    suffixes=('_symbol','_btc'), validate='one_to_one')
        for e in rows.itertuples(index=False):
            row = {key: getattr(e, key) for key in base_cols}
            for tf in FRAMES:
                result = (at_entry(aligned[tf], e.entry_ts, tf) if tf in aligned else
                          {'status': 'NO_1M_SOURCE'})
                for name, value in result.items():
                    row[f'btc_{name}_{tf}'] = value
            output.append(row)
        if i % 25 == 0 or i == total:
            print('HLL_BTC_DEPENDENCY_MTF_PROGRESS', json.dumps({'symbols':i,'total':total,'entries':len(output)}), flush=True)
    result = pd.DataFrame(output)
    if len(result) != len(entries) or result.entry_id.duplicated().any():
        raise ValueError('HLL entry feature cardinality mismatch')
    return result


def fetch_btc_1m():
    """Public Binance data to separate research cache. Explicit only, atomic."""
    if BTC_1M.exists():
        print('HLL_BTC_1M_EXISTS', str(BTC_1M), flush=True)
        return
    BTC_1M.parent.mkdir(parents=True, exist_ok=True)
    records = []
    expected = SOURCE_START
    while expected < SOURCE_END:
        url = 'https://fapi.binance.com/fapi/v1/klines?' + urlencode({
            'symbol':'BTCUSDT', 'interval':'1m', 'startTime':expected,
            'endTime':SOURCE_END-1, 'limit':1500})
        for attempt in range(4):
            try:
                with urlopen(Request(url,headers={'User-Agent':'HLL-BTC-dependency-research/1'}),timeout=35) as response:
                    page = json.load(response)
                if not isinstance(page,list) or not page:
                    raise RuntimeError('Empty BTC minute response')
                break
            except Exception:
                if attempt == 3: raise
                time.sleep(1.5*(attempt+1))
        for record in page:
            t = int(record[0])
            if t != expected:
                raise ValueError(f'BTC minute discontinuity expected {expected}, found {t}')
            records.append((t, float(record[4])))
            expected += MINUTE
        print('HLL_BTC_1M_PROGRESS', json.dumps({'bars':len(records)}), flush=True)
        time.sleep(0.15)
    if expected != SOURCE_END:
        raise ValueError('Incomplete BTC minute archive')
    fd,tmp=tempfile.mkstemp(prefix='.hll-btc1m-',suffix='.part',dir=BTC_1M.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            stream.write('timestamp,close\n')
            for t,close in records:stream.write(f'{t},{close}\n')
        if BTC_1M.exists():raise FileExistsError(BTC_1M)
        os.rename(tmp, BTC_1M)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    print('HLL_BTC_1M_SAVED',str(BTC_1M),len(records),flush=True)


def _atomic_csv(df,path):
    fd,tmp=tempfile.mkstemp(prefix='.hll-mtf-',suffix='.part',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            df.to_csv(stream,index=False)
        if path.exists():raise FileExistsError(path)
        os.rename(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def run(scope):
    source = prior_root(scope)
    prior = source / 'paired_results.csv'
    original_entries = BASE / f'entries_{scope}' / 'entry_events.csv'
    dest = output_root(scope)
    if dest.exists(): raise FileExistsError(f'No overwrite: {dest}')
    for path in (prior, original_entries, btc_path(), BTC_1M):
        if not path.exists():raise FileNotFoundError(f'Missing input: {path}')
    entries = pd.read_csv(original_entries,low_memory=False)
    btc30 = read_prices(btc_path())
    btc1m = read_minute(BTC_1M)
    if btc1m.timestamp.iloc[0] > SOURCE_START or btc1m.timestamp.iloc[-1] < SOURCE_END-MINUTE:
        raise ValueError('BTC 1m archive does not cover the historical HLL range')
    features = contextualize(entries,btc30,btc1m,minute_dir=MINUTES,candle_dir=CANDLES)
    # Assert the new 30m metrics exactly replicate the already audited BTC study.
    prev = pd.read_csv(source/'entry_features.csv', usecols=['entry_id','btc_context_status','btc_corr_30m_48h'])
    compare = features[['entry_id','btc_status_30m','btc_corr_30m']].merge(prev,on='entry_id',validate='one_to_one')
    same = compare.btc_status_30m.eq('AVAILABLE') & compare.btc_context_status.eq('AVAILABLE')
    if not (compare.btc_status_30m.eq('AVAILABLE') == compare.btc_context_status.eq('AVAILABLE')).all():
        raise ValueError('30m availability parity with frozen BTC context failed')
    if same.any() and not np.allclose(compare.loc[same,'btc_corr_30m'], compare.loc[same,'btc_corr_30m_48h'],atol=1e-10,rtol=1e-9):
        raise ValueError('30m correlation parity with frozen BTC context failed')
    keep_cols = ['entry_id','case','result','net_pct']
    frames=[]
    for chunk in pd.read_csv(prior,usecols=keep_cols,chunksize=75_000):
        frames.append(chunk)
    outcomes=pd.concat(frames,ignore_index=True)
    if outcomes.duplicated(['entry_id','case']).any():raise ValueError('Duplicate scenario')
    if len(outcomes) != len(features)*len(CASES):raise ValueError('Scenario coverage mismatch')
    paired = features.merge(outcomes,on='entry_id',validate='one_to_many')
    if len(paired) != len(outcomes):raise ValueError('Mismatched paired scenarios')
    long = pd.concat([
        paired[['entry_id','symbol','entry_month','route','universe_group','pre_pattern_48','case','result','net_pct',
                f'btc_status_{tf}',f'btc_dependency_{tf}',f'btc_corr_{tf}',f'btc_residual_pct_{tf}']]
        .rename(columns={f'btc_status_{tf}':'status',f'btc_dependency_{tf}':'dependency',
                         f'btc_corr_{tf}':'corr',f'btc_residual_pct_{tf}':'residual'})
        .assign(timeframe=tf)
        for tf in FRAMES
    ],ignore_index=True)
    valid=long.loc[long.status.eq('AVAILABLE') & long.result.isin(['TP_FIRST','SL_FIRST','TIMEOUT'])]
    summary=valid.groupby(['universe_group','entry_month','route','timeframe','dependency','case'],dropna=False).agg(
        n=('entry_id','size'), symbols=('symbol','nunique'),net_mean_pct=('net_pct','mean'),
        tp=('result',lambda x:int(x.eq('TP_FIRST').sum())),
        sl=('result',lambda x:int(x.eq('SL_FIRST').sum())),
        timeout=('result',lambda x:int(x.eq('TIMEOUT').sum()))).reset_index()
    dest.mkdir(parents=True,exist_ok=False)
    try:
        _atomic_csv(features,dest/'entry_features.csv')
        _atomic_csv(paired,dest/'paired_results.csv')
        _atomic_csv(summary,dest/'segmented_summary.csv')
        manifest={'scope':scope,'entries':len(features),'scenarios':len(paired),
                  'timeframes':list(FRAMES),
                  'available_entries':{tf:int(features[f'btc_status_{tf}'].eq('AVAILABLE').sum()) for tf in FRAMES},
                  'sources':{'btc_30m':{'path':str(btc_path()),'sha256':sha(btc_path())},
                             'btc_1m':{'path':str(BTC_1M),'sha256':sha(BTC_1M)},
                             'entry_events':{'path':str(original_entries),'sha256':sha(original_entries)}},
                  'taxonomy':'Exact app.py btc_dependency_{timeframe} thresholds: corr>=.8 and r2>=.6; LONG residual +/- .2%',
                  'lookbacks':{tf:{'correlation_returns':v[2],'movement_bars':v[3]} for tf,v in FRAMES.items()},
                  'causality':'Complete closed 1m->5m/15m, native30m->1h/4h bars only; last_close_ts<=entry_ts',
                  'limitations':['Research only. No changes to LIVE, frozen entries or scenario exits.',
                                 'Exploratory segmentation: multiple comparisons, overlap, month effects.',
                                 'TP/SL net excludes funding and slippage.']}
        (dest/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding='utf-8')
    except Exception:
        import shutil
        shutil.rmtree(dest)
        raise
    print('HLL_BTC_DEPENDENCY_MTF_DONE',json.dumps({'scope':scope,'entries':len(features),'scenarios':len(paired),
          'available':manifest['available_entries'],'output':str(dest)},ensure_ascii=False),flush=True)
    print('HLL_BTC_DEPENDENCY_MTF_5_5_48H',flush=True)
    print(summary.loc[(summary.universe_group=='ADDITIONAL_275')&(summary.case=='5_5_48h')&
                      summary.dependency.isin(['independent_strength','btc_copied_neutral',
                                              'btc_correlated_with_strength']) & (summary.n>=20)]
          .round(4).to_string(index=False),flush=True)


def plan(scope):
    paths = {'btc_1m':BTC_1M,'btc_30m':btc_path(),
             'minute_example':MINUTES / 'ETHUSDT_1m.csv',
             'prior_results':prior_root(scope)/'paired_results.csv',
             'entries':BASE/f'entries_{scope}'/'entry_events.csv',
             'result':output_root(scope)/'manifest.json'}
    print('HLL_BTC_DEPENDENCY_MTF_PLAN',json.dumps({k:{'path':str(p),'available':p.exists()}
        for k,p in paths.items()},ensure_ascii=False),flush=True)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    choices=p.add_mutually_exclusive_group(required=True)
    choices.add_argument('--plan',action='store_true')
    choices.add_argument('--fetch-btc-1m',action='store_true')
    choices.add_argument('--run',action='store_true')
    p.add_argument('--scope',choices=['FIRST_PER_LEVEL','ALL'],default='FIRST_PER_LEVEL')
    args=p.parse_args(argv)
    if args.plan:plan(args.scope)
    elif args.fetch_btc_1m:fetch_btc_1m()
    else:run(args.scope)
    return 0

if __name__=='__main__':raise SystemExit(main())
