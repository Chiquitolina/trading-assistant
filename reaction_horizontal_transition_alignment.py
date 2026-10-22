"""PRE-only directional comparison of a CONFIRMED horizontal-zone transition.

Consumes the existing, audited lifecycle dataset. Does not select levels, use
REACTION candle OHLC, outcome, First Touch, or recompute HTF transitions.

`ALINEADA` means LONG with the confirmed price side ABOVE the frozen zone or
SHORT with confirmed side BELOW. It does NOT predict or confirm a profitable
trade, a further breakout, or a bounce. Other states are deliberately kept
apart so 'no level' never becomes a negative control by accident.
"""
from __future__ import annotations

import math
import pandas as pd

VERSION = 'horizontal-transition-alignment-pre-v1.0'
ALIGN = 'ALINEADA_CON_TRANSICION_PRE'
OPPOSE = 'CONTRARIA_A_TRANSICION_PRE'
NO_CHANGE = 'SIN_TRANSICION_CONFIRMADA_PRE'
NO_LEVEL = 'SIN_NIVEL_CERCANO_PRE'
INDET = 'INDETERMINADA_PRE'

LABELS = {
    ALIGN: 'Sí · REACTION alineada con transición',
    OPPOSE: 'No · REACTION contraria a transición',
    INDET: 'Indeterminada · confirmación/cobertura insuficiente',
    NO_CHANGE: 'Nivel vigente · sin transición confirmada',
    NO_LEVEL: 'Sin nivel importante cercano PRE',
}
TRANSITIONS = {
    'NIVEL_PERDIDO_PRE',
    'RETEST_FALLIDO_PRE',
    'NIVEL_RECUPERADO_PRE',
}

def _str(x):
    if x is None or pd.isna(x):
        return ''
    return str(x).strip().upper()


def _time(x):
    try:
        v = float(x)
        return int(v) if math.isfinite(v) else None
    except (ValueError, TypeError):
        return None


def classify_transition(row):
    """Return classification based strictly on PRE lifecycle fields and side.

    Returns (label, confirmed_side, reason). Fails CLOSED if a state conflicts
    with the last confirmed price side or transition timestamps are missing.
    """
    state = _str(row.get('lifecycle_state'))
    if state == 'SIN_NIVEL_IMPORTANTE_CERCANO_PRE':
        return NO_LEVEL, None, 'NO_SELECTED_IMPORTANT_LEVEL'
    if state == 'NIVEL_VIGENTE_PRE':
        return NO_CHANGE, None, 'NO_CONFIRMED_TRANSITION'
    if state not in TRANSITIONS:
        return INDET, None, 'NO_VALID_TRANSITION_STATE'

    if _str(row.get('history_quality')) != 'COMPLETE_POST_FORMATION':
        return INDET, None, 'INCOMPLETE_PRE_HISTORY'
    # Atlas V2 `event_map.csv` also has a column named `reason` (its own
    # provenance, unrelated to lifecycle confirmation). The CLI audit reads
    # the raw lifecycle `reason`; the Streamlit view renames it to
    # `lifecycle_reason` on merge. Prefer that explicitly whenever it exists.
    # If present but blank, fail CLOSED; never substitute the Atlas reason.
    if 'lifecycle_reason' in row:
        audited_reason = _str(row.get('lifecycle_reason'))
    else:
        audited_reason = _str(row.get('reason'))
    if audited_reason != 'CLASSIFIED_FROM_CLOSED_PRE_HTF_ONLY':
        return INDET, None, 'NON_AUDITED_PRE_CLASSIFICATION'

    init = _str(row.get('initial_side'))
    current = _str(row.get('current_confirmed_side'))
    direction = _str(row.get('side'))
    if init not in ('ABOVE', 'BELOW') or current not in ('ABOVE', 'BELOW') or direction not in ('LONG','SHORT'):
        return INDET, None, 'MISSING_OR_INVALID_SIDE'

    if state == 'NIVEL_RECUPERADO_PRE':
        if current != init:
            return INDET, None, 'RECLAIM_SIDE_NOT_INITIAL'
        last_transition = _time(row.get('last_reclaim_close_ts'))
        last_break = _time(row.get('last_break_close_ts'))
        if last_break is None or last_transition is None or last_transition <= last_break:
            return INDET, None, 'MISSING_RECLAIM_AFTER_BREAK'
    else:
        if current == init:
            return INDET, None, 'LOST_SIDE_MATCHES_INITIAL'
        last_transition = _time(row.get('last_break_close_ts'))
        if last_transition is None:
            return INDET, None, 'MISSING_BREAK_CONFIRMATION'
        if state == 'RETEST_FALLIDO_PRE':
            last_failed = _time(row.get('last_failed_retest_close_ts'))
            if last_failed is None or last_failed <= last_transition:
                return INDET, None, 'MISSING_POST_BREAK_RETEST'

    retest_ts = _time(row.get('retest_ts'))
    last_pre_close_ts = _time(row.get('last_pre_close_ts'))
    if (retest_ts is None or last_pre_close_ts is None or
            last_pre_close_ts > retest_ts or last_transition > retest_ts):
        return INDET, None, 'TRANSITION_TIME_NOT_PRE'

    aligned = (direction == 'LONG' and current == 'ABOVE') or (direction == 'SHORT' and current == 'BELOW')
    return (ALIGN if aligned else OPPOSE), current, 'CONFIRMED_SIDE_VS_REACTION_DIRECTION'


def add_transition_alignment(frame):
    """Dataframe projection. Preserve rows and outcomes unchanged."""
    required = {'event_key','side','lifecycle_state','initial_side',
                'current_confirmed_side','history_quality','last_break_close_ts',
                'last_reclaim_close_ts','last_failed_retest_close_ts',
                'last_pre_close_ts','retest_ts'}
    absent = required - set(frame.columns)
    if absent:
        raise ValueError('Faltan columnas de ciclo PRE: '+', '.join(sorted(absent)))
    if not ('lifecycle_reason' in frame.columns or 'reason' in frame.columns):
        raise ValueError('Falta motivo/auditoría de ciclo PRE')
    if frame.event_key.duplicated().any():
        raise ValueError('Eventos duplicados: revisar tf_filter')
    out = frame.copy()
    if out.empty:
        for key in ('transition_alignment','transition_confirmed_side','transition_alignment_reason'):
            out[key] = pd.Series(dtype='object')
        return out
    categories = out.apply(lambda r: classify_transition(r), axis=1)
    out['transition_alignment'] = [r[0] for r in categories]
    out['transition_confirmed_side'] = [r[1] for r in categories]
    out['transition_alignment_reason'] = [r[2] for r in categories]
    return out
