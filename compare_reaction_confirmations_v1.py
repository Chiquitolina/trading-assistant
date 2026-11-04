#!/usr/bin/env python3
"""OFFLINE read-only: compare causal 1/2/3/5/15-minute confirmation of original REACTIONs.

Same cohort: original 15m 3x3, prominence 0, FIRST TOUCH classified REACTION
by original dashboard/app.py helpers via verified HLL 1m replay. This study does
not claim parity to the full online scanner; that remains a separate audit.

For k>1, require all subsequent closed 1m bars (within k including retest bar)
remain on correct side of swing (CLOSE_HOLD) or no subsequent wick penetrates
swing (WICK_HOLD); final close is at least D% from swing on expected side.
The first touch candle is treated according to ORIGINAL REACTION logic; its
wick is not retroactively invalidated. Confirm at start_ts + k * 1m.

Outcome horizons are 15/30/60 COMPLETE 1m candles starting with the NEXT bar,
using that bar's OPEN as hypothetical fill (not an executable backtest).
Outcomes are retrospective and MUST NEVER become PRE features.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd

TF1 = 60_000
HORIZONS = (15, 30, 60)
CONFIRM_MINUTES = (1, 2, 3, 5, 15)
DISPLACEMENTS_PCT = (0.00, 0.10, 0.20)
POLICIES = ('CLOSE_HOLD', 'WICK_HOLD')
FIFTEEN_BAR = '15M_BAR' # First 15m candle close after retest; variable wait
TARGETS_PCT = (0.3, 0.5, 1.0)


def parse_utc(text):
    stamp = pd.Timestamp(text)
    if stamp.tzinfo is None:
        raise ValueError('UTC cutoff must contain timezone, e.g. 2026-09-17T14:00:00Z')
    return int(stamp.tz_convert('UTC').timestamp()*1000)


def observed_segment(tape: pd.DataFrame, start_ts: int, count: int):
    """Return exactly count contiguous bars or explicit censor reason."""
    ts = tape.timestamp.to_numpy(dtype=np.int64)
    idx = int(np.searchsorted(ts, start_ts))
    if idx >= len(ts):
        return None, 'TAIL'
    if int(ts[idx]) != start_ts:
        return None, 'GAP'
    end = min(idx+count, len(ts))
    subset = tape.iloc[idx:end]
    expected = start_ts + np.arange(len(subset), dtype=np.int64)*TF1
    mismatch = np.flatnonzero(subset.timestamp.to_numpy(dtype=np.int64) != expected)
    if len(mismatch):
        return None, 'GAP'
    if len(subset) != count:
        return None, 'TAIL'
    return subset, 'COMPLETE'


def candidate_rule(tape: pd.DataFrame, event: dict, k: int, policy: str, displacement: float):
    """NO future data after confirmation; k includes original retest minute."""
    if not (k in CONFIRM_MINUTES and (policy in POLICIES or (k==15 and policy==FIFTEEN_BAR)) and displacement in DISPLACEMENTS_PCT):
        raise ValueError('Unrecognized research rule')
    if k == 1 and (policy != 'CLOSE_HOLD' or displacement != 0):
        raise ValueError('1m must be original unmodified REACTION baseline')
    if event['status'] != 'REACTION':
        raise ValueError('Only original 1m REACTION cohort is allowed')
    first_ts=int(event['retest_ts'])
    effective_k = (15-(first_ts//TF1)%15) if policy==FIFTEEN_BAR else k
    bars,coverage=observed_segment(tape,first_ts,effective_k)
    result={'confirmation_min':k,'policy':policy,'min_displacement_pct':displacement,
            'actual_wait_min':effective_k,
            'confirmed_ts':first_ts+effective_k*TF1,'confirmation_coverage':coverage,
            'confirmed':None,'swing_breached_wick_during_wait':None,
            'swing_breached_close_during_wait':None,'final_margin_pct':None}
    if coverage!='COMPLETE':return result
    side=event['side'];swing=float(event['swing_price'])
    nextbars=bars.iloc[1:]
    if policy==FIFTEEN_BAR:
        # A genuine 15m candle close, not 15 consecutive 1m closes.
        # The candle begins at the aligned UTC 15m open and ends after the touch.
        start15=(first_ts//(15*TF1))*(15*TF1)
        entire15,status15=observed_segment(tape,start15,15)
        if status15!='COMPLETE':
            result['confirmation_coverage']=status15
            return result
    close=bars.close.to_numpy(dtype=float)
    signed=(close[-1]/swing-1)*100*(1 if side=='LONG' else -1)
    if side=='LONG':
        close_breach=bool((nextbars.close.to_numpy(dtype=float)<swing).any())
        wick_breach=bool((nextbars.low.to_numpy(dtype=float)<swing).any())
    else:
        close_breach=bool((nextbars.close.to_numpy(dtype=float)>swing).any())
        wick_breach=bool((nextbars.high.to_numpy(dtype=float)>swing).any())
    result['swing_breached_wick_during_wait']=wick_breach
    result['swing_breached_close_during_wait']=close_breach
    result['final_margin_pct']=float(signed)
    # Inform when a target was already reached during the waiting interval.
    # Baseline is the first next-OPEN after original 1m confirmation.
    for target_pct in TARGETS_PCT:
        key=str(target_pct).replace('.','_')
        if len(nextbars):
            entry_at_original=float(nextbars.iloc[0]['open'])
            if side=='LONG':
                earlier=bool((nextbars.high.to_numpy(dtype=float)>=entry_at_original*(1+target_pct/100)).any())
            else:
                earlier=bool((nextbars.low.to_numpy(dtype=float)<=entry_at_original*(1-target_pct/100)).any())
        else:
            earlier=False
        result['target_'+key+'_during_wait']=earlier
    if policy==FIFTEEN_BAR:
        high=float(entire15.high.max());low=float(entire15.low.min())
        close_location=(float(close[-1])-low)/(high-low) if high>low else .5
        favorable_half=(close_location>=.5 if side=='LONG' else close_location<=.5)
        # Original REACTION's candle-side+half-range principle, applied to 15m.
        confirmed=favorable_half and signed>=displacement-1e-10
        result['15m_close_location']=close_location
    else:
        confirmed=(not close_breach if policy=='CLOSE_HOLD' else not wick_breach) and signed>=displacement-1e-10
    result['confirmed']=bool(confirmed)
    return result


def race_and_outcome(bars: pd.DataFrame, side: str, swing: float, entry: float):
    """Results known only AFTER the chosen confirmation. Full horizon mandatory."""
    hi=bars.high.to_numpy(dtype=float)
    lo=bars.low.to_numpy(dtype=float)
    cl=bars.close.to_numpy(dtype=float)
    if side=='LONG':
        invalid_wick = lo<swing
        invalid_close = cl<swing
        invalid_zone = cl<swing*(1-0.10/100)
        mfe=max(0,(hi.max()/entry-1)*100)
        mae=max(0,(1-lo.min()/entry)*100)
        endret=(cl[-1]/entry-1)*100
    else:
        invalid_wick = hi>swing
        invalid_close = cl>swing
        invalid_zone = cl>swing*(1+0.10/100)
        mfe=max(0,(1-lo.min()/entry)*100)
        mae=max(0,(hi.max()/entry-1)*100)
        endret=(1-cl[-1]/entry)*100
    result={'entry_next_open':entry,
            'held_wick':not bool(invalid_wick.any()),
            'held_close':not bool(invalid_close.any()),
            'held_close_outside_band':not bool(invalid_zone.any()),
            'mfe_pct':float(mfe),'mae_pct':float(mae),'end_return_pct':float(endret)}
    for threshold in TARGETS_PCT:
        name=str(threshold).replace('.','_')
        if side=='LONG':
            target=hi>=entry*(1+threshold/100)
        else:
            target=lo<=entry*(1-threshold/100)
        result[f'hit_{name}']=bool(target.any())
        race='NEITHER'
        for hit,failure in zip(target,invalid_wick):
            if hit and failure:
                race='AMBIGUOUS_1M';break
            if hit:
                race='FAVORABLE_FIRST';break
            if failure:
                race='INVALIDATION_FIRST';break
        result[f'race_{name}']=race
    return result


def measure(tape: pd.DataFrame, event: dict, k: int, policy: str, displacement: float, horizon: int):
    """All outcomes start at the same next OPEN following a rule's decision."""
    r=candidate_rule(tape,event,k,policy,displacement)
    start=r['confirmed_ts']
    path,coverage=observed_segment(tape,start,horizon)
    r['horizon_min']=horizon
    r['outcome_coverage']=coverage
    r['outcome_observed_after_confirm']=coverage=='COMPLETE'
    if coverage=='COMPLETE':
        values=race_and_outcome(path,event['side'],float(event['swing_price']),float(path.iloc[0]['open']))
        # Evaluate outcome for both confirmed/unconfirmed original REACTIONs,
        # to supply matched-K original baseline with the same observation date.
        r.update(values)
    else:
        for field in ('entry_next_open','held_wick','held_close','held_close_outside_band',
                      'mfe_pct','mae_pct','end_return_pct'):
            r[field]=None
        for t in TARGETS_PCT:
            key=str(t).replace('.','_')
            r[f'hit_{key}']=None;r[f'race_{key}']=None
    return r


def rules():
    yield 1,'CLOSE_HOLD',0.0
    for k in CONFIRM_MINUTES[1:]:
        for p in POLICIES:
            for d in DISPLACEMENTS_PCT:
                yield k,p,d
    for d in DISPLACEMENTS_PCT:
        yield 15,FIFTEEN_BAR,d


def evaluate(tape: pd.DataFrame, events: list[dict], symbol: str):
    """Caller supplies unique original REACTION event_keys (one row per overlay marker)."""
    allrows=[]
    for e in events:
        for k,p,d in rules():
            for h in HORIZONS:
                row={'symbol':symbol,'event_key':e['event_key'],'pivot_ts':e['pivot_ts'],
                     'retest_ts':e['retest_ts'],'known_ts':e['known_ts'],
                     'side':e['side'],'swing_price':e['swing_price'],
                     'original_status':e['status'],'original_prominence_pct':e['prominence_pct']}
                row.update(measure(tape,e,k,p,d,h))
                allrows.append(row)
    df=pd.DataFrame(allrows)
    return df,summarize_records(df,symbol)


def summarize_records(df: pd.DataFrame, symbol: str):
    summary=[]
    keys=['confirmation_min','policy','min_displacement_pct','horizon_min']
    for key,g in df.groupby(keys,dropna=False,sort=True):
        # Horizon-complete AND confirmation-complete cohort only.
        eligible=g[(g['confirmation_coverage']=='COMPLETE')&(g['outcome_coverage']=='COMPLETE')]
        selected=eligible[eligible['confirmed']==True]
        def pct(x):return round(float(x)*100,2) if pd.notna(x) else None
        row=dict(zip(keys,key))
        row.update({'symbol':symbol,'original_reaction_count':len(g),
                    'eligible_complete':len(eligible),
                    'excluded_no_full_coverage':len(g)-len(eligible),
                    'confirmed_n':len(selected),
                    'selected_pct_of_eligible':pct(len(selected)/len(eligible)) if len(eligible) else None,
                    'wick_held_pct':pct(selected.held_wick.mean()) if len(selected) else None,
                    'close_held_pct':pct(selected.held_close.mean()) if len(selected) else None,
                    'close_band_held_pct':pct(selected.held_close_outside_band.mean()) if len(selected) else None,
                    'matched_original_wick_held_pct':pct(eligible.held_wick.mean()) if len(eligible) else None,
                    'matched_original_close_held_pct':pct(eligible.held_close.mean()) if len(eligible) else None,
                    'median_mfe_pct':round(float(selected.mfe_pct.median()),5) if len(selected) else None,
                    'median_mae_pct':round(float(selected.mae_pct.median()),5) if len(selected) else None,
                    'median_wait_min':round(float(selected.actual_wait_min.median()),2) if len(selected) else None,
                    'wick_breached_during_wait_pct':pct(selected.swing_breached_wick_during_wait.mean()) if len(selected) else None})
        for threshold in TARGETS_PCT:
            t=str(threshold).replace('.','_')
            wins=int((selected[f'race_{t}']=='FAVORABLE_FIRST').sum())
            lose=int((selected[f'race_{t}']=='INVALIDATION_FIRST').sum())
            ambig=int((selected[f'race_{t}']=='AMBIGUOUS_1M').sum())
            neither=int((selected[f'race_{t}']=='NEITHER').sum())
            row[f'win_{t}_first_n']=wins
            row[f'invalidation_{t}_first_n']=lose
            row[f'ambiguous_{t}_n']=ambig
            row[f'neither_{t}_n']=neither
            row[f'win_{t}_first_pct_selected']=pct(wins/len(selected)) if len(selected) else None
            row[f'win_{t}_first_pct_all_eligible']=pct(wins/len(eligible)) if len(eligible) else None
            row[f'hit_{t}_pct_selected']=pct(selected[f'hit_{t}'].mean()) if len(selected) else None
            row[f'target_{t}_during_wait_pct_selected']=pct(selected[f'target_{t}_during_wait'].mean()) if len(selected) else None
        summary.append(row)
    return pd.DataFrame(summary)


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root',required=True)
    ap.add_argument('--symbol',required=True)
    ap.add_argument('--pre-cutoff-utc',required=True)
    ap.add_argument('--pre-candles',type=int,default=96,choices=(48,96,144,192,288,384))
    ap.add_argument('--expected-reactions',type=int)
    ap.add_argument('--out',required=True)
    args=ap.parse_args(argv)
    root=Path(args.root).resolve();out=Path(args.out).resolve()
    sym=args.symbol.upper()
    if not sym.endswith('USDT') or not sym[:-4].isalnum():
        raise ValueError('Invalid symbol')
    if not out.is_relative_to(root):
        raise ValueError('Output must be a new research directory inside project root')
    if out.is_relative_to(root/'dashboard') or out.is_relative_to(root/'engine'):
        raise ValueError('Output cannot modify source directories')
    if not (root/'dashboard/app.py').is_file():raise FileNotFoundError(root/'dashboard/app.py')
    sys.path.insert(0,str(root))
    overlay=importlib.import_module('dashboard.views.hll_reaction_pre_overlay')
    from audit_reaction_defenses_v1 import file_sha256
    one_minute=root/overlay.MINUTE_DIR/(sym+'_1m.csv')
    manifest=root/overlay.MANIFEST_DIR/(sym+'.json')
    tape=overlay.load_verified_minute(one_minute,manifest)
    pre_ms=parse_utc(args.pre_cutoff_utc)
    app=root/'dashboard/app.py'
    env, detector, apphash=overlay.canonical_functions(str(app),app.stat().st_mtime_ns)
    raw=overlay.replay_canonical_pre(
        tape,pre_ms,pre_ms-args.pre_candles*overlay.TF30,
        env=env,swing_detector_class=detector,symbol=sym,
        left_bars=3,right_bars=3,min_prominence_pct=0.,include_first_retests=True)
    reactions={}
    for e in raw:
        if e['status']=='REACTION':reactions.setdefault(e['event_key'],e)
    if args.expected_reactions is not None and len(reactions)!=args.expected_reactions:
        raise RuntimeError(f'BASELINE_MISMATCH: expected {args.expected_reactions}, got {len(reactions)} distinct REACTIONs. DO NOT interpret comparison until source/cutoff reconciled.')
    if not reactions:
        raise RuntimeError('ZERO_REACTIONS: unable to compare; inspect cutoff/data')
    event_rows,summary=evaluate(tape,list(reactions.values()),sym)
    out.mkdir(parents=True,exist_ok=True)
    files={'events':out/f'{sym}_confirmation_events.csv',
           'summary':out/f'{sym}_confirmation_comparison.csv',
           'manifest':out/f'{sym}_confirmation_manifest.json'}
    event_rows.to_csv(files['events'],index=False)
    summary.to_csv(files['summary'],index=False)
    metadata={
        'study':'REACTION_CONFIRMATION_COMPARISON_V1','created_utc':datetime.now(timezone.utc).isoformat(),
        'symbol':sym,'pre_cutoff_utc':args.pre_cutoff_utc,'pre_candles_30m':args.pre_candles,
        'baseline_3x3_unique_reactions':len(reactions),'baseline_3x3_pivot_reactions':int(sum(e['status']=='REACTION' for e in raw)),
        'all_first_retests_by_status':dict(Counter(e['status'] for e in raw)),
        'rule_count':len(list(rules())), 'candidate_minutes':list(CONFIRM_MINUTES),
        'hold_policies':list(POLICIES)+[FIFTEEN_BAR],'displacements_pct':list(DISPLACEMENTS_PCT),
        '15m_bar_note':'15M_BAR waits to next UTC-aligned 15m close (1..15 minutes), requires favorable candle-half and displacement; does NOT require all interim 1m closes to hold',
        'full_outcome_horizons_min':list(HORIZONS),'favorable_thresholds_pct':list(TARGETS_PCT),
        'file_hashes_sha256':{str(path.relative_to(root)):file_sha256(path) for path in (app,one_minute,manifest,root/'engine/live/research/swing_detector.py',Path(overlay.__file__))},
        'source_app_hash_at_replay':apphash,
        'entry_assumption':'First 1m OPEN AFTER each rule confirmation; no spread, slippage, fees',
        'causal_definition':'Original REACTION detected at touch 1m close; rules k>1 use only contiguous closed bars until k-minute confirmation',
        'outcome_definition':'Next 15/30/60 COMPLETE 1m bars after confirmation (not same entry horizon counted from original touch)',
        'strict_no_future':'No post-confirm information used in rule pass/fail; outcomes for retrospective audit only',
        'comparison_warning':'Different k changes decision time; report selection rate and matched original baseline at SAME k; high selected success alone is not sufficient',
        'replay_parity_warning':'Overlay calls original AST helpers but scanner-vs-overlay parity not independently validated',
        'events_independence_warning':'Event keys deduplicated by symbol/side/retest time, but events from same symbol/day can be correlated',
        'intra_bar':'Same 1m target and wick invalidation => AMBIGUOUS_1M (not a win)',
        'git_head':subprocess.run(['git','rev-parse','HEAD'],cwd=root,capture_output=True,text=True).stdout.strip() or None,
        'outputs':{k:str(v) for k,v in files.items()}}
    files['manifest'].write_text(json.dumps(metadata,indent=2,ensure_ascii=False),encoding='utf-8')
    print('REACTION_CONFIRMATION_COMPARISON '+json.dumps({k:metadata[k] for k in ('symbol','baseline_3x3_unique_reactions','baseline_3x3_pivot_reactions','rule_count','all_first_retests_by_status')},ensure_ascii=False))
    print('\nRESULTS 30 MIN / TARGET +0.5% (all rates complete only):')
    cols=['confirmation_min','policy','min_displacement_pct','eligible_complete','confirmed_n','selected_pct_of_eligible','wick_held_pct','matched_original_wick_held_pct','win_0_5_first_pct_selected','win_0_5_first_pct_all_eligible']
    print(summary.loc[summary.horizon_min==30,cols].to_string(index=False))
    print('OUTPUT',*[str(x) for x in files.values()])


if __name__=='__main__':main()
