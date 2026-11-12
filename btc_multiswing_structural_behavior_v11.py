#!/usr/bin/env python3
"""Multi-Swing Structural Behavior V1.1 — BTC 60D, OFFLINE, immutable.

A *new* scientific study of causal, bidirectional interactions with frozen PRE
stage bands. Source cohort: V1's 4,565 retrospectively indexed V2.4A birth-band
visits, of which V1 physically verified stage contacts are eligible. It is NOT a
complete census of all contacts with changing current-stage geometry.

Outputs are deliberately split:
  episode_pre_v11.csv            facts already closed BEFORE first touch minute
  episode_members_v11.csv        retrospective member visits / dependence info
  structural_transitions_v11.csv causal closed 15m and 1h states with parent IDs
  transition_outcomes_v11.csv   future excursions measured AFTER confirmation
  behavior_summary_v11.json     coverage, limitations, censored observations
  manifest.json                 immutable input/output provenance & SHA-256

HIGH/LOW denotes the PHYSICAL ORIGIN of a band, not a promised present-day
resistance/support role. Orientation comes from the PRE price relative to the
band (ABOVE, BELOW, INSIDE), and is never assumed from the band side.

No Streamlit, network, or LIVE imports. Never uses future pivots to rebuild PRE.
"""
from __future__ import annotations

import argparse
import json
import math
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from btc_multiswing_60d_explorer_v1 import M, aggregate_closed, digest, iso, load_snapshot

VERSION = 'btc-multiswing-structural-behavior-15m-1h-4h-v1.1'
PARENT_VERSION = 'btc-multiswing-structural-behavior-15m-v1'
DEFAULT_PARENT_V1 = Path('reports/reaction_lab/btc_multiswing_60d_v1/btc_sep09_nov08_structural_behavior_15m_v1')
DEFAULT_PARENT_V24A = Path('reports/reaction_lab/btc_multiswing_60d_v1/btc_sep09_nov08_information_universe_v24a')
DEFAULT_OUTPUT = Path('reports/reaction_lab/btc_multiswing_60d_v1/btc_sep09_nov08_structural_behavior_15m_1h_v11')
FIFTEEN = 15*M
HOUR = 60*M
FOUR_HOURS = 240*M
EPISODE_GAP_MINUTES = 240  # deterministic causal separation, not optimized
EPISODE_MAX_MINUTES = 1440  # prevent chained revisits forming multi-day episodes
HORIZON_MINUTES = 1440
DEPARTURE_PCT = 0.15
OUTCOME_HORIZONS = (30, 60, 180, 360)
V1_INPUT_FILES = ('behavior_pre_v1.csv', 'behavior_outcomes_v1.csv',
                  'behavior_transitions_v1.csv', 'behavior_summary_v1.json')

PRE_FIELDS = [
 'episode_id','event_id','region_id','visit_number','band_origin_side',
 'pre_cutoff_ts','pre_cutoff_utc','stage_known_from','stage_low','stage_high',
 'stage_extrema','approach_position','pre_close_1m',
 'pre_last_15m_close_ts','pre_last_15m_close',
 'pre_last_1h_close_ts','pre_last_1h_close', 'pre_1h_close_position',
 'pre_1h_return_4h_pct','pre_1h_return_12h_pct',
 'pre_1h_last3_above_count','pre_1h_last3_inside_count','pre_1h_last3_below_count',
 'pre_last_4h_close_ts','pre_last_4h_close','pre_4h_close_position',
 'pre_4h_return_12h_pct','pre_4h_return_24h_pct',
 'prior_completed_birth_visits_asof','source_cohort','causal_note',
]
MEMBER_FIELDS = [
 'event_id','episode_id','region_id','visit_number','birth_touch_open_ts',
 'touch_1m_known_ts','visit_end_known_ts','side','stage_low_pre','stage_high_pre',
 'stage_known_from','member_order','source_eligibility','dependence_note',
]
TRANSITION_FIELDS = [
 'transition_id','episode_id','region_id','band_origin_side',
 'source_tf','event_type','direction','origin_position','confirmation_ts',
 'confirmation_utc','bar_open_ts','confirmation_close','close_position',
 'band_low_pre','band_high_pre','parent_transition_id','linked_15m_transition_id',
 'linked_lag_minutes','evidence_status','meaning',
]
OUTCOME_FIELDS = [
 'transition_id','episode_id','source_tf','event_type','confirmation_ts',
 'confirmation_price','horizon_minutes','target_ts','observed_minutes',
 'status','max_up_pct','max_down_pct','up_0p5_touched',
 'down_0p5_touched','up_1p0_touched','down_1p0_touched',
 'intrabar_first_hit_order','assessment_note',
]


def price_position(value: float, low: float, high: float) -> str:
    return 'ABOVE' if value > high else ('BELOW' if value < low else 'INSIDE')


def verify_parent_v1(folder: Path):
    """Refuse mismatched/modified V1, keeping that original audit immutable."""
    manifest = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    if (manifest.get('version') != PARENT_VERSION or
            manifest.get('immutable_run') is not True or
            manifest.get('no_live_imports_or_network') is not True):
        raise ValueError('V11_INVALID_V1_PARENT_MANIFEST')
    for name in V1_INPUT_FILES:
        if digest(folder/name) != manifest.get('files_sha256', {}).get(name):
            raise ValueError('V11_V1_PARENT_SHA256_MISMATCH: '+name)
    pre = pd.read_csv(folder/'behavior_pre_v1.csv', low_memory=False)
    outcomes = pd.read_csv(folder/'behavior_outcomes_v1.csv', low_memory=False)
    needed_pre = {'event_id','region_id','visit_number','side','pre_cutoff_ts',
                  'birth_touch_open_ts','stage_at_pre','stage_known_from',
                  'stage_low','stage_high','stage_extrema','pre_close_1m',
                  'approach_position','pre_last_15m_close_ts','pre_last_15m_close',
                  'prior_completed_birth_visits_asof'}
    needed_outcome={'event_id','eligibility','touch_1m_confirmed_ts'}
    if not needed_pre.issubset(pre.columns) or not needed_outcome.issubset(outcomes.columns):
        raise ValueError('V11_V1_SCHEMA_MISMATCH')
    if (pre.event_id.duplicated().any() or outcomes.event_id.duplicated().any()
            or set(pre.event_id) != set(outcomes.event_id)):
        raise ValueError('V11_DUPLICATE_OR_MISSING_V1_IDS')
    merged = pre.merge(outcomes[['event_id','eligibility','touch_1m_confirmed_ts']],
                       on='event_id', how='left', validate='one_to_one')
    expected = merged.loc[merged.eligibility.eq('ELIGIBLE_CURRENT_STAGE_1M_TOUCH')].copy()
    if len(expected)==0:
        raise ValueError('V11_NO_ELIGIBLE_V1_CONTACTS')
    if ((expected.touch_1m_confirmed_ts != expected.pre_cutoff_ts + M).any() or
            expected[['stage_low','stage_high','stage_known_from','stage_extrema']].isna().any().any() or
            (expected.stage_low > expected.stage_high).any() or
            (expected.stage_known_from>expected.pre_cutoff_ts).any()):
        raise ValueError('V11_INVALID_V1_TOUCH_TIMELINE')
    return expected.sort_values(['region_id','pre_cutoff_ts','event_id'],kind='mergesort'), manifest


def verify_parent_v24a(folder: Path, parent_v1: dict):
    raw = (folder/'manifest.json')
    if digest(raw) != parent_v1.get('parent_manifest_sha256'):
        raise ValueError('V11_PARENT_V24A_MANIFEST_HASH_MISMATCH')
    m = json.loads(raw.read_text(encoding='utf-8'))
    if m.get('version') != 'btc-multiswing-information-universe-v2.4a':
        raise ValueError('V11_PARENT_V24A_VERSION_MISMATCH')
    if digest(folder/'visits_v24.csv') != m.get('sha256',{}).get('visits_v24.csv'):
        raise ValueError('V11_PARENT_V24A_VISITS_SHA_MISMATCH')
    visits = pd.read_csv(folder/'visits_v24.csv', low_memory=False)
    if (not {'region_id','visit_number','first_touch_ts','end_known_ts'}.issubset(visits.columns)
            or visits.duplicated(['region_id','visit_number']).any()):
        raise ValueError('V11_PARENT_V24A_VISIT_SCHEMA')
    return visits


def _closed_series(bars: pd.DataFrame, step: int):
    """Validated increasing UTC open timestamps; expose close timestamps."""
    if bars.empty or not {'timestamp','open','high','low','close'}.issubset(bars.columns):
        raise ValueError('V11_MISSING_OHLC')
    ts=bars.timestamp.to_numpy(dtype=np.int64)
    vals=bars[['open','high','low','close']].to_numpy(dtype=float)
    if (np.any(ts % step) or (len(ts)>1 and np.any(np.diff(ts)!=step)) or
            not np.isfinite(vals).all() or np.any(vals<=0) or
            np.any(vals[:,1]<np.maximum.reduce([vals[:,0],vals[:,2],vals[:,3]])) or
            np.any(vals[:,2]>np.minimum.reduce([vals[:,0],vals[:,1],vals[:,3]]))):
        raise ValueError('V11_BROKEN_CLOSED_BARS')
    return {'open_ts':ts, 'closed_ts':ts+step,
            'open':vals[:,0], 'high':vals[:,1],
            'low':vals[:,2], 'close':vals[:,3]}


def context_asof(major: dict, cutoff: int, low: float, high: float,
                 *, period_minutes: int) -> dict:
    """Returns only fully CLOSED HTF bars at or before PRE cutoff."""
    ts=major['closed_ts']; j=int(np.searchsorted(ts,cutoff,side='right'))-1
    if j<0:
        return {'last_close_ts':None,'last_close':None,'position':'UNKNOWN',
                'returns':{}, 'last3_counts':{'ABOVE':None,'INSIDE':None,'BELOW':None}}
    close=major['close']; price=float(close[j])
    def ret(h):
        minutes=h*60
        k=j-(minutes//period_minutes)
        return ((price/float(close[k])-1)*100
                if minutes%period_minutes==0 and k>=0 else None)
    counts={'ABOVE':None,'INSIDE':None,'BELOW':None}
    if j>=2:
        states=[price_position(float(x),low,high) for x in close[j-2:j+1]]
        counts={s:states.count(s) for s in counts}
    return {'last_close_ts':int(ts[j]), 'last_close':price,
            'position':price_position(price,low,high),
            'returns':{h:ret(h) for h in ((4,12) if period_minutes==60 else (12,24))},
            'last3_counts':counts}


def make_episodes(eligible: pd.DataFrame, visits: pd.DataFrame,
                  *, gap_minutes: int=EPISODE_GAP_MINUTES,
                  max_minutes: int=EPISODE_MAX_MINUTES):
    """Online-compatible assignment using only past known visit endings.

    Episode's ID and initial PRE never change when subsequent visits arrive.
    Grouping is within a region only; inter-region dependence remains.
    """
    if (gap_minutes<30 or gap_minutes>1440 or max_minutes<gap_minutes
            or max_minutes>2880):
        raise ValueError('V11_EPISODE_POLICY_INVALID')
    end_join=visits[['region_id','visit_number','first_touch_ts','end_known_ts']]
    joined=eligible.merge(end_join,on=['region_id','visit_number'],how='left',
                         validate='one_to_one')
    if joined.first_touch_ts.isna().any() or (joined.first_touch_ts != joined.pre_cutoff_ts).any():
        raise ValueError('V11_EPISODE_V24A_ID_MISMATCH')
    members=[]; episode_head=[]
    for rid,g in joined.groupby('region_id',sort=True):
        last=None
        for r in g.sort_values(['pre_cutoff_ts','event_id'],kind='mergesort').itertuples(index=False):
            t=int(r.pre_cutoff_ts)
            stage_signature=(int(r.stage_known_from),float(r.stage_low),float(r.stage_high))
            if last is None:
                new=True
            else:
                prior_end=last['last_visit_end_known']
                known_end=(prior_end is not None and prior_end<=t)
                gap=(t-prior_end if known_end else 0)
                new=(stage_signature!=last['stage_signature'] or
                     t-last['first_ts'] >= max_minutes*M or
                     (known_end and gap>gap_minutes*M))
            if new:
                eid=f'{rid}|EP|{t}'
                last={'episode_id':eid,'first_ts':t,'stage_signature':stage_signature,
                      'member_count':0,'last_visit_end_known':None}
                episode_head.append((eid,r))
            last['member_count']+=1
            end_known=(None if pd.isna(r.end_known_ts) else int(r.end_known_ts))
            if end_known is not None and end_known<=t:
                raise ValueError('V11_VISIT_END_BEFORE_TOUCH')
            last['last_visit_end_known']=end_known
            members.append({'event_id':str(r.event_id),'episode_id':last['episode_id'],
                'region_id':str(rid),'visit_number':int(r.visit_number),
                'birth_touch_open_ts':t,'touch_1m_known_ts':t+M,
                'visit_end_known_ts':end_known,
                'side':str(r.side),'stage_low_pre':float(r.stage_low),
                'stage_high_pre':float(r.stage_high),
                'stage_known_from':int(r.stage_known_from),
                'member_order':last['member_count'],
                'source_eligibility':'V1_VERIFIED_CURRENT_STAGE_1M_TOUCH',
                'dependence_note':'OTHER_REGIONS_AND_TIME_OVERLAP_NOT_INDEPENDENT'})
    member_frame=pd.DataFrame(members,columns=MEMBER_FIELDS).sort_values(
        ['birth_touch_open_ts','region_id','member_order'],kind='mergesort').reset_index(drop=True)
    if member_frame.event_id.duplicated().any():
        raise ValueError('V11_EPISODE_DUPLICATE_EVENT')
    return episode_head,member_frame


def _future_close_bars(major: dict, contact_open: int,
                       source_end: int, horizon_minutes: int):
    """The opening time of first contact does NOT authorize any bar before its close."""
    first=int(np.searchsorted(major['closed_ts'],contact_open+M,side='left'))
    end=min(source_end,contact_open+horizon_minutes*M)
    last=int(np.searchsorted(major['closed_ts'],end,side='right'))
    return range(first,last)


def classify_transitions(major: dict, *, episode_id: str, region_id: str,
                         origin_side: str, approach: str, band_low: float,
                         band_high: float, touch_open: int, source_end: int,
                         tf_minutes: int, horizon_minutes: int=HORIZON_MINUTES,
                         departure_pct: float=DEPARTURE_PCT):
    """Pure close-confirmed, sidedness-neutral event machine, for 15m OR 1h.

    A true failed breakout requires two closed bars back on original side.
    A retest is OHLC DESCRIPTIVE and does not establish intrabar sequence.
    The first touch-containing TF candle is never used for a wick/retest rule.
    """
    if (tf_minutes not in (15,60) or approach not in ('BELOW','ABOVE','INSIDE') or
            not (0<band_low<=band_high) or not (0<departure_pct<=2) or
            touch_open%M or not (touch_open < source_end)):
        raise ValueError('V11_CLASSIFIER_INVALID_ARGS')
    idx=_future_close_bars(major,touch_open,source_end,horizon_minutes)
    close_ts=major['closed_ts'];opens=major['open_ts'];closes=major['close']
    highs=major['high'];lows=major['low']
    if not idx:
        return []
    prefix='M15' if tf_minutes==15 else 'H1'
    rows=[]; last_position=approach; runs={'ABOVE':0,'BELOW':0,'INSIDE':0}
    away_run={'ABOVE':0,'BELOW':0}
    active=None
    seen=set();
    def emit(kind,k,direction='',parent='',meaning='',ambiguous=False):
        ts=int(close_ts[k]);tid=f'{episode_id}|{prefix}|{kind}|{ts}'
        if tid in seen: return None
        seen.add(tid)
        pos=price_position(float(closes[k]),band_low,band_high)
        r={'transition_id':tid,'episode_id':episode_id,'region_id':region_id,
           'band_origin_side':origin_side,'source_tf':prefix+'_CLOSED',
           'event_type':kind,'direction':direction,'origin_position':approach,
           'confirmation_ts':ts,'confirmation_utc':iso(ts),
           'bar_open_ts':int(opens[k]),'confirmation_close':float(closes[k]),
           'close_position':pos,'band_low_pre':band_low,'band_high_pre':band_high,
           'parent_transition_id':parent or None,'linked_15m_transition_id':None,
           'linked_lag_minutes':None,
           'evidence_status':('OHLC_INTRABAR_SEQUENCE_UNKNOWN' if ambiguous else 'CLOSED_BAR_CONFIRMED'),
           'meaning':meaning or 'RESEARCH_ONLY_NOT_AN_ENTRY'}
        rows.append(r)
        return tid
    for k in idx:
        current=price_position(float(closes[k]),band_low,band_high)
        if current!=last_position:
            emit('CLOSE_'+current,k,direction=current)
        for side in runs:
            runs[side]=runs[side]+1 if current==side else 0
        if runs['INSIDE']==3:
            emit('ACCEPT_INSIDE_3C',k,meaning='Three closed bars inside the frozen PRE stage')
        for side in ('ABOVE','BELOW'):
            bound=(band_high if side=='ABOVE' else band_low)
            away=((float(closes[k])-bound)/bound*100 if side=='ABOVE' else
                  (bound-float(closes[k]))/bound*100)
            away_run[side]=(away_run[side]+1 if current==side and away>=departure_pct else 0)
            if away_run[side]==2:
                emit('DEPART_'+side+'_2C',k,direction=side,
                     meaning='Two closed bars with minimum descriptive departure')
        if active is None:
            if approach in ('ABOVE','BELOW'):
                far='BELOW' if approach=='ABOVE' else 'ABOVE'
                if runs[far]==2:
                    tid=emit('BREAK_'+far+'_2C',k,direction=far,
                        meaning='Two closed bars beyond opposite edge; orientation defined by PRE')
                    active={'id':tid,'direction':far,'origin':approach,
                            'separated':False,'retest':False,'reentered':False}
                elif away_run[approach]==2:
                    emit('DEFEND_ORIGIN_2C',k,direction=approach,
                         meaning='Two departures on arrival side, not necessarily band support/resistance')
            elif approach=='INSIDE':
                for far in ('ABOVE','BELOW'):
                    if runs[far]==2:
                        tid=emit('EXIT_'+far+'_2C',k,direction=far,
                            meaning='Origin was inside; do NOT call this a conventional breakout')
                        active={'id':tid,'direction':far,'origin':'INSIDE',
                                'separated':False,'retest':False,'reentered':False}
                        break
        else:
            far=active['direction']; parent=active['id'];origin=active['origin']
            if (current!=far and not active['reentered']):
                emit('REENTER_'+('ORIGIN' if origin!='INSIDE' and current==origin else 'BAND')+'_FIRST_CLOSE',
                     k,direction=current,parent=parent,
                     meaning='First closed return; NOT confirmed failure')
                active['reentered']=True
            if origin!='INSIDE' and runs[origin]==2:
                emit('FAILED_BREAK_RETURN_ORIGIN_2C',k,direction=origin,parent=parent,
                     meaning='Two closed bars back beyond original edge')
                active=None
            elif origin=='INSIDE' and runs['INSIDE']==2:
                emit('FAILED_EXIT_RETURN_INSIDE_2C',k,direction='INSIDE',parent=parent,
                     meaning='Two closed bars back inside original band')
                active=None
            else:
                # Retest: after at least one full bar separated from band, a
                # later CLOSED candle's range intersects PRE geometry.
                touching=(float(highs[k])>=band_low and float(lows[k])<=band_high)
                is_first_partial=(int(opens[k])<touch_open)
                if active['separated'] and not active['retest'] and touching and not is_first_partial:
                    emit('RETEST_TOUCH',k,direction=far,parent=parent,
                         ambiguous=True,meaning='Candle intersects frozen band; intrabar order unknown')
                    active['retest']=True;active['separated']=False
                elif active['retest'] and runs[far]>=2:
                    emit('RETEST_HOLD_'+far+'_2C',k,direction=far,parent=parent,
                         meaning='Two closes on breakout/exit side after a retest')
                    active['retest']=False
                if not touching and current==far:
                    active['separated']=True
        last_position=current
    return rows


def link_h1_to_m15(transitions: pd.DataFrame) -> pd.DataFrame:
    """Post-only descriptive linkage; never injects 1h confirmation into PRE."""
    if transitions.empty:
        return transitions
    out=transitions.copy()
    m=out.loc[out.source_tf.eq('M15_CLOSED')].sort_values('confirmation_ts')
    def family(kind):
        # Link only the same structural transition family, not an arbitrary
        # nearby candle or directional CLOSE/DEPARTURE event.
        if kind.startswith(('BREAK_', 'EXIT_')): return 'BREAK' if kind.startswith('BREAK_') else 'EXIT'
        if kind.startswith('FAILED_BREAK_'): return 'FAILED_BREAK'
        if kind.startswith('FAILED_EXIT_'): return 'FAILED_EXIT'
        if kind.startswith('RETEST_'): return 'RETEST'
        if kind.startswith('DEFEND_'): return 'DEFEND'
        if kind.startswith('REENTER_'): return 'REENTER'
        if kind.startswith('ACCEPT_'): return 'ACCEPT'
        return ''
    for i,r in out.loc[out.source_tf.eq('H1_CLOSED')].iterrows():
        target=family(str(r.event_type))
        if not target: continue
        compatible=m.loc[(m.episode_id==r.episode_id) &
                         (m.confirmation_ts<=r.confirmation_ts) &
                         (m.direction==r.direction) &
                         (m.event_type.map(family)==target)]
        if not compatible.empty:
            prev=compatible.iloc[-1]
            out.at[i,'linked_15m_transition_id']=str(prev.transition_id)
            out.at[i,'linked_lag_minutes']=(int(r.confirmation_ts)-int(prev.confirmation_ts))//M
    return out


def evaluate_transition(full: dict, transition: dict, *, end: int,
                        horizons=OUTCOME_HORIZONS):
    """Outcomes start from AFTER confirming close (never inside signal bar).

    If horizon is cut off, excursions remain descriptive over observed data,
    but 0.5%/1% reachability is NULL: an incomplete horizon isn't a failure.
    """
    ts=int(transition['confirmation_ts'])
    i=int(np.searchsorted(full['open_ts'],ts,side='left'))
    if i>=len(full['open_ts']) or int(full['open_ts'][i])!=ts:
        # Confirmation may exactly equal exclusive end: no POST bars.
        if ts!=end: raise ValueError('V11_MISSING_NEXT_1M_BAR')
    ref=float(transition['confirmation_close']); out=[]
    for h in horizons:
        target=ts+h*M;last=min(end,target)
        k=int(np.searchsorted(full['open_ts'],last,side='left'))
        n=max(0,k-i)
        if n:
            hi=float(np.max(full['high'][i:k]));lo=float(np.min(full['low'][i:k]))
            up=max(0,(hi/ref-1)*100);down=max(0,(1-lo/ref)*100)
        else:
            up=None;down=None
        complete=last==target and n==h
        anyup=anydown=None
        note='POST_DESCRIPTIVE_NOT_EXECUTABLE';order='NOT_ASSESSED'
        if complete:
            anyup=up>=.5;anydown=down>=.5
            if anyup and anydown:
                order='BOTH_THRESHOLD_TOUCHES_ORDER_NOT_EVALUATED'
            else:
                order='ONLY_ONE_OR_NEITHER_0P5_PCT'
        out.append({'transition_id':transition['transition_id'],
            'episode_id':transition['episode_id'],'source_tf':transition['source_tf'],
            'event_type':transition['event_type'],'confirmation_ts':ts,
            'confirmation_price':ref,'horizon_minutes':h,
            'target_ts':target,'observed_minutes':n,
            'status':'FULL_HORIZON' if complete else 'RIGHT_CENSORED',
            'max_up_pct':up,'max_down_pct':down,
            'up_0p5_touched':up>=.5 if complete else None,
            'down_0p5_touched':down>=.5 if complete else None,
            'up_1p0_touched':up>=1 if complete else None,
            'down_1p0_touched':down>=1 if complete else None,
            'intrabar_first_hit_order':order,'assessment_note':note})
    return out


def build_v11(full: pd.DataFrame, eligible: pd.DataFrame, visits: pd.DataFrame,
              source: dict, *, gap_minutes=EPISODE_GAP_MINUTES,
              max_episode_minutes=EPISODE_MAX_MINUTES,
              horizon_minutes=HORIZON_MINUTES,
              outcome_horizons=OUTCOME_HORIZONS):
    """No source code changes to V1 or to the frozen band detector."""
    if (source['end']<=source['start'] or source['end']%HOUR or
            len(full)<1440 or full.timestamp.iloc[-1]+M!=source['end']):
        raise ValueError('V11_INVALID_FROZEN_1M_SOURCE')
    frozen=_closed_series(full,M)
    if len(frozen['open_ts'])>1 and np.any(np.diff(frozen['open_ts'])!=M):
        raise ValueError('V11_NONCONTIGUOUS_1M_SOURCE')
    fifteen=_closed_series(aggregate_closed(full,'15m',int(full.timestamp.iloc[0]),source['end']),FIFTEEN)
    onehour=_closed_series(aggregate_closed(full,'1h',int(full.timestamp.iloc[0]),source['end']),HOUR)
    fourhour=_closed_series(aggregate_closed(full,'4h',int(full.timestamp.iloc[0]),source['end']),FOUR_HOURS)
    heads,members=make_episodes(eligible,visits,gap_minutes=gap_minutes,
                                 max_minutes=max_episode_minutes)
    pre_rows=[]; transitions=[]
    for episode_id,r in heads:
        t=int(r.pre_cutoff_ts)
        low=float(r.stage_low);high=float(r.stage_high)
        if not (0<low<=high and r.side in ('HIGH','LOW')):
            raise ValueError('V11_INVALID_STAGE')
        # V1's source and the newly loaded SHA-verified freeze must be the SAME
        # physical candles. Refuse silent drift, even if provenance looked OK.
        minute_i=int(np.searchsorted(frozen['open_ts'],t))
        if (minute_i<1 or minute_i>=len(frozen['open_ts']) or
            int(frozen['open_ts'][minute_i])!=t or
            abs(float(frozen['close'][minute_i-1])-float(r.pre_close_1m))>1e-6 or
            float(frozen['high'][minute_i])<low or
            float(frozen['low'][minute_i])>high):
            raise ValueError('V11_V1_PRE_PRICE_OR_PHYSICAL_CONTACT_MISMATCH')
        # Cross-check the actual last closed 15m bar; never accept an out-of-date
        # PRE value copied from a different data snapshot.
        prior15=int(np.searchsorted(fifteen['closed_ts'],t,side='right'))-1
        if (prior15<0 or
            int(fifteen['closed_ts'][prior15])!=int(r.pre_last_15m_close_ts) or
            abs(float(fifteen['close'][prior15])-float(r.pre_last_15m_close))>1e-6):
            raise ValueError('V11_V1_PRE_15M_DRIFT')
        h1=context_asof(onehour,t,low,high,period_minutes=60)
        h4=context_asof(fourhour,t,low,high,period_minutes=240)
        pre_rows.append({'episode_id':episode_id,'event_id':str(r.event_id),
            'region_id':str(r.region_id),'visit_number':int(r.visit_number),
            'band_origin_side':str(r.side),'pre_cutoff_ts':t,'pre_cutoff_utc':iso(t),
            'stage_known_from':int(r.stage_known_from),
            'stage_low':low,'stage_high':high,'stage_extrema':int(r.stage_extrema),
            'approach_position':str(r.approach_position),
            'pre_close_1m':float(r.pre_close_1m),
            'pre_last_15m_close_ts':int(r.pre_last_15m_close_ts),
            'pre_last_15m_close':float(r.pre_last_15m_close),
            'pre_last_1h_close_ts':h1['last_close_ts'],
            'pre_last_1h_close':h1['last_close'],
            'pre_1h_close_position':h1['position'],
            'pre_1h_return_4h_pct':h1['returns'].get(4),
            'pre_1h_return_12h_pct':h1['returns'].get(12),
            'pre_1h_last3_above_count':h1['last3_counts']['ABOVE'],
            'pre_1h_last3_inside_count':h1['last3_counts']['INSIDE'],
            'pre_1h_last3_below_count':h1['last3_counts']['BELOW'],
            'pre_last_4h_close_ts':h4['last_close_ts'],
            'pre_last_4h_close':h4['last_close'],
            'pre_4h_close_position':h4['position'],
            'pre_4h_return_12h_pct':h4['returns'].get(12),
            'pre_4h_return_24h_pct':h4['returns'].get(24),
            'prior_completed_birth_visits_asof':int(r.prior_completed_birth_visits_asof),
            'source_cohort':'RETROSPECTIVE_V24A_BIRTH_BAND_VERIFIED_PRE_STAGE',
            'causal_note':'PRE closed strictly before contact minute; HTF only closed UTC bars',
            })
        for tf,bars in ((15,fifteen),(60,onehour)):
            transitions.extend(classify_transitions(bars,
                episode_id=episode_id,region_id=str(r.region_id),
                origin_side=str(r.side),approach=str(r.approach_position),
                band_low=low,band_high=high,touch_open=t,
                source_end=source['end'],tf_minutes=tf,horizon_minutes=horizon_minutes))
    pre=pd.DataFrame(pre_rows,columns=PRE_FIELDS).sort_values(
        ['pre_cutoff_ts','region_id','episode_id'],kind='mergesort').reset_index(drop=True)
    trans=pd.DataFrame(transitions,columns=TRANSITION_FIELDS).sort_values(
        ['confirmation_ts','episode_id','source_tf','transition_id'],kind='mergesort').reset_index(drop=True)
    trans=link_h1_to_m15(trans)
    out_rows=[]
    for tr in trans.to_dict('records'):
        out_rows.extend(evaluate_transition(frozen,tr,end=source['end'],
                                            horizons=outcome_horizons))
    outcomes=pd.DataFrame(out_rows,columns=OUTCOME_FIELDS)
    if not outcomes.empty:
        outcomes=outcomes.sort_values(['confirmation_ts','transition_id','horizon_minutes'],
                                     kind='mergesort').reset_index(drop=True)
    if (len(members)!=len(eligible) or len(pre)!=members.episode_id.nunique() or
            pre.episode_id.duplicated().any() or trans.transition_id.duplicated().any() or
            (not trans.empty and not trans.episode_id.isin(pre.episode_id).all()) or
            len(outcomes)!=len(trans)*len(outcome_horizons)):
        raise ValueError('V11_OUTPUT_INTEGRITY_FAILURE')
    if not trans.empty:
        pre_by=pre.set_index('episode_id').pre_cutoff_ts
        if (trans.confirmation_ts<=trans.episode_id.map(pre_by)).any():
            raise ValueError('V11_FUTURE_TO_PRE_TRANSITION')
    if ((pre.pre_last_1h_close_ts.dropna()>pre.loc[pre.pre_last_1h_close_ts.notna(),'pre_cutoff_ts']).any()
            or (pre.pre_last_4h_close_ts.dropna()>pre.loc[pre.pre_last_4h_close_ts.notna(),'pre_cutoff_ts']).any()):
        raise ValueError('V11_FUTURE_HTF_LEAK')
    summary={'version':VERSION,'source_start':source['start'],
        'source_end_exclusive':source['end'],'parent_v1_selection':'V1_ELIGIBLE_CURRENT_STAGE_1M_TOUCH',
        'eligible_member_visits':len(eligible),'episodes':len(pre),
        'episodes_by_approach':pre.approach_position.value_counts().to_dict(),
        'band_origin_counts':pre.band_origin_side.value_counts().to_dict(),
        'transition_rows':len(trans),
        '15m_events':int(trans.source_tf.eq('M15_CLOSED').sum()),
        '1h_events':int(trans.source_tf.eq('H1_CLOSED').sum()),
        'transition_outcome_rows':len(outcomes),
        'censored_outcomes':int(outcomes.status.eq('RIGHT_CENSORED').sum()),
        'episode_policy':{'gap_minutes':gap_minutes,
                          'max_episode_minutes':max_episode_minutes},
        'horizon_minutes':horizon_minutes,
        'outcome_horizons_minutes':list(outcome_horizons),
        'limitations':[
            'Retrospective V2.4A FIXED BIRTH BAND visit universe, NOT all current-stage contacts.',
            'Contact selection uses post-contact physical touch; it is NOT a PRE signal.',
            'V1 no-stage/mismatched contacts excluded from this structural episode analysis.',
            'Band geometry fixed from episode PRE for the entire analyzed trajectory.',
            'Band HIGH/LOW indicates pivot origin; behavior role derives from PRE side.',
            'Episode grouping does not eliminate cross-region/temporal dependence.',
            '1h/4h features use last fully closed UTC bars at episode PRE cutoff.',
            '15m/1h first partial bar may contain minutes before contact; no intrabar ordering inferred.',
            'Confirmation outcomes start at next minute; no real trades, fees, fills or PnL.',
            'Candidate rules descriptive, in-sample BTC 60D, not validated or optimized.',
        ]}
    return pre,members,trans,outcomes,summary


def write_immutable(folder: Path, tables: dict, summary: dict,
                    source: dict, parent_v1: Path, parent_v24a: Path):
    if folder.exists():
        raise FileExistsError('V11_IMMUTABLE_RESULT_ALREADY_EXISTS: '+str(folder))
    folder.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ms_behavior_v11_',dir=folder.parent) as tmp:
        temp=Path(tmp)
        for filename,table in tables.items():
            table.to_csv(temp/filename,index=False,float_format='%.12g')
        summary['parent_v1_manifest_sha256']=digest(parent_v1/'manifest.json')
        summary['parent_v24a_manifest_sha256']=digest(parent_v24a/'manifest.json')
        (temp/'behavior_summary_v11.json').write_text(
            json.dumps(summary,sort_keys=True,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        files=(*tables.keys(),'behavior_summary_v11.json')
        manifest={'version':VERSION,'immutable_run':True,'read_only':True,
             'no_live_imports_or_network':True,
             'parent_v1':str(parent_v1),'parent_v24a':str(parent_v24a),
             'parent_v1_manifest_sha256':digest(parent_v1/'manifest.json'),
             'parent_v24a_manifest_sha256':digest(parent_v24a/'manifest.json'),
             'source_provenance':{k:source.get(k) for k in (
                'start','end','source_sha256','catalog_sha256','source_hll_parity')},
             'files_sha256':{name:digest(temp/name) for name in files}}
        (temp/'manifest.json').write_text(json.dumps(manifest,sort_keys=True,
             ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        temp.rename(folder)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    actions=p.add_mutually_exclusive_group(required=True)
    actions.add_argument('--plan',action='store_true')
    actions.add_argument('--run',action='store_true')
    p.add_argument('--project-root',type=Path,default=Path('.'))
    p.add_argument('--parent-v1',type=Path,default=DEFAULT_PARENT_V1)
    p.add_argument('--parent-v24a',type=Path,default=DEFAULT_PARENT_V24A)
    p.add_argument('--output-dir',type=Path,default=DEFAULT_OUTPUT)
    p.add_argument('--episode-gap-minutes',type=int,default=EPISODE_GAP_MINUTES)
    p.add_argument('--episode-max-minutes',type=int,default=EPISODE_MAX_MINUTES)
    a=p.parse_args(argv)
    root=a.project_root.resolve()
    parent_v1=a.parent_v1 if a.parent_v1.is_absolute() else root/a.parent_v1
    parent_v24a=a.parent_v24a if a.parent_v24a.is_absolute() else root/a.parent_v24a
    output=a.output_dir if a.output_dir.is_absolute() else root/a.output_dir
    if (not 30<=a.episode_gap_minutes<=1440 or
        not a.episode_gap_minutes<=a.episode_max_minutes<=2880):
        p.error('episode policy invalid')
    print('MS_BEHAVIOR_V11_PLAN',json.dumps({
        'version':VERSION,'mode':'RUN' if a.run else 'PLAN',
        'project_root':str(root),'parent_v1':str(parent_v1),
        'parent_v24a':str(parent_v24a),'output_dir':str(output),
        'primary_tf':'15m','events_tf':'1h','context_tf':'4h',
        'episode_gap_minutes':a.episode_gap_minutes,
        'episode_max_minutes':a.episode_max_minutes,
        'no_live':True,'pre_post_separate':True},sort_keys=True),flush=True)
    if a.plan: return 0
    if output.exists(): raise FileExistsError('V11_IMMUTABLE_RESULT_ALREADY_EXISTS: '+str(output))
    eligible,manifest=verify_parent_v1(parent_v1)
    visits=verify_parent_v24a(parent_v24a,manifest)
    full,_,source=load_snapshot(root)
    prov=manifest.get('source_provenance',{})
    for k,src in [('start','start'),('end','end'),('source_sha256','source_sha256'),
                  ('catalog_sha256','catalog_sha256'),('source_hll_parity','source_hll_parity')]:
        if prov.get(k) != source.get(src):
            raise ValueError('V11_FROZEN_SOURCE_PROVENANCE_MISMATCH: '+k)
    pre,members,trans,outcomes,summary=build_v11(full,eligible,visits,source,
        gap_minutes=a.episode_gap_minutes,max_episode_minutes=a.episode_max_minutes)
    write_immutable(output,{
        'episode_pre_v11.csv':pre,'episode_members_v11.csv':members,
        'structural_transitions_v11.csv':trans,
        'transition_outcomes_v11.csv':outcomes,
    },summary,source,parent_v1,parent_v24a)
    print('MS_BEHAVIOR_V11_FINISHED',json.dumps({'output':str(output),
         'counts':{k:summary[k] for k in ('eligible_member_visits','episodes',
             '15m_events','1h_events','transition_outcome_rows','censored_outcomes')},
         'approaches':summary['episodes_by_approach']},sort_keys=True),flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
