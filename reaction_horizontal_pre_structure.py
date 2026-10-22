"""PRE-only HTF swing progression near an existing frozen Atlas V2 zone.

Research diagnostic, not a new support/resistance detector or trade entry rule.
No current retest candle, future 1m path, or TP/SL outcomes are accessed here.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import math
import sqlite3

import pandas as pd

MINUTE = 60_000
WINDOW_HOURS = 24
RECENCY_HOURS = 12
TF_STEPS = {'15m': 15 * MINUTE, '30m': 30 * MINUTE}
LEVEL_NEAR_ATR = 1.5  # unchanged Atlas V2 context-distance band; NOT tuned to outcomes
UNKNOWN = 'NO_EVALUABLE'


def _finite(value):
    try:
        v = float(value)
        return v if math.isfinite(v) else None
    except (ValueError, TypeError):
        return None


def _empty(reason='SIN_HISTORICO', *, role='INDETERMINADO', tf=None):
    return dict(structure_pre=UNKNOWN, structure_relation_pre=UNKNOWN,
                structure_role_pre=role, structure_tf=tf,
                structure_pivot_first=None, structure_pivot_second=None,
                structure_pivot_first_ts=None, structure_pivot_second_ts=None,
                structure_reason=reason)


def confirmed_pivots(bars, kind, left=3, right=3):
    """Return (pivot_open_ts, price, confirmation_close_ts) on PRE closed bars only."""
    if kind not in ('HIGH', 'LOW'):
        raise ValueError('kind must be HIGH/LOW')
    col = 'high' if kind == 'HIGH' else 'low'
    x = bars[col].tolist()
    stamps = bars['timestamp'].tolist()
    step = int(bars['step_ms'].iloc[0]) if len(bars) else 0
    found = []
    for i in range(left, len(x)-right):
        around = x[i-left:i] + x[i+1:i+right+1]
        is_pivot = (all(x[i] > q for q in around) if kind == 'HIGH'
                    else all(x[i] < q for q in around))
        if is_pivot:
            found.append((int(stamps[i]), float(x[i]), int(stamps[i+right]) + step))
    return found


def classify_structure_pre(event, candles):
    """Classify HTF swing pressure using only candles fully closed at retest open.

    The role is based on the 1m PRE arrival side, not on LONG/SHORT or outcome.
    A lower high against a support / higher low into resistance merely suggests
    directional compression: neither a triangle nor an eventual breakout is proven.
    """
    tf = str(event.get('primary_timeframe', ''))
    role = ('SOPORTE' if event.get('arrival_origin_pre') == 'ABOVE' else
            'RESISTENCIA' if event.get('arrival_origin_pre') == 'BELOW' else
            'INDETERMINADO')
    if tf not in TF_STEPS:
        return _empty('TIMEFRAME_DESCONOCIDO', role=role, tf=tf)
    if not bool(event.get('all_covered', False)):
        return _empty('COBERTURA_INCOMPLETA', role=role, tf=tf)
    if role == 'INDETERMINADO':
        return _empty('LADO_DE_LLEGADA_INDETERMINADO', role=role, tf=tf)
    distance = _finite(event.get('primary_distance_atr'))
    if distance is None:
        return _empty('DISTANCIA_ATR_DESCONOCIDA', role=role, tf=tf)
    if distance > LEVEL_NEAR_ATR:
        return _empty('NIVEL_DISTANTE', role=role, tf=tf)
    try:
        r = int(event['retest_ts'])
    except (TypeError, ValueError, KeyError):
        return _empty('RETEST_INVALIDO', role=role, tf=tf)
    step = TF_STEPS[tf]
    if candles is None or candles.empty:
        return _empty('SIN_VELAS_HTF', role=role, tf=tf)
    bars = candles.reset_index().rename(columns={'open_ts': 'timestamp'})
    if 'timestamp' not in bars.columns:
        return _empty('SIN_TIMESTAMP_HTF', role=role, tf=tf)
    required = ('timestamp','open','high','low','close')
    if any(c not in bars.columns for c in required):
        return _empty('CAMPOS_HTF_INCOMPLETOS', role=role, tf=tf)
    # Filtering by close time also excludes a partially formed HTF bar.
    bars = bars[(bars.timestamp + step <= r)].sort_values('timestamp')
    n = WINDOW_HOURS * 60 * MINUTE // step
    bars = bars.tail(n).copy().reset_index(drop=True)
    if len(bars) != n:
        return _empty('VENTANA_24H_INCOMPLETA', role=role, tf=tf)
    stamps = [int(v) for v in bars.timestamp]
    if any(b-a != step for a,b in zip(stamps, stamps[1:])):
        return _empty('GAP_HTF_EN_VENTANA', role=role, tf=tf)
    # Verify the last closed HTF candle is recent enough at the retest instant.
    if not (stamps[-1] + step <= r < stamps[-1] + 2*step):
        return _empty('HTF_DESACTUALIZADO', role=role, tf=tf)
    for row in bars[list(required[1:])].itertuples(index=False, name=None):
        o,h,l,c = [_finite(v) for v in row]
        if None in (o,h,l,c) or min(o,h,l,c) <= 0 or h < max(o,c,l) or l > min(o,c):
            return _empty('OHLC_INVALIDO', role=role, tf=tf)
    bars['step_ms'] = step
    pivots = confirmed_pivots(bars, 'HIGH' if role == 'SOPORTE' else 'LOW')
    pivots = [p for p in pivots if p[2] <= r]
    if len(pivots) < 2:
        return _empty('MENOS_DE_DOS_PIVOTES_3X3', role=role, tf=tf)
    a,b = pivots[-2:]
    if b[2] < r - RECENCY_HOURS * 60 * MINUTE:
        return _empty('PIVOTES_NO_RECIENTES', role=role, tf=tf)
    if role == 'SOPORTE':
        pattern = 'LH_HACIA_SOPORTE' if b[1] < a[1] else 'SIN_LH_CONFIRMADOS'
    else:
        pattern = 'HL_HACIA_RESISTENCIA' if b[1] > a[1] else 'SIN_HL_CONFIRMADOS'
    if pattern in ('LH_HACIA_SOPORTE', 'HL_HACIA_RESISTENCIA'):
        against = ((role == 'SOPORTE' and str(event.get('side')) == 'LONG') or
                   (role == 'RESISTENCIA' and str(event.get('side')) == 'SHORT'))
        relation = 'PRESION_CONTRA_REACTION' if against else 'PRESION_A_FAVOR_REACTION'
    else:
        relation = 'SIN_PRESION_CLARA'
    return dict(structure_pre=pattern, structure_relation_pre=relation,
                structure_role_pre=role, structure_tf=tf,
                structure_pivot_first=a[1],structure_pivot_second=b[1],
                structure_pivot_first_ts=a[0],structure_pivot_second_ts=b[0],
                structure_reason='DOS_PIVOTES_CONFIRMADOS_PRE')


def enrich_pre_structure(project, events):
    """Attach structural PRE context to full cohort using existing Atlas HTF readers.

    Read-only, per-symbol reuse. Fail closed (NO_EVALUABLE) on missing datasets.
    Does not alter Atlas data, previous setup, First Touch outcomes or LIVE.
    """
    if events.empty:
        return events.copy()
    import reaction_structural_level_atlas_v2 as atlas
    root = Path(project) / atlas.v1.lineage.ROOT_REL
    context = root / atlas.v1.lineage.CTX_NAME
    history = root / atlas.v1.lineage.HISTORY_NAME
    if not context.is_file():
        raise FileNotFoundError(str(context))
    annotation = []
    # The context bars can be large; retain only the current symbol.
    with closing(sqlite3.connect(context.resolve().as_uri()+'?mode=ro', uri=True)) as con:
        with closing(sqlite3.connect(history.resolve().as_uri()+'?mode=ro',uri=True)) if history.is_file() else _nullcon() as hist:
            prev_symbol = None
            tf_data = {}
            for ev in events.sort_values(['symbol','retest_ts']).to_dict('records'):
                sym = str(ev['symbol'])
                if sym != prev_symbol:
                    prev_symbol = sym
                    try:
                        df15,df30,_ = atlas.v1.v4._merge_tf(con,hist,sym)
                        tf_data = {'15m':df15,'30m':df30}
                    except (sqlite3.Error, ValueError, KeyError) as exc:
                        tf_data = {}
                try:
                    out = classify_structure_pre(ev, tf_data.get(str(ev.get('primary_timeframe'))))
                except (ValueError,TypeError,KeyError) as exc:
                    out = _empty('LECTURA_HTF_INVALIDA',tf=ev.get('primary_timeframe'))
                annotation.append({'event_key':ev['event_key'],**out})
    return events.merge(pd.DataFrame(annotation),on='event_key',validate='one_to_one')


class _nullcon:
    def __enter__(self): return None
    def __exit__(self,*args): return False
