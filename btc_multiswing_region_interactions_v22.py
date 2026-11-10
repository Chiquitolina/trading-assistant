#!/usr/bin/env python3
"""V2.2: auditoría física, read-only, de UNA etapa geométrica Multi-Swing.

La banda se congela con los límites que eran conocidos en `known_from`.
Cada evento de velas usa únicamente barras de 1 minuto CERRADAS al corte.
Es una descripción retrospectiva; no crea niveles ACTIVE, defensas, señales,
reconstrucciones, ni modifica la etapa original, el catálogo o LIVE.

Semántica estricta:
* CONTACT: rango [low,high] de una vela 1m intersecta la banda fija.
* CROSS: cambia la clasificación del CIERRE respecto a la banda.
* TWO_CLOSES: segundo cierre CONSECUTIVO fuera de la misma cara, una vez
  por racha. No se le llama aceptación, defensa ni pérdida estructural.
* EXCURSION: máximo precio visto DESPUÉS del contacto hasta horizonte fijo,
  siguiente contacto o corte. Su observación es posterior al contacto.
"""
from __future__ import annotations

import math
from typing import Any

import pandas as pd

from btc_multiswing_60d_explorer_v1 import M, group_asof

EVENT_COLUMNS = ['event_type','event_ts','bar_open_ts','side','band_low','band_high',
                 'close_price','contact_number','description']
CONTACT_COLUMNS = ['contact_number','start_ts','last_touch_close_ts','end_known_ts',
                   'touch_minutes','body_overlap_minutes','first_touch_price',
                   'last_touch_price','status','post_minutes_observed',
                   'max_above_band_pct','max_below_band_pct','post_status',
                   'post_known_until']
NEW_EXTREMA_COLUMNS = ['physical_id','side','price','plot_ts',
                        'first_available_asof','tf_set_asof','representations_asof']


def _stage_params(stage: Any, cutoff: int, horizon_minutes: int):
    try:
        t = int(stage['known_from'])
        low = float(stage['band_low'])
        high = float(stage['band_high'])
        side = str(stage['side'])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError('INVALID_FIXED_REGION_STAGE') from e
    if (side not in ('HIGH','LOW') or not math.isfinite(low)
            or not math.isfinite(high) or low <= 0 or high < low
            or t % M != 0 or int(cutoff) % M != 0 or t >= int(cutoff)
            or horizon_minutes not in (60, 240, 720)):
        raise ValueError('INVALID_FIXED_REGION_STAGE')
    return t, low, high, side


def _closed_frame(full: pd.DataFrame, anchor: int, cutoff: int) -> pd.DataFrame:
    cols = ('timestamp','open','high','low','close')
    if not set(cols).issubset(full.columns):
        raise ValueError('MISSING_FROZEN_1M_COLUMNS')
    # The frozen reader verifies hashes and the entire contiguous feed. This
    # independent guard makes synthetic/reused input equally safe.
    df = full.loc[(full.timestamp >= anchor) &
                  (full.timestamp + M <= cutoff), list(cols)].copy()
    if df.empty:
        raise ValueError('NO_CLOSED_1M_AFTER_STAGE')
    for col in cols:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    if df[list(cols)].isna().any().any():
        raise ValueError('INVALID_FROZEN_1M_NUMERIC')
    if not all(math.isfinite(float(x)) for x in df[list(cols)].to_numpy().ravel()):
        raise ValueError('INVALID_FROZEN_1M_NUMERIC')
    df = df.sort_values('timestamp').reset_index(drop=True)
    stamps = df.timestamp.astype('int64')
    if (stamps.iloc[0] != anchor or stamps.iloc[-1]+M != cutoff
            or (stamps % M != 0).any() or (stamps.diff().iloc[1:] != M).any()):
        raise ValueError('NONCONTIGUOUS_FIXED_REGION_1M')
    if ((df[['open','high','low','close']] <= 0).any().any()
            or (df.high < df[['open','close','low']].max(axis=1)).any()
            or (df.low > df[['open','close','high']].min(axis=1)).any()):
        raise ValueError('INVALID_FROZEN_1M_OHLC')
    return df


def _state(close: float, low: float, high: float) -> str:
    if close > high:
        return 'ABOVE'
    if close < low:
        return 'BELOW'
    return 'INSIDE'


def audit_fixed_region(full: pd.DataFrame, stage: Any, cutoff: int,
                       *, horizon_minutes: int = 240) -> tuple[pd.DataFrame,pd.DataFrame,dict]:
    """Audit one fixed, causally known band against 1m bars through cutoff.

    `cutoff` is the maximum allowed closed-bar END, not an OHLC timestamp.
    Returns (events, contacts, summary). Future bars provided in `full` are
    ignored, and cannot change any result for a fixed cutoff.
    """
    anchor, low, high, side = _stage_params(stage,cutoff,horizon_minutes)
    df = _closed_frame(full,anchor,int(cutoff))
    events: list[dict] = []
    contacts: list[dict] = []
    is_touch = (df.high >= low) & (df.low <= high)
    body_overlap = (df[['open','close']].max(axis=1) >= low) & (
                    df[['open','close']].min(axis=1) <= high)
    previous_state: str | None = None
    outside_run = 0
    active: dict | None = None

    def emit(kind: str, index: int, desc: str, number: int | None = None):
        bar = df.iloc[index]
        events.append({'event_type':kind,
                       'event_ts':int(bar.timestamp)+M,
                       'bar_open_ts':int(bar.timestamp), 'side':side,
                       'band_low':low,'band_high':high,
                       'close_price':float(bar.close),
                       'contact_number':number, 'description':desc})

    for i, row in enumerate(df.itertuples(index=False)):
        state = _state(float(row.close),low,high)
        if bool(is_touch.iat[i]):
            if active is None:
                active = {'contact_number':len(contacts)+1,'start_index':i,
                          'last_index':i,'touch_minutes':0,
                          'body_overlap_minutes':0,
                          'first_touch_price':float(row.close),
                          'last_touch_price':float(row.close)}
                emit('CONTACT_START',i,'Primera vela 1m que intersecta la banda',active['contact_number'])
            active['last_index'] = i
            active['touch_minutes'] += 1
            active['body_overlap_minutes'] += int(bool(body_overlap.iat[i]))
            active['last_touch_price'] = float(row.close)
        elif active is not None:
            # Episode ended only once this following NON-contact bar closes.
            active['end_known_ts'] = int(row.timestamp)+M
            active['status'] = 'ENDED_BY_NONCONTACT_BAR'
            contacts.append(active)
            emit('CONTACT_END',i,'Fin conocido tras vela 1m sin intersección',active['contact_number'])
            active = None

        if previous_state is not None and state != previous_state:
            if state == 'ABOVE':
                emit('CLOSE_CROSS_ABOVE',i,f'Cierre cambia {previous_state} → ABOVE')
            elif state == 'BELOW':
                emit('CLOSE_CROSS_BELOW',i,f'Cierre cambia {previous_state} → BELOW')
            else:
                emit('CLOSE_ENTER_BAND',i,f'Cierre cambia {previous_state} → INSIDE')
        outside_run = outside_run+1 if state == previous_state and state!='INSIDE' else (
            1 if state!='INSIDE' else 0)
        if outside_run == 2:
            emit('TWO_CLOSES_'+state,i,'Segundo cierre consecutivo fuera de la misma cara')
        previous_state = state

    if active is not None:
        active['end_known_ts'] = None
        active['status'] = 'OPEN_AT_CUTOFF'
        contacts.append(active)

    # Departure/excursion values are observed AFTER each episode. They must
    # never be treated as information available at contact START.
    for contact in contacts:
        begin = contact.pop('start_index')
        last = contact.pop('last_index')
        contact['start_ts'] = int(df.iloc[begin].timestamp)
        contact['last_touch_close_ts'] = int(df.iloc[last].timestamp)+M
        end = min(len(df),last+1+horizon_minutes)
        interval = df.iloc[last+1:end]
        if not interval.empty:
            next_touch = is_touch.iloc[last+1:end].to_numpy().nonzero()[0]
            if len(next_touch):
                interval = interval.iloc[:int(next_touch[0])]
                post_status = 'NEXT_CONTACT_CENSORED'
            elif len(interval) == horizon_minutes:
                post_status = 'FULL_HORIZON'
            else:
                post_status = 'CUTOFF_CENSORED'
        else:
            post_status = 'CUTOFF_CENSORED'
        n = len(interval)
        contact['post_minutes_observed'] = n
        contact['max_above_band_pct'] = (max(0., (float(interval.high.max())-high)/high*100)
                                          if n else None)
        contact['max_below_band_pct'] = (max(0.,(low-float(interval.low.min()))/low*100)
                                          if n else None)
        contact['post_status'] = post_status
        contact['post_known_until'] = (int(interval.timestamp.iloc[-1])+M if n
                                       else int(df.timestamp.iloc[last])+M)

    out_events = pd.DataFrame(events,columns=EVENT_COLUMNS)
    out_contacts = pd.DataFrame(contacts,columns=CONTACT_COLUMNS)
    summary = {
        'stage_known_from':anchor,'cutoff':int(cutoff),'side':side,
        'fixed_band_low':low,'fixed_band_high':high,
        'closed_minutes':len(df),'contact_episodes':len(out_contacts),
        'close_cross_above':int((out_events.event_type=='CLOSE_CROSS_ABOVE').sum()),
        'close_cross_below':int((out_events.event_type=='CLOSE_CROSS_BELOW').sum()),
        'two_closes_above':int((out_events.event_type=='TWO_CLOSES_ABOVE').sum()),
        'two_closes_below':int((out_events.event_type=='TWO_CLOSES_BELOW').sum()),
        'contact_minutes':int(is_touch.sum()),
        'last_close_position':_state(float(df.close.iloc[-1]),low,high),
        'horizon_minutes':horizon_minutes,
        'descriptive_only':True,
    }
    return out_events,out_contacts,summary


def later_compatible_extrema(known: pd.DataFrame, stage: Any, cutoff: int) -> pd.DataFrame:
    """Verified later pivots in locked band; NOT classified as successors.

    Scope is only the TF/side selection of already-filtered `known`.
    No retrospective physical_extrema_groups files are consulted.
    """
    anchor, low, high, side = _stage_params(stage,cutoff,240)
    out = pd.DataFrame(columns=NEW_EXTREMA_COLUMNS)
    if known.empty:
        return out
    if (known['available_ts'].isna().any() or (known['available_ts'] > int(cutoff)).any()):
        raise ValueError('FUTURE_COMPATIBLE_EXTREMUM_INPUT')
    grouped=group_asof(known,collapse=True,multi_only=False)
    eligible=grouped[
        grouped.physical_identity_state.eq('VERIFIED')
        & grouped.side.eq(side)
        & grouped.price.between(low,high,inclusive='both')
        & (grouped.first_available_asof > anchor)
        & (grouped.first_available_asof <= cutoff)
    ]
    return eligible[NEW_EXTREMA_COLUMNS].sort_values(
        ['first_available_asof','plot_ts','physical_id'],kind='mergesort'
    ).reset_index(drop=True)
