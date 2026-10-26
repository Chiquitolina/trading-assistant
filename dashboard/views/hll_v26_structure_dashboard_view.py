"""Read-only integrated Horizontal Structure Lab V2.6 dashboard tab.

This is deliberately NOT an independent Streamlit application.  It has no
st.set_page_config(), st.stop(), filesystem writes or access to live services.
The import is intentionally lazy in render_hll_v26_structure_dashboard().
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import pandas as pd


CATEGORY_STATES = {
    "Todas las estructuras": None,
    "1 · Presión progresiva (PRE)": "PRESSURE",
    "2 · Ruptura directa (POST)": {"DIRECT_NO_RETEST_24B"},
    "3 · Falsa ruptura (POST)": {"FAILED_BREAKOUT"},
    "4 · Ruptura + retest (POST)": {
        "RETEST_FAILED", "RETEST_UNRESOLVED", "FLIP_SUSTAINED_4B",
        "FLIP_CONFIRMED_LATER_LOST", "CENSORED_RETEST",
    },
    "5 · Resistencia → soporte (POST)": {
        "FLIP_SUSTAINED_4B", "FLIP_CONFIRMED_LATER_LOST"
    },
}

DISPLAY_STATES = {
    "DIRECT_NO_RETEST_24B": "Directa sin retest (24 velas)",
    "FLIP_SUSTAINED_4B": "Flip sostenido (4 velas)",
    "FLIP_CONFIRMED_LATER_LOST": "Flip confirmado, luego perdido",
    "RETEST_FAILED": "Retest fallido",
    "FAILED_BREAKOUT": "Falsa ruptura temprana",
    "RETEST_UNRESOLVED": "Retest sin resolución",
    "UNRESOLVED_FIRST4": "Etapa inicial sin resolución",
    "CENSORED_POST_ACCEPTANCE": "Censurado tras aceptación",
    "CENSORED_RETEST": "Retest censurado",
}

OUTCOMES = (
    "attempt_id", "status", "accepted_closed_ts", "retest_closed_ts",
    "defense_closed_ts", "failed_closed_ts", "flip_sustained_closed_ts",
)


@lru_cache(maxsize=32)
def _read_csv(filename: str, mtime: int):
    # mtime is deliberately part of the cache key to invalidate changed files.
    return pd.read_csv(filename, low_memory=False)


def _data(path: Path) -> pd.DataFrame:
    return _read_csv(str(path), path.stat().st_mtime_ns).copy()


def _read_bundle(folder: Path):
    needed = {
        "attempts": "breakout_attempts.csv",
        "outcomes": "attempt_outcomes.csv",
        "transitions": "structure_transitions.csv",
        "touches": "touches.csv",
        "summary": "structure_summary.csv",
        "factors": "structure_by_prior_features.csv",
    }
    missing = [filename for filename in needed.values() if not (folder / filename).is_file()]
    if missing:
        return None, missing
    frames = {key: _data(folder / filename) for key, filename in needed.items()}
    at, outcome = frames["attempts"], frames["outcomes"]
    if at.attempt_id.duplicated().any() or outcome.attempt_id.duplicated().any():
        raise ValueError("IDs de episodios duplicados; se detiene para no multiplicar eventos")
    frames["joined"] = at.merge(outcome[list(OUTCOMES)], on="attempt_id", validate="one_to_one")
    if len(frames["joined"]) != len(at):
        raise ValueError("Diferencia entre los episodios y resultados V2.6")
    return frames, []


def _subset(df: pd.DataFrame, *, category: str, symbol: str | None = None,
            pressure: str = "Todos", touch_min: int = 0, touch_max: int = 99,
            min_volume: float = 0.0, min_rejections: int = 0) -> pd.DataFrame:
    x = df
    states = CATEGORY_STATES[category]
    if states == "PRESSURE":
        x = x[x.pressure_build_up.eq(1)]
    elif states is not None:
        x = x[x.status.isin(states)]
    if symbol and symbol != "Todos":
        x = x[x.symbol.eq(symbol)]
    if pressure != "Todos":
        x = x[x.pressure_build_up.eq(1 if pressure == "Sí" else 0)]
    x = x[x.prior_independent_touches.between(touch_min, touch_max)]
    x = x[pd.to_numeric(x.volume_ratio_pre20, errors="coerce").ge(min_volume)]
    x = x[x.prior_confirmed_rejections.ge(min_rejections)]
    return x


def _plot_episode(bars: pd.DataFrame, record: pd.Series, touches: pd.DataFrame,
                  transitions: pd.DataFrame, show_future: bool):
    import plotly.graph_objects as go

    btc = int(record.known_closed_ts)
    idx = bars.index[bars.timestamp.eq(btc - 30 * 60 * 1000)]
    if len(idx) != 1:
        raise ValueError("No encontramos la vela 30m anterior al cierre conocido del breakout")
    at = int(idx[0]); left = max(0, at - 70)
    right = min(len(bars), at + (52 if show_future else 1))
    window = bars.iloc[left:right].copy()
    utc = pd.to_datetime(window.timestamp, unit="ms", utc=True)
    fig = go.Figure(go.Candlestick(
        x=utc, open=window.open, high=window.high, low=window.low, close=window.close,
        increasing_line_color="#159B70", decreasing_line_color="#D85B68", name="30m",
    ))
    zlo, zhi = float(record.zone_low), float(record.zone_high)
    fig.add_hrect(y0=zlo, y1=zhi, fillcolor="#DCA64F", opacity=0.16, line_width=0)
    fig.add_hline(y=zlo, line_dash="dash", line_color="#C9973D", line_width=1)
    fig.add_hline(y=zhi, line_dash="dash", line_color="#C9973D", line_width=1)
    xbreak = pd.Timestamp(btc, unit="ms", tz="UTC")
    fig.add_vline(x=xbreak, line_color="#2E73D2", line_width=2, line_dash="dot")
    fig.add_annotation(x=xbreak, y=1.07, yref="paper", text="CROSS_UP", showarrow=False,
                       font=dict(size=11, color="#2E73D2"))
    old = touches[touches.level_id.eq(record.level_id) &
                  touches.touch_closed_ts.lt(btc)].copy()
    if not old.empty:
        old = old[old.touch_closed_ts.ge(int(window.timestamp.min()))]
        if not old.empty:
            actual = bars.set_index("timestamp")
            ts = old.touch_closed_ts.astype("int64") - 30 * 60 * 1000
            highs = actual.reindex(ts)["high"].to_numpy()
            fig.add_trace(go.Scatter(
                x=pd.to_datetime(ts, unit="ms", utc=True), y=highs,
                mode="markers", marker=dict(size=9, color="#EAB66C", symbol="circle-open", line=dict(width=2)),
                name="Tests independientes PRE",
            ))
    if show_future:
        colors = {"FAILED_BREAKOUT": "#E05A61", "RETEST_FAILED": "#E05A61",
                  "FLIP_LATER_LOST": "#E05A61", "ACCEPTED_ABOVE": "#179977",
                  "FLIP_CONFIRMED": "#179977", "FLIP_SUSTAINED_4B": "#179977",
                  "RETEST_CONTACT_FROM_ABOVE": "#D1A243", "DIRECT_NO_RETEST_24B": "#6687B9"}
        for _, row in transitions[transitions.attempt_id.eq(record.attempt_id)].iterrows():
            if row.transition == "CROSS_UP":
                continue
            dt = pd.Timestamp(int(row.known_closed_ts), unit="ms", tz="UTC")
            if dt < utc.min() or dt > utc.max():
                continue
            col = colors.get(row.transition, "#94A3B8")
            fig.add_vline(x=dt, line_dash="dot", line_width=1.5, line_color=col)
            fig.add_annotation(x=dt, y=1.02, yref="paper", text=row.transition,
                               showarrow=False, textangle=-65, font=dict(size=9, color=col),
                               yanchor="bottom")
    fig.update_layout(height=570, xaxis_rangeslider_visible=False,
                      title=f"{record.symbol} · {record.known_utc}",
                      xaxis_title="UTC", yaxis_title="Precio", showlegend=True,
                      margin=dict(l=8, r=8, t=110, b=25))
    return fig


def render_hll_v26_structure_dashboard(root: Path | str):
    """Draw the integrated tab only. Safe to call from an existing st.tabs context."""
    import streamlit as st

    root = Path(root)
    st.markdown("### 🧬 Estructuras de resistencia · V2.6")
    st.caption("Atlas causal de 30m: presión previa, cruces, retests y flips. Solo lectura; NO genera órdenes ni rentabilidad.")
    reports = root / "reports/horizontal_level_structure_lab_v26/runs"
    options = sorted([p for p in reports.iterdir() if (p / "breakout_attempts.csv").is_file()],
                     key=lambda p: p.stat().st_mtime, reverse=True) if reports.is_dir() else []
    if not options:
        st.warning("No hay corridas de V2.6. Ejecutá `python3 hll_v26_structure_lab.py --run` antes de abrir esta pestaña.")
        return
    run = st.selectbox("Corrida V2.6", options, format_func=lambda p: p.name, key="hll26_run")
    try:
        frames, missing = _read_bundle(run)
    except (ValueError, OSError, KeyError) as exc:
        st.error(f"Los archivos V2.6 no tienen una estructura consistente: {exc}")
        return
    if missing:
        st.warning("Faltan archivos de la corrida: " + ", ".join(missing))
        return
    df = frames["joined"]
    summary = frames["summary"]
    counts = frames["outcomes"].status.value_counts()
    c = st.columns(5)
    for slot, label, value in zip(c,
        ["Intentos", "Resistencias", "Tests independientes", "Falsas rupturas", "Flips confirmados"],
        [len(df), df.level_id.nunique(), len(frames["touches"]),
         int(counts.get("FAILED_BREAKOUT", 0)),
         int(counts.get("FLIP_SUSTAINED_4B", 0) + counts.get("FLIP_CONFIRMED_LATER_LOST", 0))]):
        slot.metric(label, f"{value:,}".replace(",", "."))
    with st.expander("📚 Definiciones de las cinco estructuras", expanded=False):
        st.markdown("""**Presión progresiva (PRE):** mínimos ascendentes confirmados y cercanía al techo **antes del breakout**. No equivale a un resultado futuro.

**Ruptura directa (POST):** aceptación y ausencia de retest durante 24 velas posteriores.

**Falsa ruptura (POST):** pérdida temprana del nivel antes de conseguir aceptación.

**Ruptura y retest (POST):** contacto posterior desde arriba; puede fallar, quedar sin resolver o convertirse en flip.

**Resistencia → soporte (POST):** retest defendido y flip confirmado. El sostenimiento se comprueba aún más tarde.

La etiqueta POST solo se conoce cuando termina de confirmarse. **Nunca debe utilizarse como señal disponible en el CROSS_UP original.**""")

    c1, c2, c3 = st.columns([2.5, 1.5, 1.2])
    with c1:
        category = st.selectbox("Estructura", list(CATEGORY_STATES), key="hll26_category")
    with c2:
        syms = ["Todos"] + sorted(df.symbol.dropna().unique().tolist())
        symbol = st.selectbox("Símbolo", syms, key="hll26_sym")
    with c3:
        pressure = st.selectbox("Presión PRE", ["Todos", "Sí", "No"], key="hll26_pressure")
    max_touch = max(12, min(50, int(df.prior_independent_touches.max())))
    c4, c5, c6 = st.columns(3)
    with c4:
        trange = st.slider("Toques previos independientes", 0, max_touch, (0, max_touch), key="hll26_touchrange")
    with c5:
        min_volume = st.number_input("Volumen relativo mínimo (PRE)", min_value=0.0,
                                     max_value=30.0, value=0.0, step=0.25, key="hll26_vol")
    with c6:
        min_rejections = st.number_input("Rechazos confirmados previos mínimos", min_value=0,
                                         max_value=30, value=0, step=1, key="hll26_rej")
    sel = _subset(df, category=category, symbol=symbol, pressure=pressure,
                  touch_min=trange[0], touch_max=trange[1], min_volume=min_volume,
                  min_rejections=int(min_rejections))
    if category == '5 · Resistencia → soporte (POST)':
        flip_class = st.selectbox('Estado del flip',
            ['Todos los flips confirmados', 'Sostenido 4 velas', 'Confirmado y perdido'],
            key='hll26_flip_class')
        if flip_class == 'Sostenido 4 velas':
            sel = sel[sel.status.eq('FLIP_SUSTAINED_4B')]
        elif flip_class == 'Confirmado y perdido':
            sel = sel[sel.status.eq('FLIP_CONFIRMED_LATER_LOST')]
    st.caption(f"{len(sel):,} intentos con estos filtros · los episodios pueden compartir símbolo, nivel o trayectoria".replace(",", "."))
    if sel.empty:
        st.info("No hay intentos que cumplan estos filtros.")
        return
    with st.expander("📊 Tabla comparativa de estructuras", expanded=False):
        cols = ["symbol", "known_utc", "level_id", "prior_independent_touches",
                "prior_confirmed_rejections", "prior_failed_breakouts", "pressure_build_up",
                "touch_spacing_median_bars", "volume_ratio_pre20", "zone_width_atr", "status"]
        table = sel[cols].sort_values("known_utc", ascending=False)
        st.dataframe(table, use_container_width=True, hide_index=True, height=350)
        st.caption("`status` es clasificación retrospectiva; las demás características son PRE.")
        st.download_button("Descargar selección (CSV)", table.to_csv(index=False).encode("utf-8"),
                           file_name="hll_v26_structures_filtered.csv", mime="text/csv", key="hll26_download")

    ordered = sel.sort_values("known_closed_ts", ascending=False).copy()
    ordered["choice"] = ordered.known_utc.astype(str) + " | " + ordered.symbol.astype(str) + " | " + ordered.attempt_id.str[:12]
    selected = st.selectbox("Inspeccionar intento · instante del CROSS_UP (UTC)",
                            ordered.choice.tolist(), key="hll26_attempt")
    record = ordered.loc[ordered.choice.eq(selected)].iloc[0]
    show_future = st.checkbox("Mostrar transiciones posteriores (POST: aceptación, retest, flip o fracaso)",
                              value=False, key="hll26_future")
    st.caption("Con POST desactivado el gráfico termina al cierre del CROSS_UP; así podés estudiar sus condiciones conocidas antes de ver el desenlace.")
    candle_dir = root / "reports/horizontal_level_lifecycle_v21/candles_aug20_oct25_core15_btc"
    candle_path = candle_dir / f"{record.symbol}_30m.csv"
    if not candle_path.is_file():
        st.warning(f"No encontramos las velas 30m para {record.symbol} en {candle_dir}.")
        return
    try:
        bars = _data(candle_path)
        st.plotly_chart(_plot_episode(bars, record, frames["touches"],
                                      frames["transitions"], show_future),
                        use_container_width=True, key="hll26_chart")
    except Exception as exc:
        st.error(f"No se pudo dibujar el episodio: {exc}")
        return
    st.markdown("#### Datos disponibles al cerrar el CROSS_UP")
    features = frames["attempts"].loc[frames["attempts"].attempt_id.eq(record.attempt_id)].iloc[0]
    main_cols = ["formation_pivots", "pivot_separation_bars", "level_age_bars", "zone_width_atr",
                 "prior_independent_touches", "prior_touches_from_below", "prior_touches_from_above",
                 "prior_confirmed_rejections", "prior_failed_breakouts", "prior_accepted_then_failed",
                 "bars_since_last_touch", "last_touch_depth_atr", "last_touch_rejection_atr",
                 "touch_spacing_bars_json", "touch_spacing_median_bars", "touch_spacing_trend_bars",
                 "total_touch_span_bars", "rejection_distance_trend_atr", "confirmed_low_pivots_pre24",
                 "higher_low_steps_pre24", "pressure_build_up", "volume_ratio_pre20", "breakout_body_atr",
                 "breakout_close_beyond_high_atr", "next_resistance_distance_atr"]
    left, right = st.columns([1.2, 1])
    with left:
        st.dataframe(pd.DataFrame([{"variable": k, "valor": str(features[k])} for k in main_cols if k in features]),
                     hide_index=True, use_container_width=True, height=480)
    with right:
        st.markdown("**Tests independientes anteriores**")
        prev = frames["touches"].loc[frames["touches"].level_id.eq(record.level_id) &
                                     frames["touches"].touch_closed_ts.lt(int(record.known_closed_ts))]
        tcols = ["touch_utc", "side", "depth_atr", "rejection_atr", "resolver_type", "resolver_known_ts"]
        visible_touches = prev[tcols].copy()
        if not visible_touches.empty:
            # A touch's eventual defense/loss is not known at touch time.
            # Hide resolver labels that would not yet be confirmed at the breakout.
            resolved_ts = pd.to_numeric(visible_touches.resolver_known_ts, errors="coerce")
            future_resolution = resolved_ts.isna() | resolved_ts.gt(int(record.known_closed_ts))
            visible_touches.loc[future_resolution, "resolver_type"] = "PENDIENTE AL CORTE"
            visible_touches.loc[future_resolution, "resolver_known_ts"] = float("nan")
        st.dataframe(visible_touches.sort_values("touch_utc", ascending=False),
                     hide_index=True, use_container_width=True, height=240)
        if show_future:
            st.markdown("**Resultado conocido después (POST)**")
            st.warning("Las transiciones posteriores no son filtros operables al momento del CROSS_UP.")
            t = frames["transitions"].loc[frames["transitions"].attempt_id.eq(record.attempt_id)]
            st.dataframe(t[["transition", "known_utc", "note"]].sort_values("known_utc"),
                         hide_index=True, use_container_width=True, height=215)
        else:
            st.info("Activá POST para ver aceptación, retest, flip o pérdida posteriores.")
    st.divider()
    st.markdown("#### Frecuencias descriptivas por factores previos")
    factors = frames["factors"]
    dims = sorted(factors.dimension.dropna().unique().tolist())
    dimension = st.selectbox("Agrupar por", dims, key="hll26_factors")
    st.dataframe(factors[factors.dimension.eq(dimension)], use_container_width=True, hide_index=True)
    st.caption("Estadística exploratoria sin control de dependencia por nivel/símbolo/día. No son resultados de TP/SL ni inferencia causal.")
