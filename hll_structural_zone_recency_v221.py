#!/usr/bin/env python3
"""HLL Structural Zones V2.1.1 — optional as-of recent activity overlay.

Separates historical importance (untouched V2.1) from recent interactions.
All observations must be confirmed by the requested CLOSED 30m cutoff.
Recency is not support, resistance, flip or a trading signal.
This file performs no writes, exchange calls, or LIVE actions.
"""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd

TF = 1_800_000
DAY = 86_400_000
EVENTS = frozenset(('TEST_FROM_ABOVE','TEST_FROM_BELOW',
                    'DEFENSE_CONFIRMED','LOSS_CONFIRMED'))


def annotate_recency(zones: pd.DataFrame, events: pd.DataFrame,
                     cutoff: int, window_days: int = 7) -> pd.DataFrame:
    """Annotate causal activity of each subzone *since its current identity began*.

    A cluster created by a later merge is not presented as recent because an
    original line had old tests; only post-birth events count. Same-candle tests
    of constituent lines count once. We do not change any ranking or grade.
    """
    if cutoff % TF or window_days not in (3, 7, 14):
        raise ValueError('Corte cerrado 30m o ventana reciente inválida')
    if zones.empty:
        return zones.copy()
    needed={'zone_id','level_ids','members','created_at','center','lifecycle',
            'relative_tier','evidence_index'}
    if not needed.issubset(zones):
        raise ValueError('Snapshot V2.1 incompleto')
    if zones.zone_id.astype(str).duplicated().any():
        raise ValueError('Subzonas duplicadas')
    if not {'level_id','event_type','observed_closed_ts'}.issubset(events.columns):
        raise ValueError('Eventos HLL incompletos')
    e=events.loc[events.event_type.isin(EVENTS),
                 ['level_id','event_type','observed_closed_ts']].copy()
    e['observed_closed_ts']=pd.to_numeric(e.observed_closed_ts,errors='raise').astype('int64')
    if (e.observed_closed_ts%TF).any():
        raise ValueError('Evento sin vela cerrada 30m')
    # Full frozen events may include future events relative to a historical cutoff;
    # it is correct to exclude them, NEVER to score them.
    e=e.loc[e.observed_closed_ts.le(cutoff)]
    by_id={k:g.observed_closed_ts.to_numpy(dtype='int64')
           for k,g in e.groupby('level_id',sort=False)}
    start=cutoff-window_days*DAY
    result=[]
    for row in zones.itertuples(index=False):
        members=json.loads(row.level_ids) if isinstance(row.level_ids,str) else row.level_ids
        if not isinstance(members,list) or len(members)!=int(row.members) or \
           len(set(members))!=len(members) or any(not isinstance(m,str) or not m for m in members):
            raise ValueError('Miembros inválidos en subzona')
        born=int(row.created_at)
        if born>cutoff or born%TF:
            raise ValueError('Subzona futura o nacimiento no confirmado')
        times=np.concatenate([by_id.get(m,np.array([],dtype='int64')) for m in members])
        times=np.unique(times[(times>=born)&(times<=cutoff)])
        # Deduplicate identical confirmed closes across multiple original levels.
        recent=times[times>=start]
        last=int(times[-1]) if len(times) else None
        age=(cutoff-last)/DAY if last is not None else None
        is_new=born>=start
        active=last is not None and last>=start
        if active:
            status='INTERACCION_3D' if age<=3 else 'INTERACCION_RECIENTE'
        elif is_new:
            status='ZONA_NUEVA_SIN_INTERACCION_RECIENTE'
        else:
            status='SOLO_HISTORICA'
        result.append({'zone_id':str(row.zone_id), 'recent_status':status,
                       'has_recent_activity':bool(active or is_new),
                       'formed_in_window':bool(is_new),
                       'last_interaction_ts':last,
                       'days_since_last_interaction':age,
                       'interactions_in_window':int(len(recent))})
    extra=pd.DataFrame(result)
    return zones.merge(extra,on='zone_id',how='left',validate='one_to_one')


def select_recent_overlay(zones: pd.DataFrame, last_price: float,
                          excluded_ids: set[str] | None = None,
                          limit: int = 4, spacing_pct: float = .45,
                          max_distance_pct: float = 8.,
                          reserved_centers: list[float] | None = None,
                          include_partial_expired: bool = True) -> pd.DataFrame:
    """Supplement historical A-zone map with recent B/C/D zones near *current* price.

    Selected only if its causal snapshot is still inside the zone's TTL at
    cutoff (or has partially expired lines), and interaction/formation is recent.
    This intentionally does NOT upgrade the historic grade of any zone.
    """
    if not np.isfinite(last_price) or last_price<=0 or not (0<=limit<=20) or \
        not (.05<=spacing_pct<=3.) or not (0<max_distance_pct<=30):
        raise ValueError('Selección reciente inválida')
    if zones.empty or limit==0:
        return zones.iloc[0:0].copy()
    needed={'zone_id','center','recent_status','has_recent_activity',
            'days_since_last_interaction','interactions_in_window','lifecycle',
            'evidence_index','relative_tier'}
    if not needed.issubset(zones):
        raise ValueError('Faltan datos de actualidad de subzonas')
    excluded_ids=set(str(x) for x in (excluded_ids or set()))
    reserved_centers=list(reserved_centers or [])
    permitted=('ACTIVE_TTL','PARTIAL_EXPIRED') if include_partial_expired else ('ACTIVE_TTL',)
    df=zones.loc[zones.has_recent_activity.astype(bool) &
          zones.lifecycle.isin(permitted) &
          ~zones.zone_id.astype(str).isin(excluded_ids)].copy()
    if df.empty:return df
    df['distance_pct']=abs(pd.to_numeric(df.center,errors='raise')/last_price-1)*100
    df=df.loc[np.isfinite(df.distance_pct)&df.distance_pct.le(max_distance_pct)].copy()
    if df.empty:return df
    # Near-price is the principal ordering for a *local* chart. Among near
    # candidates, interaction recency and distinct confirmed candles matter.
    df['distance_band']=(df.distance_pct//2.0).astype(int)
    df['days_since_last_interaction']=pd.to_numeric(
        df.days_since_last_interaction,errors='coerce').fillna(999.0)
    df=df.sort_values(['distance_band','days_since_last_interaction',
                      'interactions_in_window','distance_pct','evidence_index','zone_id'],
                      ascending=[True,True,False,True,False,True],kind='stable')
    chosen=[]
    for r in df.itertuples(index=False):
        c=float(r.center)
        if any(100*abs(c-float(old))/min(c,float(old))<spacing_pct
               for old in reserved_centers if float(old)>0):
            continue
        if any(100*abs(c-float(other.center))/min(c,float(other.center))<spacing_pct
               for other in chosen):
            continue
        chosen.append(r)
        if len(chosen)==limit:break
    if not chosen:return df.iloc[0:0].copy()
    return df.set_index('zone_id',drop=False).loc[[r.zone_id for r in chosen]].reset_index(drop=True)
