#!/usr/bin/env python3
"""HBL V1.5: research only, break -> defended first retest of a frozen horizontal resistance.

No new indicators, downloads, LIVE writes, parameters sweeps, or modifications of the frozen
V1.2/V1.3/V1.4 engines. September and October outcomes are exploratory because this idea was
suggested after both windows were inspected. A future untouched interval is still necessary.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
import pandas as pd

import horizontal_breakout_frozen_validation_v13 as oct13
import horizontal_breakout_temporal_portfolio_v14 as sep14
import horizontal_breakout_lifecycle_v12 as eng
from horizontal_breakout_lab import normalize, TF, M

ROOT = Path(__file__).resolve().parent
BASE = ROOT / 'reports/horizontal_breakout_lab_v1/level_retest_v15'
VERSION = 'hbl-price-level-retest-exploratory-v1.5.0'
RETEST_BARS = 6  # six 30m bars (3 hours); one registered hypothesis, no grid search
TP_PCT = 2.0
SL_PCT = 1.5
HORIZON = 360
FEE_PCT = 0.1
SLIP_BPS = 5.0
FROZEN_RULE = {
    'setup': 'FIRST_LEVEL_CROSS_CLOSE_ABOVE_ZONE_THEN_FIRST_DEFENDED_RETEST',
    'retest_window_30m_bars': RETEST_BARS,
    'touch': 'later 30m low <= frozen zone_high',
    'defence': 'same candle close > frozen zone_high AND close > open',
    'invalidate': 'before confirmation, any 30m close < frozen zone_low',
    'entry': 'OPEN of next contiguous 1m candle after confirmation 30m close',
    'one_signal_per_level': True,
    'tp_pct': TP_PCT, 'sl_pct': SL_PCT, 'horizon_minutes': HORIZON,
    'roundtrip_fee_pct': FEE_PCT, 'slippage_bps_per_side': SLIP_BPS,
    'portfolio_initial_usd': 200.0, 'margin_fraction': 0.8,
    'leverage': 1.0, 'max_positions': 1,
    'analysis_status': 'EXPLORATORY_NOT_OOS',
}
PERIODS = ('september','october')
EVENT_COLUMNS = [
    'period','symbol','level_id','source_attempt_id','source_break_ts',
    'source_strength','source_level','zone_high','zone_low','signal_closed_ts',
    'entry_delay_min','defence_close','retest_low','retest_open_ts',
    'entry_vs_zone_top_close_pct','first_on_level','group','attempt_id',
    'entry_ts','exit_ts','entry_fill','exit_fill','exit_reason','both_same_minute',
    'net_pct','minutes_held','entry_vs_zone_high_pct',
]


def store_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n', encoding='utf8')
    tmp.replace(path)


def frozen_sources():
    oct13.locked()
    sep14.locked()
    oct13.verified_amendment()
    oct13.seal_data()
    sep14.inspect_seal()
    return {x:sep14.sha(ROOT/x) for x in sep14.PINS}


def configure_period(which):
    if which == 'september':
        obj = sep14
        n = len(obj.batches())
        return obj, n, obj.SIGNAL_START, obj.SIGNAL_END
    if which == 'october':
        obj = oct13
        n = len(obj.batches())
        return obj, n, '2026-10-08', oct13.END
    raise ValueError(which)


def read_engine_attempts(which):
    obj, batch_count, begin, end = configure_period(which)
    attempts = []
    symbols = {}
    for index in range(1, batch_count + 1):
        out = obj.engine_output(index) if which == 'september' else obj.batch_run_dir(index)
        audit_path = out/'audit.json'
        attempts_path = out/'attempts.csv'
        if not audit_path.is_file() or not attempts_path.is_file():
            raise RuntimeError(f'Missing completed V1.2 output: {out}')
        audit = json.loads(audit_path.read_text(encoding='utf8'))
        syms = obj.batches()[index-1] if which == 'september' else obj.eligible_batch(index)
        if not audit['success'] or audit['completed_symbols'] != len(syms) or audit['expected_symbols'] != len(syms):
            raise RuntimeError(f'Incomplete V1.2 audit {which} batch {index}: {audit}')
        att = pd.read_csv(attempts_path)
        if len(att) != audit['attempts'] or not set(att.symbol).issubset(syms):
            raise RuntimeError('V1.2 attempt count or symbol mismatch: '+str(out))
        if not att.empty:
            attempts.append(att)
        for sym in syms:
            if sym in symbols:
                raise RuntimeError(f'Duplicate symbol {sym} in {which}')
            symbols[sym] = obj.batch_dir(index)
    if not attempts:
        raise RuntimeError(f'No attempts {which}')
    a = pd.concat(attempts, ignore_index=True)
    if a.attempt_id.duplicated().any():
        raise RuntimeError('Duplicate attempt_id')
    # Record first cross over *full* engine history, including September's warm-up.
    a = a.sort_values(['level_id','signal_closed_ts','attempt_id'])
    a['first_on_level'] = ~a.duplicated('level_id')
    a = a[a.first_on_level & (a.trigger_kind == 'LEVEL_CROSS')].copy()
    a = a[a.strength_class.isin(['BREAKOUT_ATTEMPT','STRONG_BREAKOUT'])]
    begin_ts = pd.Timestamp(begin, tz='UTC').value // 10**6
    end_ts = pd.Timestamp(end, tz='UTC').value // 10**6
    a = a[(a.signal_closed_ts >= begin_ts) & (a.signal_closed_ts < end_ts)].copy()
    return a, symbols, (int(begin_ts), int(end_ts))


def find_defended_retest(attempt, bars, cutoff, window=RETEST_BARS):
    """Uses only 30m closes through the confirmation candle; never posterior POST labels."""
    ts = bars.timestamp.to_numpy(dtype='int64')
    cross_closed = int(attempt['signal_closed_ts'])
    crossing_open = cross_closed - TF
    ix = int(np.searchsorted(ts, crossing_open))
    if ix >= len(ts) or int(ts[ix]) != crossing_open:
        return None, 'CROSS_NOT_IN_CANDLES'
    zone_high, zone_low = float(attempt['zone_high']), float(attempt['zone_low'])
    if float(bars.close.iloc[ix]) <= zone_high:
        return None, 'CROSS_NOT_ABOVE_ZONE'
    for j in range(ix+1, min(ix+window+1, len(ts))):
        if ts[j] != cross_closed + (j-ix-1)*TF:
            return None, 'GAP_30M'
        closed = int(ts[j]) + TF
        if closed >= cutoff:
            return None, 'CUTOFF'
        candle = bars.iloc[j]
        if float(candle.close) < zone_low:
            return None, 'INVALIDATED'
        if float(candle.low) <= zone_high:
            if float(candle.close) > zone_high and float(candle.close) > float(candle.open):
                return {'signal_closed_ts':closed, 'entry_delay_min':int((closed-cross_closed)//M),
                        'defence_close':float(candle.close), 'retest_low':float(candle.low),
                        'retest_open_ts':int(candle.timestamp),
                        'entry_vs_zone_top_close_pct':100*(float(candle.close)/zone_high-1)}, 'CONFIRMED'
            # First touch failed to defend in same candle: do not cherry-pick later touches.
            return None, 'TOUCH_NO_DEFENCE'
    return None, 'NO_RETEST_WITHIN_WINDOW'


def analyze_period(which):
    original, symbol_paths, (_, cutoff) = read_engine_attempts(which)
    results=[]; counters={}; attempted=0
    for sym, group in original.groupby('symbol',sort=True):
        folder=symbol_paths[str(sym)]
        bars=normalize(pd.read_csv(folder/f'{sym}_30m.csv'), TF)
        minute=normalize(pd.read_csv(folder/f'{sym}_1m.csv'), M)
        sep14.validate_candles_minute(minute)
        for _,row in group.iterrows():
            attempted+=1
            trigger, why=find_defended_retest(row,bars,cutoff)
            counters[why]=counters.get(why,0)+1
            if trigger is None:
                continue
            info={
                'period': which, 'symbol': str(sym), 'level_id': str(row.level_id),
                'source_attempt_id': str(row.attempt_id),
                'source_break_ts': int(row.signal_closed_ts),
                'source_strength': str(row.strength_class),
                'source_level': float(row.level), 'zone_high':float(row.zone_high),
                'zone_low':float(row.zone_low), **trigger,
                # Compatibility with the unchanged V1.4 execution simulator.
                'first_on_level': True,'group':'DEFENDED_RETEST',
                'attempt_id': hashlib.sha256(f'{which}|{row.attempt_id}|{trigger["signal_closed_ts"]}'.encode()).hexdigest()[:22],
            }
            policy = portfolio_policy()
            trade=sep14.simulated_long(pd.Series(info),minute,policy)
            if trade is None:
                counters['NO_FILL_OR_GAPPED_1M']=counters.get('NO_FILL_OR_GAPPED_1M',0)+1
                continue
            info.update(trade)
            info['entry_vs_zone_high_pct'] = 100*(trade['entry_fill']/float(row.zone_high)-1)
            results.append(info)
    data=pd.DataFrame(results, columns=EVENT_COLUMNS)
    return data, {'original_first_crosses':attempted,'signal_count':len(data),
                  'outcomes':counters, 'symbols':len(symbol_paths)}


def portfolio_policy():
    return {**sep14.PORTFOLIO, 'portfolio_selection':'FIRST_DEFENDED_RETEST_PER_LEVEL',
            'tp_pct':TP_PCT, 'sl_pct':SL_PCT, 'max_minutes':HORIZON,
            'roundtrip_fee_pct':FEE_PCT, 'slippage_bps_per_side':SLIP_BPS,
            'margin_fraction':.8,'leverage':1.0,'max_positions':1,
            'initial_equity_usd':200.0}


def describe(frame):
    n=len(frame)
    if not n:return {'N':0, 'TP':0,'SL':0,'TIMEOUT':0,'mean_net_pct':None,'positive_symbols':0,'symbol_count':0}
    exit_counts=frame.exit_reason.value_counts().to_dict()
    by_symbol=frame.groupby('symbol').net_pct.mean()
    return {'N':int(n),'TP':int(exit_counts.get('TP_FIRST',0)),
            'SL':int(exit_counts.get('SL_FIRST',0)),
            'TIMEOUT':int(exit_counts.get('TIMEOUT',0)),
            'mean_net_pct':round(float(frame.net_pct.mean()),5),
            'median_net_pct':round(float(frame.net_pct.median()),5),
            'positive_symbols':int((by_symbol>0).sum()),
            'symbol_count':int(by_symbol.size),
            'entry_above_zone_median_pct':round(float(frame.entry_vs_zone_high_pct.median()),4),
            'median_entry_delay_min':float(frame.entry_delay_min.median()),
            'date_count':int(pd.to_datetime(frame.signal_closed_ts,unit='ms',utc=True).dt.date.nunique())}


def get_minute_provider(which):
    obj, n, *_ = configure_period(which)
    paths={}
    for index in range(1,n+1):
        syms=obj.batches()[index-1] if which=='september' else obj.eligible_batch(index)
        for sym in syms: paths[sym]=obj.batch_dir(index)/f'{sym}_1m.csv'
    cache={}
    def get(symbol):
        if symbol not in cache:
            cache[symbol]=normalize(pd.read_csv(paths[symbol]),M)
        return cache[symbol]
    return get


def plan():
    hashes=frozen_sources()
    print('HBL_V15_PLAN',json.dumps({'version':VERSION, 'rule':FROZEN_RULE,
         'periods': {'september':'2026-09-10 to 2026-09-22 UTC','october':'2026-10-08 to 2026-10-20 UTC'},
         'sha256_sources':hashes, 'no_downloads':True,'no_live_actions':True,
         'warning':'Both windows already examined; exploratory experiment ONLY'},indent=2))


def run():
    frozen_sources()
    current={'version':VERSION,'rule':FROZEN_RULE,'periods':list(PERIODS),
             'source_hashes':{k:sep14.sha(ROOT/k) for k in sep14.PINS}}
    f=BASE/'research_manifest.json'
    if f.exists():
        if json.loads(f.read_text(encoding='utf8'))!=current:
            raise RuntimeError('V15_MANIFEST_CHANGED')
    else:store_json(f,current)
    report={'version':VERSION,'label':'EXPLORATORY_NOT_TEMPORAL_OOS','periods':{},'policy':portfolio_policy(),
            'caveat':'Signals are chosen after inspecting September and October. Only a subsequent untouched window tests viability.'}
    BASE.mkdir(parents=True,exist_ok=True)
    for which in PERIODS:
        data,diag=analyze_period(which)
        data.to_csv(BASE/f'{which}_retests.csv',index=False)
        report['periods'][which]={'funnel':diag,'independent':describe(data),'by_original_strength':{name:describe(group) for name,group in data.groupby('source_strength')} }
        print('V15_PERIOD',which,json.dumps(report['periods'][which],ensure_ascii=False),flush=True)
    store_json(BASE/'research_report.json',report)
    return report


def portfolio():
    frozen_sources()
    rp=BASE/'research_report.json'
    if not rp.is_file():raise RuntimeError('Execute --run first')
    r=json.loads(rp.read_text(encoding='utf8'))
    if r['version'] != VERSION or r.get('label') != 'EXPLORATORY_NOT_TEMPORAL_OOS':
        raise RuntimeError('Unexpected report schema')
    results={}
    for which in PERIODS:
        df=pd.read_csv(BASE/f'{which}_retests.csv')
        if df.empty:
            results[which]={'trades':0,'signals':0,'reason':'NO_RETEST_SIGNALS'}
            continue
        # V1.4 simulator internally selects group == DIRECT_STRONG. This is ONLY
        # an internal adapter; V1.5 outputs retain the truthful DEFENDED_RETEST label.
        compatible = df.copy()
        compatible['group'] = 'DIRECT_STRONG'
        report, trades=sep14.replay_portfolio(compatible,get_minute_provider(which),portfolio_policy())
        pd.DataFrame(trades).to_csv(BASE/f'{which}_portfolio_trades.csv',index=False)
        results[which]=report
        print('V15_PORTFOLIO',which,json.dumps(report,ensure_ascii=False),flush=True)
    store_json(BASE/'portfolio_report.json',{'version':VERSION,'policy':portfolio_policy(),'periods':results})
    return results


def verify():
    frozen_sources()
    rp=BASE/'research_report.json'
    if not rp.exists():raise RuntimeError('Execute --run first')
    report=json.loads(rp.read_text(encoding='utf8'))
    for which in PERIODS:
        p=BASE/f'{which}_retests.csv'
        if not p.exists(): raise RuntimeError('Missing '+str(p))
        df=pd.read_csv(p)
        if len(df)!=report['periods'][which]['independent']['N']:
            raise RuntimeError(f'MISMATCH_COUNT {which}')
        if not df.empty:
            if df.level_id.duplicated().any():raise RuntimeError('DUPLICATED_LEVEL')
            if not (df.signal_closed_ts>df.source_break_ts).all():raise RuntimeError('LOOKAHEAD_SIGNAL')
            if not (df.entry_ts==df.signal_closed_ts).all():raise RuntimeError('WRONG_ENTRY')
            if not df.exit_reason.isin(['TP_FIRST','SL_FIRST','TIMEOUT']).all():raise RuntimeError('UNEXPECTED_EXIT')
    print('V15_VERIFY_SUCCESS: sealed sources, causal sequence and counts intact; NOT OOS')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    modes=parser.add_mutually_exclusive_group(required=True)
    for mode in ('plan','run','portfolio','verify'):
        modes.add_argument('--'+mode,action='store_true')
    a=parser.parse_args(argv)
    if a.plan:plan()
    elif a.run:run()
    elif a.portfolio:portfolio()
    elif a.verify:verify()
    return 0


if __name__=='__main__':
    raise SystemExit(main())
