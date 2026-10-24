#!/usr/bin/env python3
"""HBL V1.4: date-held-out research + independent, single-slot portfolio.

Research only. Reuses *unchanged*, SHA256-pinned V1.2 detector/collector;
never touches LIVE, strategies, Redis, exchange keys or source SQLite.
The historical Sep 2026 window is distinct by dates from Oct 2026 discovery,
NOT a prospective forward test. No retuning based on results.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
BASE = ROOT / 'reports/horizontal_breakout_lab_v1/temporal_v14/holdout_20260910_20260922'
ENGINE = ROOT / 'horizontal_breakout_lifecycle_v12.py'
CORE = ROOT / 'horizontal_breakout_lab.py'
COLLECTOR = ROOT / 'horizontal_breakout_native_candles_pilot_v1.py'
# Copied from registered V1.3 source hashes; deliberately not learned from future files.
PINS = {
    'horizontal_breakout_lifecycle_v12.py':'cdea32676d0d0bc08ed060ba25d08b7e7b52df115cca9892948ff6464155d9c0',
    'horizontal_breakout_lab.py':'9b772a362cc55c61c3e552ca313e910a36228cbdda1b5ee0dde7661684fb350f',
    'horizontal_breakout_native_candles_pilot_v1.py':'583d8896254c7c41d191e2fc8babf0dc1021e3df53d9e38f8664ce613bb07f7a',
}
VERSION = 'horizontal-breakout-temporal-portfolio-v1.4.0'
# Warm-up is NEVER included in performance outcomes; original frozen signal filters unchanged.
WARMUP_START = '2026-09-05'
SIGNAL_START = '2026-09-10'
SIGNAL_END = '2026-09-22'
TAIL_HOURS = 48
BATCH_SIZE = 5
COHORT = (
    'BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT','XRPUSDT',
    'DOGEUSDT','ADAUSDT','LINKUSDT','AVAXUSDT','DOTUSDT',
    'LTCUSDT','BCHUSDT','ETCUSDT','UNIUSDT','AAVEUSDT',
    'ATOMUSDT','NEARUSDT','FILUSDT','TRXUSDT','RUNEUSDT',
    'SUIUSDT','INJUSDT','OPUSDT','ARBUSDT','SEIUSDT',
    'TIAUSDT','WIFUSDT','1000SHIBUSDT','GALAUSDT',
    'HBARUSDT','ICPUSDT','XLMUSDT','VETUSDT','LDOUSDT',
    'CRVUSDT','COMPUSDT','ZECUSDT','ENSUSDT','APTUSDT',
)
FROZEN_SETTINGS = {
    'windows':[12,24,36,48],'min_touches':2,'tolerance_atr':0.5,
    'margin_pct':0.1,'max_level_bars':96,'rearm_bars':24,
    'invalidation_closes':2,'acceptance_closes':2,'min_volume_ratio':2.0,
    'min_body_atr':0.6,'min_close_position':0.75,'high_lookback':12,
    'post_bars':8,'max_horizon_min':2880,'stop_pct':1.5,'fee_roundtrip_pct':0.1,
}
PRIMARY = {'scope':'FIRST_ATTEMPT_PER_LEVEL','signal':'STRONG_BREAKOUT+LEVEL_CROSS',
           'control':'BREAKOUT_ATTEMPT+LEVEL_CROSS','tp_pct':2.0,'sl_pct':1.5,
           'horizon_minutes':360,'roundtrip_fee_pct':0.1}
# Portfolio parameters registered BEFORE held-out outcomes. No leverage, 80% equity allocation.
PORTFOLIO = {'initial_equity_usd':200.0,'margin_fraction':0.80,'leverage':1.0,
             'tp_pct':2.0,'sl_pct':1.5,'max_minutes':360,'roundtrip_fee_pct':0.1,
             'slippage_bps_per_side':5.0,'max_positions':1,
             'simultaneous_tie_break':'symbol_ascending',
             'same_minute_tp_sl':'CONSERVATIVE_SL_FIRST',
             'timeout':'last_complete_1m_close',
             'portfolio_selection':'FIRST_ATTEMPT_PER_LEVEL, DIRECT_STRONG only'}
JOIN_KEY = ['attempt_id','level_id','symbol','signal_closed_ts']
ONE = 60_000


def sha(path:Path)->str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1<<20),b''):h.update(b)
    return h.hexdigest()


def save_json(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    try:
        tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2,sort_keys=True,allow_nan=False)+'\n',encoding='utf-8')
        tmp.replace(path)
    finally:
        if tmp.exists():tmp.unlink()


def checked_sources():
    out={}
    for name,expected in PINS.items():
        p=ROOT/name
        if not p.is_file():raise RuntimeError('MISSING_PINNED_SOURCE: '+name)
        out[name]=sha(p)
        if out[name]!=expected:
            raise RuntimeError(f'PIN_MISMATCH: {name}; expected {expected}, found {out[name]}. Do not retune or overwrite the detector.')
    return out


def batches():return [COHORT[i:i+BATCH_SIZE] for i in range(0,len(COHORT),BATCH_SIZE)]

def batch_dir(index):return BASE/'native'/f'batch_{index:02d}'

def run_tag(index):return f'temporal_v14_b{index:02d}'


def manifest():
    return {'version':VERSION,'study_type':'HISTORICAL_DATE_HOLDOUT_EARLIER_THAN_DISCOVERY',
            'prospective':False,'discovery_window_utc':['2026-10-08','2026-10-20'],
            'warmup_start_utc':WARMUP_START,'signal_start_utc':SIGNAL_START,
            'signal_end_utc_exclusive':SIGNAL_END,'tail_hours':TAIL_HOURS,
            'symbols':list(COHORT),'batches':[list(x) for x in batches()],
            'pinned_source_sha256':checked_sources(),
            'frozen_engine_settings':FROZEN_SETTINGS,'primary':PRIMARY,'portfolio':PORTFOLIO,
            'caveats':['Historical separation in dates, not prospective forward validation',
                       'Symbols previously examined during October V1.3',
                       'Cross-symbol and day clustering; no change to signal filters']}


def freeze():
    if len(COHORT)!=39 or len(set(COHORT))!=39 or 'PEPEUSDT' in COHORT or '1000PEPEUSDT' in COHORT:
        raise RuntimeError('COHORT_NOT_FROZEN')
    path=BASE/'freeze_manifest.json'
    proposed=manifest()
    if path.exists():
        existing=json.loads(path.read_text(encoding='utf-8'))
        if existing!=proposed:raise RuntimeError('FROZEN_MANIFEST_CHANGED')
        print('FREEZE_ALREADY_EXISTS',path)
    else:
        save_json(path,proposed)
        print('V14_FROZEN',path,'39 symbols, 12 signal days, 5 warmup days, 48h tail')


def locked():
    path=BASE/'freeze_manifest.json'
    if not path.exists():raise RuntimeError('Run --freeze first')
    if json.loads(path.read_text(encoding='utf-8'))!=manifest():
        raise RuntimeError('FROZEN_MANIFEST_OR_CODE_CHANGED')


def call(target,*args):
    cmd=[sys.executable,str(target),*map(str,args)]
    print('EXEC',' '.join(cmd),flush=True)
    subprocess.run(cmd,cwd=ROOT,check=True)


def collector_args(index):
    return ['--start',WARMUP_START,'--end',SIGNAL_END,'--tail-hours',str(TAIL_HOURS),
            '--symbols',','.join(batches()[index-1]),'--output',str(batch_dir(index))]


def inspect_seal(create=False):
    import horizontal_breakout_native_candles_pilot_v1 as collector
    hashes={}
    st=collector.parse_date(WARMUP_START)
    en=collector.parse_date(SIGNAL_END)
    for i,syms in enumerate(batches(),1):
        folder=batch_dir(i)
        cfg=folder/'dataset_config.json'
        if not cfg.exists():raise RuntimeError(f'MISSING_DATASET_CONFIG_BATCH_{i}')
        expected={'version':collector.VERSION,'symbols':list(syms),'start':WARMUP_START,
                  'end':SIGNAL_END,'tail_hours':TAIL_HOURS,
                  'source':'Binance USD-M Futures public klines','no_live_writes':True}
        if json.loads(cfg.read_text(encoding='utf-8'))!=expected:
            raise RuntimeError('DATASET_CONFIG_MISMATCH:'+str(folder))
        for sym in syms:
            for tf in ('30m','1m'):
                f=folder/f'{sym}_{tf}.csv'
                meta=f.with_suffix('.meta.json')
                if not f.exists() or not meta.exists():raise RuntimeError('MISSING_OHLCV:'+str(f))
                row=json.loads(meta.read_text(encoding='utf-8'))
                end=en+TAIL_HOURS*3600000 if tf=='1m' else en
                interval=collector.ONE if tf=='1m' else collector.THIRTY
                if row.get('symbol')!=sym or row.get('timeframe')!=tf or row.get('sha256')!=sha(f):
                    raise RuntimeError('OHLCV_HASH_OR_META_MISMATCH:'+str(f))
                collector.validate_csv(f,st,end,interval)
                hashes[str(f.relative_to(BASE))]=sha(f)
    seal=BASE/'dataset_seal.json'
    if seal.exists():
        if json.loads(seal.read_text(encoding='utf-8'))!=hashes:
            raise RuntimeError('DATASET_CHANGED_AFTER_SEAL')
    elif create:save_json(seal,hashes)
    else:raise RuntimeError('Run --audit-data before computing outcomes')
    print('V14_DATASET_VERIFIED',len(hashes),'CSV files',flush=True)
    return hashes


def engine_output(index):
    from argparse import Namespace
    import horizontal_breakout_lifecycle_v12 as eng
    a=Namespace(root=ROOT,candles_dir=batch_dir(index),max_symbols=0,
                symbols=','.join(batches()[index-1]),run_tag=run_tag(index))
    out,rec=eng.fingerprint(a,eng.LifecycleConfig())
    if json.loads(json.dumps(rec['settings']))!=FROZEN_SETTINGS:
        raise RuntimeError('ENGINE_DEFAULT_SETTINGS_CHANGED')
    return out


def aggregate():
    import horizontal_breakout_lifecycle_v12 as eng
    raw=[]
    symset=set()
    for i,syms in enumerate(batches(),1):
        out=engine_output(i)
        report,levels,att,oc,tests=eng.run_audit(out)
        if not report['success'] or report['expected_symbols']!=len(syms) or report['completed_symbols']!=len(syms):
            raise RuntimeError(f'ENGINE_AUDIT_FAILED batch {i}: {report}')
        snap=json.loads((out/'source_snapshot.json').read_text(encoding='utf-8'))
        if len(snap)!=len(syms)*2:raise RuntimeError('BAD_INPUT_SNAPSHOT')
        if len(att)!=len(oc):raise RuntimeError('ATTEMPT_OUTCOME_MISMATCH')
        if len(att):
            if att.duplicated(JOIN_KEY).any() or oc.duplicated(JOIN_KEY).any():
                raise RuntimeError('DUPLICATE_ATTEMPT_KEYS')
            d=att.merge(oc,on=JOIN_KEY,how='outer',indicator=True,validate='one_to_one')
            if len(d)!=len(att) or (d['_merge']!='both').any():raise RuntimeError('JOIN_MISMATCH')
            raw.append(d.drop(columns=['_merge']))
            symset.update(att.symbol.unique())
    if not raw:raise RuntimeError('NO_SIGNALS_FOR_COMPARISON')
    d=pd.concat(raw,ignore_index=True)
    if d.duplicated('attempt_id').any():raise RuntimeError('DUPLICATE_GLOBAL_ID')
    if not set(d.symbol).issubset(COHORT):raise RuntimeError('WRONG_SYMBOLS')
    # Full warmup history determines first-attempt membership BEFORE signal date filtering.
    return select_temporal_events(d)


def select_temporal_events(d, start_date=SIGNAL_START, end_date=SIGNAL_END):
    """Freeze first attempts against ALL history, then restrict to held-out dates."""
    d=d.sort_values(['level_id','signal_closed_ts','attempt_id']).copy()
    d['first_on_level']=~d.duplicated('level_id')
    start=pd.Timestamp(start_date,tz='UTC').value//10**6
    end=pd.Timestamp(end_date,tz='UTC').value//10**6
    d=d[(d.signal_closed_ts>=start)&(d.signal_closed_ts<end)].copy()
    d['group']=np.select([
        (d.strength_class=='STRONG_BREAKOUT')&(d.trigger_kind=='LEVEL_CROSS'),
        (d.strength_class=='STRONG_BREAKOUT')&(d.trigger_kind=='EXPANSION_CONTINUATION'),
        (d.strength_class=='BREAKOUT_ATTEMPT')&(d.trigger_kind=='LEVEL_CROSS')],
        ['DIRECT_STRONG','CONTINUATION_STRONG','WEAK_CROSS'],default='OTHER')
    d['signal_day_utc']=pd.to_datetime(d.signal_closed_ts,unit='ms',utc=True).dt.strftime('%Y-%m-%d')
    return d


def wilson(success,total):
    if not total:return [None,None]
    z=1.959963984540054
    p=success/total
    base=1+z*z/total
    centre=(p+z*z/(2*total))/base
    half=z*math.sqrt((p*(1-p)/total)+(z*z/(4*total**2)))/base
    return [round(100*(centre-half),2),round(100*(centre+half),2)]


def summarize(frame,name):
    n=len(frame)
    counts=frame.tp2_h360.value_counts().to_dict() if n else {}
    hits={x:int(counts.get(x,0)) for x in ['TP_FIRST','SL_FIRST','NO_HIT','CENSORED','BOTH_SAME_1M']}
    net=pd.to_numeric(frame.net_tp2_h360_pct,errors='coerce') if n else pd.Series(dtype=float)
    return {'group':name,'N':int(n),'symbols_with_signal':int(frame.symbol.nunique()) if n else 0,
            'unique_levels':int(frame.level_id.nunique()) if n else 0,
            **hits,'tp_rate_pct':round(100*hits['TP_FIRST']/n,2) if n else None,
            'wilson95_tp_rate_pct':wilson(hits['TP_FIRST'],n),
            'mean_net_theoretical_pct':round(float(net.mean()),5) if net.notna().any() else None,
            'return_count':int(net.notna().sum()),
            'positive_symbols':int((frame.groupby('symbol').net_tp2_h360_pct.mean()>0).sum()) if n else 0}


def cluster_interval(first, unit='symbol', repetitions=2000, seed=20261024):
    """Exploratory cluster bootstrap: resample entire symbols or entire UTC days.

    Symbols share market shocks; the day bootstrap is a second sensitivity check.
    Empty strong/control draws are excluded, reported explicitly.
    """
    if first.empty:return {'unit':unit,'valid_draws':0,'total_draws':repetitions,'interval_net_diff_pct':[None,None],
                            'interval_tp_rate_diff_pp':[None,None]}
    if unit not in ('symbol','signal_day_utc'):raise ValueError('Invalid bootstrap grouping')
    grouped={str(k):g for k,g in first.groupby(unit)}
    keys=sorted(grouped)
    if len(keys)<2:return {'unit':unit,'valid_draws':0,'total_draws':repetitions,'interval_net_diff_pct':[None,None],
                            'interval_tp_rate_diff_pp':[None,None]}
    rng=np.random.default_rng(seed if unit=='symbol' else seed+1)
    deltas_net=[];deltas_hit=[]
    for _ in range(repetitions):
        pick=rng.choice(len(keys),size=len(keys),replace=True)
        sample=pd.concat([grouped[keys[int(i)]] for i in pick],ignore_index=True)
        a=sample[sample.group=='DIRECT_STRONG']
        b=sample[sample.group=='WEAK_CROSS']
        if a.empty or b.empty:continue
        av=pd.to_numeric(a.net_tp2_h360_pct,errors='coerce').dropna()
        bv=pd.to_numeric(b.net_tp2_h360_pct,errors='coerce').dropna()
        if av.empty or bv.empty:continue
        deltas_net.append(float(av.mean()-bv.mean()))
        deltas_hit.append(100*float((a.tp2_h360=='TP_FIRST').mean()-(b.tp2_h360=='TP_FIRST').mean()))
    def q(x):return [round(float(t),5) for t in np.quantile(x,[0.025,0.975])] if x else [None,None]
    return {'unit':unit,'clusters':len(keys),'valid_draws':len(deltas_net),'total_draws':repetitions,
            'interval_net_diff_pct':q(deltas_net),'interval_tp_rate_diff_pp':q(deltas_hit)}


def audit_signals():
    inspect_seal()
    d=aggregate()
    first=d[d.first_on_level]
    primary={name:summarize(first[first.group==name],name) for name in ('DIRECT_STRONG','WEAK_CROSS')}
    secondary={name:summarize(d[d.group==name],name) for name in ('DIRECT_STRONG','CONTINUATION_STRONG','WEAK_CROSS','OTHER')}
    intervals={unit:cluster_interval(first,unit=unit) for unit in ('symbol','signal_day_utc')}
    results={'version':VERSION,'success':True,'temporal_window_utc':[SIGNAL_START,SIGNAL_END],
             'study_type':'EARLIER_HISTORICAL_DATE_HOLDOUT_NOT_PROSPECTIVE',
             'eligible_symbols':len(COHORT),'attempts_in_signal_window':len(d),
             'first_attempts':len(first),'primary_first_attempt_per_level':primary,
             'secondary_all_attempts':secondary,'exploratory_cluster_intervals':intervals,'limitations':[
                 'Earlier dates than discovery; not prospective and may have shared macro effects',
                 'No retuning allowed on this cohort','Per-signal outcomes allow simultaneous trades; portfolio separate']}
    save_json(BASE/'temporal_report.json',results)
    d.to_csv(BASE/'temporal_attempts_with_outcomes.csv',index=False)
    print('HBL_V14_TEMPORAL',json.dumps(results,indent=2,ensure_ascii=False),flush=True)
    return results


def validate_candles_minute(frame):
    if not {'timestamp','open','high','low','close'}.issubset(frame.columns):
        raise RuntimeError('MINUTE_SCHEMA_MISSING')
    a=frame.timestamp.to_numpy(dtype='int64')
    if len(a)>1 and np.any(np.diff(a)!=ONE):raise RuntimeError('MINUTE_CANDLES_GAPPED')
    for c in ['open','high','low','close']:
        if not np.isfinite(pd.to_numeric(frame[c],errors='coerce')).all():raise RuntimeError('INVALID_MINUTE_CANDLES')


def simulated_long(row, minute, policy=PORTFOLIO):
    """Deterministic replay. Future bars are read only to resolve selected trade."""
    validate_candles_minute(minute)
    when=int(row['signal_closed_ts'])
    ts=minute.timestamp.to_numpy(dtype='int64')
    ix=int(np.searchsorted(ts,when))
    if ix==len(ts) or int(ts[ix])!=when:return None
    h=int(policy['max_minutes'])
    if ix+h>len(ts) or not np.all(ts[ix:ix+h]==when+np.arange(h,dtype='int64')*ONE):return None
    slip=float(policy['slippage_bps_per_side'])/10000
    entry_raw=float(minute.open.iloc[ix])
    entry_fill=entry_raw*(1+slip)
    tp=entry_fill*(1+float(policy['tp_pct'])/100)
    sl=entry_fill*(1-float(policy['sl_pct'])/100)
    exit_price=None; reason=None; ambiguous=False; end_ms=None
    for k in range(ix,ix+h):
        v=minute.iloc[k]
        hit_tp=float(v.high)>=tp
        hit_sl=float(v.low)<=sl
        if hit_sl:
            exit_price=sl; reason='SL_FIRST'; ambiguous=hit_tp
        elif hit_tp:
            exit_price=tp; reason='TP_FIRST'
        if reason:
            end_ms=int(v.timestamp)+ONE
            break
    if reason is None:
        last=minute.iloc[ix+h-1]
        exit_price=float(last.close)
        end_ms=int(last.timestamp)+ONE
        reason='TIMEOUT'
    exit_fill=exit_price*(1-slip)
    net_pct=100*(exit_fill/entry_fill-1)-float(policy['roundtrip_fee_pct'])
    return {'entry_ts':when,'exit_ts':end_ms,'entry_fill':entry_fill,'exit_fill':exit_fill,
            'exit_reason':reason,'both_same_minute':ambiguous,'net_pct':net_pct,
            'minutes_held':int((end_ms-when)//ONE)}


def replay_portfolio(events,get_minute,policy=PORTFOLIO):
    """Only first-attempt DIRECT_STRONG; no lookahead in eligibility or tie breaking."""
    chosen=events[(events.first_on_level)&(events.group=='DIRECT_STRONG')].copy()
    chosen=chosen.sort_values(['signal_closed_ts','symbol','attempt_id'])
    equity=float(policy['initial_equity_usd']); peak=equity; maxdd=0.; current=None
    record=[]; busy=0; no_data=0; ties=0; signal_count=len(chosen)
    for _,ev in chosen.iterrows():
        t=int(ev.signal_closed_ts)
        if current is not None and int(current['exit_ts'])<=t:
            equity=float(current['equity_after']); current=None
        if current is not None:
            busy+=1;continue
        # Multiple symbols at exact timestamp: alphabetical order, first wins.
        minute=get_minute(str(ev.symbol))
        trade=simulated_long(ev,minute,policy)
        if trade is None:
            no_data+=1;continue
        stake=equity*float(policy['margin_fraction'])*float(policy['leverage'])
        pnl=stake*trade['net_pct']/100
        after=equity+pnl
        if after<=0:raise RuntimeError('NONPOSITIVE_PORTFOLIO_EQUITY')
        current={**trade,'attempt_id':str(ev.attempt_id),'level_id':str(ev.level_id),
                 'symbol':str(ev.symbol),'equity_before':equity,'notional_usd':stake,
                 'pnl_usd':pnl,'equity_after':after}
        record.append(current)
        # Capital reserved until exit; current remains occupied until then.
    if current is not None:
        equity=float(current['equity_after'])
    # DD and realised path chronological by EXIT, not by signal.
    equity_curve=float(policy['initial_equity_usd']); peak=equity_curve
    for r in sorted(record,key=lambda x:(x['exit_ts'],x['entry_ts'])):
        equity_curve += float(r['pnl_usd'])
        peak=max(peak,equity_curve)
        maxdd=max(maxdd,100*(peak-equity_curve)/peak)
    if abs(equity_curve-equity)>1e-7:
        raise RuntimeError('EQUITY_LEDGER_MISMATCH')
    positives=sum(max(0,float(r['pnl_usd'])) for r in record)
    negatives=sum(-min(0,float(r['pnl_usd'])) for r in record)
    reasons=pd.Series([r['exit_reason'] for r in record]).value_counts().to_dict() if record else {}
    report={'trades':len(record),'signals':signal_count,'skipped_slot_busy':busy,
            'no_fill_or_incomplete':no_data,'start_equity_usd':float(policy['initial_equity_usd']),
            'end_equity_usd':round(equity,5),
            'return_pct':round(100*(equity/float(policy['initial_equity_usd'])-1),4),
            'max_drawdown_realised_pct':round(maxdd,4),
            'profit_factor_dollar':round(positives/negatives,4) if negatives else None,
            'exit_counts':reasons,'ambiguous_both_1m':sum(r['both_same_minute'] for r in record),
            'policy':policy,'limitations':['Realised-only drawdown, not intratrade drawdown',
                'Fees and fixed slippage model, no funding or order book fills',
                'Single slot, alphabetical timestamp ties, full-size fills assumed',
                'No liquidations at 1x; 80% capital allocation; not an exchange execution backtest']}
    return report,record


def portfolio():
    inspect_seal()
    if not (BASE/'temporal_report.json').exists():raise RuntimeError('Run --audit before --portfolio')
    d=aggregate()
    if d.empty:raise RuntimeError('NO_ATTEMPTS')
    cache={}
    def minute(symbol):
        if symbol not in cache:
            ids=[i for i,syms in enumerate(batches(),1) if symbol in syms]
            if len(ids)!=1:raise RuntimeError('SYMBOL_NOT_IN_UNIVERSE: '+symbol)
            f=batch_dir(ids[0])/f'{symbol}_1m.csv'
            cache[symbol]=pd.read_csv(f)
            validate_candles_minute(cache[symbol])
        return cache[symbol]
    summary,trades=replay_portfolio(d,minute)
    save_json(BASE/'portfolio_report.json',{'version':VERSION,'success':True,**summary})
    pd.DataFrame(trades).to_csv(BASE/'portfolio_trades.csv',index=False)
    print('HBL_V14_PORTFOLIO',json.dumps(summary,indent=2,ensure_ascii=False),flush=True)
    return summary


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    mode=p.add_mutually_exclusive_group(required=True)
    for x in ('freeze','plan','fetch','audit-data','run','audit','portfolio','verify'):
        mode.add_argument('--'+x,action='store_true')
    a=p.parse_args(argv)
    if a.freeze:freeze();return 0
    locked()
    if a.plan:
        print('V14_PLAN',json.dumps({'period_utc':[SIGNAL_START,SIGNAL_END],
            'download_start_utc':WARMUP_START,'tail_hours':TAIL_HOURS,
            'eligible_symbols':len(COHORT),'batches':len(batches()),
            'data_files':len(COHORT)*2,'frozen_primary':PRIMARY,
            'portfolio_policy':PORTFOLIO,'output':str(BASE),
            'note':'Earlier-date historical holdout, NOT prospective.'},indent=2))
        return 0
    if a.fetch:
        for i in range(1,len(batches())+1):call(COLLECTOR,'--run',*collector_args(i))
        print('V14_FETCH_COMPLETE; next --audit-data')
        return 0
    if a.audit_data:
        for i in range(1,len(batches())+1):call(COLLECTOR,'--audit',*collector_args(i))
        inspect_seal(create=True)
        return 0
    inspect_seal()
    if a.run:
        for i in range(1,len(batches())+1):
            args=['--candles-dir',str(batch_dir(i)),
                  '--symbols',','.join(batches()[i-1]),'--run-tag',run_tag(i)]
            call(ENGINE,'--run',*args)
            call(ENGINE,'--audit',*args)
        print('V14_REPLAY_COMPLETE; next --audit')
        return 0
    if a.audit:audit_signals();return 0
    if a.portfolio:portfolio();return 0
    if a.verify:
        d=aggregate()
        paths=[BASE/'temporal_report.json',BASE/'portfolio_report.json',BASE/'portfolio_trades.csv']
        if not all(x.exists() for x in paths):raise RuntimeError('MISSING_FINAL_OUTPUTS')
        print('V14_VERIFY_OK',json.dumps({'attempts_in_window':len(d),
               'temporal':json.loads(paths[0].read_text()),
               'portfolio':json.loads(paths[1].read_text())},ensure_ascii=False,indent=2))
        return 0
    return 2


if __name__=='__main__':
    try:raise SystemExit(main())
    except (RuntimeError,subprocess.CalledProcessError,ValueError) as exc:
        print('HBL_V14_ERROR',str(exc),file=sys.stderr)
        raise SystemExit(2)
