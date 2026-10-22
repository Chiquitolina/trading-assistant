"""Read-only, PRE-only semantic descriptions of the audited lifecycle states.

The underlying engine defines an 'initial side' as the first two closes
outside the frozen band after zone formation.  That side is NOT necessarily
the origin of a support/resistance level.  Avoid calling every return a
'support recovery'. This presentation layer never sees a trade outcome.
"""
from __future__ import annotations

ACTIVE = 'NIVEL_VIGENTE_PRE'
LOST = 'NIVEL_PERDIDO_PRE'
FAILED = 'RETEST_FALLIDO_PRE'
RECLAIMED = 'NIVEL_RECUPERADO_PRE'
UNKNOWN = 'INDETERMINADO_PRE'
NO_NEAR = 'SIN_NIVEL_IMPORTANTE_CERCANO_PRE'


def _side(side):
    return str(side).upper() if side is not None else ''


def meaning_label(state, initial_side):
    """Literal, side-specific PRE description; never infer intent or outcome."""
    s = _side(initial_side)
    if state == RECLAIMED:
        if s == 'ABOVE':
            return 'Regresó al lado SUPERIOR inicialmente aceptado'
        if s == 'BELOW':
            return 'Regresó al lado INFERIOR inicialmente aceptado'
        return 'Retorno al lado inicial sin lado verificable'
    if state == LOST:
        if s == 'ABOVE':
            return 'Perdió el lado SUPERIOR aceptado (ahora debajo)'
        if s == 'BELOW':
            return 'Superó el lado INFERIOR aceptado (ahora encima)'
        return 'Cambio de lado con origen no verificable'
    if state == FAILED:
        if s == 'ABOVE':
            return 'Rompió a la BAJA; retest desde debajo rechazado'
        if s == 'BELOW':
            return 'Rompió al ALZA; retest desde encima sostenido'
        return 'Retest posterior a cambio de lado sin origen verificable'
    if state == ACTIVE:
        if s == 'ABOVE':
            return 'Permanece sobre la zona inicialmente aceptada'
        if s == 'BELOW':
            return 'Permanece bajo la zona inicialmente aceptada'
        return 'Vigente sin lado inicial verificable'
    if state == UNKNOWN:
        return 'Indeterminado / cobertura o confirmación insuficiente'
    if state == NO_NEAR:
        return 'Sin nivel importante cercano PRE'
    return 'Estado desconocido: revisar versión del estudio'


def origin_consistency(initial_side, origin_kind):
    """Optional diagnostic only, not a trading rule nor a status override.

    LOW is originally a support-type pivot; HIGH a resistance-type pivot.
    Two closes after formation can show another side if the zone has flipped.
    """
    side = _side(initial_side)
    kind = _side(origin_kind)
    if side not in ('ABOVE', 'BELOW') or kind not in ('HIGH','LOW'):
        return 'NO_EVALUABLE'
    return 'COINCIDE' if (side=='ABOVE' and kind=='LOW') or (side=='BELOW' and kind=='HIGH') else 'DIFIERE: posible flip o base inicial equivocada'
