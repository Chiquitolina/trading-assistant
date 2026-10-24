#!/usr/bin/env python3
"""Historical Horizontal Breakout Lab V1.2 — causal level lifecycle and expansion.

Offline only. Keeps ALL observable crossing attempts grouped by a stable level_id.
Strength is assessed at the closing 30m candle; POST resolution is a separate label.
Never uses POST results to form levels or score trigger strength.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from horizontal_breakout_lab import (
    Archive, ROOT, REL, TF, M, Config, normalize, select_setup, path_outcome,
    utc, atomic_json, atomic_write_csv, source_snapshot, serial,
)

VERSION = 'horizontal-breakout-lifecycle-v1.2.0'
OUTPUT = REL / 'lifecycle_v12' / 'runs'


@dataclass(frozen=True)
class LifecycleConfig:
    windows: tuple[int, ...] = (12, 24, 36, 48)
    min_touches: int = 2
    tolerance_atr: float = 0.5
    margin_pct: float = 0.10
    max_level_bars: int = 96
    rearm_bars: int = 24
    invalidation_closes: int = 2
    acceptance_closes: int = 2
    min_volume_ratio: float = 2.0
    min_body_atr: float = 0.6
    min_close_position: float = 0.75
    high_lookback: int = 12
    post_bars: int = 8
    max_horizon_min: int = 2880
    stop_pct: float = 1.50
    fee_roundtrip_pct: float = 0.10


def formation_config(cfg):
    return Config(windows=cfg.windows, min_touches=cfg.min_touches,
                  tolerance_atr=cfg.tolerance_atr, margin_pct=cfg.margin_pct,
                  max_horizon_min=cfg.max_horizon_min, stop_pct=cfg.stop_pct,
                  fee_roundtrip_pct=cfg.fee_roundtrip_pct)


def continuous_segment(frame, left, right):
    """Inclusive positions. An empty interval is contiguous."""
    if right <= left:
        return True
    return bool(np.all(np.diff(frame.timestamp.iloc[left:right+1].to_numpy(dtype='int64')) == TF))


def strength_on_close(frame, idx, level, cfg):
    """Only OHLCV of bar idx and the cfg lookback of earlier closed bars."""
    candle = frame.iloc[idx]
    prior20 = frame.iloc[max(0, idx-20):idx]
    valid20 = len(prior20) == 20 and continuous_segment(frame, idx-20, idx)
    avg_vol = float(prior20.volume.mean()) if valid20 else 0
    vol_ratio = float(candle.volume) / avg_vol if avg_vol > 0 else None
    atr = float(level['atr_pre'])
    body_atr = (float(candle.close)-float(candle.open)) / atr if atr > 0 else None
    spread = float(candle.high)-float(candle.low)
    close_pos = (float(candle.close)-float(candle.low))/spread if spread>0 else None
    priorhigh = frame.iloc[max(0,idx-cfg.high_lookback):idx]
    has_prior = len(priorhigh) == cfg.high_lookback and continuous_segment(frame,idx-cfg.high_lookback,idx)
    new_high = bool(float(candle.high)>float(priorhigh.high.max())) if has_prior else None
    checks = {
        'zone_cleared': float(candle.close)>float(level['zone_high']),
        'volume_2x': vol_ratio is not None and vol_ratio >= cfg.min_volume_ratio,
        'body_atr': body_atr is not None and body_atr >= cfg.min_body_atr,
        'upper_close': close_pos is not None and close_pos >= cfg.min_close_position,
        'new_local_high': new_high is True,
    }
    complete = vol_ratio is not None and body_atr is not None and close_pos is not None and new_high is not None
    strong = complete and all(checks.values())
    return {
        'volume_ratio20': vol_ratio,
        'body_atr': body_atr,
        'close_position': close_pos,
        'new_high_lookback': new_high,
        'zone_cleared': checks['zone_cleared'],
        'strength_checks_passed': sum(checks.values()),
        'strength_coverage': 'COMPLETE' if complete else 'INSUFFICIENT_PRE',
        'strength_class': 'STRONG_BREAKOUT' if strong else ('UNKNOWN_STRENGTH' if not complete else 'BREAKOUT_ATTEMPT'),
        'close_above_level_pct': 100*(float(candle.close)/float(level['level'])-1),
        'bar_change_pct': 100*(float(candle.close)/float(candle.open)-1),
        'bar_open_ts': int(candle.timestamp),
    }


def post_label(frame, ix, zone_high, zone_low, cfg):
    """Outcomes using ONLY bars after the signal; never fed into strength or formation."""
    above, below = 0, 0
    for d in range(1, cfg.post_bars+1):
        j = ix+d
        if j >= len(frame) or int(frame.timestamp.iloc[j])-int(frame.timestamp.iloc[j-1])!=TF:
            return 'POST_CENSORED', None, d-1
        c = float(frame.close.iloc[j])
        above = above+1 if c > zone_high else 0
        below = below+1 if c < zone_low else 0
        if above >= cfg.acceptance_closes:
            return 'POST_HOLD', int(frame.timestamp.iloc[j])+TF, d
        if below >= cfg.invalidation_closes:
            return 'FAILED_BREAKOUT', int(frame.timestamp.iloc[j])+TF, d
    return 'NO_DECISION', None, cfg.post_bars


def scan_symbol(symbol, thirty, minute, cfg: LifecycleConfig, source30='UNKNOWN', source1='UNKNOWN'):
    """Produces one frozen level row per lifecycle; many attempts per level.

    A level may be retired after two consecutive accepts, two downside failures,
    long age or a gap. No closing classification is known at the formation time.
    """
    bars = normalize(thirty, TF)
    one = normalize(minute, M)
    levels, attempts, outcomes, tests = [], [], [], []
    active = None
    retired = []
    min_history = max(cfg.windows)+2
    prev_below = 0
    prev_above = 0
    gap_count = 0
    for i in range(len(bars)):
        candle = bars.iloc[i]
        clock = int(candle.timestamp)+TF
        if i and int(candle.timestamp)-int(bars.timestamp.iloc[i-1]) != TF:
            gap_count += 1
            if active is not None:
                active['end_state'] = 'GAP_30M'
                active['end_ts'] = int(bars.timestamp.iloc[i-1])+TF
                levels.append(active)
                active = None
            prev_below, prev_above = 0, 0
        if active is not None:
            x = active
            upper = float(x['level'])*(1+cfg.margin_pct/100)
            previous_close = float(bars.close.iloc[i-1])
            crossed = previous_close<=upper and float(candle.close)>upper
            # Subsequent tests of this same frozen ceiling, recorded at the
            # close of the touching bar; separate visits by >=2 closed 30m bars.
            high_now=float(candle.high)
            touched=(float(x['zone_low']) <= high_now <= float(x['zone_high'])
                     and float(candle.close)<=upper)
            if touched and i-x['last_test_index']>=2:
                x['last_test_index']=i
                x['level_tests_post']+=1
                tests.append({'level_id':x['level_id'],'symbol':symbol,
                              'event_type':'LEVEL_TEST','test_closed_ts':clock,
                              'test_utc':utc(clock),'test_high':high_now,
                              'test_number_post':x['level_tests_post'],
                              'level':x['level'],'zone_low':x['zone_low'],'zone_high':x['zone_high']})
            metrics = strength_on_close(bars,i,x,cfg)
            strong = metrics['strength_class']=='STRONG_BREAKOUT'
            # A strong candle one bar AFTER a weak cross can be a separate
            # observable continuation, even though the price is already above.
            continuation = (not crossed and strong and not x['seen_strong']
                            and x['attempt_count']>0 and float(candle.close)>upper)
            if crossed or continuation:
                sequence = x['attempt_count'] + 1
                attempt_id = hashlib.sha256(f'{x["level_id"]}|{clock}|{sequence}'.encode()).hexdigest()[:22]
                event = {
                    'attempt_id':attempt_id, 'level_id':x['level_id'], 'symbol':symbol,
                    'attempt_number':sequence, 'signal_closed_ts':clock,
                    'signal_utc':utc(clock), 'formation_closed_ts':x['formation_closed_ts'],
                    'level':x['level'],'zone_low':x['zone_low'],'zone_high':x['zone_high'],
                    'touch_count_pre':x['touch_count'], 'window':x['window'],
                    'atr_pre':x['atr_pre'], 'pressure_higher_lows_pct':x['pressure_higher_lows_pct'],
                    'volume_pre_recent_ratio':x['volume_pre_recent_ratio'],
                    'trigger_kind':'LEVEL_CROSS' if crossed else 'EXPANSION_CONTINUATION',
                    'signal_close':float(candle.close), 'trigger_tf':'30m',
                    'source_30m':source30, 'source_1m':source1,
                    **metrics,
                }
                post, post_ts, evaluated = post_label(bars,i,float(x['zone_high']),float(x['zone_low']),cfg)
                event.update(post_label=post,post_decided_ts=post_ts,post_bars_evaluated=evaluated)
                attempts.append(event)
                outcomes.append({'attempt_id':attempt_id,'level_id':x['level_id'],'symbol':symbol,
                                 'signal_closed_ts':clock,
                                 **path_outcome(one,clock,float(x['level']),formation_config(cfg))})
                x['attempt_count'] = sequence
                if strong: x['seen_strong'] = True
            # A level retires only when the condition has been observed on
            # *two* full 30m closes. This is a later level state, not a label
            # used to justify any earlier signal.
            prev_above = prev_above+1 if float(candle.close)>float(x['zone_high']) else 0
            prev_below = prev_below+1 if float(candle.close)<float(x['zone_low'])-float(x['atr_pre']) else 0
            terminal = None
            # A pair of minor closes over the zone is NOT necessarily the
            # final expansion; keep weak attempts on the same ceiling until
            # a strong candle is observed or another terminal condition hits.
            if x['seen_strong'] and prev_above>=cfg.acceptance_closes:
                terminal='ACCEPTED_UP_AFTER_STRONG'
            elif prev_below>=cfg.invalidation_closes:
                terminal='INVALIDATED_DOWN'
            elif clock - x['formation_closed_ts'] >= cfg.max_level_bars*TF:
                terminal='MAX_AGE'
            if terminal:
                x['end_state'],x['end_ts']=terminal,clock
                levels.append(x)
                retired.append((float(x['level']),float(x['tolerance_price']),i))
                active = None
                prev_below,prev_above=0,0
        if active is not None or i+1<min_history:
            continue
        # Do not select a new level using a candle that just closed an old
        # lifecycle. Re-seeding on the same candle would double count.
        if levels and levels[-1].get('end_ts')==clock:
            continue
        candidate = select_setup(bars.iloc[:i+1],formation_config(cfg))
        if candidate is None:
            continue
        anchor = float(candidate['level'])
        # Keep same/nearby ceilings as one episode instead of counting a
        # swarm of new levels after every weak touch / failed cross.
        retired=[(lv,tol,j) for lv,tol,j in retired if i-j<=cfg.rearm_bars]
        if any(abs(anchor-lv)<=max(tol,float(candidate['tolerance_price'])) for lv,tol,j in retired):
            continue
        level_id=hashlib.sha256(f'{symbol}|{clock}|{anchor:.12g}|{candidate["window"]}'.encode()).hexdigest()[:22]
        active={
            'level_id':level_id,'symbol':symbol,'formation_closed_ts':clock,
            'formation_utc':utc(clock),'source_30m':source30,'source_1m':source1,
            **candidate,'end_state':'ACTIVE_AT_END','end_ts':None,
            'attempt_count':0,'seen_strong':False,
            'level_tests_post':0,'last_test_index':i,
        }
        prev_below,prev_above=0,0
    if active is not None:
        levels.append(active)
    diag={
        'bars30':len(bars), 'bars1':len(one),
        'gaps30':int(np.count_nonzero(np.diff(bars.timestamp.to_numpy(dtype='int64'))!=TF)) if len(bars)>1 else 0,
        'level_count':len(levels), 'attempt_count':len(attempts), 'level_tests_post':len(tests),
        'strong_count':sum(a['strength_class']=='STRONG_BREAKOUT' for a in attempts),
        'filled_count':sum(o['path_state']!='NO_FILL_1M' for o in outcomes),
        'complete48h':sum(o['path_state']=='COMPLETE' for o in outcomes),
    }
    # Index is runtime-only; stored level history includes immutable times and
    # recorded test counts, not any future-derived features.
    for level in levels: level.pop('last_test_index',None)
    return levels,attempts,outcomes,tests,diag


def args_parser(argv=None):
    p=argparse.ArgumentParser(description='Offline 30m horizontal level lifecycle and breakout expansion, no LIVE')
    modes=p.add_mutually_exclusive_group(required=True)
    for name in ('plan','run','audit'): modes.add_argument('--'+name,action='store_true')
    p.add_argument('--root',type=Path,default=ROOT)
    p.add_argument('--candles-dir',type=Path,default=None)
    p.add_argument('--max-symbols',type=int,default=0)
    p.add_argument('--symbols',default='')
    p.add_argument('--run-tag',default='default')
    p.add_argument('--windows',default='12,24,36,48')
    p.add_argument('--margin-pct',type=float,default=.10)
    p.add_argument('--min-free-gb',type=float,default=1.)
    return p.parse_args(argv)


def build_config(a):
    windows=tuple(sorted(set(int(x.strip()) for x in a.windows.split(',') if x.strip())))
    if not windows or min(windows)<12 or max(windows)>120:
        raise ValueError('Window sizes must be in 12..120')
    if not 0<=a.margin_pct<=2:
        raise ValueError('Invalid margin')
    return LifecycleConfig(windows=windows,margin_pct=a.margin_pct)


def fingerprint(a,cfg):
    if not a.run_tag or len(a.run_tag)>80 or not all(c.isalnum() or c in '._-' for c in a.run_tag):
        raise ValueError('Invalid --run-tag')
    m=dict(version=VERSION,root=str(a.root.resolve()),candles_dir=str(a.candles_dir.resolve()) if a.candles_dir else None,
           symbols=a.symbols.upper(),max_symbols=a.max_symbols,run_tag=a.run_tag,
           settings=asdict(cfg),source_type='CANDLES_DIR' if a.candles_dir else 'LOCAL_SQLITE',
           note='No LIVE access. 30m-only causal strength; POST results separate.')
    key=hashlib.sha256(json.dumps(m,sort_keys=True).encode()).hexdigest()[:14]
    return a.root/OUTPUT/key,m


def run_audit(out):
    conf=out/'run_config.json'
    if not conf.exists():raise RuntimeError('Missing run_config; execute --run')
    expected=json.loads((out/'expected_symbols.json').read_text())
    completed=set()
    issues=[]
    for status in (out/'shards').glob('*.status.json'):
        s=json.loads(status.read_text())
        completed.add(s['symbol'])
        for k in ('levels','attempts','outcomes','tests'):
            p=out/'shards'/f'{s["symbol"]}.{k}.csv'
            if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest()!=s['sha256'].get(k):
                issues.append(f'{s["symbol"]}:{k}:BAD_CHECKSUM')
    missing=sorted(set(expected)-completed)
    if missing:issues.append(f'MISSING_SYMBOLS:{len(missing)}')
    def concat(name):
        items=[]
        for sym in expected:
            p=out/'shards'/f'{sym}.{name}.csv'
            if p.is_file() and p.stat().st_size:
                try: f=pd.read_csv(p)
                except pd.errors.EmptyDataError:continue
                if not f.empty:items.append(f)
        return pd.concat(items,ignore_index=True) if items else pd.DataFrame()
    levels,attempts,outcomes,tests=(concat(k) for k in ('levels','attempts','outcomes','tests'))
    if not levels.empty and levels.level_id.duplicated().any():issues.append('DUPLICATE_LEVEL')
    if not attempts.empty:
        if attempts.attempt_id.duplicated().any(): issues.append('DUPLICATE_ATTEMPT')
        if not set(attempts.level_id).issubset(set(levels.level_id)):issues.append('ORPHAN_ATTEMPT')
        if any(attempts.signal_closed_ts<=attempts.formation_closed_ts):issues.append('FUTURE_LEAK_PRE')
        if (attempts.groupby('level_id').attempt_number.nunique() != attempts.groupby('level_id').size()).any():
            issues.append('REPEATED_ATTEMPT_NUMBER')
    if not tests.empty:
        if not set(tests.level_id).issubset(set(levels.level_id)):issues.append('ORPHAN_LEVEL_TEST')
        if tests.duplicated(['level_id','test_closed_ts']).any():issues.append('DUPLICATE_LEVEL_TEST')
    if len(attempts)!=len(outcomes):issues.append('ATTEMPT_OUTCOME_COUNT_MISMATCH')
    if not outcomes.empty:
        if outcomes.attempt_id.duplicated().any():issues.append('DUPLICATE_OUTCOME')
        if not attempts.empty and set(outcomes.attempt_id)!=set(attempts.attempt_id):issues.append('OUTCOME_LINK_MISMATCH')
    report=dict(version=VERSION,success=not issues,completed_symbols=len(completed),expected_symbols=len(expected),
                missing_symbols=missing,levels=len(levels),attempts=len(attempts),level_tests_post=len(tests),
                strong=int((attempts.strength_class=='STRONG_BREAKOUT').sum()) if not attempts.empty else 0,
                attempts_per_level=float(attempts.shape[0]/levels.shape[0]) if not levels.empty else 0.,
                path_counts=outcomes.path_state.value_counts(dropna=False).to_dict() if not outcomes.empty else {},
                post_counts=attempts.post_label.value_counts(dropna=False).to_dict() if not attempts.empty else {},
                errors=issues,
                caveat='POST_HOLD is future observation; never a signal. No strategy profitability proven.')
    atomic_json(out/'audit.json',report)
    return report,levels,attempts,outcomes,tests


def main(argv=None):
    a=args_parser(argv)
    cfg=build_config(a)
    out,manifest=fingerprint(a,cfg)
    if a.audit:
        r,*_=run_audit(out)
        print(json.dumps(r,ensure_ascii=False,indent=2,default=serial))
        return 0 if r['success'] else 2
    with Archive(a.root,a.candles_dir) as archive:
        symbols=archive.symbols()
        if a.symbols:
            wanted=set(x.strip().upper() for x in a.symbols.split(',') if x.strip())
            symbols=[s for s in symbols if s in wanted]
            if set(symbols)!=wanted:print('SYMBOLS_MISSING',sorted(wanted-set(symbols)),flush=True)
        if a.max_symbols>0:symbols=symbols[:a.max_symbols]
        print(json.dumps(dict(mode='PLAN' if a.plan else 'RUN',output=str(out),symbols=len(symbols),settings=asdict(cfg),
                              note='Only archived OHLCV; no network downloads, no LIVE actions.'),ensure_ascii=False),flush=True)
        if a.plan:
            for sym in symbols[:min(len(symbols),5)]:
                thirty,one,s30,s1=archive.load(sym)
                print('COVERAGE',json.dumps(dict(symbol=sym,bars30=len(thirty),bars1=len(one),source30=s30,source1=s1)),flush=True)
            return 0
        if shutil.disk_usage(a.root).free/1024**3 < a.min_free_gb:raise RuntimeError('Insufficient disk free')
        out.mkdir(parents=True,exist_ok=True)
        for fn,value in [('run_config.json',manifest),('expected_symbols.json',symbols)]:
            p=out/fn
            if p.exists() and json.loads(p.read_text()) != value:
                raise RuntimeError(f'{fn} changed. Use another --run-tag')
            atomic_json(p,value)
        snap=source_snapshot(a)
        sp=out/'source_snapshot.json'
        if sp.exists() and json.loads(sp.read_text())!=snap:raise RuntimeError('Input changed; use new --run-tag')
        atomic_json(sp,snap)
        (out/'shards').mkdir(exist_ok=True)
        for i,sym in enumerate(symbols,1):
            status=out/'shards'/f'{sym}.status.json'
            if status.exists():
                old=json.loads(status.read_text())
                if all((out/'shards'/f'{sym}.{k}.csv').exists() and hashlib.sha256((out/'shards'/f'{sym}.{k}.csv').read_bytes()).hexdigest()==old['sha256'].get(k) for k in ('levels','attempts','outcomes','tests')):
                    print(f'[{i}/{len(symbols)}] RESUME {sym}',flush=True)
                    continue
            try:
                b30,b1,s30,s1=archive.load(sym)
                lv,att,oc,te,diag=scan_symbol(sym,b30,b1,cfg,s30,s1)
                hashes={}
                for kind,rows in [('levels',lv),('attempts',att),('outcomes',oc),('tests',te)]:
                    p=out/'shards'/f'{sym}.{kind}.csv'
                    atomic_write_csv(p,rows)
                    hashes[kind]=hashlib.sha256(p.read_bytes()).hexdigest()
                atomic_json(status,dict(symbol=sym,diag=diag,sha256=hashes))
                print(f'[{i}/{len(symbols)}] OK {sym}: levels={len(lv)} attempts={len(att)} strong={diag["strong_count"]} full48={diag["complete48h"]}',flush=True)
            except Exception as exc:
                print(f'[{i}/{len(symbols)}] ERROR {sym}: {type(exc).__name__}: {exc}',flush=True)
        report,lv,att,oc,te=run_audit(out)
        for kind,rows in [('levels',lv),('attempts',att),('outcomes',oc),('tests',te)]:
            atomic_write_csv(out/f'{kind}.csv',rows.to_dict('records'))
        print('HBL_V12_FINAL',json.dumps(report,ensure_ascii=False,default=serial),flush=True)
        return 0 if report['success'] else 2


if __name__=='__main__':
    raise SystemExit(main())
