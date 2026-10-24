#!/usr/bin/env python3
"""Legacy Immediate Fail Reversal v1.9 — research-only OHLC historical study.

Preserves canonical Legacy TOUCH_FAILED: first touch of confirmed 15m 3x3 swing,
move-away >= 0.20%, retest tolerance 0.10%, first retest max age 360m.
Original LONG TOUCH_FAILED -> enter SHORT at next contiguous 1m OPEN; vice versa.

No network; no connection to Redis, exchange, or live SQLite databases.
Uses already downloaded frozen native 1m CSVs (September / October).
Sources are read-only; new results stored under isolated reports/reaction_lab path.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

import numpy as np
import pandas as pd

VERSION = 'legacy-immediate-fail-reversal-v1.9.0'
ROOT = Path(__file__).resolve().parent
ONE = 60_000
FIFTEEN = 15 * ONE
TPS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
SLS = (0.5, 0.75, 1.0, 1.25, 1.5)
HORIZONS = (15, 30, 60, 120)
FEE = 0.1
SLIPPAGE_BPS = 5.0
PORTFOLIO_CONFIGS = ((0.75, 0.75, 30), (1.0, 1.0, 60))
PERIODS = {
    'september': ('reports/horizontal_breakout_lab_v1/temporal_v14/holdout_20260910_20260922/native', '2026-09-10', '2026-09-22'),
    'october': ('reports/horizontal_breakout_lab_v1/frozen_v13/symbol_holdout_40_20261008_20261020/native', '2026-10-09', '2026-10-20'),
}
RESULTS = ROOT / 'reports/reaction_lab/legacy_immediate_fail_reversal_v19'
CANDLE_COLS = ('timestamp', 'open', 'high', 'low', 'close', 'volume', 'quoteVolume')
SIGNAL_COLS = ('event_id','period','symbol','original_side','inverse_side','detector','pivot_ts','actionable_ts',
               'touch_ts','entry_ts','entry_open','swing_price','failure_close','failure_depth_pct','source_batch')
TRADE_COLS = (*SIGNAL_COLS,'trade_direction','tp_pct','sl_pct','horizon_min','status',
              'entry_effective','exit_ts','exit_price_effective','held_minutes','net_pct')


def utc_ms(date):
    return int(datetime.strptime(date, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp() * 1000)


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def safe_write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, prefix='.tmp_', delete=False) as fh:
        tmp = Path(fh.name)
        json.dump(payload, fh, ensure_ascii=False, indent=2, allow_nan=False)
    os.replace(tmp, path)


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', newline='', dir=path.parent, prefix='.tmp_', delete=False) as fh:
        tmp=Path(fh.name)
        writer=csv.DictWriter(fh,fieldnames=fields,extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp,path)


def native_sources(root, period):
    subdir, start, end = PERIODS[period]
    basedir = root / subdir
    if not basedir.is_dir():
        raise FileNotFoundError(f'{period}: missing native OHLC folder: {basedir}')
    src={}
    for batch in sorted(basedir.glob('batch_*')):
        config=batch/'dataset_config.json'
        if not config.is_file():
            raise RuntimeError(f'No dataset config in {batch}')
        cfg=json.loads(config.read_text())
        if str(cfg.get('source'))!='Binance USD-M Futures public klines' or int(cfg.get('tail_hours',0))<48:
            raise RuntimeError(f'Unexpected source/config in {batch}')
        if not cfg.get('start') or not cfg.get('end') or utc_ms(cfg['start'])>utc_ms(start) or utc_ms(cfg['end'])<utc_ms(end):
            raise RuntimeError(f'Insufficient source date coverage in {batch}')
        for symbol in cfg['symbols']:
            if period == 'october' and symbol == 'PEPEUSDT':
                # Explicit frozen availability amendment from V1.3; never substitute 1000PEPEUSDT.
                continue
            # native collector's canonical layout, discover filenames below if changes.
            matches=[p for p in batch.glob(f'{symbol}*1m*.csv') if p.is_file()]
            if not matches:
                matches=[p for p in batch.glob('*.csv') if symbol in p.name and '1m' in p.name]
            if len(matches)!=1:
                raise RuntimeError(f'Expected exactly one native 1m CSV for {symbol} in {batch}: {matches}')
            if symbol in src:
                raise RuntimeError(f'Duplicate symbol {symbol} in {period}')
            src[symbol]=(matches[0],batch.name)
    if len(src)!=39:
        raise RuntimeError(f'{period}: expected 39 frozen symbols, got {len(src)}')
    return src


def read_candles(path):
    df=pd.read_csv(path)
    if not set(CANDLE_COLS).issubset(df.columns):
        raise ValueError(f'Unexpected OHLC schema: {path}: {list(df.columns)}')
    df=df[list(CANDLE_COLS)].copy()
    df['timestamp']=pd.to_numeric(df.timestamp,errors='raise').astype('int64')
    for c in CANDLE_COLS[1:]:
        df[c]=pd.to_numeric(df[c],errors='raise')
    if df.empty or not np.isfinite(df[list(CANDLE_COLS[1:])].to_numpy()).all():
        raise ValueError(f'Empty/nonfinite candles: {path}')
    ts=df.timestamp.to_numpy()
    if ts[0]%ONE or np.any(np.diff(ts)!=ONE):
        raise ValueError(f'Gap, duplicate or unordered candles: {path}')
    if np.any(df.low.to_numpy()<=0) or np.any(df.high.to_numpy()<np.maximum(df.open.to_numpy(),df.close.to_numpy())) or np.any(df.low.to_numpy()>np.minimum(df.open.to_numpy(),df.close.to_numpy())):
        raise ValueError(f'Invalid OHLC: {path}')
    return df


def aggregate_15m(one):
    ts=one.timestamp.to_numpy()
    groups=ts//FIFTEEN
    out=one.assign(_g=groups).groupby('_g',sort=True).agg(
        timestamp=('timestamp','min'),open=('open','first'),high=('high','max'),
        low=('low','min'),close=('close','last'),volume=('volume','sum'),
        quoteVolume=('quoteVolume','sum'),_n=('timestamp','count'))
    out=out.loc[(out._n==15) & (out.timestamp.to_numpy()%FIFTEEN==0)].drop(columns='_n').reset_index(drop=True)
    if out.empty: raise RuntimeError('No complete 15m candles')
    return out


def inverted(side):
    if side=='LONG': return 'SHORT'
    if side=='SHORT': return 'LONG'
    raise ValueError(f'Invalid original side {side}')


def _constant_tpsl_evaluator(one, entry_i, side, tp, sl, horizon, fee=FEE, slippage_bps=SLIPPAGE_BPS):
    """Fills: worse entry and worse exit; target/stop on raw 1m prices relative to effective entry.

    Both in same 1m bar always SL. Incomplete bars: CENSORED, not TIMEOUT.
    """
    ts=one['timestamp']
    n=len(ts)
    entry_i=int(entry_i)
    if entry_i<0 or entry_i>=n:
        return dict(status='CENSORED_NO_ENTRY',entry_effective=None,exit_ts=None,exit_price_effective=None,held_minutes=0,net_pct=None)
    if entry_i>0 and int(ts[entry_i])!=int(ts[entry_i-1])+ONE:
        return dict(status='CENSORED_GAP',entry_effective=None,exit_ts=None,exit_price_effective=None,held_minutes=0,net_pct=None)
    direction=1 if side=='LONG' else -1 if side=='SHORT' else 0
    if not direction:raise ValueError('Invalid direction')
    slip=slippage_bps/10000.0
    entry=float(one['open'][entry_i])*(1+direction*slip)
    target=entry*(1+direction*tp/100.0)
    stop=entry*(1-direction*sl/100.0)
    if entry<=0 or min(target,stop)<=0:raise ValueError('Invalid prices')
    status=None; exit_price=None; exit_i=None
    for j in range(entry_i, min(n,entry_i+horizon)):
        if j>entry_i and int(ts[j])!=int(ts[j-1])+ONE:
            return dict(status='CENSORED_GAP',entry_effective=entry,exit_ts=None,exit_price_effective=None,held_minutes=j-entry_i,net_pct=None)
        hi=float(one['high'][j]);lo=float(one['low'][j])
        tp_hit=hi>=target if direction==1 else lo<=target
        sl_hit=lo<=stop if direction==1 else hi>=stop
        if sl_hit:
            status='SL_AMBIGUOUS' if tp_hit else 'SL_FIRST';exit_i=j;exit_price=stop;break
        if tp_hit:
            status='TP_FIRST';exit_i=j;exit_price=target;break
    if status is None:
        if entry_i+horizon>n:
            return dict(status='CENSORED_END',entry_effective=entry,exit_ts=None,exit_price_effective=None,held_minutes=n-entry_i,net_pct=None)
        status='TIMEOUT';exit_i=entry_i+horizon-1;exit_price=float(one['close'][exit_i])
    exit_effective=exit_price*(1-direction*slip)
    net=direction*(exit_effective/entry-1.0)*100.0-fee
    return dict(status=status,entry_effective=entry,exit_ts=int(ts[exit_i]),
                exit_price_effective=exit_effective,held_minutes=exit_i-entry_i+1,net_pct=net)


def replay_matrix(one, signals, period):
    ohlc={k:one[k].to_numpy() for k in ('timestamp','open','high','low','close')}
    ts=ohlc['timestamp']
    rows=[]
    for s in signals:
        entry_ts=int(s['touch_ts'])+ONE
        i=int(np.searchsorted(ts,entry_ts))
        if i>=len(ts) or int(ts[i])!=entry_ts:
            continue  # Fail closed, signal retained by parent count
        s['entry_ts']=entry_ts
        s['entry_open']=float(ohlc['open'][i])
        for side in (s['inverse_side'],s['original_side']):
            trade_direction='INVERSE' if side==s['inverse_side'] else 'ORIGINAL'
            for tp in TPS:
                for sl in SLS:
                    for horizon in HORIZONS:
                        result=_constant_tpsl_evaluator(ohlc,i,side,tp,sl,horizon)
                        rows.append({**s,'trade_direction':trade_direction,'tp_pct':tp,'sl_pct':sl,'horizon_min':horizon,**result})
    return rows


def safe_stats(rows):
    valid=[r for r in rows if r['net_pct'] is not None]
    outcomes=Counter(r['status'] for r in rows)
    wins=[float(r['net_pct']) for r in valid if float(r['net_pct'])>0]
    losses=[-float(r['net_pct']) for r in valid if float(r['net_pct'])<0]
    mean=float(sum(float(r['net_pct']) for r in valid)/len(valid)) if valid else None
    pf=(sum(wins)/sum(losses)) if losses else (None if not wins else 'INF')
    return dict(N=len(rows),evaluable=len(valid),tp_first=outcomes['TP_FIRST'],sl_first=outcomes['SL_FIRST']+outcomes['SL_AMBIGUOUS'],
                timeout=outcomes['TIMEOUT'],censored=len(rows)-len(valid),net_mean_pct=round(mean,5) if mean is not None else None,
                profit_factor=round(pf,5) if isinstance(pf,float) else pf)


def portfolio(rows, period, direction, config, initial=200.0, allocation=0.8):
    tp,sl,horizon=config
    eligible=[x for x in rows if x['period']==period and x['trade_direction']==direction and
              x['tp_pct']==tp and x['sl_pct']==sl and x['horizon_min']==horizon]
    eligible.sort(key=lambda x:(int(x['entry_ts']),x['symbol'],x['event_id']))
    busy_until=-1; equity=initial; peak=initial; maxdd=0.0
    chosen=[]; skips=0;censored=0
    for row in eligible:
        if int(row['entry_ts'])<busy_until:
            skips+=1;continue
        if row['net_pct'] is None:
            censored+=1;continue
        equity *= (1.0+allocation*float(row['net_pct'])/100.0)
        if equity<=0: raise RuntimeError('Equity nonpositive in x1 replay')
        busy_until=int(row['exit_ts'])+ONE
        peak=max(equity,peak)
        maxdd=max(maxdd,(1-equity/peak)*100)
        chosen.append(row)
    returns=[x['net_pct'] for x in chosen]
    win=sum(max(0.,float(r)) for r in returns)
    loss=-sum(min(0.,float(r)) for r in returns)
    return dict(period=period,direction=direction,tp=tp,sl=sl,horizon=horizon,
                signals=len(eligible),trades=len(chosen),skipped_slot_busy=skips,censored_not_filled=censored,
                initial_usd=initial,final_usd=round(equity,5),return_pct=round((equity/initial-1)*100,5),
                max_drawdown_realised_pct=round(maxdd,5),profit_factor=round(win/loss,5) if loss else None,
                outcome_counts=dict(Counter(x['status'] for x in chosen)),selected_event_ids=[x['event_id'] for x in chosen])


def scan_symbol(one, sym, period, scan, first_ts, last_ts, batch):
    from engine.live.research.swing_detector import SwingDetector
    g=scan.__globals__
    confirm=g['get_volume_exhaustion_swing_confirmation_info']
    find=g['_find_confirmed_swing_retest']
    fif=aggregate_15m(one)
    points=SwingDetector(3,3,0.0).detect_all(fif.to_dict('records'))
    ts=one.timestamp.to_numpy(dtype='int64')
    out=[];counter=Counter();keys=set()
    for point in points:
        info=confirm(point=point,timeframe_candles=fif,swing_timeframe='15m')
        if info is None:continue
        action=int(info['actionable_timestamp'])
        if action<first_ts-360*ONE or action>=last_ts:continue
        original='LONG' if point.side=='LOW' else 'SHORT' if point.side=='HIGH' else None
        if not original:continue
        left=int(np.searchsorted(ts,action))
        right=int(np.searchsorted(ts,action+361*ONE))
        if right<=left: continue
        partial=one.iloc[left:right]
        if int(partial.timestamp.iloc[0])!=action:continue
        pivotrow=dict(timeframe='15m',detector='3x3',signal=original,
            pivot_timestamp=int(point.pivot_timestamp),actionable_timestamp=action,
            pivot_price=float(point.price),entry_price=float(info['confirmation_close']),
            pivot_to_confirmation_pct=float(info['move_pct']),prominence_pct=float(point.prominence_pct or 0))
        retest=find(one_minute=partial,swing_row=pivotrow,retest_tolerance_pct=0.10,
                    min_departure_pct=0.20,max_age_minutes=360)
        if retest is None:continue
        touch=int(retest['retest_timestamp'])
        if not(first_ts<=touch<last_ts):continue
        status=retest['status'];counter[status]+=1
        if status!='TOUCH_FAILED':continue
        key=f'{sym}|{original}|{int(point.pivot_timestamp)}|{touch}'
        if key in keys: raise RuntimeError('Duplicate first touch')
        keys.add(key)
        swing=float(retest['swing_price']);closing=float(retest['retest_close'])
        depth=(swing-closing)/swing*100 if original=='LONG' else (closing-swing)/swing*100
        if depth <= 0.1-1e-9:
            raise RuntimeError(f'Canonical FAILED not below/above tolerance: {key} {depth}')
        out.append(dict(event_id=key,period=period,symbol=sym,original_side=original,
            inverse_side=inverted(original),detector='15m_3x3',pivot_ts=int(point.pivot_timestamp),
            actionable_ts=action,touch_ts=touch,entry_ts=touch+ONE,entry_open=None,
            swing_price=swing,failure_close=closing,failure_depth_pct=round(depth,6),source_batch=batch))
    return out,counter,len(points)


def get_canonical_scanner(root):
    from reaction_lab_continuous_scanner import compile_canonical_scanner
    def blocked_loader(*args,**kwargs):
        raise RuntimeError('Network/Redis loader prohibited')
    scanner,fingerprint=compile_canonical_scanner(root/'dashboard/app.py',blocked_loader)
    return scanner,fingerprint


def report_grid(rows):
    groups=defaultdict(list)
    for r in rows:
        groups[(r['period'],r['trade_direction'],r['tp_pct'],r['sl_pct'],r['horizon_min'])].append(r)
    result=[]
    for k,group in sorted(groups.items()):
        result.append(dict(zip(('period','direction','tp','sl','horizon'),k),**safe_stats(group)))
    return result


def run(root,period_names):
    scanner,fp=get_canonical_scanner(root)
    source_paths={};reports={};signal_rows=[];trade_rows=[]
    for period in period_names:
        sources=native_sources(root,period)
        first_ts,last_ts=map(utc_ms,PERIODS[period][1:])
        totals=Counter();failed=[];swings=0
        for sym,(path,batch) in sorted(sources.items()):
            meta_path=path.with_suffix('.meta.json')
            if not meta_path.is_file(): raise RuntimeError(f'Native source has no SHA seal: {path}')
            native_meta=json.loads(meta_path.read_text())
            current_sha=file_sha(path)
            if native_meta.get('sha256')!=current_sha:
                raise RuntimeError(f'Native source SHA does not match collector seal: {path}')
            one=read_candles(path)
            source_paths[str(path.resolve())]=current_sha
            signals,counts,detected=scan_symbol(one,sym,period,scanner,first_ts,last_ts,batch)
            swings+=detected;totals.update(counts)
            trades=replay_matrix(one,signals,period)
            failed.extend(signals);trade_rows.extend(trades)
            print('IFR_SYMBOL',json.dumps(dict(period=period,symbol=sym,swings=detected,
                  first_touch=dict(counts),failed=len(signals)),ensure_ascii=False),flush=True)
            del one
        signal_rows.extend(failed)
        reports[period]=dict(symbols=len(sources),pivots_detected=swings,first_touches=dict(totals),
                             immediate_fails=len(failed),inverse_direction='opposite of original signal')
    expected=len(signal_rows)*2*len(TPS)*len(SLS)*len(HORIZONS)
    # Ensure every signal had a contiguous entry; incomplete data must NOT silently vanish.
    if len(trade_rows)!=expected:
        raise RuntimeError(f'Unexpected missing first 1m entries: {len(trade_rows)}/{expected}')
    grid=report_grid(trade_rows)
    side_grid=[]
    for original_side in ('LONG','SHORT'):
        subset=[x for x in trade_rows if x['original_side']==original_side]
        side_grid.extend({**x,'original_side':original_side} for x in report_grid(subset))
    portfolios=[portfolio(trade_rows,period,direction,config)
                for period in period_names for direction in ('INVERSE','ORIGINAL') for config in PORTFOLIO_CONFIGS]
    output=root/'reports/reaction_lab/legacy_immediate_fail_reversal_v19'
    write_csv(output/'events.csv',signal_rows,SIGNAL_COLS)
    write_csv(output/'grid.csv',grid,('period','direction','tp','sl','horizon','N','evaluable','tp_first','sl_first','timeout','censored','net_mean_pct','profit_factor'))
    write_csv(output/'side_grid.csv',side_grid,('period','direction','original_side','tp','sl','horizon','N','evaluable','tp_first','sl_first','timeout','censored','net_mean_pct','profit_factor'))
    write_csv(output/'trades.csv',trade_rows,TRADE_COLS)
    meta=dict(version=VERSION,study='EXPLORATORY_SEPTEMBER_OCTOBER_INSPECTED',
              canary_scanner_fingerprint=fp,app_sha256=file_sha(root/'dashboard/app.py'),
              scanner_sha256=file_sha(root/'reaction_lab_continuous_scanner.py'),
              data_sha256=source_paths,periods=reports,settings=dict(
                  original_failure='CANONICAL_15m_3x3_TOUCH_FAILED',swing_prominence=0,
                  departure_pct=0.2,tolerance_pct=0.1,max_age_min=360,
                  entry='OPEN next contiguous 1m after 1m FAILED candle closes',
                  inverse='LONG fail -> SHORT, SHORT fail -> LONG',tp=list(TPS),sl=list(SLS),
                  horizons=list(HORIZONS),fee_roundtrip_pct=FEE,slippage_bps_per_side=SLIPPAGE_BPS,
                  portfolio_configs=[list(c) for c in PORTFOLIO_CONFIGS],
                  portfolio_initial_usd=200,portfolio_allocation=0.8,portfolio_max_positions=1),
              portfolio=portfolios,total_trade_rows=len(trade_rows),
              limitations=['Previous dates inspected; in-sample exploratory, not OOS',
                           'Historical 1m reconstructions not original live timing; no LIVE_OBSERVED latency',
                           'No orderbook, partial fills or funding',
                           'Many correlated pivots/symbols; grid maxima cannot be treated as validated',
                           'Censor incomplete paths; TP/SL same minute => SL',
                           'October first UTC day used as 15m swing warmup, signals from Oct 9'],
              outputs_sha256={name:file_sha(output/name) for name in ('events.csv','grid.csv','side_grid.csv','trades.csv')})
    safe_write_json(output/'report.json',meta)
    print('IFR_REPORT',json.dumps(dict(periods=reports,grid_leaders_exploratory_only=[x for x in sorted((g for g in grid if g['direction']=='INVERSE'),key=lambda z:z['net_mean_pct'] if z['net_mean_pct'] is not None else -999,reverse=True)[:8]],portfolios=portfolios),ensure_ascii=False),flush=True)
    print('IFR_REPORT_PATH',output,flush=True)
    return meta


def verify(root):
    output=root/'reports/reaction_lab/legacy_immediate_fail_reversal_v19'
    m=json.loads((output/'report.json').read_text())
    if m['version']!=VERSION: raise RuntimeError('Version mismatch')
    if file_sha(root/'dashboard/app.py')!=m['app_sha256'] or file_sha(root/'reaction_lab_continuous_scanner.py')!=m['scanner_sha256']:
        raise RuntimeError('Canonical detector sources changed')
    for name,expected in m['data_sha256'].items():
        if file_sha(Path(name))!=expected:raise RuntimeError(f'Native input changed: {name}')
    for name,expected in m['outputs_sha256'].items():
        if file_sha(output/name)!=expected:raise RuntimeError(f'Output changed: {name}')
    events=pd.read_csv(output/'events.csv')
    if events.event_id.duplicated().any(): raise RuntimeError('Duplicate event id')
    trades=pd.read_csv(output/'trades.csv')
    if len(trades)!=m['total_trade_rows']: raise RuntimeError('Traded grid rows changed')
    print('IFR_VERIFY_SUCCESS',json.dumps(dict(events=len(events),grid_rows=len(trades),sources=len(m['data_sha256']))))


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    mode=ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true')
    mode.add_argument('--run',action='store_true')
    mode.add_argument('--verify',action='store_true')
    ap.add_argument('--period',choices=('september','october','both'),default='both')
    args=ap.parse_args()
    root=ROOT
    if args.plan:
        print('IFR_PLAN',json.dumps(dict(version=VERSION,periods={p:dict(native_path=str(root/PERIODS[p][0]),signals=f'{PERIODS[p][1]} -> {PERIODS[p][2]} exclusive UTC') for p in PERIODS},
            planned_symbols={p:len(native_sources(root,p)) for p in PERIODS},
            output=str(RESULTS),read_only_sources=True,never_opens_network_or_LIVE=True,
            canonical_source='dashboard/app.py AST via reaction_lab_continuous_scanner.py',
            tp=TPS,sl=SLS,horizons=HORIZONS,portfolio_configs=PORTFOLIO_CONFIGS),ensure_ascii=False,indent=2))
    elif args.run:
        run(root,tuple(PERIODS) if args.period=='both' else (args.period,))
    else:
        verify(root)

if __name__=='__main__':
    main()
