#!/usr/bin/env python3
"""BTC Multi-Swing Structural Behavior V1.2 — offline causal audit.

Preserves immutable V1/V1.1 outputs. New run:
- visit_pre_v12.csv: one PRE snapshot per physical 1m arrival
- transition_pre_v12.csv: one PRE snapshot BEFORE each confirming candle
- transition_observations_v12.csv: full original episode/event references
- physical_transitions_v12.csv: unique canonical geometry/time/event keys
- physical_memberships_v12.csv: N:M references and context distinctions
- h1_m15_links_v12.csv: bounded retrospective H1 to M15 links
- behavior_summary_v12.json / manifest.json: SHA and limitations

Not an execution engine or independent statistical sample. Only fully CLOSED
HTF bars enter PRE. POST events and retrospective links are never PRE features.
The geometry used for event sequencing is frozen at episode start; no claim
of ACTIVE/current-stage validity at an arbitrary later transition timestamp.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

import btc_multiswing_structural_behavior_v11 as parent
from btc_multiswing_structural_behavior_v11 import _future_close_bars, price_position
from btc_multiswing_60d_explorer_v1 import M, aggregate_closed, digest, iso, load_snapshot

VERSION='btc-multiswing-structural-behavior-15m-1h-4h-v1.2'
DEFAULT_PARENT=parent.DEFAULT_OUTPUT
DEFAULT_OUTPUT=Path('reports/reaction_lab/btc_multiswing_60d_v1/btc_sep09_nov08_structural_behavior_15m_1h_v12')
PARENT_FILES=('episode_pre_v11.csv','episode_members_v11.csv',
              'structural_transitions_v11.csv','transition_outcomes_v11.csv',
              'behavior_summary_v11.json')
LAG_LIMIT_MINUTES=120  # research policy; NOT optimized
HORIZON_MINUTES=parent.HORIZON_MINUTES
DEPARTURE_PCT=parent.DEPARTURE_PCT
OUTPUT_TABLES=('visit_pre_v12.csv','transition_pre_v12.csv',
               'transition_observations_v12.csv','physical_transitions_v12.csv',
               'physical_memberships_v12.csv','h1_m15_links_v12.csv')


def verify_v11(folder: Path):
    m=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    if (m.get('version')!=parent.VERSION or m.get('immutable_run') is not True
            or m.get('read_only') is not True or m.get('no_live_imports_or_network') is not True):
        raise ValueError('V12_INVALID_V11_PARENT')
    for file in PARENT_FILES:
        if digest(folder/file)!=m.get('files_sha256',{}).get(file):
            raise ValueError('V12_PARENT_HASH_MISMATCH: '+file)
    ep=pd.read_csv(folder/PARENT_FILES[0],low_memory=False)
    members=pd.read_csv(folder/PARENT_FILES[1],low_memory=False)
    trans=pd.read_csv(folder/PARENT_FILES[2],low_memory=False)
    required={
        'episode':{'episode_id','region_id','pre_cutoff_ts','stage_low','stage_high','stage_known_from'},
        'member':{'event_id','episode_id','birth_touch_open_ts','visit_number','region_id'},
        'transition':set(parent.TRANSITION_FIELDS),
    }
    if not (required['episode']<=set(ep) and required['member']<=set(members)
            and required['transition']<=set(trans)):
        raise ValueError('V12_PARENT_SCHEMA')
    if (ep.episode_id.duplicated().any() or members.event_id.duplicated().any()
            or trans.transition_id.duplicated().any() or
            not members.episode_id.isin(ep.episode_id).all() or
            not trans.episode_id.isin(ep.episode_id).all()):
        raise ValueError('V12_PARENT_IDENTITY_BROKEN')
    if (trans.confirmation_ts<=trans.episode_id.map(ep.set_index('episode_id').pre_cutoff_ts)).any():
        raise ValueError('V12_PARENT_CAUSAL_TIMELINE')
    return ep,members,trans,m


def _asof(frozen: dict, t: int, low: float, high: float, step_minutes: int):
    """At UTC t, the most recent fully closed candle only."""
    h=parent.context_asof(frozen,t,low,high,period_minutes=step_minutes)
    if h['last_close_ts'] is not None and h['last_close_ts']>t:
        raise ValueError('V12_HTF_FUTURE_LEAK')
    return h


def build_visit_pre(frozen: dict, m15: dict, h1: dict, h4: dict,
                    member: pd.DataFrame, eligible: pd.DataFrame):
    selected=member[['event_id','episode_id','region_id','visit_number',
                     'birth_touch_open_ts','stage_low_pre','stage_high_pre',
                     'stage_known_from']].merge(eligible,on='event_id',validate='one_to_one',
                                      suffixes=('_member',''))
    if len(selected)!=len(member): raise ValueError('V12_VISIT_MISSING_V1_PRE')
    rows=[]
    for r in selected.itertuples(index=False):
        t=int(r.pre_cutoff_ts);low=float(r.stage_low);high=float(r.stage_high)
        if (t!=int(r.birth_touch_open_ts) or t<int(r.stage_known_from)
                or float(r.stage_low_pre)!=low or float(r.stage_high_pre)!=high):
            raise ValueError('V12_VISIT_PRE_STAGE_MISMATCH')
        i=int(np.searchsorted(frozen['open_ts'],t))
        if (i<1 or i>=len(frozen['open_ts']) or frozen['open_ts'][i]!=t
                or abs(float(frozen['close'][i-1])-float(r.pre_close_1m))>1e-6):
            raise ValueError('V12_VISIT_PRE_CANDLE_DRIFT')
        if frozen['high'][i]<low or frozen['low'][i]>high:
            raise ValueError('V12_VISIT_NO_PHYSICAL_TOUCH')
        c15=_asof(m15,t,low,high,15)
        c1=_asof(h1,t,low,high,60)
        c4=_asof(h4,t,low,high,240)
        if c15['last_close_ts']!=int(r.pre_last_15m_close_ts):
            raise ValueError('V12_VISIT_PRE_15M_DRIFT')
        rows.append({'event_id':str(r.event_id),'episode_id':str(r.episode_id),
          'region_id':str(r.region_id),'visit_number':int(r.visit_number),
          'pre_cutoff_ts':t,'pre_cutoff_utc':iso(t),
          'stage_known_from':int(r.stage_known_from),
          'stage_low':low,'stage_high':high,
          'stage_extrema':int(r.stage_extrema),'band_origin_side':str(r.side),
          'approach_position':str(r.approach_position),
          'prior_completed_birth_visits_asof':int(r.prior_completed_birth_visits_asof),
          'pre_last_1m_close_ts':t,'pre_1m_close':float(r.pre_close_1m),
          'pre_last_15m_close_ts':c15['last_close_ts'],'pre_15m_close':c15['last_close'],
          'pre_last_1h_close_ts':c1['last_close_ts'],'pre_1h_close':c1['last_close'],
          'pre_1h_position':c1['position'],'pre_1h_ret_4h_pct':c1['returns'].get(4),
          'pre_1h_ret_12h_pct':c1['returns'].get(12),
          'pre_last_4h_close_ts':c4['last_close_ts'],'pre_4h_close':c4['last_close'],
          'pre_4h_position':c4['position'],'pre_4h_ret_12h_pct':c4['returns'].get(12),
          'pre_4h_ret_24h_pct':c4['returns'].get(24),
          'data_role':'PRE_BEFORE_TOUCH_1M_OPEN','physical_touch_verified_post_only':None})
    out=pd.DataFrame(rows).sort_values(['pre_cutoff_ts','region_id','event_id'],kind='mergesort')
    if out.event_id.duplicated().any():raise ValueError('V12_DUPLICATED_VISIT')
    return out.reset_index(drop=True)

def classify_transitions_v12(major: dict, *, episode_id: str, region_id: str,
                         origin_side: str, approach: str, band_low: float,
                         band_high: float, touch_open: int, source_end: int,
                         tf_minutes: int, horizon_minutes: int=HORIZON_MINUTES,
                         departure_pct: float=DEPARTURE_PCT,
                         previous_closed_position: str | None = None):
    """Pure close-confirmed, sidedness-neutral event machine, for 15m OR 1h.

    A true failed breakout requires two closed bars back on original side.
    V1.2: for H1 CLOSE_* changes, initialize the prior state from the last
    CLOSED H1 candle at touch PRE, not the latest 1m close.
    A retest is OHLC DESCRIPTIVE and does not establish intrabar sequence.
    The first touch-containing TF candle is never used for a wick/retest rule.
    """
    if previous_closed_position is not None and previous_closed_position not in ('ABOVE','BELOW','INSIDE'):
        raise ValueError('V12_INVALID_PREVIOUS_CLOSED_POSITION')
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
    rows=[]; last_position=(previous_closed_position if previous_closed_position is not None
                      else approach); runs={'ABOVE':0,'BELOW':0,'INSIDE':0}
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


def rebuild_h1(episodes: pd.DataFrame, h1: dict, *, source_end: int,
               horizon_minutes: int=1440):
    """Replace V1.1 H1 event stream; last position comes from last CLOSED H1."""
    rows=[]
    for r in episodes.itertuples(index=False):
        t=int(r.pre_cutoff_ts)
        pos=_asof(h1,t,float(r.stage_low),float(r.stage_high),60)['position']
        rows.extend(classify_transitions_v12(h1,episode_id=str(r.episode_id),
            region_id=str(r.region_id),origin_side=str(r.band_origin_side),
            approach=str(r.approach_position),band_low=float(r.stage_low),
            band_high=float(r.stage_high),touch_open=t,source_end=source_end,
            tf_minutes=60,horizon_minutes=horizon_minutes,
            previous_closed_position=None if pos=='UNKNOWN' else pos))
    return pd.DataFrame(rows,columns=parent.TRANSITION_FIELDS)


def _canonical_signature(r) -> tuple:
    return (str(r.region_id),str(r.source_tf),str(r.event_type),
            int(r.confirmation_ts),float(r.band_low_pre),float(r.band_high_pre))


def canonicalize_events(observations: pd.DataFrame):
    """Consolidate physical-descriptive event while preserving each context.

    A canonical event MAY have different origin_position across episodes.
    Such variants are explicitly exported and MUST NOT be assumed identical
    trade setups. This is a physical/time identity, not a strategy identity.
    """
    if observations.transition_id.duplicated().any():
        raise ValueError('V12_DUPLICATE_OBSERVATION_ID')
    if not observations.empty and ((observations.band_low_pre>observations.band_high_pre).any()
             or (observations.confirmation_ts<=observations.bar_open_ts).any()):
        raise ValueError('V12_INVALID_CANONICAL_GEOMETRY_OR_CLOCK')
    raw=observations.copy()
    keys=[_canonical_signature(r) for r in raw.itertuples(index=False)]
    raw['physical_transition_id']=[
        'msp12_'+hashlib.sha256(json.dumps(k,separators=(',',':')).encode()).hexdigest()[:24]
        for k in keys]
    signature_columns=['region_id','source_tf','event_type','confirmation_ts',
                       'band_low_pre','band_high_pre']
    if (len(raw[['physical_transition_id']+signature_columns].drop_duplicates())
             !=raw.physical_transition_id.nunique()):
        raise ValueError('V12_CANONICAL_HASH_COLLISION')
    group=raw.groupby('physical_transition_id',sort=True)
    summary=group.agg(observation_count=('transition_id','size'),
                      episode_count=('episode_id','nunique'),
                      origin_context_count=('origin_position','nunique'),
                      direction_count=('direction','nunique'),
                      first_episode_id=('episode_id','min')).reset_index()
    if (summary.direction_count>1).any():
        raise ValueError('V12_CANONICAL_DIRECTION_CONFLICT')
    sample=raw.sort_values(['confirmation_ts','episode_id','transition_id'],
                           kind='mergesort').drop_duplicates('physical_transition_id')
    keep=['physical_transition_id','region_id','band_origin_side','source_tf',
          'event_type','direction','confirmation_ts','confirmation_utc',
          'confirmation_close','bar_open_ts','band_low_pre','band_high_pre']
    phys=sample[keep].merge(summary,on='physical_transition_id',validate='one_to_one')
    phys['context_dependent_origin']=(phys.origin_context_count>1)
    if phys.physical_transition_id.duplicated().any():
        raise ValueError('V12_CANONICAL_NOT_UNIQUE')
    return phys.sort_values(['confirmation_ts','physical_transition_id'],
                            kind='mergesort').reset_index(drop=True),raw


def link_bounded(observations: pd.DataFrame, *, max_lag_minutes: int=LAG_LIMIT_MINUTES):
    """Retrospective link only, exact type/direction/origin, parent-lineage guarded.

    Matching by episode, kind and direction alone allowed V1.1 to link events
    up to 23h apart. The lineages are explicit, and ambiguous pairs never
    receive a silently guessed 'confirmed by 1h' status.
    """
    if not 0<max_lag_minutes<=240: raise ValueError('V12_INVALID_LINK_LAG')
    all_by_id=observations.set_index('transition_id')
    m=observations.loc[observations.source_tf.eq('M15_CLOSED')]
    h=observations.loc[observations.source_tf.eq('H1_CLOSED')]
    m_lookup={eid:g.sort_values(['confirmation_ts','transition_id'],kind='mergesort')
              for eid,g in m.groupby('episode_id',sort=False)}
    records=[]
    for r in h.itertuples(index=False):
        tm=int(r.confirmation_ts)
        item={'h1_transition_id':str(r.transition_id),'h1_physical_transition_id':str(r.physical_transition_id),
              'episode_id':str(r.episode_id),'event_type':str(r.event_type),
              'h1_confirmation_ts':tm,'m15_transition_id':None,
              'm15_physical_transition_id':None,'m15_confirmation_ts':None,
              'lag_minutes':None,'link_status':'NO_MATCH',
              'link_policy':f'SAME_EPISODE_EXACT_KIND_DIRECTION_ORIGIN_PARENT_MAX_{max_lag_minutes}M'}
        if str(r.event_type).startswith(('CLOSE_','DEPART_')):
            item['link_status']='NOT_APPLICABLE'
            records.append(item);continue
        cand=m_lookup.get(r.episode_id)
        if cand is None:
            records.append(item);continue
        cand=cand.loc[(cand.event_type.eq(r.event_type)) &
              (cand.direction.fillna('').eq('' if pd.isna(r.direction) else str(r.direction))) &
              (cand.origin_position.eq(r.origin_position)) &
              (cand.band_low_pre.eq(r.band_low_pre)) &
              (cand.band_high_pre.eq(r.band_high_pre)) &
              (cand.confirmation_ts<=tm)].copy()
        if cand.empty:
            records.append(item);continue
        cand['lag']=((tm-cand.confirmation_ts)//M).astype(int)
        within=cand.loc[cand.lag<=max_lag_minutes]
        if within.empty:
            item['link_status']='OUTSIDE_WINDOW'
            records.append(item);continue
        def lineage_ok(mrow):
            a=r.parent_transition_id;b=mrow.parent_transition_id
            if pd.isna(a) and pd.isna(b):return True
            if pd.isna(a) or pd.isna(b):return False
            if a not in all_by_id.index or b not in all_by_id.index:return False
            x=all_by_id.loc[a];y=all_by_id.loc[b]
            if (str(x.event_type)!=str(y.event_type) or str(x.direction)!=str(y.direction)
                    or int(x.confirmation_ts)>tm or int(y.confirmation_ts)>tm):
                return False
            return abs(int(x.confirmation_ts)-int(y.confirmation_ts))<=max_lag_minutes*M
        ok=within.loc[[lineage_ok(p) for p in within.itertuples(index=False)]]
        if ok.empty:
            item['link_status']='PARENT_MISMATCH'
            records.append(item);continue
        minlag=int(ok.lag.min())
        best=ok.loc[ok.lag.eq(minlag)]
        if best.physical_transition_id.nunique()>1:
            item['link_status']='AMBIGUOUS'
            records.append(item);continue
        chosen=best.sort_values('transition_id',kind='mergesort').iloc[0]
        item.update({'m15_transition_id':str(chosen.transition_id),
           'm15_physical_transition_id':str(chosen.physical_transition_id),
           'm15_confirmation_ts':int(chosen.confirmation_ts),
           'lag_minutes':minlag,'link_status':'SAME_CLOSE' if minlag==0 else 'LAGGED_CONFIRMATION'})
        records.append(item)
    return pd.DataFrame(records)


def transition_pre(frozen: dict, m15: dict, h1: dict, h4: dict,
                   transitions: pd.DataFrame, episode_pre: pd.DataFrame):
    eps=episode_pre.set_index('episode_id')
    cache={}
    rows=[]
    for r in transitions.itertuples(index=False):
        t=int(r.bar_open_ts)
        parent_pre=eps.loc[r.episode_id]
        low=float(r.band_low_pre);high=float(r.band_high_pre)
        if not (float(parent_pre.stage_low)==low and float(parent_pre.stage_high)==high):
            raise ValueError('V12_EVENT_REF_STAGE_CHANGED_SILENTLY')
        key=(t,low,high)
        if key not in cache:
            i=int(np.searchsorted(frozen['open_ts'],t))
            if i<1 or i>=len(frozen['open_ts']) or frozen['open_ts'][i]!=t:
                raise ValueError('V12_PRE_BAR_OPEN_NOT_IN_FREEZE')
            cache[key]=(float(frozen['close'][i-1]),
                        _asof(m15,t,low,high,15),
                        _asof(h1,t,low,high,60),
                        _asof(h4,t,low,high,240))
        px,c15,c1,c4=cache[key]
        stage_known=int(parent_pre.stage_known_from)<=t
        touch_known=int(parent_pre.pre_cutoff_ts)<=t
        rows.append({'transition_id':str(r.transition_id),
           'episode_id':str(r.episode_id),'region_id':str(r.region_id),
           'pre_cutoff_ts':t,'pre_cutoff_utc':iso(t),
           'pre_cutoff_role':'BEFORE_EVENT_CONFIRMING_CANDLE',
           'pre_episode_touch_occurred':touch_known,
           'pre_reference_stage_known':stage_known,
           'pre_1m_close':px,'pre_1m_position':parent.price_position(px,low,high),
           'pre_last_15m_close_ts':c15['last_close_ts'],
           'pre_15m_close':c15['last_close'],'pre_15m_position':c15['position'],
           'pre_last_1h_close_ts':c1['last_close_ts'],
           'pre_1h_close':c1['last_close'],'pre_1h_position':c1['position'],
           'pre_1h_ret_4h_pct':c1['returns'].get(4),
           'pre_last_4h_close_ts':c4['last_close_ts'],
           'pre_4h_close':c4['last_close'],'pre_4h_position':c4['position'],
           'pre_4h_ret_12h_pct':c4['returns'].get(12),
           'pre_stage_reference_low':low,'pre_stage_reference_high':high,
           'stage_policy':'EPISODE_PRE_FIXED_NOT_CURRENT_STAGE_ASOF',
           'actionable_pre_status':'REFERENCE_STAGE_KNOWN' if stage_known
                     else 'REFERENCE_NOT_YET_KNOWN',
           'retrospective_id_not_a_pre_feature':True})
    out=pd.DataFrame(rows)
    if not out.empty and ((out.pre_last_1h_close_ts.dropna()>out.loc[
           out.pre_last_1h_close_ts.notna(),'pre_cutoff_ts']).any() or
        (out.pre_last_4h_close_ts.dropna()>out.loc[
           out.pre_last_4h_close_ts.notna(),'pre_cutoff_ts']).any()):
        raise ValueError('V12_PRE_HTF_FUTURE_LEAK')
    return out


def build_v12(full: pd.DataFrame, source: dict, episodes: pd.DataFrame,
              members: pd.DataFrame, v11_events: pd.DataFrame,
              eligible: pd.DataFrame, *, max_lag_minutes=LAG_LIMIT_MINUTES):
    if (source['end']%parent.HOUR or len(full)<1440 or
            full.timestamp.iloc[-1]+M!=source['end']):
        raise ValueError('V12_INVALID_FROZEN_SOURCE')
    frozen=parent._closed_series(full,M)
    if np.any(np.diff(frozen['open_ts'])!=M):
        raise ValueError('V12_NONCONTIGUOUS_1M_SOURCE')
    start=int(full.timestamp.iloc[0]);end=int(source['end'])
    m15=parent._closed_series(aggregate_closed(full,'15m',start,end),parent.FIFTEEN)
    h1=parent._closed_series(aggregate_closed(full,'1h',start,end),parent.HOUR)
    h4=parent._closed_series(aggregate_closed(full,'4h',start,end),parent.FOUR_HOURS)
    visit=build_visit_pre(frozen,m15,h1,h4,members,eligible)
    # Preserve V1.1 M15 untouched. Replace H1 with a corrected causal
    # replay: prior H1 state must be the LAST *CLOSED H1* state.
    m15_old=v11_events.loc[v11_events.source_tf.eq('M15_CLOSED')].copy()
    if len(m15_old)!=int(v11_events.source_tf.eq('M15_CLOSED').sum()):
        raise ValueError('V12_M15_INPUT_MISMATCH')
    h1_new=rebuild_h1(episodes,h1,source_end=end)
    obs=pd.concat([m15_old,h1_new],ignore_index=True)
    obs=obs.sort_values(['confirmation_ts','episode_id','source_tf','transition_id'],
                        kind='mergesort').reset_index(drop=True)
    all_ids=set(obs.transition_id)
    if (obs.transition_id.duplicated().any() or
           obs.parent_transition_id.dropna().map(lambda k:k not in all_ids).any()):
        raise ValueError('V12_PARENT_EVENT_NOT_REBUILT')
    physical,observations=canonicalize_events(obs)
    mapping=observations.set_index('transition_id').physical_transition_id.to_dict()
    membership=observations[['transition_id','episode_id','region_id','event_type',
        'source_tf','confirmation_ts','origin_position','direction',
        'parent_transition_id','physical_transition_id']].copy()
    membership['parent_physical_transition_id']=membership.parent_transition_id.map(mapping)
    # A canonical physical event can have multiple FIRST-touch or episode
    # histories. Keep all rows and provide explicit context flags.
    origins=observations.groupby('physical_transition_id').origin_position.nunique()
    membership['origin_context_conflict']=membership.physical_transition_id.map(origins).gt(1)
    links=link_bounded(observations,max_lag_minutes=max_lag_minutes)
    # Retrospective cross-timeframe linkage: never included in PRE datasets.
    link_map=links.set_index('h1_transition_id').link_status.to_dict()
    observations['h1_m15_link_status']=observations.transition_id.map(link_map).fillna('NOT_H1')
    before=transition_pre(frozen,m15,h1,h4,observations,episodes)
    report=physical.groupby(['source_tf','event_type'],dropna=False).agg(
       physical_unique_events=('physical_transition_id','size'),
       episodes_represented=('episode_count','sum'),
       observation_rows=('observation_count','sum'),
       physical_with_origin_context_variants=('context_dependent_origin','sum')
    ).reset_index().sort_values(['source_tf','event_type'])
    if (len(visit)!=len(members) or len(observations)!=len(before)
           or len(membership)!=len(obs) or len(links)!=len(h1_new)
           or membership.transition_id.duplicated().any()
           or membership.physical_transition_id.isna().any()
           or before.transition_id.duplicated().any()):
        raise ValueError('V12_OUTPUT_IDENTITY_FAILURE')
    totals={'version':VERSION,'episodes':len(episodes),'member_visits':len(visit),
            'original_m15_observations':len(m15_old),
            'original_h1_observations':int(v11_events.source_tf.eq('H1_CLOSED').sum()),
            'corrected_h1_observations':len(h1_new),
            'new_event_observations':len(obs),
            'canonical_physical_events':len(physical),
            'duplicate_observation_rows':len(obs)-len(physical),
            'canonical_events_with_multiple_origin_contexts':int(physical.context_dependent_origin.sum()),
            'link_status_counts':links.link_status.value_counts().to_dict(),
            'lag_limit_minutes':max_lag_minutes,
            'context_4h':'only fully closed at visit or transition PRE',
            'source_start':source['start'],'source_end_exclusive':end,
            'limitations':[
                'Physical identity is SAME region, TF, event type, close timestamp and fixed reference geometry; distinct episode interpretations are retained.',
                'Events in different regions remain correlated; canonical event counts are NOT independent trade samples.',
                'H1 is reconstructed using last CLOSED H1 position; 15m event observations are unchanged from V1.1.',
                'Each visit PRE precedes the 1m first-touch candle. Transition PRE precedes its confirming candle, not its confirming close.',
                'Retrospective event labels and IDs must NOT be used as PRE predictive features.',
                'At transition PRE, band geometry is the EPISODE-FIXED historic reference, NOT an asof-reconstructed current stage.',
                'A partially completed H1/15m candle at first touch is handled only at its closed UTC timestamp; bars may start before touch.',
                'H1-M15 links are retrospective, bounded, lineage-checked; same-close is not a subsequent confirmation.',
                'No profitability, next-open fills, costs, benchmark or LIVE integration.',
                'All parent frozen V1, V1.1 and V2.4A outputs remain untouched.',
            ]}
    tables={'episode_index_v12.csv':episodes,
            'visit_pre_v12.csv':visit,
            'transition_pre_v12.csv':before,
            'transition_observations_v12.csv':observations,
            'physical_transitions_v12.csv':physical,
            'physical_memberships_v12.csv':membership,
            'h1_m15_links_v12.csv':links,
            'physical_summary_by_family_v12.csv':report}
    return tables,totals


def write_immutable(output: Path, tables: dict, summary: dict,
                   *, source: dict, parent_dir: Path, parent_v1_dir: Path):
    if output.exists():
        raise FileExistsError('V12_IMMUTABLE_RESULT_ALREADY_EXISTS: '+str(output))
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ms_v12_',dir=output.parent) as tmp:
        work=Path(tmp)
        for name,frame in tables.items():
            frame.to_csv(work/name,index=False,float_format='%.12g')
        summary={**summary,'parent_v11_manifest_sha256':digest(parent_dir/'manifest.json'),
                 'parent_v1_manifest_sha256':digest(parent_v1_dir/'manifest.json')}
        (work/'behavior_summary_v12.json').write_text(
            json.dumps(summary,sort_keys=True,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
        files=list(tables)+['behavior_summary_v12.json']
        manifest={'version':VERSION,'immutable_run':True,'read_only':True,
                  'no_live_imports_or_network':True,
                  'parent_v11':str(parent_dir),'parent_v1':str(parent_v1_dir),
                  'parent_v11_manifest_sha256':digest(parent_dir/'manifest.json'),
                  'parent_v1_manifest_sha256':digest(parent_v1_dir/'manifest.json'),
                  'source_provenance':{k:source.get(k) for k in
                      ('start','end','source_sha256','catalog_sha256','source_hll_parity')},
                  'files_sha256':{f:digest(work/f) for f in files}}
        (work/'manifest.json').write_text(
            json.dumps(manifest,sort_keys=True,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
        work.rename(output)


@lru_cache(maxsize=4)
def _read_ui_tables_cached(path: str, manifest_digest: str):
    folder=Path(path)
    m=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    if m.get('version')!=VERSION or m.get('immutable_run') is not True:
        raise ValueError('V12_UI_WRONG_RUN_VERSION')
    names=('episode_index_v12.csv','visit_pre_v12.csv','transition_observations_v12.csv')
    for f in names:
        if digest(folder/f)!=m.get('files_sha256',{}).get(f):
            raise ValueError('V12_UI_OUTPUT_SHA_MISMATCH: '+f)
    return tuple(pd.read_csv(folder/f,low_memory=False) for f in names)


def read_ui_index(project_root: Path):
    folder=(Path(project_root)/DEFAULT_OUTPUT).resolve()
    if not folder.is_dir():
        raise FileNotFoundError('Behavior V1.2 no generado: ejecutar --run OFFLINE en VPS')
    manifest=folder/'manifest.json'
    return _read_ui_tables_cached(str(folder),digest(manifest))


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    a=p.add_mutually_exclusive_group(required=True)
    a.add_argument('--plan',action='store_true')
    a.add_argument('--run',action='store_true')
    p.add_argument('--project-root',type=Path,default=Path('.'))
    p.add_argument('--parent-v11',type=Path,default=DEFAULT_PARENT)
    p.add_argument('--parent-v1',type=Path,default=parent.DEFAULT_PARENT_V1)
    p.add_argument('--output-dir',type=Path,default=DEFAULT_OUTPUT)
    p.add_argument('--max-link-lag-minutes',type=int,default=LAG_LIMIT_MINUTES)
    args=p.parse_args(argv)
    root=args.project_root.resolve()
    v11=args.parent_v11 if args.parent_v11.is_absolute() else root/args.parent_v11
    v1=args.parent_v1 if args.parent_v1.is_absolute() else root/args.parent_v1
    output=args.output_dir if args.output_dir.is_absolute() else root/args.output_dir
    if not 0<args.max_link_lag_minutes<=240:
        p.error('--max-link-lag-minutes must be 1..240')
    print('MS_BEHAVIOR_V12_PLAN',json.dumps({'version':VERSION,
          'mode':'RUN' if args.run else 'PLAN','output_dir':str(output),
          'parent_v11':str(v11),'parent_v1':str(v1),
          'lag_limit_minutes':args.max_link_lag_minutes,
          'read_only':True,'no_live':True,'ui_path':'Reaction Lab / BTC 60D / Preparación multi-TF'},
          sort_keys=True),flush=True)
    if args.plan:return 0
    if output.exists():
        raise FileExistsError('V12_IMMUTABLE_RESULT_ALREADY_EXISTS: '+str(output))
    episodes,members,events,manifest=verify_v11(v11)
    eligible,manifest_v1=parent.verify_parent_v1(v1)
    if (manifest.get('parent_v1_manifest_sha256')!=digest(v1/'manifest.json')
            or manifest_v1.get('source_provenance')!=manifest.get('source_provenance')):
        raise ValueError('V12_PARENT_PROVENANCE_MISMATCH')
    full,_,source=load_snapshot(root)
    for key in ('start','end','source_sha256','catalog_sha256'):
        expected=manifest.get('source_provenance',{}).get(key)
        if expected is not None and source.get(key)!=expected:
            raise ValueError('V12_FREEZE_NOT_EQUAL_V11: '+key)
    tables,summary=build_v12(full,source,episodes,members,events,eligible,
                             max_lag_minutes=args.max_link_lag_minutes)
    write_immutable(output,tables,summary,source=source,parent_dir=v11,parent_v1_dir=v1)
    print('MS_BEHAVIOR_V12_FINISHED',json.dumps({'counts':{
        k:v for k,v in summary.items() if k in ('episodes','member_visits',
            'original_m15_observations','original_h1_observations',
            'corrected_h1_observations','new_event_observations',
            'canonical_physical_events','duplicate_observation_rows',
            'canonical_events_with_multiple_origin_contexts')},
        'link_status_counts':summary['link_status_counts'],
        'output':str(output)},sort_keys=True),flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
