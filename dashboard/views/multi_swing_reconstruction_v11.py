#!/usr/bin/env python3
"""Reconstruccion estructural causal PRE-TOUCH — research OFFLINE V1.1.

Usa los swings *ya confirmados* por el SwingDetector canónico. Cuenta
extremos físicos independientes sólo cuando el OHLC 1m permite demostrar
su identidad. No altera la etiqueta REACTION/INDECISIVE/FAILED ni crea señales.

Las cuatro ventanas son ventanas de *pivotes*; hay que tener cobertura OHLC 1m
completa y warmup/continuidad del detector en cada TF. Una ventana no cubierta
NO se interpreta como ausencia de pivotes.
"""
from __future__ import annotations

from collections import Counter
import math
from typing import Iterable, Mapping

from multi_swing_structural_quality_v1 import (
    MINUTE_MS as M, TF_MINUTES, ResearchInputError,
    _normalize_event, _normalize_pivot, _verify_bars, _physical_minute,
)

SUPPORTED_TF = ("1m", "5m", "15m")
DEFAULT_WINDOWS = (30, 60, 120, 240)
DEFAULT_BARS = {"1m": 5, "5m": 5, "15m": 3}
SCHEMA = "MULTI_SWING_RECONSTRUCTION_PRE_TOUCH_V11"


def _contiguous(frame_ts: set[int], first: int, last_exclusive: int, step: int) -> bool:
    """Comprueba el intervalo semiabierto completo; cero rellenos."""
    return all(t in frame_ts for t in range(first, last_exclusive, step))


def _coverage(timestamps: Mapping[str, set[int]], first: int, cutoff: int,
              bars: Mapping[str, int]) -> bool:
    """Exige todos los datos y warmup del detector antes del comienzo."""
    for tf in SUPPORTED_TF:
        step = TF_MINUTES[tf] * M
        # Primer comienzo de TF cubriendo cualquier pivot dentro de la ventana.
        aligned_start = first // step * step
        begin = aligned_start - bars[tf] * step
        # Ultima vela TF cerrada al instante de PRE-TOUCH.
        end = cutoff // step * step
        if begin >= end or not _contiguous(timestamps[tf], begin, end, step):
            return False
    return True


def _movement_metrics(points: list[dict], side: str, anchor: float) -> dict:
    """Secuencias de 5m opuestas y del lado de retest (no duplica TF)."""
    opposing = "HIGH" if side == "LOW" else "LOW"
    five = sorted((p for p in points if p['timeframe'] == '5m'),
                  key=lambda p: (p['pivot_ts'], p['side']))
    same = [p for p in five if p['side'] == side]
    opp = [p for p in five if p['side'] == opposing]

    def values(seq):
        if len(seq) < 2:
            return None, None
        # LONG/soporte: HIGHs descendentes indican presion hacia soporte.
        # SHORT/resistencia: LOWs ascendentes indican presion hacia resistencia.
        sign = -1 if side == 'LOW' else 1
        steps = [sign*(b['price']-a['price']) > 0 for a,b in zip(seq,seq[1:])]
        return sum(steps)/len(steps), sign*(seq[-1]['price']/seq[0]['price']-1)*100

    opp_fraction, opp_net = values(opp)
    # Estructura del lado propio: LONG LOWs crecientes; SHORT HIGHs decrecientes.
    if len(same) >= 2:
        sign = 1 if side == 'LOW' else -1
        same_fraction = sum(sign*(b['price']-a['price']) > 0
                            for a,b in zip(same,same[1:]))/(len(same)-1)
        same_net = sign*(same[-1]['price']/same[0]['price']-1)*100
    else:
        same_fraction, same_net = None, None
    return {'opposing_5m_count': len(opp), 'same_side_5m_count': len(same),
            'opposing_pressure_step_fraction': opp_fraction,
            'opposing_pressure_net_pct': opp_net,
            'own_reconstruction_step_fraction': same_fraction,
            'own_reconstruction_net_pct': same_net}


def compute_reconstruction(event: Mapping, pivots: Iterable[Mapping],
                           minute_bars: Iterable[Mapping],
                           tf_timestamps: Mapping[str, Iterable[int]],
                           *, windows: Iterable[int] = DEFAULT_WINDOWS,
                           proximity_pct: float = 0.10,
                           detector_bars: Mapping[str, int] = DEFAULT_BARS) -> dict:
    """Features estructurales a PRE-TOUCH (retest_ts, sin vela del retest).

    pivots incluye todos los swings confirmados de 1m/5m/15m, y la ancla
    15m original con su actionable_ts. La zona cercana a la ancla define
    contactos independientes; la secuencia 5m estudia TODOS los pivotes.
    """
    e = _normalize_event(event)
    cutoff = e['retest_ts']                  # estricto PRE-TOUCH
    # Una barra 15m abre en pivot_ts y las tres derechas deben CERRAR.
    # El journal historico puede conservar confirmed_timestamp como OPEN.
    min_safe_anchor = e['pivot_ts'] + 4 * 15 * M
    if min_safe_anchor > cutoff:
        raise ResearchInputError('ANCLA_15M_NO_CONFIRMADA_ANTES_DEL_RETEST')
    side = 'LOW' if e['side'] == 'LONG' else 'HIGH'
    if not math.isfinite(proximity_pct) or not 0 <= proximity_pct <= 5:
        raise ResearchInputError('proximity_pct invalida')
    requested = tuple(int(v) for v in windows)
    if not requested or any(w <= 0 or w > 1440 for w in requested) or len(set(requested)) != len(requested):
        raise ResearchInputError('ventanas de reconstruccion invalidas')
    bars = {tf: int(detector_bars[tf]) for tf in SUPPORTED_TF}
    if any(v not in (2, 3, 5) for v in bars.values()):
        raise ResearchInputError('configuracion de detector invalida')

    # Este corte excluye por contrato la vela 1m donde se decide REACTION.
    index = _verify_bars(minute_bars, cutoff)
    stamps = {tf: {int(t) for t in tf_timestamps.get(tf,()) if int(t) + TF_MINUTES[tf]*M <= cutoff}
              for tf in SUPPORTED_TF}
    stamps['1m'] = set(index)

    normalized = [_normalize_pivot(p) for p in pivots]
    canonical = [p for p in normalized if p['timeframe']=='15m'
                 and p['pivot_ts']==e['pivot_ts'] and p['side']==side
                 and math.isclose(p['price'],e['swing_price'],rel_tol=1e-10,abs_tol=1e-10)
                 and p['available_ts'] >= max(e['actionable_ts'],min_safe_anchor)]
    if not canonical:
        raise ResearchInputError('No hay ancla canónica 15m confirmada')
    # No exigir que el detector original pueda reproducir la ancla en el snapshot recortado.
    dedup={}
    for p in normalized:
        tf_step=TF_MINUTES[p['timeframe']]*M
        safe_confirmation=p['pivot_ts']+(bars[p['timeframe']]+1)*tf_step
        if p['available_ts'] < safe_confirmation:
            raise ResearchInputError('Pivote marcado disponible antes del cierre de barras derechas')
        if p['available_ts'] > cutoff or p['pivot_ts'] >= cutoff:
            continue
        if p['side'] not in ('LOW','HIGH'):
            continue
        key=(p['timeframe'],p['side'],p['pivot_ts'])
        previous=dedup.get(key)
        if previous is not None and (previous['price'] != p['price']
                                     or previous['available_ts'] != p['available_ts']):
            raise ResearchInputError('Pivotes duplicados contradictorios')
        dedup[key]=p
    eligible=list(dedup.values())

    out={'reconstruction_schema': SCHEMA,
         'structural_cutoff_ts': cutoff,
         'original_known_ts': e['known_ts'],
         'canonical_actionable_ts_journal':e['actionable_ts'],
         'canonical_available_ts_safe':max(e['actionable_ts'],min_safe_anchor),
         'cutoff_mode': 'PRE_TOUCH',
         'requested_windows_min': ','.join(map(str,requested)),
         'proximity_pct_v11': float(proximity_pct)}
    for w in requested:
        prefix=f'w{w}_'
        first=cutoff-w*M
        covered=_coverage(stamps,first,cutoff,bars)
        out[prefix+'coverage_state'] = 'COVERED' if covered else 'NOT_COVERED'
        names=('swing_count','near_zone_swings','near_zone_unique_extrema',
               'near_zone_unknown_identity','near_zone_shared_tf_extrema',
               'near_zone_independent_extrema','opposing_5m_count',
               'same_side_5m_count','opposing_pressure_step_fraction',
               'opposing_pressure_net_pct','own_reconstruction_step_fraction',
               'own_reconstruction_net_pct')
        if not covered:
            out.update({prefix+k:None for k in names})
            continue
        selected=[p for p in eligible if first <= p['pivot_ts'] < cutoff]
        out[prefix+'swing_count']=len(selected)
        direction=[p for p in selected if p['side']==side]
        near=[p for p in direction
              if abs((p['price']/e['swing_price']-1)*100) <= proximity_pct]
        out[prefix+'near_zone_swings']=len(near)
        checked=[]; invalid=Counter()
        for p in near:
            physical,state=_physical_minute(p,index)
            if state=='VERIFIED':
                checked.append((physical,p['timeframe']))
            else:
                invalid[state]+=1
        out[prefix+'near_zone_unknown_identity']=sum(invalid.values())
        if invalid:
            # Nunca presentar conteo parcial como cantidad exacta de extremos.
            out[prefix+'near_zone_unique_extrema']=None
            out[prefix+'near_zone_shared_tf_extrema']=None
            out[prefix+'near_zone_independent_extrema']=None
        else:
            ids={minute for minute, _ in checked}
            per_id=Counter(minute for minute,_ in checked)
            out[prefix+'near_zone_unique_extrema']=len(ids)
            out[prefix+'near_zone_shared_tf_extrema']=sum(n>1 for n in per_id.values())
            anchor_in_window=(first<=e['pivot_ts']<cutoff)
            out[prefix+'near_zone_independent_extrema']=max(0,len(ids)-int(anchor_in_window))
        out.update({prefix+k:v for k,v in _movement_metrics(selected,side,e['swing_price']).items()})
    return out
