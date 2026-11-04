#!/usr/bin/env python3
"""REACTION research V2: BOTH SHORT->bullish breakout and LONG->bearish breakdown.

Read-only analysis of the original 15m/3x3 first retests, using the canonical
scanner helpers extracted by the existing verified HLL replay. Does not change
existing scanner/labels, does not infer trade entries or HLL horizontal levels.

The adverse-pressure features are known as of the initial retest 1m CLOSE.
The independent opposite-direction candidate requires two CLOSED 1m bars
beyond swing +/-0.10%. Post-cutoff candidates are retrospective outcomes only.
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
import compare_first_touch_confirmations_v2 as v2
from audit_reaction_defenses_v1 import file_sha256

VERSION = 'REACTION_BIDIRECTIONAL_BREAKOUT_AUDIT_V2'
PRE_WINDOWS = (15, 30, 60)
OUTCOME_HORIZONS = (30, 60)
BREAKOUT_SCAN_MIN = 60
BREAKOUT_BAND_PCT = 0.10
BREAKOUT_CONFIRM_BARS = 2
TARGET_PCT = 0.50
SIDES = ('SHORT', 'LONG')


def opposite(side: str) -> str:
    if side not in SIDES:
        raise ValueError('Unsupported side: '+str(side))
    return 'LONG' if side == 'SHORT' else 'SHORT'


def causal_context(tape: pd.DataFrame, known_ts: int, swing: float, side: str) -> dict:
    """Mirror-symmetric adverse-pressure diagnostics; NEVER use an unclosed bar.

    Against SHORT => upward pressure toward HIGH; against LONG => downward
    pressure toward LOW. Flags are diagnostic hypotheses, NOT trained vetoes.
    """
    opposite(side)
    result = {'context_known_ts':int(known_ts), 'adverse_pressure_direction':opposite(side)}
    for n in PRE_WINDOWS:
        bars,coverage = v1.observed_segment(tape,int(known_ts)-n*v1.TF1,n)
        result[f'context_{n}m_coverage'] = coverage
        if coverage != 'COMPLETE':
            result[f'return_{n}m_pct'] = None
            result[f'adverse_return_{n}m_pct'] = None
            if n==30:
                result.update({'range_position_30m':None,'structure_lift_against_original_30m_pct':None,
                               'near_swing_minutes_30m':None})
            continue
        first_open=float(bars.iloc[0]['open'])
        last_close=float(bars.iloc[-1]['close'])
        raw_ret=(last_close/first_open-1)*100
        result[f'return_{n}m_pct'] = raw_ret
        result[f'adverse_return_{n}m_pct'] = raw_ret*(1 if side=='SHORT' else -1)
        if n==30:
            lo=float(bars.low.min()); hi=float(bars.high.max())
            position=(last_close-lo)/(hi-lo) if hi>lo else .5
            result['range_position_30m'] = position
            if side=='SHORT':
                first_extreme=float(bars.iloc[:15].low.min())
                last_extreme=float(bars.iloc[15:].low.min())
                # Higher lows suggest adverse buying pressure against the SHORT.
                lift=(last_extreme/first_extreme-1)*100
                wick=bars.high.to_numpy(dtype=float)
            else:
                first_extreme=float(bars.iloc[:15].high.max())
                last_extreme=float(bars.iloc[15:].high.max())
                # Lower highs suggest adverse selling pressure against the LONG.
                lift=(1-last_extreme/first_extreme)*100
                wick=bars.low.to_numpy(dtype=float)
            result['structure_lift_against_original_30m_pct']=lift
            result['near_swing_minutes_30m']=int(np.count_nonzero(
                (wick >= swing*(1-BREAKOUT_BAND_PCT/100)) &
                (wick <= swing*(1+BREAKOUT_BAND_PCT/100))))
    if any(result[f'context_{n}m_coverage']!='COMPLETE' for n in (30,60)):
        result.update({key:None for key in ('pressure_adverse_structure','pressure_adverse_momentum',
                          'pressure_repeated_tests','pressure_any','pressure_two_of_three')})
    else:
        adverse_return=result['adverse_return_30m_pct']
        rising=(result['structure_lift_against_original_30m_pct']>=0.05 and adverse_return>0)
        extreme_position=(result['range_position_30m']>=.70 if side=='SHORT'
                          else result['range_position_30m']<=.30)
        momentum=(adverse_return>=.20 and extreme_position)
        repeated=(result['near_swing_minutes_30m']>=3 and adverse_return>0)
        result.update({'pressure_adverse_structure':bool(rising),
                       'pressure_adverse_momentum':bool(momentum),
                       'pressure_repeated_tests':bool(repeated),
                       'pressure_any':bool(rising or momentum or repeated),
                       'pressure_two_of_three':bool(sum((rising,momentum,repeated))>=2)})
    return result


def first_opposite_breakout(tape: pd.DataFrame, from_known_ts: int, swing: float,
                            original_side: str, scan_minutes: int=BREAKOUT_SCAN_MIN,
                            band_pct: float=BREAKOUT_BAND_PCT,
                            consecutive_closes: int=BREAKOUT_CONFIRM_BARS) -> dict:
    """Independent opposite candidate from TWO consecutive closed 1m bars.

    SHORT retest HIGH -> upward break: 2 closes > HIGH*(1+band%).
    LONG retest LOW  -> downward break: 2 closes < LOW*(1-band%).
    Report unknown if entire scan is not contiguous/complete (conservative).
    """
    other=opposite(original_side)
    if not 0<consecutive_closes<=scan_minutes:
        raise ValueError('Bad confirmation window')
    bars,coverage=v1.observed_segment(tape,int(from_known_ts),scan_minutes)
    result={'opposite_breakout_scan_coverage':coverage,
            'opposite_breakout_side':other,
            'opposite_breakout_confirmed':None,'opposite_breakout_confirm_ts':None,
            'opposite_breakout_delay_min':None}
    if coverage!='COMPLETE': return result
    threshold=swing*(1+band_pct/100) if other=='LONG' else swing*(1-band_pct/100)
    streak=0
    for bar in bars.itertuples(index=False):
        good=(float(bar.close)>threshold if other=='LONG' else float(bar.close)<threshold)
        streak=streak+1 if good else 0
        if streak>=consecutive_closes:
            t=int(bar.timestamp)+v1.TF1
            result.update({'opposite_breakout_confirmed':True,
                           'opposite_breakout_confirm_ts':t,
                           'opposite_breakout_delay_min':(t-int(from_known_ts))/v1.TF1})
            return result
    result['opposite_breakout_confirmed']=False
    return result


def outcome_at(tape: pd.DataFrame, from_ts: int, side: str, swing: float,
               horizon: int, prefix: str) -> dict:
    """Hypothetical next OPEN after confirmation; no claim of execution."""
    opposite(side)
    bars,coverage=v1.observed_segment(tape,int(from_ts),horizon)
    r={prefix+'coverage':coverage}
    if coverage!='COMPLETE':return r
    entry=float(bars.iloc[0]['open'])
    r.update({prefix+k:v for k,v in v1.race_and_outcome(bars,side,swing,entry).items()})
    r[prefix+'race_close_band_0_5']=v2.chronological_race_close_band(
        bars,side,swing,entry,TARGET_PCT)
    return r


def analyze_touch(tape: pd.DataFrame, event: dict, pre_cutoff_ms: int) -> dict:
    """Original side may be LONG or SHORT; label and timestamp are immutable."""
    side=event['side'];other=opposite(side)
    known=int(event['known_ts'])
    if known>int(pre_cutoff_ms):
        raise ValueError('Original first retest not known by PRE cutoff')
    if known != int(event['retest_ts'])+v1.TF1:
        raise ValueError('Original 1m retest known timestamp mismatch')
    swing=float(event['swing_price'])
    result={'symbol':event['symbol'],'pivot_touch_id':v2.pivot_touch_key(event),
            'event_key':event['event_key'],'original_side':side,'opposite_side':other,
            'original_swing_kind':'HIGH' if side=='SHORT' else 'LOW',
            'opposite_scenario':'BULLISH_BREAKOUT' if side=='SHORT' else 'BEARISH_BREAKDOWN',
            'original_status':event['status'],'pivot_ts':int(event['pivot_ts']),
            'retest_ts':int(event['retest_ts']),'original_known_ts':known,
            'original_swing_price':swing,'prominence_pct':float(event['prominence_pct']),
            'pre_cutoff_ms':int(pre_cutoff_ms),'original_reaction':event['status']=='REACTION'}
    # Causal inputs only. No future candles of POST can affect pressure or 5m decision.
    pre_tape=tape.loc[tape.timestamp<int(pre_cutoff_ms)].reset_index(drop=True)
    result.update(causal_context(pre_tape,known,swing,side))
    delayed=v2.candidate_pre_only(pre_tape,event,5,'CLOSE_HOLD',0.10,pre_cutoff_ms)
    result.update({'five_min_confirmed_pre':delayed['confirmed'],
                   'five_min_coverage':delayed['confirmation_coverage'],
                   'five_min_confirm_ts':delayed['confirmed_ts'],
                   'five_min_pre_eligible':delayed['pre_eligible'],
                   'five_min_initial_wick_breach':delayed['t0_wick_beyond_swing'],
                   'five_min_close_wrong_side_at_touch':not delayed['t0_closed_expected_side']})
    for h in OUTCOME_HORIZONS:
        result.update(outcome_at(tape,known,side,swing,h,f'original_{h}m_'))
    result.update(first_opposite_breakout(tape,known,swing,side))
    if result['opposite_breakout_confirmed'] is True:
        ts=int(result['opposite_breakout_confirm_ts'])
        n=(ts-known)//v1.TF1
        between,coverage=v1.observed_segment(tape,known,n)
        result['original_race_to_opposite_coverage']=coverage
        result['original_race_before_opposite_breakout']=(v1.race_and_outcome(
            between,side,swing,float(between.iloc[0]['open']))['race_0_5']
            if coverage=='COMPLETE' else None)
        result['opposite_breakout_phase']='PRE' if ts<=pre_cutoff_ms else 'POST_OUTCOME_ONLY'
        for h in OUTCOME_HORIZONS:
            result.update(outcome_at(tape,ts,other,swing,h,f'opposite_{h}m_'))
    else:
        result['opposite_breakout_phase']='NOT_CONFIRMED_OR_CENSORED'
        result['original_race_to_opposite_coverage']='NOT_APPLICABLE'
        result['original_race_before_opposite_breakout']=None
    return result


def _pct(n,d):return round(n/d*100,2) if d else None


def summarize(details: pd.DataFrame) -> pd.DataFrame:
    """Side-stratified, coverage-aware descriptive groups. No pooled LONG/SHORT edge claims."""
    records=[]
    flags=('ALL','pressure_adverse_structure','pressure_adverse_momentum',
           'pressure_repeated_tests','pressure_any','pressure_two_of_three')
    for side in SIDES:
        sides=details.loc[details.original_side==side]
        for source in ('ALL_FIRST_TOUCH','ORIGINAL_REACTION','OTHER_FIRST_TOUCH'):
            s=(sides if source=='ALL_FIRST_TOUCH' else
               sides.loc[sides.original_status=='REACTION' if source=='ORIGINAL_REACTION'
                         else sides.original_status!='REACTION'])
            for flag in flags:
                for value in ([None] if flag=='ALL' else [True,False]):
                    z=s if flag=='ALL' else s.loc[s[flag]==value]
                    if z.empty:continue
                    row={'original_side':side,'opposite_side':opposite(side),
                         'cohort':source,'flag':flag,'flag_value':value,
                         'first_touch_n':len(z),'unique_retest_minutes':z.retest_ts.nunique(),
                         'original_reaction_n':int((z.original_status=='REACTION').sum())}
                    for h in OUTCOME_HORIZONS:
                        available=z.loc[z[f'original_{h}m_coverage']=='COMPLETE']
                        wins=int((available[f'original_{h}m_race_0_5']=='FAVORABLE_FIRST').sum())
                        band=int((available[f'original_{h}m_race_close_band_0_5']=='FAVORABLE_FIRST').sum())
                        row.update({f'original_{h}m_complete':len(available),
                                    f'original_{h}m_win_wick_n':wins,
                                    f'original_{h}m_win_wick_pct':_pct(wins,len(available)),
                                    f'original_{h}m_win_close_band_n':band,
                                    f'original_{h}m_win_close_band_pct':_pct(band,len(available))})
                    bc=z.loc[z.opposite_breakout_scan_coverage=='COMPLETE']
                    n=int((bc.opposite_breakout_confirmed==True).sum())
                    row.update({'opposite_breakout_scan_complete':len(bc),
                                'opposite_breakout_n':n,
                                'opposite_breakout_pct':_pct(n,len(bc)),
                                'opposite_breakout_pre_n':int((bc.opposite_breakout_phase=='PRE').sum()),
                                'opposite_breakout_post_n':int((bc.opposite_breakout_phase=='POST_OUTCOME_ONLY').sum())})
                    field='opposite_30m_coverage'
                    opp=z.loc[z[field]=='COMPLETE'] if field in z else z.iloc[0:0]
                    row['opposite_30m_complete']=len(opp)
                    row['opposite_30m_win_wick_n']=int((opp['opposite_30m_race_0_5']=='FAVORABLE_FIRST').sum()) if len(opp) else 0
                    records.append(row)
    return pd.DataFrame(records)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',required=True)
    p.add_argument('--symbol',required=True)
    p.add_argument('--pre-cutoff-utc',required=True)
    p.add_argument('--pre-candles',type=int,default=96,choices=(48,96,144,192,288,384))
    p.add_argument('--expected-first-touches',type=int,required=True)
    p.add_argument('--expected-reactions',type=int,required=True)
    p.add_argument('--out',required=True)
    args=p.parse_args(argv)
    root=Path(args.root).resolve(); symbol=v2.symbol_name(args.symbol)
    out=Path(args.out).resolve(); base=root/'reports'/'reaction_bidirectional_breakout_audit_v2'
    if not out.is_relative_to(base) or out==base:
        raise ValueError('Use an isolated --out directory below '+str(base))
    output={'events':out/(symbol+'_bidirectional_events.csv'),
            'summary':out/(symbol+'_bidirectional_summary.csv'),
            'manifest':out/(symbol+'_bidirectional_manifest.json')}
    if any(path.exists() for path in output.values()):
        raise FileExistsError('Refusing to overwrite an existing audit result')
    if str(root) not in sys.path:sys.path.insert(0,str(root))
    overlay=importlib.import_module('dashboard.views.hll_reaction_pre_overlay')
    app=root/'dashboard/app.py'
    tape_file=root/overlay.MINUTE_DIR/(symbol+'_1m.csv')
    manifest_file=root/overlay.MANIFEST_DIR/(symbol+'.json')
    tape=overlay.load_verified_minute(tape_file,manifest_file)
    cutoff=v1.parse_utc(args.pre_cutoff_utc)
    env,swing_class,source_hash=overlay.canonical_functions(str(app),app.stat().st_mtime_ns)
    events=overlay.replay_canonical_pre(
        tape,cutoff,cutoff-args.pre_candles*overlay.TF30,
        env=env,swing_detector_class=swing_class,symbol=symbol,
        left_bars=3,right_bars=3,min_prominence_pct=0.0,include_first_retests=True)
    reactions={e['event_key'] for e in events if e['status']=='REACTION'}
    if len(events)!=args.expected_first_touches or len(reactions)!=args.expected_reactions:
        raise RuntimeError('Original detector baseline mismatch: do not interpret outputs')
    if not events:raise ValueError('No first retests in requested PRE window')
    rows=pd.DataFrame([analyze_touch(tape,e,cutoff) for e in events])
    report=summarize(rows)
    out.mkdir(parents=True,exist_ok=True)
    rows.to_csv(output['events'],index=False)
    report.to_csv(output['summary'],index=False)
    inputs=[app,tape_file,manifest_file,root/'engine/live/research/swing_detector.py',
            Path(overlay.__file__),root/'compare_reaction_confirmations_v1.py',
            root/'compare_first_touch_confirmations_v2.py']
    manifest={'version':VERSION,'created_utc':datetime.now(timezone.utc).isoformat(),
              'symbol':symbol,'cutoff':args.pre_cutoff_utc,'pre_candles':args.pre_candles,
              'all_first_touches':len(events),'all_original_reactions':len(reactions),
              'side_x_status':{s:dict(Counter(e['status'] for e in events if e['side']==s)) for s in SIDES},
              'canonical_app_functions_hash':source_hash,
              'source_sha256':{str(f.relative_to(root)) if f.is_relative_to(root) else str(f):file_sha256(f) for f in inputs},
              'script_sha256':file_sha256(Path(__file__).resolve()),
              'git_head':subprocess.run(['git','rev-parse','HEAD'],cwd=root,capture_output=True,text=True).stdout.strip(),
              'baseline':'Original 15m/3x3/0% first touches; statuses immutable.',
              'pressure':'Adverse directional 1m context known by ORIGINAL retest close only; not HLL levels, not a fitted veto.',
              'breakout':'2 consecutive CLOSED 1m bars > swing HIGH+0.10% (SHORT->LONG) or < swing LOW-0.10% (LONG->SHORT), scan 60m.',
              'phase':'Only opposite candidates confirmed by PRE cutoff can be PRE; later ones POST_OUTCOME_ONLY.',
              'outcomes':'Original and opposite post-confirmation +0.5% vs swing invalidation 30/60m; next OPEN; wick and close band separately.',
              'unknown':'Incomplete 1m coverage explicitly censored; same-bar order ambiguous, never credited winner.',
              'validation':'NOT online/full scanner parity proof; NOT a live strategy, and not an HLL level breakout.',
              'outputs':{k:str(v) for k,v in output.items()}}
    output['manifest'].write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
    print('REACTION_BIDIRECTIONAL_BREAKOUT_AUDIT '+json.dumps({
        'symbol':symbol,'first_touches':len(events),'original_reactions':len(reactions),
        'original_side_counts':dict(Counter(e['side'] for e in events)),
        'opposite_60m_confirmations':dict(Counter(rows.loc[rows.opposite_breakout_confirmed==True,'original_side'])),
        'unknown_pressure':int(rows.pressure_any.isna().sum())}))
    print(report.loc[(report.cohort=='ORIGINAL_REACTION')&(report.flag=='ALL'),
           ['original_side','first_touch_n','original_30m_complete','original_30m_win_wick_n',
            'opposite_breakout_scan_complete','opposite_breakout_n']].to_string(index=False))
    print('OUTPUT',*[str(f) for f in output.values()])


if __name__=='__main__':main()
