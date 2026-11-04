#!/usr/bin/env python3
"""READ-ONLY experimental follow-up: 28 confirmation rules on ALL original first retests.

Original 15m 3x3, first touch and labels are reconstructed with the ORIGINAL
app.py helper functions through the existing HLL historical overlay. No LIVE changes.
The 1m rule accepts ONLY original REACTION; 2/3/5/15m alternatives may confirm
TOUCH_INDECISIVE and TOUCH_FAILED, whose original labels remain unchanged.

Critically: the confirmation tape is physically cut at the PRE cutoff.
Future candles are used ONLY to evaluate complete OUTCOMES after a PRE-confirmed
candidate. No subsequent candle can turn a PRE-ineligible event into a signal.
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

import compare_reaction_confirmations_v1 as v1
from audit_reaction_defenses_v1 import file_sha256

STATUSES = ('REACTION', 'TOUCH_INDECISIVE', 'TOUCH_FAILED')
TARGET_KEYS = ('0_3', '0_5', '1_0')
VERSION = 'FIRST_TOUCH_CONFIRMATIONS_V2'


def pivot_touch_key(event: dict) -> str:
    return '|'.join(str(x) for x in
                    (event['symbol'],event['side'],int(event['pivot_ts']),int(event['retest_ts'])))


def status_at_touch(event: dict):
    swing=float(event['swing_price']); close=float(event['retest_close'])
    side=event['side']
    if side not in ('LONG','SHORT') or swing<=0 or close<=0:
        raise ValueError('Invalid first-touch swing/price/side')
    margin=(close/swing-1)*100*(1 if side=='LONG' else -1)
    return margin, bool(margin >= -1e-10)


def candidate_pre_only(pre_tape: pd.DataFrame, event: dict, k: int,
                       policy: str, displacement: float, pre_cutoff_ms: int):
    """Use v1 candle math. Delayed rules need NOT inherit original 1m REACTION.

    We pass a temporary comparison-only copy into v1's REACTION-cohort guard;
    the original status is NEVER changed, and the 1m original is status-gated.
    Initial retest wick is excluded by v1 for ALL variants; this matters for
    'reclaim after touch' vs 'continuous hold' interpretation.
    """
    if event['status'] not in STATUSES:
        raise ValueError('Unexpected original first-touch status')
    synthetic=dict(event, status='REACTION')
    result=v1.candidate_rule(pre_tape,synthetic,k,policy,displacement)
    t0_margin, t0_correct_side=status_at_touch(event)
    first_minute, first_coverage=v1.observed_segment(pre_tape,int(event['retest_ts']),1)
    if first_coverage!='COMPLETE':
        raise RuntimeError('Original canonical first touch missing its closed 1m candle')
    candle=first_minute.iloc[0]
    swing=float(event['swing_price'])
    result['t0_wick_beyond_swing']=bool(float(candle['low'])<swing if event['side']=='LONG'
                                       else float(candle['high'])>swing)
    result['t0_close_margin_pct']=float(t0_margin)
    result['t0_closed_expected_side']=t0_correct_side
    result['initial_status']=event['status']
    result['pre_eligible']=(result['confirmation_coverage']=='COMPLETE'
                            and int(result['confirmed_ts'])<=pre_cutoff_ms)
    if k==1:
        # The only canonical 1m reference: NEVER turn an INDECISIVE/FAILED
        # into a new 1m REACTION merely because its close is on the right side.
        if result['pre_eligible']:
            if event['status']=='REACTION' and result['confirmed'] is not True:
                raise AssertionError('Original REACTION not consistent with v1 close')
            result['confirmed']=bool(event['status']=='REACTION')
    if not result['pre_eligible']:
        result['confirmed']=None
    accepted=result['confirmed'] is True
    result['new_confirmation_from_indecisive']=bool(accepted and event['status']=='TOUCH_INDECISIVE')
    result['late_reclaim_after_failed_touch']=bool(accepted and event['status']=='TOUCH_FAILED')
    result['initial_close_wrong_side_but_later_accepted']=bool(accepted and not t0_correct_side)
    result['original_reaction_retained']=bool(accepted and event['status']=='REACTION')
    result['origin_group']=('ORIGINAL_REACTION' if event['status']=='REACTION' else
                            'INDECISIVE_RESCUE' if event['status']=='TOUCH_INDECISIVE' else
                            'FAILED_RECLAIM')
    return result


def chronological_race_close_band(bars: pd.DataFrame, side: str, swing: float,
                                  entry: float, target_pct: float, band_pct: float=0.10):
    """Target by wick vs invalidation by 1m CLOSE outside swing band.

    A target and adverse close in the same 1m are conservatively ambiguous,
    even though actual intraminute order is unknown; never count as a win.
    """
    if side not in ('LONG','SHORT'):
        raise ValueError('Invalid trade direction')
    for _,bar in bars.iterrows():
        if side=='LONG':
            target=float(bar['high'])>=entry*(1+target_pct/100)
            failed=float(bar['close'])<swing*(1-band_pct/100)
        else:
            target=float(bar['low'])<=entry*(1-target_pct/100)
            failed=float(bar['close'])>swing*(1+band_pct/100)
        if target and failed:return 'AMBIGUOUS_1M'
        if target:return 'FAVORABLE_FIRST'
        if failed:return 'INVALIDATION_FIRST'
    return 'NEITHER'


def measure_pre_confirmed(full_tape: pd.DataFrame, pre_tape: pd.DataFrame,
                          event: dict, k: int, policy: str, displacement: float,
                          horizon: int, pre_cutoff_ms: int):
    result=candidate_pre_only(pre_tape,event,k,policy,displacement,pre_cutoff_ms)
    result['horizon_min']=horizon
    result['outcome_observed_after_confirm']=False
    result['outcome_coverage']='NOT_EVALUATED_NO_PRE_CONFIRMATION_DATA'
    # All first touches with complete PRE confirmation-window data may have
    # matched outcome measurements, even rejected ones; these are expressly
    # counterfactual and NOT signals. Never measure when confirmation is censored.
    if not result['pre_eligible']:
        return result
    future,coverage=v1.observed_segment(full_tape,int(result['confirmed_ts']),horizon)
    result['outcome_coverage']=coverage
    if coverage!='COMPLETE':
        return result
    result['outcome_observed_after_confirm']=True
    result.update(v1.race_and_outcome(future,event['side'],float(event['swing_price']),
                                      float(future.iloc[0]['open'])))
    result['matched_counterfactual_if_rejected']=result['confirmed'] is False
    for target in v1.TARGETS_PCT:
        key=str(target).replace('.','_')
        result[f'race_close_band_{key}']=chronological_race_close_band(
            future,event['side'],float(event['swing_price']),
            float(future.iloc[0]['open']),target)
    return result


def evaluate_all(full_tape: pd.DataFrame, events: list[dict], symbol: str,
                 pre_cutoff_ms: int):
    if len(events)==0:raise ValueError('No original first-touch events')
    if any(e['symbol']!=symbol for e in events):raise ValueError('Mismatched symbols')
    if len({pivot_touch_key(e) for e in events})!=len(events):
        raise ValueError('Duplicated pivot first-touch candidates')
    # PRE confirmation uses ONLY the closed bars before the cutoff.
    if pre_cutoff_ms % v1.TF1:
        raise ValueError('PRE cutoff not aligned to closed 1m candles')
    pre_tape=full_tape.loc[full_tape.timestamp<pre_cutoff_ms].reset_index(drop=True)
    out=[]
    for event in events:
        if event['status'] not in STATUSES:
            raise ValueError('Unexpected original status')
        if int(event['known_ts'])!=int(event['retest_ts'])+v1.TF1:
            raise ValueError('Original first touch known timestamp inconsistent with 1m close')
        if int(event['known_ts'])>pre_cutoff_ms:
            raise ValueError('Original first touch known only after PRE cutoff')
        for k,p,d in v1.rules():
            # Once per rule, not once per horizon: still inexpensive for pilot,
            # and avoids a post-PRE confirmation path appearing on a reread.
            for h in v1.HORIZONS:
                row={'symbol':symbol,'pivot_touch_id':pivot_touch_key(event),
                     'event_key':event['event_key'],'side':event['side'],
                     'original_status':event['status'],
                     'pivot_ts':int(event['pivot_ts']),
                     'retest_ts':int(event['retest_ts']),
                     'original_known_ts':int(event['known_ts']),
                     'swing_price':float(event['swing_price']),
                     'prominence_pct':float(event['prominence_pct']),
                     'pre_cutoff_ms':int(pre_cutoff_ms)}
                row.update(measure_pre_confirmed(full_tape,pre_tape,event,k,p,d,h,pre_cutoff_ms))
                out.append(row)
    details=pd.DataFrame(out)
    return details,summarize_all(details)


def _pct(n,d):
    return round(100*float(n)/float(d),2) if d else None


def summarize_all(rows: pd.DataFrame):
    """True pooled numerators and denominators; never average percentages."""
    grouped=[]
    rules=['confirmation_min','policy','min_displacement_pct','horizon_min']
    for side_group in ('ALL','LONG','SHORT'):
        s=rows if side_group=='ALL' else rows[rows.side==side_group]
        if s.empty:continue
        for status_group in ('ALL',)+STATUSES:
            z=s if status_group=='ALL' else s[s.original_status==status_group]
            if z.empty:continue
            for cfg,g in z.groupby(rules,sort=True,dropna=False):
                gpre=g[g.pre_eligible==True]
                complete=gpre[gpre.outcome_coverage=='COMPLETE']
                selected=complete[complete.confirmed==True]
                all_selected=gpre[gpre.confirmed==True]
                n=len(complete);s_n=len(selected)
                result=dict(zip(rules,cfg))
                result.update({'symbol_count':int(g.symbol.nunique()),
                  'side_group':side_group,'original_status_group':status_group,
                  'candidate_pivot_touches':len(g),
                  'pre_confirmation_complete':len(gpre),
                  'pre_confirmation_incomplete':len(g)-len(gpre),
                  'selected_pre_all_outcomes':len(all_selected),
                  'eligible_complete_outcomes':n,
                  'censored_outcomes':len(gpre)-n,
                  'selected_complete_outcomes':s_n,
                  'selection_pct_complete':_pct(s_n,n),
                  'selected_original_reaction_n':int((selected.original_status=='REACTION').sum()),
                  'selected_rescued_indecisive_n':int((selected.original_status=='TOUCH_INDECISIVE').sum()),
                  'selected_reclaimed_failed_n':int((selected.original_status=='TOUCH_FAILED').sum()),
                  'selected_initial_wrong_side_close_n':int(selected.initial_close_wrong_side_but_later_accepted.sum()),
                  'selected_initial_wick_breach_n':int(selected.t0_wick_beyond_swing.sum()),
                  'accepted_held_wick_pct':_pct(int(selected.held_wick.sum()),s_n) if s_n else None,
                  'accepted_held_close_pct':_pct(int(selected.held_close.sum()),s_n) if s_n else None,
                  'accepted_held_close_band_pct':_pct(int(selected.held_close_outside_band.sum()),s_n) if s_n else None,
                  'median_mfe_pct_accepted':round(float(selected.mfe_pct.median()),6) if s_n else None,
                  'median_mae_pct_accepted':round(float(selected.mae_pct.median()),6) if s_n else None,
                  'median_actual_wait_min_accepted':round(float(selected.actual_wait_min.median()),2) if s_n else None,
                  'matched_all_wick_held_pct':_pct(int(complete.held_wick.sum()),n) if n else None,
                  'selected_target_0_5_during_wait_n':int(selected.target_0_5_during_wait.sum()) if s_n and 'target_0_5_during_wait' in selected else 0})
                for target in TARGET_KEYS:
                    key=f'race_{target}'
                    wins=int((selected[key]=='FAVORABLE_FIRST').sum())
                    amb=int((selected[key]=='AMBIGUOUS_1M').sum())
                    invalid=int((selected[key]=='INVALIDATION_FIRST').sum())
                    result[f'win_{target}_n']=wins
                    result[f'win_{target}_pct_selected']=_pct(wins,s_n)
                    result[f'win_{target}_pct_all_eligible']=_pct(wins,n)
                    result[f'invalidation_{target}_n']=invalid
                    result[f'ambiguous_{target}_n']=amb
                    alt_key=f'race_close_band_{target}'
                    band_win=int((selected[alt_key]=='FAVORABLE_FIRST').sum())
                    band_amb=int((selected[alt_key]=='AMBIGUOUS_1M').sum())
                    result[f'win_close_band_{target}_n']=band_win
                    result[f'win_close_band_{target}_pct_selected']=_pct(band_win,s_n)
                    result[f'win_close_band_{target}_pct_all_eligible']=_pct(band_win,n)
                    result[f'ambiguous_close_band_{target}_n']=band_amb
                grouped.append(result)
    return pd.DataFrame(grouped)


def symbol_name(symbol):
    s=symbol.upper()
    if not s.endswith('USDT') or not s[:-4].isalnum():
        raise ValueError('Unexpected/unsafe symbol')
    return s


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True)
    parser.add_argument('--symbol',required=True)
    parser.add_argument('--pre-cutoff-utc',required=True)
    parser.add_argument('--pre-candles',type=int,default=96,choices=(48,96,144,192,288,384))
    parser.add_argument('--expected-first-touches',type=int,required=True)
    parser.add_argument('--expected-reactions',type=int,required=True)
    parser.add_argument('--out',required=True)
    args=parser.parse_args(argv)
    root=Path(args.root).resolve(); symbol=symbol_name(args.symbol)
    out=Path(args.out).resolve()
    safe=root/'reports'/'reaction_first_touch_confirmation_v2'
    if not out.is_relative_to(safe) or out==safe:
        raise ValueError(f'Output must be an isolated new directory under {safe}')
    files={'events':out/f'{symbol}_all_first_touch_confirmations.csv',
           'summary':out/f'{symbol}_all_first_touch_summary.csv',
           'manifest':out/f'{symbol}_all_first_touch_manifest.json'}
    if any(f.exists() for f in files.values()):
        raise FileExistsError('V2 output already exists: choose a fresh --out directory')
    if not (root/'dashboard/app.py').is_file():
        raise FileNotFoundError(root/'dashboard/app.py')
    if str(root) not in sys.path: sys.path.insert(0,str(root))
    overlay=importlib.import_module('dashboard.views.hll_reaction_pre_overlay')
    tape_file=root/overlay.MINUTE_DIR/(symbol+'_1m.csv')
    tape_manifest=root/overlay.MANIFEST_DIR/(symbol+'.json')
    tape=overlay.load_verified_minute(tape_file,tape_manifest)
    pre_ms=v1.parse_utc(args.pre_cutoff_utc)
    app=root/'dashboard/app.py'
    env,swing_class,source_hash=overlay.canonical_functions(str(app),app.stat().st_mtime_ns)
    originals=overlay.replay_canonical_pre(tape,pre_ms,pre_ms-args.pre_candles*overlay.TF30,
               env=env,swing_detector_class=swing_class,symbol=symbol,
               left_bars=3,right_bars=3,min_prominence_pct=0.0,include_first_retests=True)
    statuses=Counter(e['status'] for e in originals)
    uniq_reactions={e['event_key'] for e in originals if e['status']=='REACTION'}
    if len(originals)!=args.expected_first_touches or len(uniq_reactions)!=args.expected_reactions:
        raise RuntimeError(f'BASELINE_MISMATCH: {symbol}, first_touches={len(originals)} '
                           f'(expected {args.expected_first_touches}), '
                           f'unique_reactions={len(uniq_reactions)} '
                           f'(expected {args.expected_reactions}). No comparisons generated.')
    details,summary=evaluate_all(tape,originals,symbol,pre_ms)
    # CSV provenance: all rows preserve original status and cutoff eligibility.
    out.mkdir(parents=True,exist_ok=True)
    details.to_csv(files['events'],index=False)
    summary.to_csv(files['summary'],index=False)
    meta={'study':VERSION,'created_utc':datetime.now(timezone.utc).isoformat(),
          'symbol':symbol,'pre_cutoff_utc':args.pre_cutoff_utc,'pre_candles_30m':args.pre_candles,
          'original_first_touches':len(originals),
          'original_by_status':dict(statuses),'original_unique_reactions':len(uniq_reactions),
          'pivot_key':'symbol|side|pivot_ts|retest_ts',
          'rule_count':len(list(v1.rules())),'horizons_min':list(v1.HORIZONS),
          'favorable_targets_pct':list(v1.TARGETS_PCT),
          'source_sha256':{str(x.relative_to(root)):file_sha256(x) for x in
                           (app,tape_file,tape_manifest,root/'engine/live/research/swing_detector.py',
                            Path(overlay.__file__),root/'compare_reaction_confirmations_v1.py',
                            root/'audit_reaction_defenses_v1.py')},
          'v2_source_sha256':file_sha256(Path(__file__).resolve()),
          'canonical_app_functions_hash':source_hash,
          'git_head':subprocess.run(['git','rev-parse','HEAD'],cwd=root,capture_output=True,text=True).stdout.strip() or None,
          'rule_1m':'Original REACTION only, untouched; INDECISIVE/FAILED always rejected at minute 1',
          'rules_delayed':'Use V1 candidate math on research copy of each first touch, preserving original status; initial minute wick exempt; CLOSE_HOLD/WICK_HOLD check following minutes',
          'initial_wrong_side_warning':'Recovery after closing wrong side at first touch is a LATE RECLAIM, not an uninterrupted defense; separate via initial_close_wrong_side_but_later_accepted',
          'initial_wick_warning':'WICK_HOLD only checks candles AFTER initial retest; initial wick sweep is separately reported as t0_wick_beyond_swing.',
          '15m_note':'15M_BAR waits for next actual 15m close (1-15min), may include price action prior to first touch in its candle shape',
          'causal_pre_guard':'Candidate confirmation tape physically clipped at PRE cutoff. Post-cutoff confirmation is censored, never eligible.',
          'outcome_note':'Retrospective 15/30/60 complete minutes after each candidate confirmation; no PRE features; rejected candidate outcomes are matched counterfactual references only.',
          'coverage_note':'All selection and performance rates use FULL confirmation + FULL outcome eligible set. Missing is never failure.',
          'independence_note':'Multiple first touches may share a retest minute/side and market path; pivot-touch unit is not independent statistical sample.',
          'parity_note':'This historical overlay replays canonical AST helpers. Full online scanner vs replay parity remains unverified.',
          'same_bar_note':'Favorable target and swing invalidation in same 1m candle => AMBIGUOUS_1M, not a win.',
          'parallel_race':'race_* uses strict wick invalidation; race_close_band_* uses close beyond 0.10% adverse band. Same-minute target+adverse close is conservatively AMBIGUOUS_1M.',
          'files':{k:str(x) for k,x in files.items()}}
    files['manifest'].write_text(json.dumps(meta,indent=2,ensure_ascii=False),encoding='utf-8')
    print('FIRST_TOUCH_CONFIRMATION_V2 '+json.dumps({
        'symbol':symbol,'first_touches':len(originals),'by_original_status':dict(statuses),
        'original_unique_reactions':len(uniq_reactions),'rules':len(list(v1.rules()))},ensure_ascii=False))
    view=summary[(summary.side_group=='ALL')&(summary.original_status_group=='ALL')&
                 (summary.horizon_min==30)]
    columns=['confirmation_min','policy','min_displacement_pct','eligible_complete_outcomes',
             'selected_complete_outcomes','selected_rescued_indecisive_n',
             'selected_reclaimed_failed_n','win_0_5_n','win_0_5_pct_selected',
             'win_0_5_pct_all_eligible']
    print('\n30m +0.5% BEFORE WICK LOSS · by 1m vs delayed confirmation:')
    print(view[columns].to_string(index=False))
    print('OUTPUT',*[str(p) for p in files.values()])


if __name__=='__main__':main()
