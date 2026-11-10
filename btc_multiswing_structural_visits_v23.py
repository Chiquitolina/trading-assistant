#!/usr/bin/env python3
"""V2.3 | Auditar visitas a UNA banda Multi-Swing V2 fija, sin alterar V2/V2.2.

Agrupa episodios V2.2 en una visita si la separación entre el cierre de la
última vela que tocó la banda y la apertura de la próxima vela de contacto es
<= gap_minutes. Una visita se declara terminada SOLO después de observar
(gap_minutes + 1) velas 1m consecutivas SIN contacto, todas cerradas.

Los resultados de horizonte fijo se miden DESDE ese cierre conocido (nunca
retroactivamente desde el primer toque). Si inicia otra visita o termina el
freeze, el resultado es CENSORED, NO un recorrido de longitud completa.
Se informan máximos hacia ambos lados; no se clasifican defensas, rupturas,
retornos rentables ni señales LIVE. El grupo se observa retrospectivamente,
pero timestamps de disponibilidad son explícitos.
"""
from __future__ import annotations

import pandas as pd

from btc_multiswing_60d_explorer_v1 import M
from btc_multiswing_region_interactions_v22 import (
    _closed_frame, _stage_params, _state, audit_fixed_region,
)

ALLOWED_GAPS = (5, 15, 30, 60)
ALLOWED_HORIZONS = (60, 240, 720, 1440, 2880)
VISIT_COLUMNS = [
    'region_id', 'gap_minutes', 'visit_number', 'first_touch_ts',
    'first_known_ts', 'last_touch_close_ts', 'end_known_ts', 'status',
    'arrival_side', 'exit_side', 'contact_episodes', 'touch_minutes',
    'body_overlap_minutes', 'bridged_noncontact_minutes', 'span_minutes',
    'two_closes_above', 'two_closes_below',
    'in_visit_max_above_pct', 'in_visit_max_below_pct',
]
OUTCOME_COLUMNS = [
    'region_id', 'gap_minutes', 'visit_number', 'horizon_minutes',
    'evaluation_from_ts', 'observed_minutes', 'status',
    'max_above_band_pct', 'max_below_band_pct', 'last_known_ts',
]


def _pct_excursion(frame: pd.DataFrame, low: float, high: float):
    if frame.empty:
        return None, None
    return (max(0.0, (float(frame.high.max()) - high) * 100.0 / high),
            max(0.0, (low - float(frame.low.min())) * 100.0 / low))


def visit_gap_sensitivity(contacts: pd.DataFrame) -> pd.DataFrame:
    """Comparación descriptiva de umbrales sobre los mismos contactos V2.2."""
    if contacts.empty:
        return pd.DataFrame({'gap_minutes': list(ALLOWED_GAPS),
                             'visits': [0]*len(ALLOWED_GAPS)})
    if not {'start_ts','last_touch_close_ts'}.issubset(contacts.columns):
        raise ValueError('INVALID_CONTACT_SCHEMA')
    ordered = contacts.sort_values('start_ts',kind='mergesort')
    pairs = [(int(c.start_ts), int(c.last_touch_close_ts))
             for c in ordered.itertuples(index=False)]
    if any(end <= start for start,end in pairs) or any(
            pairs[i][0] < pairs[i-1][1] for i in range(1,len(pairs))):
        raise ValueError('INVALID_CONTACT_ORDER')
    rows=[]
    for gap in ALLOWED_GAPS:
        count=0
        prev=None
        for start,end in pairs:
            if prev is None or start-prev>gap*M:
                count+=1
            prev=end
        rows.append({'gap_minutes':gap,'visits':count})
    return pd.DataFrame(rows)


def audit_structural_visits(
    full: pd.DataFrame, stage, cutoff: int, *, gap_minutes: int = 30,
    horizons: tuple[int, ...] = ALLOWED_HORIZONS,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """(visitas, resultados POST, resumen); todo conocido solo hasta cutoff.

    `first_touch_ts` indica apertura de vela, `first_known_ts` su cierre.
    `end_known_ts` se determina en el cierre de la vela no-contacto número
    gap+1. Un visit OPEN_AT_CUTOFF no tiene end_known ni outcomes completos.
    """
    if type(gap_minutes) is not int or gap_minutes not in ALLOWED_GAPS:
        raise ValueError('INVALID_VISIT_GAP')
    if (not isinstance(horizons, tuple) or not horizons
            or len(set(horizons)) != len(horizons)
            or any(type(h) is not int or h not in ALLOWED_HORIZONS for h in horizons)):
        raise ValueError('INVALID_VISIT_HORIZONS')
    anchor, low, high, side = _stage_params(stage, cutoff, 240)
    region_id = str(stage.get('region_id', '') if hasattr(stage, 'get') else stage['region_id'])
    if not region_id:
        raise ValueError('INVALID_REGION_ID')
    frame = _closed_frame(full, anchor, int(cutoff))
    events, contacts, _ = audit_fixed_region(full, stage, int(cutoff), horizon_minutes=60)
    visits = []
    groups = []
    for rec in contacts.to_dict('records'):
        if not groups or int(rec['start_ts']) - int(groups[-1][-1]['last_touch_close_ts']) > gap_minutes*M:
            groups.append([rec])
        else:
            groups[-1].append(rec)

    for number, group in enumerate(groups, 1):
        first = int(group[0]['start_ts'])
        last_close = int(group[-1]['last_touch_close_ts'])
        # At cutoff we cannot infer that future bars will remain non-contact.
        finish = last_close + (gap_minutes + 1)*M
        closed = finish <= cutoff
        start_index = (first-anchor)//M
        last_index = (last_close-anchor)//M  # exclusive
        known_index = (finish-anchor)//M - 1 if closed else None
        if start_index < 0 or last_index > len(frame):
            raise ValueError('INVALID_CONTACT_POSITION')
        arrivals = (_state(float(frame.iloc[start_index-1].close),low,high)
                    if start_index > 0 else 'UNKNOWN')
        leaving = (_state(float(frame.iloc[known_index].close),low,high)
                   if closed else 'UNKNOWN')
        inside = frame.iloc[start_index:last_index]
        up, down = _pct_excursion(inside,low,high)
        in_scope = events.loc[
            (events.event_ts > first) & (events.event_ts <= last_close),
            'event_type',
        ]
        touch_count = sum(int(c['touch_minutes']) for c in group)
        span = (last_close-first)//M
        visits.append({
            'region_id':region_id, 'gap_minutes':gap_minutes,
            'visit_number':number, 'first_touch_ts':first,
            'first_known_ts':first+M,
            'last_touch_close_ts':last_close,
            'end_known_ts':finish if closed else None,
            'status':'ENDED_BY_INACTIVITY' if closed else 'OPEN_AT_CUTOFF',
            'arrival_side':arrivals, 'exit_side':leaving,
            'contact_episodes':len(group), 'touch_minutes':touch_count,
            'body_overlap_minutes':sum(int(c['body_overlap_minutes']) for c in group),
            'bridged_noncontact_minutes':span-touch_count,
            'span_minutes':span,
            'two_closes_above':int((in_scope=='TWO_CLOSES_ABOVE').sum()),
            'two_closes_below':int((in_scope=='TWO_CLOSES_BELOW').sum()),
            'in_visit_max_above_pct':up, 'in_visit_max_below_pct':down,
        })

    results = []
    for i, v in enumerate(visits):
        for horizon in horizons:
            base = dict(region_id=region_id, gap_minutes=gap_minutes,
                        visit_number=v['visit_number'], horizon_minutes=horizon)
            if v['end_known_ts'] is None:
                results.append({**base,'evaluation_from_ts':None,
                    'observed_minutes':0,'status':'VISIT_NOT_CLOSED',
                    'max_above_band_pct':None,'max_below_band_pct':None,
                    'last_known_ts':None})
                continue
            start_ts = int(v['end_known_ts'])
            next_start = (int(visits[i+1]['first_touch_ts']) if i+1<len(visits)
                          else None)
            target = start_ts + horizon*M
            stop = min(target, int(cutoff), next_start if next_start is not None else target)
            observed = max(0, (stop-start_ts)//M)
            left = (start_ts-anchor)//M
            portion = frame.iloc[left:left+observed]
            if len(portion) != observed:
                raise ValueError('INCOMPLETE_VISIT_POST_PATH')
            up, down = _pct_excursion(portion,low,high)
            if stop == target:
                status = 'FULL_HORIZON'
            elif next_start is not None and stop == next_start:
                status = 'NEXT_VISIT_CENSORED'
            else:
                status = 'CUTOFF_CENSORED'
            results.append({**base,'evaluation_from_ts':start_ts,
                'observed_minutes':observed,'status':status,
                'max_above_band_pct':up,'max_below_band_pct':down,
                'last_known_ts':stop})
    visits_df = pd.DataFrame(visits, columns=VISIT_COLUMNS)
    outcomes_df = pd.DataFrame(results, columns=OUTCOME_COLUMNS)
    summary = {'region_id':region_id,'side':side,'fixed_band_low':low,
        'fixed_band_high':high,'stage_known_from':anchor,'cutoff':int(cutoff),
        'gap_minutes':gap_minutes,'contact_episodes_v22':len(contacts),
        'visits':len(visits_df),'visits_closed':sum(v['end_known_ts'] is not None for v in visits),
        'visits_open':sum(v['end_known_ts'] is None for v in visits),
        'horizons_minutes':horizons,'descriptive_only':True,
        'outcomes_only_known_after_visit_closure':True}
    return visits_df, outcomes_df, summary
