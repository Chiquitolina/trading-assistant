"""Multi-Swing Structural Quality V1 — núcleo causal OFFLINE (research only).

Fase 1: convierte un primer retest canónico y pivotes ya confirmados en
features de confluencia e identidad física, verificando extremos por OHLC 1m.

No detecta REACTIONs, no altera el scanner, no produce órdenes ni lee Redis.
No utiliza resultados posteriores al retest. Nombres y unidades de entrada
son explícitos: timestamps Unix UTC en milisegundos y porcentajes como 0.10.

Entrada normalizada (el adaptador de SQLite/Continuous se construirá cuando
estén disponibles las fuentes vigentes):
  event = {symbol, side LONG|SHORT, status, pivot_ts, actionable_ts,
           retest_ts, known_ts, swing_price, event_key [opcional]}
  pivots = [{timeframe 1m|5m|15m, side LOW|HIGH, pivot_ts,
             available_ts, price, prominence_pct [opcional], source [opcional]}]
  minute_bars = [{timestamp, high, low}, ...] de 1m verificado.

La coincidencia primaria de TF se restringe a pivotes cuya vela de origen
está dentro de la vela 15m del pivot canónico. Es una definición V1 explícita,
NO una réplica certificada del selector actual de Multi-Swing Confluence.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable, Mapping

MINUTE_MS = 60_000
TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15}
SUPPORTED_STATUS = {"REACTION", "TOUCH_INDECISIVE", "TOUCH_FAILED"}


class ResearchInputError(ValueError):
    """Contrato de datos inválido. No debe traducirse a NO_SIGNAL."""


def _integer(row: Mapping[str, Any], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResearchInputError(f"{key} debe ser epoch UTC en milisegundos (int)")
    return value


def _finite_positive(row: Mapping[str, Any], key: str) -> float:
    try:
        value = float(row[key])
    except (KeyError, ValueError, TypeError) as exc:
        raise ResearchInputError(f"{key} inválido") from exc
    if not math.isfinite(value) or value <= 0:
        raise ResearchInputError(f"{key} debe ser finito y positivo")
    return value


def _same_price(a: float, b: float) -> bool:
    # Comparación instrumental, NO tolerancia de proximidad de niveles.
    # Si una fuente redondeó el pivot, la validación quedará UNKNOWN.
    return math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-10)


def _normalize_event(row: Mapping[str, Any]) -> dict[str, Any]:
    out = {"symbol": str(row.get("symbol", "")).strip().upper(),
           "side": row.get("side"), "status": row.get("status"),
           "pivot_ts": _integer(row, "pivot_ts"),
           "actionable_ts": _integer(row, "actionable_ts"),
           "retest_ts": _integer(row, "retest_ts"),
           "known_ts": _integer(row, "known_ts"),
           "swing_price": _finite_positive(row, "swing_price")}
    if not out["symbol"] or out["side"] not in ("LONG", "SHORT"):
        raise ResearchInputError("Evento sin símbolo o dirección canónica")
    if out["status"] not in SUPPORTED_STATUS:
        raise ResearchInputError("Estado de primer retest desconocido")
    if out["known_ts"] != out["retest_ts"] + MINUTE_MS:
        raise ResearchInputError("known_ts debe ser cierre de vela 1m del retest")
    if (out["pivot_ts"] % (15 * MINUTE_MS)
            or out["retest_ts"] % MINUTE_MS
            or out["actionable_ts"] % MINUTE_MS):
        raise ResearchInputError("Alineación temporal inválida")
    if not (out["pivot_ts"] + 15 * MINUTE_MS <= out["actionable_ts"]
            <= out["retest_ts"]):
        raise ResearchInputError("Swing original aún no confirmado o tiempo inválido")
    out["event_key"] = str(row.get("event_key") or
                           f'{out["symbol"]}|{out["side"]}|{out["retest_ts"]}')
    return out


def _normalize_pivot(row: Mapping[str, Any]) -> dict[str, Any]:
    tf = str(row.get("timeframe", ""))
    if tf not in TF_MINUTES:
        raise ResearchInputError(f"Timeframe no admitido: {tf!r}")
    out = {"timeframe": tf, "side": row.get("side"),
           "pivot_ts": _integer(row, "pivot_ts"),
           "available_ts": _integer(row, "available_ts"),
           "price": _finite_positive(row, "price"),
           "prominence_pct": row.get("prominence_pct"),
           "source": str(row.get("source") or "UNSPECIFIED")}
    if out["side"] not in ("LOW", "HIGH"):
        raise ResearchInputError("Side de pivot debe ser LOW o HIGH")
    tf_ms = TF_MINUTES[tf] * MINUTE_MS
    if out["pivot_ts"] % tf_ms or out["available_ts"] % MINUTE_MS:
        raise ResearchInputError("Pivote o confirmación fuera de grilla UTC")
    if out["available_ts"] < out["pivot_ts"] + tf_ms:
        raise ResearchInputError("Pivot disponible antes del cierre de su vela")
    if out["prominence_pct"] is not None:
        try:
            out["prominence_pct"] = float(out["prominence_pct"])
        except (ValueError, TypeError) as exc:
            raise ResearchInputError("Prominencia inválida") from exc
        if not math.isfinite(out["prominence_pct"]) or out["prominence_pct"] < 0:
            raise ResearchInputError("Prominencia inválida")
    return out


def _verify_bars(minute_bars: Iterable[Mapping[str, Any]], known_ts: int) -> dict[int, tuple[float, float]]:
    """Solo indexa velas cerradas al known_ts. No rellena gaps."""
    index = {}
    for bar in minute_bars:
        ts = _integer(bar, "timestamp")
        if ts % MINUTE_MS:
            raise ResearchInputError("OHLC 1m desalineado")
        if ts + MINUTE_MS > known_ts:
            continue  # POST no puede influir ni en cobertura ni en features
        hi = _finite_positive(bar, "high")
        lo = _finite_positive(bar, "low")
        if hi < lo or ts in index:
            raise ResearchInputError("OHLC 1m inválido o timestamp duplicado")
        index[ts] = (hi, lo)
    return index


def _physical_minute(pivot: dict[str, Any], bars: Mapping[int, tuple[float, float]]):
    """(minute, estado) con verificación instrumental de extremo físico único.

    Si faltan velas, hay ties o el precio agregado no coincide con el snapshot,
    no asigna artificialmente una identidad física.
    """
    start = pivot["pivot_ts"]
    width = TF_MINUTES[pivot["timeframe"]]
    stamps = [start + j * MINUTE_MS for j in range(width)]
    if any(t not in bars for t in stamps):
        return None, "MISSING_1M"
    idx = 1 if pivot["side"] == "LOW" else 0
    values = [bars[t][idx] for t in stamps]
    extremum = min(values) if pivot["side"] == "LOW" else max(values)
    if not _same_price(extremum, pivot["price"]):
        return None, "PRICE_MISMATCH"
    matches = [t for t, price in zip(stamps, values) if _same_price(price, extremum)]
    if len(matches) != 1:
        return None, "TIED_EXTREMA"
    return matches[0], "VERIFIED"


def compute_features(event: Mapping[str, Any],
                     pivots: Iterable[Mapping[str, Any]],
                     minute_bars: Iterable[Mapping[str, Any]],
                     *, proximity_pct: float = 0.10) -> dict[str, Any]:
    """Devuelve una fila de features PRE/L0; no calcula outcomes ni entrenamientos.

    Un TF se cuenta una vez por evento, independientemente de pivotes duplicados.
    Se exigen swing confirmado, precio cercano, mismo lado y pivot originado
    dentro de la vela original 15m. La identidad física sólo se declara tras
    verificar el OHLC de cada ventana; de otro modo queda UNKNOWN.
    """
    if not math.isfinite(proximity_pct) or not (0 <= proximity_pct <= 5):
        raise ResearchInputError("proximity_pct inválido")
    e = _normalize_event(event)
    wanted_side = "LOW" if e["side"] == "LONG" else "HIGH"
    normalized = [_normalize_pivot(p) for p in pivots]
    # Pivotes futuros o conocidos tras la REACTION: excluidos totalmente.
    eligible = [p for p in normalized if p["side"] == wanted_side
                and p["available_ts"] <= e["known_ts"]
                and p["pivot_ts"] < e["retest_ts"]
                and e["pivot_ts"] <= p["pivot_ts"] < e["pivot_ts"] + 15 * MINUTE_MS
                and abs((p["price"] / e["swing_price"] - 1) * 100) <= proximity_pct]
    # Fuente original: 15m, mismo pivot y precio. No mezclar otro 15m cercano.
    anchor = [p for p in eligible if p["timeframe"] == "15m"
              and p["pivot_ts"] == e["pivot_ts"]
              and _same_price(p["price"], e["swing_price"])]
    if not anchor:
        raise ResearchInputError("Falta swing 15m canónico en la entrada normalizada")
    if any(p["available_ts"] != e["actionable_ts"] for p in anchor):
        raise ResearchInputError("15m original y actionable_ts difieren")
    # Desduplicación de snapshots. Duplicados contradictorios => error.
    dedup = {}
    for p in eligible:
        key = (p["timeframe"], p["side"], p["pivot_ts"])
        if key in dedup:
            old = dedup[key]
            if (not _same_price(old["price"], p["price"])
                    or old["available_ts"] != p["available_ts"]):
                raise ResearchInputError("Snapshots contradictorios para un mismo pivot")
            continue
        dedup[key] = p
    selected = list(dedup.values())
    selected.sort(key=lambda p: (TF_MINUTES[p["timeframe"]], p["pivot_ts"]))
    bars = _verify_bars(minute_bars, e["known_ts"])
    physical_minutes = []
    states = defaultdict(int)
    for p in selected:
        minute, state = _physical_minute(p, bars)
        states[state] += 1
        if state == "VERIFIED":
            physical_minutes.append(minute)
    all_verified = states["VERIFIED"] == len(selected)
    unique_extrema = len(set(physical_minutes)) if all_verified else None
    tf_set = {p["timeframe"] for p in selected}
    if not all_verified:
        physical_type = "UNKNOWN"
    elif len(tf_set) == 1:
        physical_type = "ORIGINAL_ONLY"
    elif unique_extrema == 1:
        physical_type = "SAME_PHYSICAL_EXTREME"
    else:
        physical_type = "DISTINCT_PHYSICAL_EXTREMES"
    distances = [abs((p["price"] / e["swing_price"] - 1) * 100) for p in selected]
    ages = [(e["known_ts"] - p["available_ts"]) / MINUTE_MS for p in selected]
    return {
        "event_key": e["event_key"], "symbol": e["symbol"],
        "side": e["side"], "original_status": e["status"],
        "pivot_ts": e["pivot_ts"], "retest_ts": e["retest_ts"],
        "known_ts": e["known_ts"], "swing_price": e["swing_price"],
        "proximity_pct": proximity_pct,
        "coincident_tf_count": len(tf_set),
        "coincident_pivot_count": len(selected),
        "has_1m": "1m" in tf_set, "has_5m": "5m" in tf_set,
        "has_15m": "15m" in tf_set,
        "max_level_distance_pct": max(distances),
        "oldest_confirmation_age_min": max(ages),
        "youngest_confirmation_age_min": min(ages),
        "verified_pivots": states["VERIFIED"],
        "missing_ohlc_pivots": states["MISSING_1M"],
        "tied_extrema_pivots": states["TIED_EXTREMA"],
        "price_mismatch_pivots": states["PRICE_MISMATCH"],
        "unique_physical_extrema": unique_extrema,
        "physical_structure_type": physical_type,
        "physical_identity_complete": all_verified,
        "feature_time_ms": e["known_ts"],
        "feature_version": "MULTISWING_STRUCTURAL_QUALITY_V1_PHASE1",
    }
