"""Read-only Continuous V1 first-touch explorer (REACTION / INDECISIVE / FAILED).

Uses the separate swing-specific scanner_first_touches_v1.sqlite3 journal;
never writes to either scanner journal, refreshes scanner, or imports any trading
engine. Charts use retained *closed* Redis candles only, not invented archives.
"""
from __future__ import annotations

import json
from contextlib import closing
from math import ceil
from pathlib import Path
import sqlite3
from typing import Callable

DB_RELPATH = "reports/reaction_lab/scanner_first_touches_v1.sqlite3"
STATUSES = ("REACTION", "TOUCH_FAILED", "TOUCH_INDECISIVE")
UI_STATUSES = {"Todos": None, "REACTION": "REACTION", "FAILED": "TOUCH_FAILED",
               "INDECISIVE": "TOUCH_INDECISIVE"}


def _open_readonly(path: Path):
    """No CREATE statements or WAL checkpoints: only SELECT on an existing DB."""
    if not path.is_file():
        raise FileNotFoundError(str(path))
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=7)
    db.execute("PRAGMA query_only=ON")
    db.execute("PRAGMA busy_timeout=7000")
    return db


def _allowed_symbols(symbols):
    if symbols is None:
        return None
    return tuple(sorted({str(s).strip().upper() for s in symbols if str(s).strip()}))


def _where_clause(status=None, symbol=None, side=None, origin=None, symbols=None):
    if status is not None and status not in STATUSES:
        raise ValueError("Unsupported first-touch status")
    if side is not None and side not in ("LONG", "SHORT"):
        raise ValueError("Unsupported first-touch side")
    if origin is not None and origin not in ("LIVE_OBSERVED", "RECOVERED_WINDOW"):
        raise ValueError("Unsupported first-touch origin")
    parts, params = [], []
    for col, val in (("status", status), ("symbol", symbol),
                     ("side", side), ("observation_type", origin)):
        if val is not None:
            parts.append(f"{col}=?")
            params.append(val)
    allowed = _allowed_symbols(symbols)
    if allowed is not None:
        if not allowed:
            parts.append("1=0")
        else:
            # Do not assume SQLite builds include a large parameter limit.
            # The canonical universe is 290 symbols; constants are safe after validation.
            if len(allowed) > 900:
                raise ValueError("Maximum supported symbol universe is 900")
            parts.append("symbol IN (" + ",".join("?" for _ in allowed) + ")")
            params.extend(allowed)
    return (" WHERE " + " AND ".join(parts)) if parts else "", params


def get_summary(path: Path, symbols=None):
    """Counts are unfiltered by UI status but limited to permitted symbol universe."""
    with closing(_open_readonly(path)) as con:
        where, params = _where_clause(symbols=symbols)
        rows = con.execute("SELECT status, COUNT(*) FROM first_touches" + where +
                           " GROUP BY status", params).fetchall()
        where_origin = where + (" AND " if where else " WHERE ") + "observation_type=?"
        live = con.execute("SELECT COUNT(*) FROM first_touches" + where_origin,
                           [*params, "LIVE_OBSERVED"]).fetchone()[0]
        conflicts = con.execute("SELECT COUNT(*) FROM classification_conflicts").fetchone()[0]
        last_cycle = con.execute("SELECT ended_ms FROM cycles ORDER BY cycle_id DESC LIMIT 1").fetchone()
    counts = {status: 0 for status in STATUSES}
    counts.update(dict(rows))
    return {"counts": counts, "total": sum(counts.values()), "live": live,
            "conflicting_keys_all_symbols": conflicts,
            "last_cycle_ms": last_cycle[0] if last_cycle else None}


def get_symbols(path: Path, symbols=None):
    with closing(_open_readonly(path)) as con:
        where, params = _where_clause(symbols=symbols)
        return [s for (s,) in con.execute(
            "SELECT DISTINCT symbol FROM first_touches" + where + " ORDER BY symbol", params)]


def get_page(path: Path, *, status=None, symbol=None, side=None, origin=None,
             symbols=None, limit=100, offset=0):
    """Stable newest-first paginated rows, no JSON blobs for the list."""
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    where, params = _where_clause(status, symbol, side, origin, symbols)
    with closing(_open_readonly(path)) as con:
        total = con.execute("SELECT COUNT(*) FROM first_touches" + where, params).fetchone()[0]
        sql = ("SELECT touch_key, legacy_event_key, symbol, side, status, timeframe, "
               "detector, pivot_ts, actionable_ts, retest_ts, known_ts, "
               "observation_type, first_seen_ms FROM first_touches" + where +
               " ORDER BY retest_ts DESC, touch_key ASC LIMIT ? OFFSET ?")
        rows = con.execute(sql, [*params, limit, offset]).fetchall()
    fields = ("touch_key", "legacy_event_key", "symbol", "side", "status", "timeframe",
              "detector", "pivot_ts", "actionable_ts", "retest_ts", "known_ts",
              "observation_type", "first_seen_ms")
    return total, [dict(zip(fields, row)) for row in rows]


def get_touch(path: Path, touch_key: str):
    with closing(_open_readonly(path)) as con:
        row = con.execute("SELECT touch_key, legacy_event_key, symbol, side, status, "
                          "retest_ts, pivot_ts, actionable_ts, observation_type, "
                          "first_seen_ms, causal_json FROM first_touches WHERE touch_key=?",
                          (touch_key,)).fetchone()
    if row is None:
        return None
    obj = json.loads(row[10])
    if not isinstance(obj, dict):
        raise ValueError("Invalid causal_json for first touch")
    # Immutable indexed DB columns take precedence over any JSON fields.
    obj.update(touch_key=row[0], legacy_event_key=row[1], symbol=row[2],
               signal=row[3], status=row[4], retest_timestamp=row[5],
               pivot_timestamp=row[6], actionable_timestamp=row[7],
               observation_type=row[8], first_seen_ms=row[9])
    return obj


def _closed_candles(frame, *, tf_ms):
    """Normalize the known Redis closed-candle provider without filling gaps."""
    import pandas as pd
    if frame is None or frame.empty:
        return pd.DataFrame()
    if not {"timestamp", "open", "high", "low", "close"}.issubset(frame.columns):
        return pd.DataFrame()
    bars = frame[["timestamp", "open", "high", "low", "close"]].copy()
    for col in bars:
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    bars = bars.dropna().sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    bars = bars.loc[
        (bars[["open", "high", "low", "close"]] > 0).all(axis=1)
        & (bars["high"] >= bars[["open", "low", "close"]].max(axis=1))
        & (bars["low"] <= bars[["open", "high", "close"]].min(axis=1))
        & (bars["timestamp"] % tf_ms == 0)
    ]
    return bars


def make_touch_figure(candles, touch: dict, timezone: str = "America/Argentina/Buenos_Aires",
                      *, timeframe="15m", before=90, after=30):
    """Chart only when the retest is inside retained *closed* bars (or next open 15m)."""
    import pandas as pd
    import plotly.graph_objects as go
    tf_ms = 900_000 if timeframe == "15m" else 60_000
    df = _closed_candles(candles, tf_ms=tf_ms)
    if df.empty:
        return None
    retest = int(touch["retest_timestamp"])
    times = df["timestamp"].astype("int64")
    # The enclosing candle may still be open. Plot the marker at its exact
    # 1m timestamp without fabricating an open/partial 15m candle.
    if retest < int(times.min()) or retest >= int(times.max()) + (2 * tf_ms if timeframe == "15m" else tf_ms):
        return None
    position = int(times.searchsorted(retest, side="right") - 1)
    start = max(0, position - before)
    end = min(len(df), position + after + 1)
    pivot = int(touch.get("pivot_timestamp") or 0)
    pivot_pos = int(times.searchsorted(pivot, side="right") - 1)
    if pivot_pos >= 0 and position-pivot_pos <= 250:
        start = max(0, min(start, pivot_pos-4))
    view = df.iloc[start:end].copy()
    x = pd.to_datetime(view["timestamp"], unit="ms", utc=True).dt.tz_convert(timezone)
    fig = go.Figure(data=[go.Candlestick(x=x, open=view["open"], high=view["high"],
                                         low=view["low"], close=view["close"],
                                         name=timeframe)])
    try:
        swing = float(touch.get("swing_price"))
        if swing > 0:
            fig.add_hline(y=swing, line_dash="dash", annotation_text="Swing original")
    except (TypeError, ValueError):
        pass
    for title, ts_field, price_field, shape in (
        ("Pivot", "pivot_timestamp", "swing_price", "triangle-up" if touch.get("signal") == "LONG" else "triangle-down"),
        ("Confirmación", "actionable_timestamp", "entry_price", "circle-open"),
        ("Alejamiento", "departure_timestamp", "departure_price", "square"),
        ("Primer retesteo · " + touch["status"], "retest_timestamp", "retest_price", "diamond"),
    ):
        try:
            ts = int(touch[ts_field]); price = float(touch[price_field])
        except (KeyError, TypeError, ValueError):
            continue
        if not (int(view["timestamp"].min()) <= ts < int(view["timestamp"].max()) + tf_ms):
            if ts_field != "retest_timestamp":
                continue
        when = pd.to_datetime(ts, unit="ms", utc=True).tz_convert(timezone)
        fig.add_trace(go.Scatter(x=[when], y=[price], mode="markers+text", text=[title],
                                 textposition="top center", name=title,
                                 marker={"symbol": shape, "size": 11}))
    fig.update_layout(title=f"{touch['symbol']} · {touch['signal']} · {touch['status']}",
                      xaxis_title=f"Hora ({timezone})", yaxis_title="Precio",
                      xaxis_rangeslider_visible=False, height=550,
                      margin={"l": 5, "r": 5, "t": 55, "b": 5})
    return fig


def render_first_touches_journal(project: Path, *, symbol_universe,
                                 candles_reader: Callable, timezone="America/Argentina/Buenos_Aires"):
    """Streamlit view for the existing Continuous V1 journal & retest panels."""
    import streamlit as st
    import pandas as pd

    db = Path(project) / DB_RELPATH
    st.markdown("### 📚 Continuous V1 · Primeros retesteos persistidos")
    st.caption("Journal causal 15m/3x3. Cada swing conserva su primer toque como "
               "REACTION, FAILED o INDECISIVE. No modifica el scanner, los "
               "candidatos, las operaciones ni el histórico Legacy.")
    if not db.is_file():
        st.warning("Todavía no existe scanner_first_touches_v1.sqlite3. "
                   "Verificá que el nuevo Continuous V1 esté ejecutando ciclos y "
                   "guardando los primeros toques; el dashboard no ejecuta el scanner.")
        return
    try:
        summary = get_summary(db, symbols=symbol_universe)
        available = get_symbols(db, symbols=symbol_universe)
    except (sqlite3.Error, ValueError, OSError) as exc:
        st.error(f"No se pudo consultar el journal First Touches (solo lectura): {exc}")
        return
    columns = st.columns(4)
    columns[0].metric("Total First Touches", f"{summary['total']:,}")
    columns[1].metric("REACTION", f"{summary['counts']['REACTION']:,}")
    columns[2].metric("FAILED", f"{summary['counts']['TOUCH_FAILED']:,}")
    columns[3].metric("INDECISIVE", f"{summary['counts']['TOUCH_INDECISIVE']:,}")
    if summary["conflicting_keys_all_symbols"]:
        st.warning(f"Clasificaciones discrepantes detectadas por el scanner: "
                   f"{summary['conflicting_keys_all_symbols']} identidades. "
                   "El journal conserva la clasificación original.")
    st.caption(f"Live observed: {summary['live']} · Resto: recuperados de ventana. "
               "Los contadores son de eventos únicos guardados, no de scans. "
               "El journal causal y los recorridos posteriores 1m viven en SQLite independientes.")

    a, b, c, d = st.columns([1.5, 1.4, 1, 1.5])
    with a:
        selected_label = st.selectbox("Clasificación", list(UI_STATUSES),
                                      key="continuous_first_touch_status_v1")
    with b:
        selected_symbol = st.selectbox("Símbolo", ["ALL"] + available,
                                       key="continuous_first_touch_symbol_v1")
    with c:
        selected_side = st.selectbox("Side", ["ALL", "LONG", "SHORT"],
                                     key="continuous_first_touch_side_v1")
    with d:
        selected_origin = st.selectbox("Origen", ["ALL", "LIVE_OBSERVED", "RECOVERED_WINDOW"],
                                       key="continuous_first_touch_origin_v1")
    opts = st.columns([1, 2])
    with opts[0]:
        per_page = st.selectbox("Filas por página", [50, 100, 250, 500], index=1,
                                key="continuous_first_touch_page_size_v1")
    status = UI_STATUSES[selected_label]
    symbol = None if selected_symbol == "ALL" else selected_symbol
    side = None if selected_side == "ALL" else selected_side
    origin = None if selected_origin == "ALL" else selected_origin

    # Forward metrics are read from a separate immutable 1m path collector.
    # Original causal first-touch SQLite and Legacy views remain untouched.
    from dashboard.views.reaction_lab_first_touch_metrics_view import (
        render_overview as render_first_touch_metrics_overview,
        enrich_page as first_touch_metrics_page,
        render_touch as render_first_touch_metrics_detail,
    )
    metrics_selection = render_first_touch_metrics_overview(
        project, db, symbols=symbol_universe, status=status,
        side=side, symbol=symbol, origin=origin,
    )
    # Independent descriptive research: never changes the classifier or trading.
    if status in (None, "TOUCH_FAILED"):
        with st.expander("🔬 FAILED Behavior Lab V2 · impulso, retroceso y Market Flow", expanded=False):
            try:
                from dashboard.views.reaction_lab_failed_behavior_view_v2 import render_failed_behavior_panel
                render_failed_behavior_panel(project, symbol=symbol, side=side, origin=origin)
            except Exception as exc:
                st.warning(f"FAILED Behavior Lab V2 no disponible: {exc}")

    if status in (None, "TOUCH_FAILED"):
        with st.expander("📐 FAILED Geometry Lab V3 · cronología desde el retroceso", expanded=False):
            try:
                from dashboard.views.reaction_lab_failed_geometry_view_v3 import render_failed_geometry_panel
                render_failed_geometry_panel(project, symbol=symbol, side=side, origin=origin)
            except Exception as exc:
                st.warning(f"FAILED Geometry Lab V3 no disponible: {exc}")

    try:
        # First query obtains the current filtered cardinality to bound pages.
        total, _ = get_page(db, status=status, symbol=symbol, side=side, origin=origin,
                            symbols=symbol_universe, limit=1)
        pages = max(1, ceil(total / per_page))
        with opts[1]:
            page = st.selectbox("Página", list(range(1, pages+1)),
                                 key="continuous_first_touch_page_v1")
        _, rows = get_page(db, status=status, symbol=symbol, side=side, origin=origin,
                           symbols=symbol_universe, limit=per_page,
                           offset=(page-1)*per_page)
    except (sqlite3.Error, ValueError, OSError) as exc:
        st.error(f"Error al leer primeros retesteos: {exc}")
        return
    st.caption(f"Coincidencias: {total:,} · Página {page}/{pages} · "
               f"Mostrando {len(rows)} eventos (orden: más recientes primero).")
    if not rows:
        st.info("No hay primeros retesteos para estos filtros.")
        return
    df = pd.DataFrame(rows)
    df.insert(0, "retest_ARG", pd.to_datetime(
        df["retest_ts"], unit="ms", utc=True).dt.tz_convert(timezone))
    df.insert(1, "confirmado_ARG", pd.to_datetime(
        df["known_ts"], unit="ms", utc=True).dt.tz_convert(timezone))
    display_cols = ["retest_ARG", "symbol", "side", "status", "timeframe",
                    "detector", "observation_type", "confirmado_ARG", "touch_key"]
    if metrics_selection:
        metric_mode, metric_horizon = metrics_selection
        try:
            df = first_touch_metrics_page(project, db, rows, metric_mode, metric_horizon)
            df.insert(0, "retest_ARG", pd.to_datetime(
                df["retest_ts"], unit="ms", utc=True).dt.tz_convert(timezone))
            df.insert(1, "confirmado_ARG", pd.to_datetime(
                df["known_ts"], unit="ms", utc=True).dt.tz_convert(timezone))
            display_cols += ["bars_1m", "path_state", "MFE_pct", "MAE_pct", "Return_pct"]
            display_cols += ["TP_SL"]
        except (sqlite3.Error, OSError, ValueError) as exc:
            st.warning(f"La tabla individual no pudo consultar los recorridos 1m: {exc}")
    st.dataframe(df[display_cols], use_container_width=True, hide_index=True,
                 key="continuous_first_touch_table_v1")
    st.download_button("⬇ Exportar página (CSV)",
                       data=df.to_csv(index=False).encode("utf-8-sig"),
                       file_name="continuous_v1_first_touches_page.csv", mime="text/csv",
                       key="continuous_first_touch_export_v1")

    pretty = {row["touch_key"]: (f"{row['symbol']} · {row['side']} · {row['status']} · "
              f"{pd.to_datetime(row['retest_ts'],unit='ms',utc=True).tz_convert(timezone):%Y-%m-%d %H:%M} · "
              f"swing {row['pivot_ts']}") for row in rows}
    chosen = st.selectbox("Inspeccionar primer retesteo", list(pretty),
                          format_func=lambda key: pretty[key],
                          key="continuous_first_touch_inspector_v1")
    try:
        touch = get_touch(db, chosen)
    except (sqlite3.Error, ValueError, OSError, json.JSONDecodeError) as exc:
        st.error(f"No se pudo cargar el evento seleccionado: {exc}")
        return
    if not touch:
        st.warning("El evento seleccionado ya no existe en la base.")
        return
    if metrics_selection:
        render_first_touch_metrics_detail(
            project, db, chosen, metrics_selection[0], metrics_selection[1], touch
        )
    with st.expander("Identidad causal y campos registrados", expanded=False):
        st.json(touch)
        st.caption("Registro congelado al primer guardado, sin reconstrucciones futuras.")
    symbol_name = touch["symbol"]
    try:
        candles = candles_reader(symbol=symbol_name, timeframe="15m", limit=400)
        fig = make_touch_figure(candles, touch, timezone=timezone, timeframe="15m")
    except Exception as exc:
        st.warning(f"No se pudo cargar el gráfico 15m desde Redis: {exc}")
        fig = None
    if fig is None:
        st.info("Este evento sigue guardado, pero las velas 15m necesarias ya "
                "no están disponibles en la ventana Redis actual. No se "
                "reconstruye un gráfico histórico con velas inventadas.")
    else:
        st.plotly_chart(fig, use_container_width=True,
                        key="continuous_first_touch_15m_chart_v1",
                        config={"displaylogo": False, "scrollZoom": True})
        st.caption("Gráfico con velas cerradas 15m de Redis; el diamante marca "
                   "el primer toque en su minuto exacto. Si el bloque 15m "
                   "aún no cerró, no se dibuja una vela sintética.")
    if st.checkbox("Ver detalle 1m disponible", value=False,
                   key="continuous_first_touch_show_1m_v1"):
        try:
            one_min = candles_reader(symbol=symbol_name, timeframe="1m", limit=400)
            minute_fig = make_touch_figure(one_min, touch, timezone=timezone,
                                           timeframe="1m", before=60, after=120)
            if minute_fig is not None:
                st.plotly_chart(minute_fig, use_container_width=True,
                                key="continuous_first_touch_1m_chart_v1",
                                config={"displaylogo": False, "scrollZoom": True})
            else:
                st.info("Este gráfico de Redis no tiene velas 1m para el evento. "
                        "Si el colector ya archivó su trayectoria posterior, usá "
                        "'Ver velas 1m guardadas de este evento' en el bloque MFE/MAE.")
        except Exception as exc:
            st.warning(f"No se pudieron consultar las velas 1m: {exc}")
