#!/usr/bin/env python3
"""HBL V1.6 exploratory: first failed retest of a formerly broken horizontal resistance -> SHORT.

Only cached, sealed Sep/Oct OHLCV and original frozen V1.2 first crosses are used.
No external data, new indicators, parameter search, or LIVE actions. Sep/Oct have
already been inspected; do not treat these results as out-of-sample validation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

import horizontal_level_retest_lab_v15 as v15
from horizontal_breakout_lab import normalize, TF, M

ROOT = Path(__file__).resolve().parent
BASE = ROOT / 'reports/horizontal_breakout_lab_v1/failed_retest_v16'
VERSION = 'hbl-failed-horizontal-retest-v1.6.0'
WINDOW_BARS = 6
POLICY = dict(initial_equity_usd=200.0, margin_fraction=0.8, leverage=1.0,
              tp_pct=2.0, sl_pct=1.5, max_minutes=360, roundtrip_fee_pct=0.1,
              slippage_bps_per_side=5.0, max_positions=1,
              portfolio_selection='FIRST_FAILED_RETEST_PER_LEVEL_SHORT',
              tie_break='SYMBOL_ASCENDING', both_same_1m='SL_FIRST')
RULE = dict(
    setup='FIRST_GENUINE_CLOSE_ABOVE_FROZEN_ZONE_THEN_FIRST_FAILED_CONTACT',
    bars_after_break=WINDOW_BARS,
    crossing='first V1.2 LEVEL_CROSS per level; source 30m close must exceed frozen zone_high',
    first_contact='first later 30m bar with low <= frozen zone_high',
    signal='on FIRST contact only, if 30m close < frozen zone_high',
    no_signal='if first contact closes at/above frozen zone_high; no later cherry-pick',
    direction='SHORT', entry='OPEN next contiguous 1m after signal 30m close',
    stop_pct=POLICY['sl_pct'], target_pct=POLICY['tp_pct'],
    max_horizon_min=POLICY['max_minutes'],
    trading_disabled=True, analysis_status='EXPLORATORY_NOT_OOS'
)
FIELDS = [
    'period','symbol','level_id','source_attempt_id','source_break_ts',
    'source_strength','zone_high','zone_low','signal_closed_ts',
    'entry_delay_min','first_contact_open_ts','contact_low','failure_close',
    'failure_depth_pct','location','attempt_id','entry_ts','exit_ts','entry_fill',
    'exit_fill','exit_reason','both_same_minute','net_pct','minutes_held',
    'entry_below_zone_top_pct'
]


def find_first_failure(row, bars, cutoff, window=WINDOW_BARS):
    """Reject, not keep searching, if first level contact does not close beneath zone top."""
    ts = bars.timestamp.to_numpy(dtype='int64')
    cross_closed = int(row['signal_closed_ts'])
    cross_open = cross_closed - TF
    ix = int(np.searchsorted(ts, cross_open))
    if ix >= len(ts) or int(ts[ix]) != cross_open:
        return None, 'CROSS_NOT_IN_CANDLES'
    top, bottom = float(row['zone_high']), float(row['zone_low'])
    if not (np.isfinite(top) and np.isfinite(bottom) and 0 < bottom <= top):
        raise ValueError('INVALID_FROZEN_ZONE')
    if not float(bars.close.iloc[ix]) > top:
        return None, 'CROSS_NOT_ABOVE_ZONE'
    for j in range(ix+1, min(ix+window+1, len(ts))):
        if int(ts[j]) != cross_closed + (j-ix-1)*TF:
            return None, 'GAP_30M'
        closed = int(ts[j]) + TF
        if closed >= int(cutoff):
            return None, 'CUTOFF'
        candle = bars.iloc[j]
        if float(candle.low) <= top:
            if float(candle.close) < top:
                return dict(signal_closed_ts=closed,
                            entry_delay_min=int((closed-cross_closed)//M),
                            first_contact_open_ts=int(candle.timestamp),
                            contact_low=float(candle.low),
                            failure_close=float(candle.close),
                            failure_depth_pct=100*(1-float(candle.close)/top),
                            location='BELOW_ZONE_LOW' if float(candle.close)<bottom else 'INSIDE_ZONE'), 'FAILED_RETEST'
            return None, 'FIRST_CONTACT_NOT_FAILED'
    return None, 'NO_CONTACT_WITHIN_WINDOW'


def short_trade(signal, minute, policy=POLICY):
    """USDT-linear short on 1m contiguous OHLC; adverse slip both sides; SL first for both hits."""
    ts = minute.timestamp.to_numpy(dtype='int64')
    when = int(signal['signal_closed_ts'])
    ix = int(np.searchsorted(ts,when))
    n = int(policy['max_minutes'])
    if ix+n>len(ts) or ix>=len(ts) or not np.all(ts[ix:ix+n] == when+np.arange(n,dtype='int64')*M):
        return None
    slip = float(policy['slippage_bps_per_side'])/10000
    price = float(minute.open.iloc[ix])
    if price<=0 or not np.isfinite(price):
        return None
    entry = price*(1-slip)
    tp = entry*(1-float(policy['tp_pct'])/100)
    sl = entry*(1+float(policy['sl_pct'])/100)
    price_out=None; reason=None; ambiguous=False; end=None
    for k in range(ix,ix+n):
        row = minute.iloc[k]
        hit_tp = float(row.low)<=tp
        hit_sl = float(row.high)>=sl
        if hit_sl:
            price_out=sl; reason='SL_FIRST'; ambiguous=bool(hit_tp)
        elif hit_tp:
            price_out=tp; reason='TP_FIRST'
        if reason is not None:
            end=int(row.timestamp)+M
            break
    if reason is None:
        row=minute.iloc[ix+n-1]
        price_out=float(row.close)
        reason='TIMEOUT'
        end=int(row.timestamp)+M
    exit_fill=price_out*(1+slip)
    net=100*(entry-exit_fill)/entry-float(policy['roundtrip_fee_pct'])
    return dict(entry_ts=when, exit_ts=end, entry_fill=entry, exit_fill=exit_fill,
                exit_reason=reason, both_same_minute=ambiguous, net_pct=net,
                minutes_held=int((end-when)//M))


def desc(data):
    if data.empty:
        return dict(N=0,TP=0,SL=0,TIMEOUT=0,mean_net_pct=None,symbols=0,days=0)
    counts=data.exit_reason.value_counts()
    daily=pd.to_datetime(data.signal_closed_ts,unit='ms',utc=True).dt.date
    return dict(N=int(len(data)),TP=int(counts.get('TP_FIRST',0)),
                SL=int(counts.get('SL_FIRST',0)),TIMEOUT=int(counts.get('TIMEOUT',0)),
                mean_net_pct=round(float(data.net_pct.mean()),5),
                median_net_pct=round(float(data.net_pct.median()),5),
                symbols=int(data.symbol.nunique()),days=int(daily.nunique()),
                positive_symbols=int((data.groupby('symbol').net_pct.mean()>0).sum()),
                first_contact_below_zone_pct=round(float((data.location=='BELOW_ZONE_LOW').mean()*100),2))


def scan_period(period):
    prior, folders, (_, cutoff) = v15.read_engine_attempts(period)
    counted={}; out=[]
    for symbol, group in prior.groupby('symbol', sort=True):
        folder=folders[str(symbol)]
        bars=normalize(pd.read_csv(folder/f'{symbol}_30m.csv'),TF)
        minute=normalize(pd.read_csv(folder/f'{symbol}_1m.csv'),M)
        for _, row in group.iterrows():
            setup, status = find_first_failure(row,bars,cutoff)
            counted[status]=counted.get(status,0)+1
            if setup is None:continue
            event = dict(period=period,symbol=str(symbol),level_id=str(row.level_id),
                         source_attempt_id=str(row.attempt_id),source_break_ts=int(row.signal_closed_ts),
                         source_strength=str(row.strength_class),zone_high=float(row.zone_high),
                         zone_low=float(row.zone_low),**setup)
            event['attempt_id']=hashlib.sha256(f'failed-retest|{period}|{row.attempt_id}|{setup["signal_closed_ts"]}'.encode()).hexdigest()[:24]
            trade = short_trade(event,minute)
            if trade is None:
                counted['NO_FILL_OR_GAP_1M']=counted.get('NO_FILL_OR_GAP_1M',0)+1
                continue
            event.update(trade)
            event['entry_below_zone_top_pct']=100*(1-event['entry_fill']/event['zone_high'])
            out.append(event)
    return pd.DataFrame(out,columns=FIELDS),dict(source_first_attempts=int(len(prior)),
                                                   counters=counted, symbols=int(len(folders)))


def save_json(path, obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    p=path.with_suffix('.tmp')
    p.write_text(json.dumps(obj,indent=2,ensure_ascii=False,allow_nan=False)+'\n',encoding='utf8')
    p.replace(path)


def frozen():
    return v15.frozen_sources()


def plan():
    hashes=frozen()
    print('V16_PLAN',json.dumps(dict(version=VERSION,rule=RULE,policy=POLICY,
              source_hashes=hashes,source_periods=['september','october'],
              no_downloads=True,no_live_actions=True,
              caveat='Both periods already inspected; exploratory, NOT OOS.'),indent=2))


def run():
    hashes=frozen()
    manifest=dict(version=VERSION,rule=RULE,policy=POLICY,source_hashes=hashes,
                  source_periods=['september','october'])
    f=BASE/'manifest.json'
    if f.exists() and json.loads(f.read_text(encoding='utf8'))!=manifest:
        raise RuntimeError('V16_MANIFEST_CHANGED')
    if not f.exists():save_json(f,manifest)
    result=dict(version=VERSION,label='EXPLORATORY_NOT_OOS',periods={})
    for which in ('september','october'):
        events, funnel=scan_period(which)
        events.to_csv(BASE/f'{which}_failed_retests.csv',index=False)
        detail={ 'funnel':funnel,'independent':desc(events),
                 'by_first_breakout_strength':{name:desc(g) for name,g in events.groupby('source_strength')},
                 'by_failure_location':{name:desc(g) for name,g in events.groupby('location')} }
        result['periods'][which]=detail
        print('V16_PERIOD',which,json.dumps(detail,ensure_ascii=False),flush=True)
    save_json(BASE/'research_report.json',result)
    return result


def replay_short_portfolio(events, policy=POLICY):
    """Deterministic, one slot. Reuse sealed per-signal fills; all first contacts counted."""
    events=events.sort_values(['signal_closed_ts','symbol','attempt_id'])
    equity=float(policy['initial_equity_usd'])
    max_equity=equity;max_dd=0.0; active=None; busy=0
    ledger=[]; n=int(len(events))

    def realise(position, equity, max_equity, max_dd):
        risk_notional=equity*float(policy['margin_fraction'])*float(policy['leverage'])
        profit=risk_notional*float(position['net_pct'])/100
        after=equity+profit
        max_equity=max(max_equity,after)
        max_dd=max(max_dd,100*(1-after/max_equity))
        trade=dict(position)
        trade['pnl_usd']=profit;trade['equity_before']=equity;trade['equity_after']=after
        ledger.append(trade)
        return after,max_equity,max_dd

    for _, row in events.iterrows():
        t=int(row.signal_closed_ts)
        if active is not None and int(active['exit_ts'])<=t:
            equity,max_equity,max_dd=realise(active,equity,max_equity,max_dd)
            active=None
        if active is not None:
            busy+=1
            continue
        active=row.to_dict()
    if active is not None:
        equity,max_equity,max_dd=realise(active,equity,max_equity,max_dd)
    profit=sum(t['pnl_usd'] for t in ledger if t['pnl_usd']>0)
    losses=-sum(t['pnl_usd'] for t in ledger if t['pnl_usd']<0)
    pf=profit/losses if losses>0 else (None if profit==0 else float('inf'))
    if pf==float('inf'):pf=None
    counts=pd.Series([t['exit_reason'] for t in ledger]).value_counts()
    report=dict(signals=n,trades=len(ledger),skipped_slot_busy=busy,
                start_equity_usd=policy['initial_equity_usd'],
                end_equity_usd=round(equity,5),return_pct=round(100*(equity/policy['initial_equity_usd']-1),5),
                max_drawdown_realised_pct=round(max_dd,5),
                profit_factor_dollar=round(pf,5) if pf is not None else None,
                exit_counts={k:int(counts.get(k,0)) for k in ('TP_FIRST','SL_FIRST','TIMEOUT')},
                policy=policy,limitations=['single slot, symbol-ascending tie break','fixed OHLC fills and slippage only','no order-book fills, funding or liquidation','not an exchange execution backtest'])
    return report,ledger


def portfolio():
    frozen()
    rp=BASE/'research_report.json'
    if not rp.is_file():raise RuntimeError('RUN_FIRST')
    info=json.loads(rp.read_text(encoding='utf8'))
    if info.get('version')!=VERSION:raise RuntimeError('VERSION_MISMATCH')
    out={}
    for period in ('september','october'):
        events=pd.read_csv(BASE/f'{period}_failed_retests.csv')
        res, trades=replay_short_portfolio(events)
        pd.DataFrame(trades).to_csv(BASE/f'{period}_portfolio_trades.csv',index=False)
        out[period]=res
        print('V16_PORTFOLIO',period,json.dumps(res,ensure_ascii=False),flush=True)
    save_json(BASE/'portfolio_report.json',dict(version=VERSION,label='EXPLORATORY_NOT_OOS',periods=out))
    return out


def verify():
    frozen()
    rp=BASE/'research_report.json'
    if not rp.is_file():raise RuntimeError('RUN_FIRST')
    report=json.loads(rp.read_text(encoding='utf8'))
    if report.get('version')!=VERSION:raise RuntimeError('INVALID_VERSION')
    for period in ('september','october'):
        data=pd.read_csv(BASE/f'{period}_failed_retests.csv')
        if len(data)!=report['periods'][period]['independent']['N']:
            raise RuntimeError('EVENT_COUNT_CHANGED')
        if data.empty:continue
        if data.level_id.duplicated().any() or data.attempt_id.duplicated().any():
            raise RuntimeError('DUPLICATE_EVENT')
        if not (data.signal_closed_ts>data.source_break_ts).all():
            raise RuntimeError('NO_CAUSAL_ORDER')
        if not (data.entry_ts==data.signal_closed_ts).all():
            raise RuntimeError('ENTRY_TIMING_INVALID')
        if not (data.failure_close<data.zone_high).all():
            raise RuntimeError('INVALID_FAILURE_SIGNAL')
        if not data.exit_reason.isin(('TP_FIRST','SL_FIRST','TIMEOUT')).all():
            raise RuntimeError('UNEXPECTED_EXIT_STATUS')
    print('V16_VERIFY_SUCCESS: sealed source data; first failure causality and events intact; EXPLORATORY_NOT_OOS')


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    group=p.add_mutually_exclusive_group(required=True)
    for x in ('plan','run','portfolio','verify'):
        group.add_argument('--'+x,action='store_true')
    args=p.parse_args(argv)
    if args.plan:plan()
    elif args.run:run()
    elif args.portfolio:portfolio()
    elif args.verify:verify()
    return 0

if __name__=='__main__':raise SystemExit(main())
