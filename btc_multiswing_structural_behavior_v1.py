#!/usr/bin/env python3
"""BTC Multi-Swing Structural Behavior V1 | 15m, offline, read-only.

Reuses hash-verified 60D 1m candles and the immutable V2.4A *birth-band*
visit index. A visit is NOT assumed to touch the causal V2 stage: eligibility
requires a stage drawable at the 1m contact open AND an actual intersection of
that 1m candle with the frozen-as-of-PRE stage.

For an eligible arrival, geometry is frozen AT PRE. All behavioral transitions
are stamped at their 15m *closing time* and never placed in PRE features.
Post-contact 15m bars can establish departure, two-close breakout, reentry,
failed breakout, retest, and retest hold; they are descriptions, not signals.
A 15m OHLC cannot establish intrabar order. Visits and bands can overlap.
No HLL, REACTION, Streamlit, online requests or live trading imports.
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from btc_multiswing_60d_explorer_v1 import M, aggregate_closed, digest, iso, load_snapshot
from btc_multiswing_arrival_lab_v1 import (
    DEFAULT_PARENT, _last_confirmed_stage, _parent_run, _validate_and_reconstruct,
)

VERSION = 'btc-multiswing-structural-behavior-15m-v1'
DEFAULT_OUTPUT = Path('reports/reaction_lab/btc_multiswing_60d_v1/btc_sep09_nov08_structural_behavior_15m_v1')
TF_MINUTES = 15
TF = TF_MINUTES * M
HORIZON_BARS = 96  # initial contact 15m close plus the following 95 15m closes
DEPARTURE_PCT = .15  # descriptive distance from the near band edge, not an entry rule
INSIDE_CLOSES = 3
MAX_STAGE_AGE_MINUTES = 7 * 1440
PRE_COLUMNS = [
    'event_id','region_id','visit_number','side','birth_touch_open_ts','pre_cutoff_ts',
    'pre_cutoff_utc','birth_known_ts','birth_band_low','birth_band_high',
    'source_kind','stage_at_pre','pre_stage_status','stage_known_from',
    'stage_extrema','stage_low','stage_high','stage_age_minutes',
    'stage_differs_from_birth','pre_close_1m','approach_position',
    'conventional_approach','pre_last_15m_close_ts','pre_last_15m_close',
    'prior_completed_birth_visits_asof','causality_note',
]
TRANSITION_COLUMNS = [
    'event_id','region_id','visit_number','transition_id','event_type',
    'confirmed_ts','confirmed_utc','bar_open_ts','bar_close','close_position',
    'approach_position','band_low_pre','band_high_pre','source_tf',
    'structural_reference','ambiguity_note',
]
OUTCOME_COLUMNS = [
    'event_id','region_id','visit_number','eligibility','touch_1m_confirmed_ts',
    'first_15m_confirmed_ts','horizon_target_ts','last_15m_confirmed_ts',
    'observed_15m_bars','horizon_status','first_resolution','first_resolution_ts',
    'first_breakout_ts','first_defense_ts','first_failed_breakout_ts',
    'first_retest_touch_ts','first_retest_hold_ts','first_reentry_ts',
    'first_wick_reversal_ts',
    'breakout_count','defense_count','failed_breakout_count',
    'retest_touch_count','retest_hold_count','inside_acceptance_count',
    'wick_reversal_count',
    'max_above_pre_band_pct','max_below_pre_band_pct',
    'max_up_from_pre_close_pct','max_down_from_pre_close_pct',
    'last_close_position','overlaps_next_birth_visit_in_horizon',
    'reference_note',
]


def _pos(price:float, low:float, high:float) -> str:
    return 'ABOVE' if price > high else ('BELOW' if price < low else 'INSIDE')


def _opposite(side:str) -> str:
    return 'ABOVE' if side == 'BELOW' else 'BELOW'


def _validate_bars(bars:pd.DataFrame, *, source_end:int) -> pd.DataFrame:
    cols={'timestamp','open','high','low','close'}
    if not cols.issubset(bars.columns) or bars.empty or source_end % TF:
        raise ValueError('BEHAVIOR_INVALID_15M_SCHEMA')
    frame=bars.sort_values('timestamp',kind='mergesort').reset_index(drop=True)
    ts=frame.timestamp.to_numpy(dtype=np.int64)
    if (np.any(ts % TF) or (len(ts)>1 and np.any(np.diff(ts)!=TF))
            or ts[-1]+TF < source_end):
        raise ValueError('BEHAVIOR_NONCONTIGUOUS_15M')
    a=frame[['open','high','low','close']].to_numpy(dtype=float)
    if (not np.isfinite(a).all() or np.any(a<=0)
            or np.any(a[:,1]<np.maximum.reduce([a[:,0],a[:,2],a[:,3]]))
            or np.any(a[:,2]>np.minimum.reduce([a[:,0],a[:,1],a[:,3]]))):
        raise ValueError('BEHAVIOR_INVALID_15M_OHLC')
    return frame


def analyze_15m_sequence(bars:pd.DataFrame, *, event_id:str, region_id:str,
                         visit_number:int, touch_open:int, source_end:int,
                         side:str, approach:str, band_low:float, band_high:float,
                         pre_close:float, horizon_bars:int=HORIZON_BARS,
                         departure_pct:float=DEPARTURE_PCT):
    """Pure chronological states. A bar is usable ONLY after it has CLOSED.

    First touching 15m candle may contain earlier minutes: its OHLC can be
    used as a POST descriptive maximum, but never for a sweep/retest ordering.
    Two consecutive closed 15m bars are required for a canonical breakout,
    defense, failed breakout or retest hold. An INSIDE close breaks the run.
    """
    if (side not in ('HIGH','LOW') or approach not in ('ABOVE','BELOW','INSIDE')
        or not (0<band_low<=band_high and pre_close>0)
        or source_end%TF or touch_open%M or touch_open>=source_end
        or type(horizon_bars) is not int or not 1<=horizon_bars<=192
        or not 0<departure_pct<=2):
        raise ValueError('BEHAVIOR_INVALID_PARAMETERS')
    first_close=(touch_open//TF+1)*TF
    target=first_close+(horizon_bars-1)*TF
    ordered=_validate_bars(bars,source_end=source_end)
    follow=ordered.loc[(ordered.timestamp+TF>=first_close) &
                       (ordered.timestamp+TF<=min(source_end,target))].copy()
    if follow.empty or int(follow.timestamp.iloc[0])+TF != first_close:
        raise ValueError('BEHAVIOR_CONTACT_15M_BAR_UNAVAILABLE')
    times=follow.timestamp.to_numpy(dtype=np.int64)+TF
    if len(times)>1 and np.any(np.diff(times)!=TF):
        raise ValueError('BEHAVIOR_GAPPED_FOLLOWUP')
    reference = 'PRE_STAGE_LOCKED_REFERENCE_ONLY_NOT_ACTIVE'
    expected = 'BELOW' if side=='HIGH' else 'ABOVE'
    conventional = approach==expected
    far=_opposite(expected)
    emit_rows=[]
    types=set()
    side_run=0; origin_run=0; far_run=0; inside_run=0
    break_active=False; broke_away=False; retest_pending=False
    failed=False; returned_once=False
    previous_position=approach

    def emit(kind:str, i:int, ambiguous:bool=False):
        t=int(times[i]); key=(kind,t)
        if key in types: return
        types.add(key)
        bar=follow.iloc[i]
        emit_rows.append({
            'event_id':event_id,'region_id':region_id,'visit_number':visit_number,
            'transition_id':f'{event_id}|{kind}|{t}', 'event_type':kind,
            'confirmed_ts':t,'confirmed_utc':iso(t),
            'bar_open_ts':int(bar.timestamp), 'bar_close':float(bar.close),
            'close_position':_pos(float(bar.close),band_low,band_high),
            'approach_position':approach,'band_low_pre':band_low,
            'band_high_pre':band_high,'source_tf':'15m_CLOSED',
            'structural_reference':reference,
            'ambiguity_note':('15M_OHLC_INTRABAR_ORDER_UNKNOWN' if ambiguous
                              else 'CLOSE_CONFIRMED_AT_BAR_END'),
        })

    for i,row in enumerate(follow.itertuples(index=False)):
        close=float(row.close)
        position=_pos(close,band_low,band_high)
        if position!=previous_position:
            emit('CLOSE_'+position, i)
        inside_run=inside_run+1 if position=='INSIDE' else 0
        if inside_run==INSIDE_CLOSES:
            emit('INSIDE_3_CLOSES',i)
        # 'Wick reversal' is a descriptive OHLC shape, not a claim about the
        # sequence of trades within the 15m candle. The initial bar may start
        # before the touch-open; skip its wick unless it starts exactly there.
        if conventional and (i>0 or touch_open%TF==0):
            wick_reversal=(side=='HIGH' and float(row.high)>band_high and
                           position=='BELOW') or (side=='LOW' and
                           float(row.low)<band_low and position=='ABOVE')
            if wick_reversal:
                emit('WICK_REVERSAL_15M_DESCRIPTIVE',i,ambiguous=True)

        # Outside-position counts reset at every contradictory/inside close.
        if conventional:
            distance=(band_low-close)/band_low*100 if expected=='BELOW' else (close-band_high)/band_high*100
            away=position==expected and distance>=departure_pct
            side_run=side_run+1 if away else 0
            origin_run=origin_run+1 if position==expected else 0
            far_run=far_run+1 if position==far else 0
            if side_run==2:
                emit('DEFENSE_2_CLOSES_WITH_DEPARTURE',i)
            if far_run==2 and not break_active:
                emit('BREAKOUT_2_CLOSES',i)
                break_active=True;broke_away=False;retest_pending=False;failed=False;returned_once=False
            if break_active:
                if position==expected and not returned_once:
                    emit('REENTRY_ORIGINAL_SIDE',i)
                    returned_once=True
                if origin_run==2 and not failed:
                    emit('FAILED_BREAKOUT_2_CLOSES',i)
                    break_active=False; failed=True;retest_pending=False
                else:
                    # A genuine retest needs at least one entire 15m candle
                    # outside the far edge AFTER confirmation. The touching
                    # candle may close INSIDE: do not require a far close yet.
                    contact_bar=(float(row.high)>=band_low and float(row.low)<=band_high)
                    if not retest_pending and i>0 and broke_away and contact_bar:
                        emit('RETEST_TOUCH_15M',i,ambiguous=True)
                        retest_pending=True;broke_away=False
                    elif retest_pending and far_run>=2:
                        emit('RETEST_HOLD_2_CLOSES',i)
                        retest_pending=False
                    if not contact_bar and position==far:
                        broke_away=True
        previous_position=position

    trans=pd.DataFrame(emit_rows,columns=TRANSITION_COLUMNS)
    def first(kind):
        s=trans.loc[trans.event_type.eq(kind),'confirmed_ts']
        return int(s.iloc[0]) if len(s) else None
    fb=first('BREAKOUT_2_CLOSES')
    fd=first('DEFENSE_2_CLOSES_WITH_DEPARTURE')
    if fb is not None and (fd is None or fb<fd):
        resolution='BREAKOUT_2_CLOSES';resolution_ts=fb
    elif fd is not None:
        resolution='DEFENSE_2_CLOSES_WITH_DEPARTURE';resolution_ts=fd
    else:
        resolution='UNRESOLVED';resolution_ts=None
    full_horizon=len(follow)==horizon_bars
    hi=float(follow.high.max());lo=float(follow.low.min())
    result={
        'event_id':event_id,'region_id':region_id,'visit_number':visit_number,
        'eligibility':'ELIGIBLE_CURRENT_STAGE_1M_TOUCH',
        'touch_1m_confirmed_ts':touch_open+M,
        'first_15m_confirmed_ts':first_close,
        'horizon_target_ts':target,'last_15m_confirmed_ts':int(times[-1]),
        'observed_15m_bars':len(follow),
        'horizon_status':'FULL_HORIZON' if full_horizon else 'FREEZE_CENSORED',
        'first_resolution':resolution,'first_resolution_ts':resolution_ts,
        'first_breakout_ts':fb,'first_defense_ts':fd,
        'first_failed_breakout_ts':first('FAILED_BREAKOUT_2_CLOSES'),
        'first_retest_touch_ts':first('RETEST_TOUCH_15M'),
        'first_retest_hold_ts':first('RETEST_HOLD_2_CLOSES'),
        'first_reentry_ts':first('REENTRY_ORIGINAL_SIDE'),
        'first_wick_reversal_ts':first('WICK_REVERSAL_15M_DESCRIPTIVE'),
        'breakout_count':int(trans.event_type.eq('BREAKOUT_2_CLOSES').sum()),
        'defense_count':int(trans.event_type.eq('DEFENSE_2_CLOSES_WITH_DEPARTURE').sum()),
        'failed_breakout_count':int(trans.event_type.eq('FAILED_BREAKOUT_2_CLOSES').sum()),
        'retest_touch_count':int(trans.event_type.eq('RETEST_TOUCH_15M').sum()),
        'retest_hold_count':int(trans.event_type.eq('RETEST_HOLD_2_CLOSES').sum()),
        'inside_acceptance_count':int(trans.event_type.eq('INSIDE_3_CLOSES').sum()),
        'wick_reversal_count':int(trans.event_type.eq('WICK_REVERSAL_15M_DESCRIPTIVE').sum()),
        'max_above_pre_band_pct':max(0,(hi-band_high)/band_high*100),
        'max_below_pre_band_pct':max(0,(band_low-lo)/band_low*100),
        'max_up_from_pre_close_pct':max(0,(hi-pre_close)/pre_close*100),
        'max_down_from_pre_close_pct':max(0,(pre_close-lo)/pre_close*100),
        'last_close_position':_pos(float(follow.close.iloc[-1]),band_low,band_high),
        'overlaps_next_birth_visit_in_horizon':None,
        'reference_note':'POST DESCRIPTIVE ONLY; PRE STAGE FIXED; 15M OHLC NO INTRABAR ORDER',
    }
    if not conventional:
        result['first_resolution']='NONCONVENTIONAL_APPROACH_NOT_RATED'
        result['first_resolution_ts']=None
    return trans,result


def build_behavior_dataset(full:pd.DataFrame,regions:pd.DataFrame,
                           visits:pd.DataFrame,stages:pd.DataFrame,
                           source_start:int,source_end:int,
                           *,horizon_bars:int=HORIZON_BARS,
                           departure_pct:float=DEPARTURE_PCT,
                           max_stage_age_minutes:int=MAX_STAGE_AGE_MINUTES):
    """All V2.4A visits are retained, even when no current V2 stage exists."""
    if (not all(k in full for k in ('timestamp','open','high','low','close')) or
        not all(k in regions for k in ('region_id','side','birth_known_ts','band_low_birth','band_high_birth')) or
        not all(k in visits for k in ('region_id','visit_number','first_touch_ts','end_known_ts')) or
        not all(k in stages for k in ('region_id','known_from','known_until','band_low','band_high','independent_extrema'))):
        raise ValueError('BEHAVIOR_MISSING_SOURCE_SCHEMA')
    if (source_start%M or source_end%TF or source_end<=source_start or
        type(max_stage_age_minutes) is not int or max_stage_age_minutes<=0):
        raise ValueError('BEHAVIOR_INVALID_CUTOFF')
    frame=full.sort_values('timestamp',kind='mergesort').reset_index(drop=True)
    mins=frame.timestamp.to_numpy(dtype=np.int64)
    if (len(mins)<2 or np.any(np.diff(mins)!=M) or mins[-1]+M!=source_end or mins[0]>source_start):
        raise ValueError('BEHAVIOR_NONCONTIGUOUS_1M')
    ohlc=frame[['open','high','low','close']].to_numpy(dtype=float)
    if (not np.isfinite(ohlc).all() or (ohlc<=0).any() or
        np.any(ohlc[:,1]<np.maximum.reduce([ohlc[:,0],ohlc[:,2],ohlc[:,3]])) or
        np.any(ohlc[:,2]>np.minimum.reduce([ohlc[:,0],ohlc[:,1],ohlc[:,3]]))):
        raise ValueError('BEHAVIOR_INVALID_1M_OHLC')
    fifteen=aggregate_closed(frame,'15m',int(mins[0]),source_end)
    fifteen=_validate_bars(fifteen,source_end=source_end)
    # no forward bar beyond freeze, and every open-side close is computed only
    # from already CLOSED 1m candles.
    region_index=regions.set_index('region_id')
    stage_groups={str(rid):g.sort_values(['known_from','independent_extrema'],
                             kind='mergesort') for rid,g in stages.groupby('region_id')}
    sorted_visits=visits.sort_values(['region_id','first_touch_ts','visit_number'],
                                   kind='mergesort').reset_index(drop=True)
    pre_rows=[]; transition_frames=[];out_rows=[]
    for rid,g in sorted_visits.groupby('region_id',sort=True):
        if rid not in region_index.index:
            raise ValueError('BEHAVIOR_UNKNOWN_PARENT_REGION')
        r=region_index.loc[rid]
        side=str(r['side'])
        if side not in ('HIGH','LOW'):
            raise ValueError('BEHAVIOR_INVALID_REGION_SIDE')
        birth=int(r['birth_known_ts'])
        group=stage_groups.get(str(rid),pd.DataFrame())
        previous=[]
        all_g=list(g.itertuples(index=False))
        for j,v in enumerate(all_g):
            t=int(v.first_touch_ts)
            if (t%M or t<birth or t<source_start or t>=source_end or
                    (t-int(mins[0]))%M):
                raise ValueError('BEHAVIOR_INVALID_BIRTH_TOUCH_TS')
            i=(t-int(mins[0]))//M
            if i<1 or i>=len(frame):
                raise ValueError('BEHAVIOR_INVALID_CONTACT_INDEX')
            pre_close=float(frame.close.iat[i-1])
            stage=(_last_confirmed_stage(group,t,max_age_minutes=max_stage_age_minutes)
                   if not group.empty else None)
            low=float(stage.band_low) if stage is not None else None
            high=float(stage.band_high) if stage is not None else None
            actual_touch=bool(stage is not None and float(frame.high.iat[i])>=low and
                              float(frame.low.iat[i])<=high)
            if stage is None:
                eligibility='NO_GEOMETRIC_STAGE_AT_PRE'
            elif not actual_touch:
                eligibility='BIRTH_BAND_TOUCH_NOT_CURRENT_STAGE_TOUCH'
            else:
                eligibility='ELIGIBLE_CURRENT_STAGE_1M_TOUCH'
            prior15=fifteen.loc[fifteen.timestamp+TF<=t]
            if prior15.empty:
                raise ValueError('BEHAVIOR_NO_CLOSED_PRE_15M')
            pre15=prior15.iloc[-1]
            approach=_pos(pre_close,low,high) if stage is not None else 'UNKNOWN'
            conventional=approach==('BELOW' if side=='HIGH' else 'ABOVE')
            eid=f'{rid}|V{int(v.visit_number)}|{t}'
            end_known=(None if pd.isna(v.end_known_ts) else int(v.end_known_ts))
            if end_known is not None and end_known<=t:
                raise ValueError('BEHAVIOR_BAD_PARENT_VISIT_END')
            pre_rows.append({
                'event_id':eid,'region_id':rid,'visit_number':int(v.visit_number),
                'side':side,'birth_touch_open_ts':t,'pre_cutoff_ts':t,
                'pre_cutoff_utc':iso(t),'birth_known_ts':birth,
                'birth_band_low':float(r.band_low_birth),
                'birth_band_high':float(r.band_high_birth),
                'source_kind':'RETROSPECTIVE_V24A_FIXED_BIRTH_BAND_FIRST_TOUCH',
                'stage_at_pre':stage is not None,
                'pre_stage_status':('PRE_STAGE_PRESENT_TOUCH_NOT_YET_OBSERVED'
                                    if stage is not None else 'NO_GEOMETRIC_STAGE_AT_PRE'),
                'stage_known_from':int(stage.known_from) if stage is not None else None,
                'stage_extrema':int(stage.independent_extrema) if stage is not None else None,
                'stage_low':low,'stage_high':high,
                'stage_age_minutes':(t-int(stage.known_from))//M if stage is not None else None,
                'stage_differs_from_birth':(abs(low-float(r.band_low_birth))>1e-7 or
                     abs(high-float(r.band_high_birth))>1e-7) if stage is not None else None,
                'pre_close_1m':pre_close,'approach_position':approach,
                'conventional_approach':bool(conventional),
                'pre_last_15m_close_ts':int(pre15.timestamp)+TF,
                'pre_last_15m_close':float(pre15.close),
                'prior_completed_birth_visits_asof':sum(
                    x is not None and x<=t for x in previous),
                'causality_note':'RETROSPECTIVE TOUCH INDEX; CURRENT 1M OHLC EXCLUDED FROM PRE',
            })
            previous.append(end_known)
            if eligibility!='ELIGIBLE_CURRENT_STAGE_1M_TOUCH':
                out_rows.append({
                    'event_id':eid,'region_id':rid,'visit_number':int(v.visit_number),
                    'eligibility':eligibility,'touch_1m_confirmed_ts':None,
                    'first_15m_confirmed_ts':None,'horizon_target_ts':None,
                    'last_15m_confirmed_ts':None,'observed_15m_bars':0,
                    'horizon_status':'NOT_ELIGIBLE',
                    'first_resolution':'NOT_EVALUATED','first_resolution_ts':None,
                    'first_breakout_ts':None,'first_defense_ts':None,
                    'first_failed_breakout_ts':None,'first_retest_touch_ts':None,
                    'first_retest_hold_ts':None,'first_reentry_ts':None,
                    'first_wick_reversal_ts':None,
                    'breakout_count':0,'defense_count':0,'failed_breakout_count':0,
                    'retest_touch_count':0,'retest_hold_count':0,
                    'inside_acceptance_count':0,'wick_reversal_count':0,
                    'max_above_pre_band_pct':None,
                    'max_below_pre_band_pct':None,'max_up_from_pre_close_pct':None,
                    'max_down_from_pre_close_pct':None,'last_close_position':None,
                    'overlaps_next_birth_visit_in_horizon':None,
                    'reference_note':'NON_ELIGIBLE_BIRTH_FIXED_VISIT_NOT_A_CURRENT_STAGE_TOUCH',
                })
                continue
            transitions,out=analyze_15m_sequence(
                fifteen,event_id=eid,region_id=rid,visit_number=int(v.visit_number),
                touch_open=t,source_end=source_end,side=side,approach=approach,
                band_low=low,band_high=high,pre_close=pre_close,
                horizon_bars=horizon_bars,departure_pct=departure_pct)
            nxt=int(all_g[j+1].first_touch_ts) if j+1<len(all_g) else None
            out['overlaps_next_birth_visit_in_horizon']=(
                nxt is not None and nxt<out['horizon_target_ts'])
            transition_frames.append(transitions)
            out_rows.append(out)
    pre=pd.DataFrame(pre_rows,columns=PRE_COLUMNS).sort_values(
        ['pre_cutoff_ts','region_id','visit_number'],kind='mergesort').reset_index(drop=True)
    transitions=pd.concat(transition_frames,ignore_index=True) if transition_frames else pd.DataFrame(columns=TRANSITION_COLUMNS)
    transitions=transitions.sort_values(['confirmed_ts','event_id','event_type'],kind='mergesort').reset_index(drop=True)
    outcomes=pd.DataFrame(out_rows,columns=OUTCOME_COLUMNS).sort_values(
        ['event_id'],kind='mergesort').reset_index(drop=True)
    if (len(pre)!=len(visits) or len(outcomes)!=len(visits)
            or pre.event_id.duplicated().any() or outcomes.event_id.duplicated().any()
            or transitions.transition_id.duplicated().any()
            or not transitions.event_id.isin(pre.event_id).all()):
        raise ValueError('BEHAVIOR_CENSUS_INTEGRITY_FAILURE')
    if not transitions.empty:
        cutoff_by_id=pre.set_index('event_id').pre_cutoff_ts
        if (transitions.confirmed_ts<=transitions.event_id.map(cutoff_by_id)).any():
            raise ValueError('BEHAVIOR_FUTURE_BECAME_PRE')
    count_elig=int(outcomes.eligibility.eq('ELIGIBLE_CURRENT_STAGE_1M_TOUCH').sum())
    summary={
        'version':VERSION,'freeze_start':source_start,'freeze_end_exclusive':source_end,
        'tf':'15m','horizon_bars':horizon_bars,
        'departure_pct_threshold':departure_pct,
        'birth_fixed_visits':len(pre),'eligible_current_stage_contacts':count_elig,
        'excluded_no_pre_stage':int(outcomes.eligibility.eq('NO_GEOMETRIC_STAGE_AT_PRE').sum()),
        'excluded_birth_stage_contact_mismatch':int(outcomes.eligibility.eq('BIRTH_BAND_TOUCH_NOT_CURRENT_STAGE_TOUCH').sum()),
        'nonconventional_approaches':int((outcomes.eligibility.eq('ELIGIBLE_CURRENT_STAGE_1M_TOUCH') & ~pre.set_index('event_id').loc[outcomes.event_id,'conventional_approach'].to_numpy(dtype=bool)).sum()),
        'transitions':len(transitions),
        'full_horizon':int(outcomes.horizon_status.eq('FULL_HORIZON').sum()),
        'right_censored':int(outcomes.horizon_status.eq('FREEZE_CENSORED').sum()),
        'breakouts_2closes':int(transitions.event_type.eq('BREAKOUT_2_CLOSES').sum()),
        'defenses_2closes':int(transitions.event_type.eq('DEFENSE_2_CLOSES_WITH_DEPARTURE').sum()),
        'failed_breakouts_2closes':int(transitions.event_type.eq('FAILED_BREAKOUT_2_CLOSES').sum()),
        'retest_touches_15m':int(transitions.event_type.eq('RETEST_TOUCH_15M').sum()),
        'retest_holds_2closes':int(transitions.event_type.eq('RETEST_HOLD_2_CLOSES').sum()),
        'wick_reversals_15m':int(transitions.event_type.eq('WICK_REVERSAL_15M_DESCRIPTIVE').sum()),
        'limits':[
            'V24A visits are retrospective FIXED BIRTH band contacts, not all possible current-stage contacts',
            'Stage geometry is locked as known immediately before birth-band first touch',
            'Stages are visual geometric candidates, not proven active support/resistance',
            '15m OHLC cannot order highs and lows within one candle',
            'Transition states are only confirmed at bar CLOSE (not at touch-open)',
            'One arrival may generate multiple transitions, not independent samples',
            'Overlapping regions/visits are dependent; no hit-rate or strategy PnL',
            'Thresholds are descriptive starting assumptions, not optimized/validated',
            'Outcomes are recorded even for censored horizons with explicit status',
        ],
    }
    return pre,transitions,outcomes,summary


def _write_immutable(dest:Path,pre:pd.DataFrame,trans:pd.DataFrame,
                     post:pd.DataFrame,summary:dict,source:dict,
                     parent_manifest:dict,parent_path:Path):
    if dest.exists():
        raise FileExistsError('BEHAVIOR_IMMUTABLE_OUTPUT_EXISTS: '+str(dest))
    dest.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ms_behavior15m_v1_',dir=dest.parent) as temp:
        out=Path(temp)
        csvs={'behavior_pre_v1.csv':pre,'behavior_transitions_v1.csv':trans,
              'behavior_outcomes_v1.csv':post}
        for filename,data in csvs.items():
            data.to_csv(out/filename,index=False)
        (out/'behavior_summary_v1.json').write_text(json.dumps(summary,ensure_ascii=False,
                indent=2,sort_keys=True)+'\n',encoding='utf-8')
        names=(*csvs.keys(),'behavior_summary_v1.json')
        manifest={'version':VERSION,'immutable_run':True,'read_only':True,
                  'no_live_imports_or_network':True,'parent_v24a':str(parent_path),
                  'parent_manifest_sha256':digest(parent_path/'manifest.json'),
                  'source_provenance':{k:source.get(k) for k in (
                      'start','end','source_sha256','catalog_sha256','source_hll_parity')},
                  'parent_config':parent_manifest.get('config'),
                  'files_sha256':{name:digest(out/name) for name in names}}
        (out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,
                  indent=2,sort_keys=True)+'\n',encoding='utf-8')
        out.rename(dest)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    m=p.add_mutually_exclusive_group(required=True)
    m.add_argument('--plan',action='store_true')
    m.add_argument('--run',action='store_true')
    p.add_argument('--project-root',type=Path,default=Path('.'))
    p.add_argument('--parent',type=Path,default=DEFAULT_PARENT)
    p.add_argument('--output-dir',type=Path,default=DEFAULT_OUTPUT)
    p.add_argument('--horizon-bars',type=int,default=HORIZON_BARS)
    p.add_argument('--departure-pct',type=float,default=DEPARTURE_PCT)
    a=p.parse_args(argv)
    if (not 1<=a.horizon_bars<=192 or not 0<a.departure_pct<=2 or
        not math.isfinite(a.departure_pct)):
        p.error('invalid horizon/threshold')
    root=a.project_root.resolve()
    parent=a.parent if a.parent.is_absolute() else root/a.parent
    output=a.output_dir if a.output_dir.is_absolute() else root/a.output_dir
    plan={'version':VERSION,'mode':'RUN' if a.run else 'PLAN',
          'project_root':str(root),'parent_v24a':str(parent),
          'output_dir':str(output),'tf':'15m','horizon_bars':a.horizon_bars,
          'departure_pct':a.departure_pct,'no_live':True,
          'pre_post_separate':True}
    print('MS_BEHAVIOR_V1_PLAN',json.dumps(plan,sort_keys=True),flush=True)
    if a.plan:return 0
    if output.exists():
        raise FileExistsError('BEHAVIOR_IMMUTABLE_OUTPUT_EXISTS: '+str(output))
    full,catalog,source=load_snapshot(root)
    regions,visits,manifest,_=_parent_run(parent)
    stages=_validate_and_reconstruct(catalog,source,regions,visits,manifest)
    pre,trans,post,summary=build_behavior_dataset(full,regions,visits,stages,
        source['start'],source['end'],horizon_bars=a.horizon_bars,
        departure_pct=a.departure_pct,
        max_stage_age_minutes=int(manifest['config']['geometry']['lookback_days'])*1440)
    _write_immutable(output,pre,trans,post,summary,source,manifest,parent)
    print('MS_BEHAVIOR_V1_FINISHED',json.dumps({'output':str(output),
        'counts':{k:summary[k] for k in ('birth_fixed_visits',
         'eligible_current_stage_contacts','excluded_no_pre_stage',
         'excluded_birth_stage_contact_mismatch','transitions',
         'full_horizon','right_censored','breakouts_2closes',
         'defenses_2closes','failed_breakouts_2closes','retest_touches_15m')}},
         sort_keys=True),flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
