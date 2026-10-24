"""Read-only 1m adverse-move tracker for the Legacy REACTION/Retests inspector.

Entry reference: OPEN of the 1m bar immediately following the fully closed
first-touch candle. Uses closed 1m bars only, never imputes timestamp gaps.
All excursions are descriptive; a maximum observed later is never an entry.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd

ONE_MIN = 60_000
HORIZONS = (15, 30, 60, 120)


def _number(value):
    try:
        num = float(value)
        return num if math.isfinite(num) else None
    except (TypeError, ValueError, OverflowError):
        return None


def build_legacy_adverse_path(candles: pd.DataFrame, event: dict, horizon: int = 120) -> dict:
    """Pure calculation; no Streamlit/market dependencies.

    Returns NO_DATA / NO_EVENT_BAR / NO_CONTIGUOUS_ENTRY / OK. Horizons that
    are not covered are explicitly INCOMPLETE, not zero, loss or timeout.
    """
    result = {"state": "NO_DATA", "path": pd.DataFrame(), "summary": pd.DataFrame(),
              "entry_price": None, "entry_ts": None, "covered_minutes": 0,
              "original_side": None, "inverse_side": None}
    if candles is None or candles.empty or not isinstance(event, dict):
        return result
    side = str(event.get("signal", "")).upper()
    event_ts = _number(event.get("retest_timestamp"))
    swing = _number(event.get("swing_price"))
    if side not in ("LONG", "SHORT") or event_ts is None or swing is None or swing <= 0:
        result["state"] = "INVALID_EVENT"
        return result
    event_ts = int(event_ts)
    horizon = int(horizon)
    if horizon <= 0 or horizon > 360:
        result["state"] = "INVALID_HORIZON"
        return result
    required = ("timestamp", "open", "high", "low", "close")
    if any(col not in candles.columns for col in required):
        result["state"] = "INVALID_CANDLES"
        return result
    bars = candles.loc[:, required].copy()
    for col in required:
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    if bars[list(required)].isna().any().any():
        result["state"] = "INVALID_CANDLES"
        return result
    bars = bars.sort_values("timestamp", kind="mergesort").reset_index(drop=True)
    ts = bars["timestamp"].to_numpy(dtype="int64")
    o, h, l, c = (bars[col].to_numpy(dtype="float64") for col in ("open", "high", "low", "close"))
    if (np.diff(ts) <= 0).any() or np.any(o <= 0) or np.any(l <= 0) or np.any(h < l) or np.any(c <= 0) or np.any((o > h) | (o < l) | (c > h) | (c < l)):
        result["state"] = "INVALID_CANDLES"
        return result
    event_i = int(np.searchsorted(ts, event_ts))
    if event_i >= len(ts) or int(ts[event_i]) != event_ts:
        result["state"] = "NO_EVENT_BAR"
        return result
    first = event_i + 1
    if first >= len(ts) or ts[first] != event_ts + ONE_MIN:
        result["state"] = "NO_CONTIGUOUS_ENTRY"
        return result
    entry = float(o[first])
    stop = first + 1
    last = first
    while stop < len(ts) and stop < first + horizon and ts[stop] == ts[stop-1] + ONE_MIN:
        last = stop
        stop += 1
    # first 1m candle is included, so coverage is 1 even if following data is missing
    covered = last - first + 1
    hi = h[first:last+1]
    lo = l[first:last+1]
    closes = c[first:last+1]
    cum_hi = np.maximum.accumulate(hi)
    cum_lo = np.minimum.accumulate(lo)
    if side == "LONG":
        mae_orig = np.maximum(0., (entry - cum_lo) / entry * 100.)
        mfe_orig = np.maximum(0., (cum_hi - entry) / entry * 100.)
        inverse_close = (entry - closes) / entry * 100.
        beyond_swing = np.maximum(0., (swing - cum_lo) / swing * 100.)
        closed_beyond = (swing - closes) / swing * 100.
        inverse_side = "SHORT"
        recovered = closes >= swing
    else:
        mae_orig = np.maximum(0., (cum_hi - entry) / entry * 100.)
        mfe_orig = np.maximum(0., (entry - cum_lo) / entry * 100.)
        inverse_close = (closes - entry) / entry * 100.
        beyond_swing = np.maximum(0., (cum_hi - swing) / swing * 100.)
        closed_beyond = (closes - swing) / swing * 100.
        inverse_side = "LONG"
        recovered = closes <= swing
    # Exact same entry price for both hypothetical directions; no assumed fees.
    path = pd.DataFrame({
        "Minuto": np.arange(1, covered + 1, dtype=int),
        "UTC": pd.to_datetime(ts[first:last+1], unit="ms", utc=True),
        "Cierre": closes,
        "Inversa al cierre %": inverse_close,
        "Contra REACTION (max) %": mae_orig,
        "A favor REACTION (max) %": mfe_orig,
        "Penetración swing (max) %": beyond_swing,
        "Cierre fuera swing %": closed_beyond,
        "Swing recuperado": recovered,
    })
    summaries = []
    for minutes in HORIZONS:
        if minutes > horizon:
            continue
        complete = covered >= minutes
        point = path.iloc[minutes-1] if complete else None
        summaries.append({
            "Horizonte": f"{minutes}m", "Cobertura": "COMPLETO" if complete else f"INCOMPLETO ({covered}/{minutes})",
            "Contra REACTION máx %": float(point["Contra REACTION (max) %"]) if complete else np.nan,
            "Inversa al cierre %": float(point["Inversa al cierre %"]) if complete else np.nan,
            "A favor REACTION máx %": float(point["A favor REACTION (max) %"]) if complete else np.nan,
            "Penetración swing máx %": float(point["Penetración swing (max) %"]) if complete else np.nan,
            "Swing recuperado al cierre": bool(point["Swing recuperado"]) if complete else None,
        })
    result.update({"state": "OK", "path": path, "summary": pd.DataFrame(summaries),
                   "entry_price": entry, "entry_ts": int(ts[first]), "covered_minutes": covered,
                   "original_side": side, "inverse_side": inverse_side, "swing_price": swing})
    return result


def render_legacy_retest_adverse_tracker(event, candles_reader, ui_key_prefix=""):
    """Render immediately BELOW the existing 15m inspector chart, Legacy only."""
    import streamlit as st
    import plotly.graph_objects as go

    st.markdown("#### Seguimiento adverso · primer toque (1m)")
    side = str(event.get("signal", "")).upper()
    fail = str(event.get("status", "")).upper() == "TOUCH_FAILED"
    if fail:
        st.caption(f"**Fallo confirmado**: REACTION {side} → posible {('SHORT' if side == 'LONG' else 'LONG')}. "
                   "El seguimiento comienza en la apertura de la vela 1m siguiente al fallo, no antes.")
    else:
        st.caption("Seguimiento descriptivo contra la dirección original. El análisis de operación inversa "
                   "solo representa una hipótesis, no una señal detectada para este estado.")
    horizon = st.select_slider("Seguimiento desde entrada (minutos)", options=[15, 30, 60, 120],
                               value=60, key=ui_key_prefix + "legacy_retest_adverse_horizon")
    event_ts = _number(event.get("retest_timestamp"))
    if event_ts is None:
        st.info("El evento no tiene timestamp 1m válido.")
        return
    # Size only for the selected symbol, not for the whole scanner universe.
    current_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    elapsed = max(0, math.ceil((current_ms - event_ts) / ONE_MIN))
    requested = min(12000, max(350, elapsed + int(horizon) + 60))
    candles = candles_reader(symbol=str(event.get("symbol", "")), timeframe="1m", limit=requested)
    result = build_legacy_adverse_path(candles, dict(event), int(horizon))
    if result["state"] != "OK":
        reasons = {
            "NO_DATA": "No se recibieron velas 1m cerradas.",
            "NO_EVENT_BAR": "La vela 1m del primer toque ya no está en el histórico cargado.",
            "NO_CONTIGUOUS_ENTRY": "Falta la vela 1m siguiente al cierre del fallo (sin imputar huecos).",
            "INVALID_CANDLES": "Las velas 1m tienen datos inválidos o duplicados.",
        }
        st.info(reasons.get(result["state"], "No se puede reconstruir la trayectoria 1m de este evento.") +
                " No se infieren movimientos con velas 15m ni con MFE agregado.")
        return
    path = result["path"]
    first = path.iloc[0]
    last = path.iloc[-1]
    a, b, c, d = st.columns(4)
    a.metric("Operación inversa", result["inverse_side"] if fail else "Solo estudio")
    b.metric("Entrada 1m", f"{result['entry_price']:.8g}")
    c.metric("Contra original (máx)", f"{last['Contra REACTION (max) %']:.3f}%")
    d.metric("Inversa al cierre", f"{last['Inversa al cierre %']:+.3f}%")
    if result["covered_minutes"] < int(horizon):
        st.warning(f"Trayectoria incompleta: {result['covered_minutes']}/{horizon} minutos. "
                   "Los horizontes incompletos NO se clasifican como pérdidas ni timeouts.")
    chart = go.Figure()
    chart.add_trace(go.Scatter(x=path["Minuto"], y=path["Contra REACTION (max) %"],
                               name="MAE original / MFE inversa", mode="lines", line=dict(color="#E59F42", width=2)))
    chart.add_trace(go.Scatter(x=path["Minuto"], y=path["A favor REACTION (max) %"],
                               name="MFE original / MAE inversa", mode="lines", line=dict(color="#7891AD", width=2)))
    chart.add_trace(go.Scatter(x=path["Minuto"], y=path["Inversa al cierre %"],
                               name="Inversa al cierre (sin costos)", mode="lines", line=dict(color="#25A68B", width=2)))
    chart.add_hline(y=0, line_width=1, line_dash="dash", line_color="gray")
    chart.update_layout(height=320, margin=dict(l=5, r=5, t=20, b=15),
                        xaxis_title="Minutos desde el OPEN ejecutable", yaxis_title="Movimiento desde entrada (%)",
                        legend=dict(orientation="h", y=-0.35))
    st.plotly_chart(chart, use_container_width=True, config={"displaylogo": False},
                    key=ui_key_prefix + "legacy_retest_adverse_path_chart")
    st.caption("La línea naranja representa el máximo movimiento contrario a la REACTION original; "
               "es el MFE potencial de invertirla. La línea azul es el riesgo máximo de la inversa. "
               "Ambas incluyen máximos/mínimos posteriores observados, NO son beneficios realizados. "
               "Todas las cifras parten del OPEN de entrada; excluyen costos.")
    st.markdown("**Resumen por horizonte (solo si hay trayectoria completa)**")
    st.dataframe(result["summary"].round(4), hide_index=True, use_container_width=True)
    with st.expander("Ver cada vela 1m — seguimiento completo", expanded=False):
        st.dataframe(path.round(5), hide_index=True, use_container_width=True,
                     height=330)
