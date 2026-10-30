"""Read-only batch inspection of HLL Phase Reconstruction PRE V1.

Additive section of the existing HLL inspector. No recomputation or trading logic.
All overlay features were created from the 96 fully closed 30m candles before PRE.
"""
from __future__ import annotations

import math
from pathlib import Path

import pandas as pd

TF_MS = 1_800_000
REL = Path("reports/horizontal_level_second_ascent_pre/core15_phase_reconstruction_pre_v1")
PHASE_REL = Path("reports/horizontal_level_second_ascent_pre/core15_two_leg_v12/segments_pre.csv")
CANDLES_REL = Path("reports/horizontal_level_lifecycle_v21/candles_aug20_oct25_core15_btc")
FILES = {
    "summary": "phase_summary_pre.csv",
    "pivots": "phase_confirmed_pivots_pre.csv",
    "legs": "phase_legs_pre.csv",
    "candidates": "phase_recovery_candidates_pre.csv",
}

# Visual judgments are contextual notes, never features or filter rules.
# The six third-batch judgments were made by the assistant, not the user.
TANDAS = {
    "Tanda 1 · referencias (7)": [
        ("AAVEUSDT", "2221ee173c945cf17b026f2d", "Constructiva", "Revisión visual preliminar"),
        ("AIAUSDT", "81bcd23506a40114b2eba209", "Constructiva", "Revisión visual preliminar"),
        ("1000PEPEUSDT", "5c16416a996220d47fe32c96", "Constructiva", "Revisión visual preliminar"),
        ("2ZUSDT", "ffcb487cc3523ae7d1c48dbe", "Constructiva", "Revisión visual preliminar"),
        ("ACUUSDT", "b7b84e538c0fbe8799b2762a", "No convence", "Revisión visual preliminar"),
        ("1000FLOKIUSDT", "a8cd48b0f38f97d50a91be6d", "No convence", "Revisión visual preliminar"),
        ("1INCHUSDT", "917bcd9be0ad18833727dc3d", "No convence", "Revisión visual preliminar"),
    ],
    "Tanda 2 · tres fantásticos y BONK (4)": [
        ("ACUUSDT", "bb5f7458c98c04efff3edaa0", "Fantástica", "Usuario"),
        ("AAVEUSDT", "abe8975e8b1ff733e8513f76", "Fantástica", "Usuario"),
        ("2ZUSDT", "412e50a892f6e90f492ceaaa", "Fantástica", "Usuario"),
        ("1000BONKUSDT", "a419c2ae10918faceff87cc4", "No convence", "Usuario"),
    ],
    "Tanda 3 · contraste adicional (6)": [
        ("4USDT", "edc22fc645c77831fe3f6a48", "Constructiva", "Asistente · provisional"),
        ("1000FLOKIUSDT", "405fb903640b8544e19ab2e0", "Dudosa", "Asistente · provisional"),
        ("ADAUSDT", "4958928649fa8e6fe52be59e", "Constructiva/dudosa", "Asistente · provisional"),
        ("1000PEPEUSDT", "b00b2dac6b48b6fa67caa257", "Constructiva", "Asistente · provisional"),
        ("1000BONKUSDT", "a3015ca2f41fa79ef27de2c1", "No convence", "Asistente · provisional"),
        ("AIAUSDT", "2731c934c96999d326127e57", "Constructiva", "Asistente · provisional"),
    ],
}
ALL = "Todos los 50 intentos CORE15"


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, low_memory=False)


def _required_sources(root: Path) -> dict[str, Path]:
    sources = {k: root / REL / v for k, v in FILES.items()}
    sources["levels"] = root / PHASE_REL
    return sources


def validate_sources(summaries: pd.DataFrame, levels: pd.DataFrame) -> None:
    """Fail closed for mismatched attempt/symbol/cutoff or incomplete phases."""
    must = {"attempt_id", "symbol", "pre_closed_ts", "scale_atr", "status"}
    needed = {"attempt_id", "symbol", "pre_closed_ts", "zone_low", "zone_high"}
    if not must.issubset(summaries.columns):
        raise ValueError("Faltan columnas de Phase Summary PRE")
    if not needed.issubset(levels.columns):
        raise ValueError("Faltan columnas de zonas PRE originales")
    if summaries.duplicated(["attempt_id", "scale_atr"]).any():
        raise ValueError("Phase Summary tiene attempt/scale duplicados")
    if levels.duplicated(["attempt_id"]).any():
        raise ValueError("La fuente de zonas tiene attempt_id duplicados")
    if not summaries.status.eq("COMPLETE_PRE").all():
        raise ValueError("Hay reconstrucciones PRE incompletas")
    meta = summaries[["attempt_id", "symbol", "pre_closed_ts"]].drop_duplicates()
    if meta.duplicated(["attempt_id"]).any():
        raise ValueError("Una estructura tiene más de un símbolo o PRE")
    if set(meta.attempt_id) != set(levels.attempt_id):
        raise ValueError("Los attempt_id del nivel no coinciden con Phase V1")
    joined = meta.merge(levels[["attempt_id", "symbol", "pre_closed_ts", "zone_low", "zone_high"]],
                        on="attempt_id", validate="one_to_one", suffixes=("_phase", "_level"))
    if not ((joined.symbol_phase.astype(str) == joined.symbol_level.astype(str)) &
            (joined.pre_closed_ts_phase.astype("int64") == joined.pre_closed_ts_level.astype("int64"))).all():
        raise ValueError("Incompatibilidad de símbolo o corte PRE con zona")
    if (pd.to_numeric(joined.zone_low, errors="coerce") >=
        pd.to_numeric(joined.zone_high, errors="coerce")).any():
        raise ValueError("Resistencia sin banda válida")


def validate_closed_bars(candles: pd.DataFrame, pre_closed_ts: int, lookback: int = 96) -> pd.DataFrame:
    """Only fully closed, contiguous PRE candles. Never fill a data gap."""
    columns = {"timestamp", "open", "high", "low", "close"}
    if not columns.issubset(candles.columns):
        raise ValueError("OHLC 30m incompleto")
    a = candles.copy()
    a["timestamp"] = pd.to_numeric(a.timestamp, errors="raise").astype("int64")
    if a.timestamp.duplicated().any():
        raise ValueError("Timestamps OHLC duplicados")
    a = a.sort_values("timestamp")
    a = a.loc[a.timestamp + TF_MS <= int(pre_closed_ts)].tail(lookback).copy()
    if len(a) != lookback:
        raise ValueError(f"Cobertura PRE insuficiente: {len(a)}/{lookback}")
    stamps = a.timestamp.tolist()
    if stamps[-1] + TF_MS != int(pre_closed_ts):
        raise ValueError("La última vela cerrada no coincide con corte PRE")
    if any(b - a0 != TF_MS for a0, b in zip(stamps, stamps[1:])):
        raise ValueError("Gap de velas 30m en PRE; no se interpola")
    for k in ["open", "high", "low", "close"]:
        a[k] = pd.to_numeric(a[k], errors="raise")
    if not (a[["open", "high", "low", "close"]] > 0).all().all():
        raise ValueError("OHLC contiene precios no positivos")
    if not ((a.high >= a[["open", "close", "low"]].max(axis=1)) &
            (a.low <= a[["open", "close", "high"]].min(axis=1))).all():
        raise ValueError("OHLC inconsistente")
    return a


def _at_scale(table: pd.DataFrame, aid: str, scale: float) -> pd.DataFrame:
    z = table.loc[table.attempt_id.astype(str).eq(aid)]
    return z.loc[(z.scale_atr.astype(float) - scale).abs() < 1e-6].copy()


def valid_pivots(table: pd.DataFrame, aid: str, scale: float, pre: int) -> pd.DataFrame:
    a = _at_scale(table, aid, scale)
    if not len(a):
        return a
    if not {"confirmed_known_closed_ts", "pivot_open_ts", "pivot_type", "pivot_price_close"}.issubset(a.columns):
        raise ValueError("Columnas de pivotes faltantes")
    # Guard remains in the view even if source CSV is changed accidentally.
    a = a.loc[(a.status == "CONFIRMED_PRE") &
              (pd.to_numeric(a.confirmed_known_closed_ts) <= int(pre)) &
              (pd.to_numeric(a.pivot_open_ts) + TF_MS <= int(pre))]
    return a.sort_values("pivot_open_ts")


def valid_legs(table: pd.DataFrame, aid: str, scale: float, pre: int) -> pd.DataFrame:
    a = _at_scale(table, aid, scale)
    if not len(a):
        return a
    fields = {"start_pivot_open_ts", "start_pivot_known_closed_ts", "end_extreme_open_ts", "status"}
    if not fields.issubset(a.columns):
        raise ValueError("Columnas de legs faltantes")
    a = a.loc[(pd.to_numeric(a.start_pivot_known_closed_ts) <= int(pre)) &
              (pd.to_numeric(a.end_extreme_open_ts) + TF_MS <= int(pre)) &
              (a.status.isin(["CONFIRMED_PRE", "PROVISIONAL"]))].copy()
    if "end_extreme_confirmed_known_closed_ts" in a.columns:
        known = pd.to_numeric(a.end_extreme_confirmed_known_closed_ts, errors="coerce")
        a = a.loc[(a.status == "PROVISIONAL") | (known <= int(pre))]
    return a.sort_values("leg_index")


def build_phase_chart(candles: pd.DataFrame, pre_closed_ts: int, zone_low: float, zone_high: float,
                      pivots: pd.DataFrame, legs: pd.DataFrame, *, show_post: bool = False,
                      post_bars: int = 24, show_pivots: bool = True,
                      show_legs: bool = True, show_candidates: bool = False,
                      candidates: pd.DataFrame | None = None):
    """Pure Plotly chart; layers use only causally available PRE detections."""
    import plotly.graph_objects as go
    pre_closed_ts = int(pre_closed_ts)
    shown = validate_closed_bars(candles, pre_closed_ts)
    if show_post:
        tail = candles.loc[pd.to_numeric(candles.timestamp) >= pre_closed_ts].sort_values("timestamp").head(post_bars)
        shown = pd.concat([shown, tail], ignore_index=True)
    t = pd.to_datetime(shown.timestamp, unit="ms", utc=True)
    fig = go.Figure(go.Candlestick(x=t, open=shown.open, high=shown.high,
                     low=shown.low, close=shown.close, name="OHLC 30m",
                     increasing_line_color="#169774", decreasing_line_color="#d35e68"))
    fig.add_hrect(y0=float(zone_low), y1=float(zone_high), opacity=0.16,
                  fillcolor="#d6a24c", line_width=0)
    fig.add_hline(y=float(zone_low), line_dash="dot", line_color="#bb8b38", line_width=1)
    fig.add_hline(y=float(zone_high), line_dash="dot", line_color="#bb8b38", line_width=1)
    pre_time = pd.Timestamp(pre_closed_ts, unit="ms", tz="UTC")
    fig.add_vline(x=pre_time, line_dash="dash", line_color="#8b5cf6", line_width=2)
    if show_legs and not legs.empty:
        for status in ["CONFIRMED_PRE", "PROVISIONAL"]:
            for direction, clr in [("UP", "#278f78"), ("DOWN", "#cf8341")]:
                s = legs.loc[(legs.status == status) & (legs.leg_direction == direction)]
                if s.empty:
                    continue
                xx, yy = [], []
                for r in s.itertuples():
                    xx += [pd.Timestamp(int(r.start_pivot_open_ts), unit="ms", tz="UTC"),
                           pd.Timestamp(int(r.end_extreme_open_ts), unit="ms", tz="UTC"), None]
                    yy += [float(r.start_pivot_price_close), float(r.end_extreme_price_close), None]
                fig.add_trace(go.Scatter(x=xx, y=yy, mode="lines",
                    line=dict(color=clr, width=2.3 if status == "PROVISIONAL" else 1.6,
                              dash="dash" if status == "PROVISIONAL" else "solid"),
                    name=f'{"Provisional" if status == "PROVISIONAL" else "Confirmado"} {direction}',
                    hoverinfo="skip"))
    if show_pivots and not pivots.empty:
        for kind, clr, symbol in [("LOW", "#29a880", "triangle-up"),
                                  ("HIGH", "#e9a356", "triangle-down")]:
            q = pivots.loc[pivots.pivot_type == kind]
            if q.empty:
                continue
            tx = pd.to_datetime(q.pivot_open_ts.astype("int64"), unit="ms", utc=True)
            known = pd.to_datetime(q.confirmed_known_closed_ts.astype("int64"), unit="ms", utc=True)
            hover = [f'Pivote {kind} (retroactivo)<br>Conocido: {dt:%Y-%m-%d %H:%M} UTC'
                     for dt in known]
            fig.add_trace(go.Scatter(x=tx, y=q.pivot_price_close, mode="markers",
                marker=dict(size=9, color=clr, symbol=symbol), name=f"Pivote {kind} confirmado",
                text=hover, hovertemplate="%{text}<br>Precio cierre: %{y}<extra></extra>"))
    if show_candidates and candidates is not None and not candidates.empty:
        q = candidates.loc[pd.to_numeric(candidates.anchor_low_known_closed_ts) <= pre_closed_ts]
        fig.add_trace(go.Scatter(x=pd.to_datetime(q.anchor_low_open_ts.astype("int64"),unit="ms",utc=True),
            y=q.anchor_low_close, mode="markers", name="Anclas candidatas (sin selección)",
            marker=dict(color="#748dec", size=7, symbol="circle-open"),
            hovertemplate="Mínimo candidato: %{y}<extra></extra>"))
    fig.update_layout(height=395, xaxis_rangeslider_visible=False, showlegend=True,
        margin=dict(l=12, r=12, t=24, b=14),
        legend=dict(orientation="h", y=-0.21, font=dict(size=9)),
        xaxis_title="UTC · corte PRE morado", yaxis_title="Precio")
    return fig


def _review_table(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame([{"symbol": s, "attempt_id": a, "revision_visual": label,
                         "origen_revision": origin} for s, a, label, origin in rows])


def render_hll_phase_batch(root) -> None:
    """Render inside existing HLL view (no new dashboard page)."""
    import streamlit as st

    root = Path(root)
    src = _required_sources(root)
    with st.expander("🧩 HLL · Tandas PRE · detecciones de fases (CORE15)", expanded=False):
        st.caption("Mismo inspector HLL. Auditoría de 50 intentos y 3 escalas ATR, solo lectura. "
                   "Las fases NO son una clasificación de calidad ni una señal de trading.")
        absent = [str(p.relative_to(root)) for p in src.values() if not p.is_file()]
        if absent:
            st.info("Falta ejecutar Phase Reconstruction PRE V1 o copiar sus resultados al VPS.")
            st.code("\n".join(absent))
            return

        @st.cache_data(show_spinner=False, max_entries=24)
        def read_cached(path_s: str, mtime_ns: int) -> pd.DataFrame:
            return _read_csv(Path(path_s))

        try:
            data = {name: read_cached(str(path), path.stat().st_mtime_ns) for name, path in src.items()}
            validate_sources(data["summary"], data["levels"])
        except (OSError, KeyError, ValueError, pd.errors.ParserError) as exc:
            st.error(f"No se puede abrir esta auditoría PRE: {exc}")
            return
        counts = len(data["summary"]), data["summary"].attempt_id.nunique()
        st.caption(f"Cargados: {counts[1]} intentos, {counts[0]} combinaciones intento × escala. "
                   "Muestra histórica exploratoria: ejemplos cercanos pueden compartir casi todas las velas.")
        group = st.selectbox("Tanda para revisar", list(TANDAS) + [ALL], key="hll_phase_group")
        m1, m2, m3 = st.columns([1, 1, 1])
        with m1:
            scale = st.selectbox("Sensibilidad de swings (ATR)", [0.5, 1.0, 1.5], index=1,
                                 key="hll_phase_scale")
        with m2:
            per_page = st.selectbox("Gráficos por página", [2, 4, 6], index=1,
                                    key="hll_phase_per_page")
        with m3:
            post = st.checkbox("Revelar velas POST", value=False, key="hll_phase_reveal_post")
        c1, c2, c3 = st.columns(3)
        with c1:
            with_pivots = st.checkbox("Pivotes confirmados", value=True, key="hll_phase_pivots")
        with c2:
            with_legs = st.checkbox("Tramos y fase provisional", value=True, key="hll_phase_legs")
        with c3:
            with_candidates = st.checkbox("Todos los mínimos candidatos", value=False,
                                           key="hll_phase_candidates")
        st.caption("Pivotes dibujados retrospectivamente sobre su vela de origen: "
                   "el hover indica cuándo quedaron confirmados. Tramos discontinuos = provisionales en PRE. "
                   "El POST, si se revela, es solo visual y nunca cambia las detecciones.")
        if group == ALL:
            metadata = data["summary"].loc[data["summary"].scale_atr.eq(1.0)]
            symbols = sorted(metadata.symbol.unique())
            sym = st.selectbox("Símbolo", ["Todos"] + symbols, key="hll_phase_symbol")
            if sym != "Todos":
                metadata = metadata.loc[metadata.symbol.eq(sym)]
            metadata = metadata.sort_values(["symbol", "pre_closed_ts", "attempt_id"])
            rows = [(str(r.symbol), str(r.attempt_id), "Sin evaluación visual", "Sin etiqueta")
                    for r in metadata.itertuples()]
        else:
            rows = TANDAS[group]
        if not rows:
            st.info("No hay casos bajo esta selección.")
            return
        n_pages = math.ceil(len(rows) / per_page)
        page = st.number_input(f"Página (1–{n_pages})", min_value=1, max_value=n_pages,
                               value=1, step=1, key=f"hll_phase_page_{group}_{per_page}")
        st.dataframe(_review_table(rows), hide_index=True, use_container_width=True, height=min(320, 34 + len(rows) * 32))
        selected = rows[(int(page) - 1) * per_page:int(page) * per_page]
        for start in range(0, len(selected), 2):
            left, right = st.columns(2)
            for col, item in zip((left, right), selected[start:start+2]):
                symbol, aid, label, origin = item
                with col:
                    st.markdown(f"**{symbol}** · `{aid}`")
                    st.caption(f"Visual: {label} · {origin}")
                    sr = _at_scale(data["summary"], aid, float(scale))
                    levels = data["levels"].loc[data["levels"].attempt_id.astype(str).eq(aid)]
                    if len(sr) != 1 or len(levels) != 1:
                        st.error("Intento ausente o duplicado en fuente PRE")
                        continue
                    r = sr.iloc[0]
                    lev = levels.iloc[0]
                    candle_path = root / CANDLES_REL / f"{symbol}_30m.csv"
                    if not candle_path.is_file():
                        st.warning(f"Sin OHLC: {symbol}")
                        continue
                    try:
                        candles = read_cached(str(candle_path), candle_path.stat().st_mtime_ns)
                        pv = valid_pivots(data["pivots"], aid, float(scale), int(r.pre_closed_ts))
                        lg = valid_legs(data["legs"], aid, float(scale), int(r.pre_closed_ts))
                        cand = _at_scale(data["candidates"], aid, float(scale))
                        fig = build_phase_chart(candles, int(r.pre_closed_ts), float(lev.zone_low),
                             float(lev.zone_high), pv, lg, show_post=post, show_pivots=with_pivots,
                             show_legs=with_legs, show_candidates=with_candidates, candidates=cand)
                        st.plotly_chart(fig, use_container_width=True, key=f"hll_phase_{aid}_{scale}")
                        tip = str(r.terminal_leg_direction)
                        st.caption(f"Terminal PRE: {tip} PROVISIONAL · "
                                   f"pivotes confirmados: {int(r.confirmed_pivots)} · "
                                   f"mínimos candidatos: {int(r.recovery_candidates)}")
                        with st.expander("Ver detecciones detalladas", expanded=False):
                            st.dataframe(lg[["leg_index", "leg_role", "status", "amplitude_atr",
                                             "duration_to_extreme_bars"]], hide_index=True,
                                         use_container_width=True)
                            if not cand.empty:
                                cs = ["candidate_index", "anchor_low_open_ts", "age_from_low_bars",
                                      "net_change_from_low_atr", "gap_reduction_atr",
                                      "zone_touch_episodes_since_low", "later_lowest_close_undercut"]
                                st.dataframe(cand[cs], hide_index=True, use_container_width=True)
                    except (OSError, ValueError, TypeError, KeyError) as exc:
                        st.error(f"No se pudo representar {aid}: {exc}")
        st.caption("Ningún umbral de calidad aplicado. Ninguna modificación de señales, clasificadores o LIVE.")
