"""Interactive Competition Lab: slot Gantt and isolated candidate shadow paths.

Read-only. All charts show historical research simulations, never live execution.
No portfolio selection is changed by this dashboard.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from dashboard.views.reaction_lab_candidate_selection_view import render_selection_research
from dashboard.views.reaction_lab_policy_research_view import render_policy_research

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

TZ = ZoneInfo("America/Argentina/Buenos_Aires")
PALETTE = {
    "TP": "#22c55e", "SL": "#ef4444", "AMBIGUOUS_BOTH_AS_SL": "#ef4444",
    "TIMEOUT": "#f59e0b", "NO_FILL": "#64748b",
    "CENSORED_AFTER_FILL": "#a78bfa", "UNOBSERVABLE_WINDOW": "#a78bfa",
    "AMBIGUOUS_FILL_BAR": "#a78bfa",
}


@st.cache_data(show_spinner=False)
def _load(folder: str, manifest_mtime: int, shadow_mtime: int):
    root = Path(folder)
    manifest = json.loads((root / "competition_manifest.json").read_text(encoding="utf-8"))
    decisions = pd.read_csv(root / "competition_decisions.csv", keep_default_na=False)
    episodes = pd.read_csv(root / "slot_episodes.csv", keep_default_na=False)
    groups = pd.read_csv(root / "ready_groups.csv", keep_default_na=False)
    shadow_path = root / "shadow_manifest.json"
    if shadow_path.is_file():
        shadow_manifest = json.loads(shadow_path.read_text(encoding="utf-8"))
        outcomes = pd.read_csv(root / "shadow_outcomes.csv", keep_default_na=False)
    else:
        shadow_manifest = None
        outcomes = pd.DataFrame()
    return manifest, decisions, episodes, groups, shadow_manifest, outcomes


@st.cache_data(show_spinner=False)
def _paths(folder: str, mtime_ns: int, event_keys: tuple[str, ...]):
    """Indexed point queries; 290-symbol results never get fully loaded."""
    file = Path(folder) / "shadow_paths_1m.sqlite3"
    if not event_keys:
        return pd.DataFrame()
    if len(event_keys) > 100:
        raise ValueError("Too many simultaneous candidates for interactive inspection")
    placeholders = ",".join("?" for _ in event_keys)
    connection = sqlite3.connect(file.as_uri() + "?mode=ro", uri=True)
    try:
        return pd.read_sql_query(
            "SELECT event_key,timestamp_ms,open,high,low,close FROM paths "
            f"WHERE event_key IN ({placeholders}) ORDER BY event_key,timestamp_ms",
            connection, params=list(event_keys))
    finally:
        connection.close()


def _time(ms):
    return pd.to_datetime(int(ms), unit="ms", utc=True).tz_convert(TZ)


def _clock(ms):
    if ms is None or str(ms).strip() == "":
        return "—"
    return _time(ms).strftime("%d/%m %H:%M")


def _label(key):
    parts = str(key).split("|")
    if len(parts) != 3:
        return str(key)
    return f"{parts[0]} {parts[1]} · {_clock(parts[2])}"


def _numeric(dataframe, *columns):
    for col in columns:
        if col in dataframe.columns:
            dataframe[col] = pd.to_numeric(dataframe[col], errors="coerce")


def _slot_figure(display, blocked, *, max_height=750):
    """Gantt lanes are SYMBOL categories, not monotonically increasing event IDs."""
    fig = go.Figure()
    shown = display.sort_values("ready_ms")
    categories = list(dict.fromkeys(shown["owner_symbol"].tolist()))
    for _, row in shown.iterrows():
        symbol = str(row["owner_symbol"])
        start, end = _time(row["ready_ms"]), _time(row["reserved_until_ms"])
        outcome = str(row["order_outcome"])
        fig.add_trace(go.Scatter(
            x=[start, end], y=[symbol, symbol], mode="lines",
            line=dict(width=13, color=PALETTE.get(outcome, "#38bdf8")),
            showlegend=False, customdata=[[row["owner_event_key"], outcome,
                                           int(row["blocked_count"])] for _ in (0, 1)],
            hovertemplate="<b>%{customdata[0]}</b><br>Resultado: %{customdata[1]}"
                          "<br>Bloqueadas: %{customdata[2]}<br>%{x}<extra></extra>",
        ))
    if not blocked.empty:
        b = blocked[blocked["owner_at_decision"].isin(set(shown["owner_event_key"]))].copy()
        if not b.empty:
            sym_by_key = dict(zip(shown["owner_event_key"], shown["owner_symbol"]))
            fig.add_trace(go.Scatter(
                x=b["ready_ms"].map(_time),
                y=b["owner_at_decision"].map(sym_by_key),
                mode="markers", name="Candidatas bloqueadas",
                marker=dict(color="#f8fafc", size=8, symbol="x", line=dict(width=1, color="#0f172a")),
                text=b["event_key"],
                hovertemplate="Candidata bloqueada: %{text}<br>%{x}<extra></extra>",
            ))
    fig.update_layout(
        height=min(max_height, max(340, 185 + 29 * len(categories))),
        xaxis_title="Tiempo (Argentina)", yaxis_title="Símbolo dueño del slot",
        yaxis=dict(categoryorder="array", categoryarray=list(reversed(categories))),
        margin=dict(l=15, r=12, t=18, b=24), hovermode="closest",
        dragmode="zoom", legend=dict(orientation="h", y=-.12),
    )
    return fig


def _outcome_table(shadow):
    cols = ["event_key", "actual_decision", "shadow_class", "shadow_status",
            "shadow_net_pct", "shadow_mfe_raw_pct", "shadow_mae_raw_pct",
            "room_1h", "return_rank", "volume_rank", "shadow_duration_min"]
    return shadow[[k for k in cols if k in shadow.columns]].rename(columns={
        "event_key": "Candidata", "actual_decision": "Decisión real simulada",
        "shadow_class": "Clase de candidata", "shadow_status": "Resultado aislado",
        "shadow_net_pct": "Net % aislado", "shadow_mfe_raw_pct": "MFE bruto %",
        "shadow_mae_raw_pct": "MAE bruto %", "room_1h": "Room 1h %",
        "return_rank": "Return Rank 4h", "volume_rank": "Volume Rank 4h",
        "shadow_duration_min": "Duración min",
    })


def _trajectory_segments(bars, *, anchor, side, fill_ms=None, exit_ms=None, exit_price=None):
    """Separate observed 1m CLOSEs from the exact modeled fill/exit prices.

    A candle's close at exit time might happen AFTER the SL/TP hit. Therefore,
    never use that close as an executable exit or continue the solid trade line
    after `exit_ms`. `market_after` is observational, not a held position.
    """
    series = bars.sort_values("timestamp_ms").copy()
    direction = 1 if side == "LONG" else -1
    series["directional_pct"] = direction * (series["close"] / float(anchor) - 1) * 100
    if fill_ms is None:  # NO_FILL: this is a market-only reference path
        return {"pre_fill": series.iloc[:0], "active": series.iloc[:0],
                "market_after": series, "exit_point": None}
    pre = series[series["timestamp_ms"] < fill_ms]
    held = series[series["timestamp_ms"] >= fill_ms]
    exit_point = None
    if exit_ms is not None:
        held = held[held["timestamp_ms"] < exit_ms]
        after = series[series["timestamp_ms"] > exit_ms]
        if exit_price is not None and pd.notna(exit_price):
            exit_point = (int(exit_ms), direction * (float(exit_price) / float(anchor) - 1) * 100)
    else:
        after = series.iloc[:0]
    return {"pre_fill": pre, "active": held, "market_after": after,
            "exit_point": exit_point}


def _blocked_summary(shadow, decisions):
    """Descriptive individual outcomes for *eligible slot-blocked* candidates.

    Never aggregate hypothetical isolated returns as portfolio PnL.
    """
    blocked = shadow.loc[shadow["shadow_class"] == "ELIGIBLE_BLOCKED_SLOT"].copy()
    blocked["shadow_net_pct"] = pd.to_numeric(blocked["shadow_net_pct"], errors="coerce")
    blocked["shadow_fill_ts"] = pd.to_numeric(blocked["shadow_fill_ts"], errors="coerce")
    owner_fields = decisions[["event_key", "owner_stage", "owner_at_decision"]].drop_duplicates("event_key")
    blocked = blocked.merge(owner_fields, on="event_key", how="left", validate="one_to_one")
    summary = {
        "blocked": len(blocked),
        "filled": int(blocked["shadow_fill_ts"].notna().sum()),
        "tp": int(blocked["shadow_status"].eq("TP").sum()),
        "sl": int(blocked["shadow_status"].isin(["SL", "AMBIGUOUS_BOTH_AS_SL"]).sum()),
        "timeout": int(blocked["shadow_status"].eq("TIMEOUT").sum()),
        "no_fill": int(blocked["shadow_status"].eq("NO_FILL").sum()),
        "unresolved": int((~blocked["shadow_status"].isin(
            ["TP", "SL", "AMBIGUOUS_BOTH_AS_SL", "TIMEOUT", "NO_FILL"])).sum()),
    }
    filled = blocked.loc[blocked["shadow_fill_ts"].notna(), "shadow_net_pct"].dropna()
    summary["mean_net_pct_filled"] = float(filled.mean()) if not filled.empty else None
    return summary, blocked


def _blocked_panel(shadow, decisions):
    stats, blocked = _blocked_summary(shadow, decisions)
    st.markdown("#### Resumen de candidatas elegibles bloqueadas por el slot")
    c = st.columns(4)
    c[0].metric("Bloqueadas", stats["blocked"])
    c[1].metric("Fill hipotético", f'{stats["filled"]}/{stats["blocked"]}')
    c[2].metric("TP aislados", stats["tp"])
    c[3].metric("SL aislados", stats["sl"])
    st.caption(f'TIMEOUT: {stats["timeout"]} · NO_FILL: {stats["no_fill"]} · '
               f'No resueltos/ambiguos: {stats["unresolved"]}. '
               "Los outcomes pertenecen a órdenes aisladas, NO a una cartera alternativa.")
    if blocked.empty:
        return
    if stats["mean_net_pct_filled"] is not None:
        st.caption(f'Retorno neto medio individual entre fills: '
                   f'{stats["mean_net_pct_filled"]:+.3f}%. '
                   'No es un rendimiento alcanzable de cartera ni puede sumarse.')
    stage = blocked.groupby(["owner_stage", "shadow_status"], dropna=False).size().unstack(fill_value=0)
    st.markdown("**Motivo de ocupación × resultado aislado**")
    st.dataframe(stage, use_container_width=True)
    owners = (blocked.groupby("owner_at_decision", dropna=False).size()
              .sort_values(ascending=False).head(10).rename("bloqueadas"))
    with st.expander("Operaciones que más candidatas bloquearon", expanded=False):
        st.dataframe(owners, use_container_width=True)


def _trajectory(root, keys, shadow):
    f = root / "shadow_paths_1m.sqlite3"
    if not f.is_file():
        st.warning("No existe shadow_paths_1m.sqlite3; regenerá el export Phase 2.")
        return
    data = _paths(str(root), f.stat().st_mtime_ns, tuple(keys))
    if data.empty:
        st.info("Sin velas 1m para las candidatas seleccionadas")
        return
    index = shadow.set_index("event_key")
    fig = go.Figure()
    for key in keys:
        if key not in index.index:
            continue
        row = index.loc[key]
        bars = data[data["event_key"] == key].sort_values("timestamp_ms").copy()
        if bars.empty:
            continue
        fill_price = pd.to_numeric(pd.Series([row["shadow_fill_price"]]), errors="coerce").iloc[0]
        fill = pd.to_numeric(pd.Series([row["shadow_fill_ts"]]), errors="coerce").iloc[0]
        exit_ = pd.to_numeric(pd.Series([row["shadow_exit_ts"]]), errors="coerce").iloc[0]
        exit_price = pd.to_numeric(pd.Series([row["shadow_exit_price"]]), errors="coerce").iloc[0]
        anchor = float(fill_price) if pd.notna(fill_price) and float(fill_price) > 0 else float(bars.iloc[0]["open"])
        fill_ms = int(fill) if pd.notna(fill) else None
        exit_ms = int(exit_) if pd.notna(exit_) else None
        parts = _trajectory_segments(bars, anchor=anchor, side=row["side"],
                                     fill_ms=fill_ms, exit_ms=exit_ms,
                                     exit_price=float(exit_price) if pd.notna(exit_price) else None)
        status = str(row["shadow_status"])
        tag = "SELECCIONADO" if row["actual_decision"] == "ORDER_SUBMITTED" else "SHADOW"
        color = PALETTE.get(status, "#38bdf8")
        name = f"{row['symbol']} {row['side']} · {tag} · {status}"
        if fill_ms is None:
            market = parts["market_after"]
            if not market.empty:
                fig.add_trace(go.Scatter(
                    x=market["timestamp_ms"].map(_time), y=market["directional_pct"],
                    mode="lines", name=f"{name} · SIN FILL (mercado)",
                    line=dict(color=color, width=1.4, dash="dot"), opacity=0.52,
                    hovertemplate="Precio observado, sin posición<br>%{x}<br>%{y:.3f}%<extra></extra>"))
            continue
        before = parts["pre_fill"]
        if not before.empty:
            fig.add_trace(go.Scatter(
                x=before["timestamp_ms"].map(_time), y=before["directional_pct"],
                mode="lines", showlegend=False,
                line=dict(color=color, width=1, dash="dot"), opacity=0.22,
                hovertemplate="Antes del fill · sin posición<br>%{x}<br>%{y:.3f}%<extra></extra>"))
        active = parts["active"]
        x_active = [_time(fill_ms)] + list(active["timestamp_ms"].map(_time))
        y_active = [0.0] + list(active["directional_pct"])
        exit_point = parts["exit_point"]
        if exit_point is not None:
            x_active.append(_time(exit_point[0]))
            y_active.append(exit_point[1])
        fig.add_trace(go.Scatter(
            x=x_active, y=y_active, mode="lines", name=name,
            line=dict(color=color, width=2.5 if tag == "SELECCIONADO" else 1.8),
            hovertemplate="Posición hipotética abierta<br>%{x}<br>Movimiento bruto: %{y:.3f}%<extra></extra>"))
        fig.add_trace(go.Scatter(
            x=[_time(fill_ms)], y=[0], mode="markers", showlegend=False,
            marker=dict(color=color, size=9, symbol="triangle-up"),
            hovertemplate=f"FILL {_label(key)} (estimado)<extra></extra>"))
        if exit_point is not None:
            fig.add_trace(go.Scatter(
                x=[_time(exit_point[0])], y=[exit_point[1]], mode="markers", showlegend=False,
                marker=dict(color=color, size=12, symbol="x", line=dict(width=2, color="#f8fafc")),
                hovertemplate=f"SALIDA ESTIMADA {_label(key)} · {status}<br>Movimiento bruto: %{{y:.3f}}%"
                              "<extra></extra>"))
            after = parts["market_after"]
            if not after.empty:
                fig.add_trace(go.Scatter(
                    x=[_time(exit_point[0])] + list(after["timestamp_ms"].map(_time)),
                    y=[exit_point[1]] + list(after["directional_pct"]), mode="lines",
                    showlegend=False,
                    line=dict(color=color, width=1.4, dash="dash"), opacity=0.42,
                    hovertemplate="Mercado después de cerrar; sin posición<br>%{x}<br>%{y:.3f}%<extra></extra>"))
    fig.add_hline(y=0, line_dash="dot", line_color="#64748b")
    fig.update_layout(xaxis_title="Tiempo Argentina", yaxis_title="Movimiento direccional vs fill/base (%)",
                      height=480, margin=dict(l=10, r=10, t=18, b=10),
                      legend=dict(orientation="h", y=-.22))
    st.plotly_chart(fig, use_container_width=True, config={"scrollZoom": True, "displaylogo": False})
    st.caption("Línea continua: desde FILL hasta SALIDA del escenario. X: salida a su precio estimado TP/SL/TIMEOUT "
               "(no al cierre de la vela). Línea tenue discontinua: trayectoria de mercado DESPUÉS de la salida, "
               "sin posición. Antes del fill no hay exposición; NO_FILL = mercado de referencia, nunca operación. "
               "Retorno bruto direccional ≠ PnL neto después de comisiones. OHLC 1m no resuelve orden intravela.")


def render_competition_lab(output_dir):
    root = Path(output_dir)
    path = root / "competition_manifest.json"
    st.title("Candidate Competition Lab")
    if not path.is_file():
        st.error(f"No existe el Competition Ledger: {path}")
        return
    sp = root / "shadow_manifest.json"
    try:
        manifest, decisions, episodes, groups, shadow_manifest, shadow = _load(
            str(root), path.stat().st_mtime_ns, sp.stat().st_mtime_ns if sp.is_file() else 0)
    except (OSError, ValueError, KeyError) as exc:
        st.error(f"No se pudo leer el ledger: {exc}")
        return
    has_shadow = shadow_manifest is not None
    st.caption("Fase 1.1 + 2 · Histórico de investigación · Solo lectura · " +
               ("Todas las candidatas con Shadow Outcomes independientes" if has_shadow
                else "Fase 1 disponible, aún no hay Shadow Outcomes"))
    options = list(manifest.get("strategies", {}))
    if not options:
        st.warning("No hay carteras en el ledger")
        return
    strategy = st.selectbox("Cartera congelada", options, key="competition_strategy_v2")
    stats = manifest["strategies"][strategy]
    c = st.columns(4)
    c[0].metric("Órdenes enviadas", stats["orders_submitted"])
    c[1].metric("Bloqueadas por slot", stats["skipped_slot_busy"])
    c[2].metric("Dueño verificado", f'{stats["slot_busy_owner_verified"]}/{stats["skipped_slot_busy"]}')
    c[3].metric("Competencias simultáneas", stats["simultaneous_eligible_groups"])
    d = decisions[decisions["strategy"] == strategy].copy()
    e = episodes[episodes["strategy"] == strategy].copy()
    g = groups[groups["strategy"] == strategy].copy()
    s = shadow[shadow["strategy"] == strategy].copy() if has_shadow else pd.DataFrame()
    _numeric(d, "ready_ms", "room_1h")
    _numeric(e, "ready_ms", "reserved_until_ms", "blocked_count", "reservation_minutes")
    if has_shadow:
        _numeric(s, "ready_ms", "shadow_net_pct", "shadow_mfe_raw_pct", "shadow_mae_raw_pct",
                 "shadow_duration_min", "room_1h", "return_rank", "volume_rank")
        if len(s) != len(d):
            st.error("Shadow Coverage no coincide con Decision Ledger; se detuvo la visualización")
            return
        st.caption(f"Shadow {len(s)}/{len(d)} · Órdenes ejecutadas con paridad "
                   f'{shadow_manifest.get("selected_orders_parity_verified", "?")} · '
                   "sin sumar operaciones hipotéticas a la equity real simulada")
    else:
        st.info("Generá Fase 2 para visualizar resultados individuales (TP/SL/TIMEOUT/NO_FILL, MFE y MAE).")

    tab_time, tab_cand, tab_groups, tab_select, tab_policy = st.tabs([
        "⏱️ Timeline del slot", "🧪 Todas las candidatas", "⚔️ Simultáneas",
        "🔬 Selection Research", "📊 Portfolio Policy Research"
    ])
    with tab_time:
        st.subheader("Timeline horizontal de ocupación del slot")
        if e.empty:
            st.info("Sin reservas de slot")
            return
        dates = [x.date() for x in pd.to_datetime(e["ready_ms"], unit="ms", utc=True).dt.tz_convert(TZ)]
        first, last = min(dates), max(dates)
        second = min(first + pd.Timedelta(days=1), last)
        drange = st.date_input("Ventana temporal", value=(first, second), min_value=first, max_value=last,
                               key="competition_dates_v2")
        if isinstance(drange, (tuple, list)) and len(drange) == 2:
            left, right = drange
        else:
            left, right = first, second
        left_ms = int(pd.Timestamp(left, tz=TZ).timestamp() * 1000)
        right_ms = int((pd.Timestamp(right, tz=TZ) + pd.Timedelta(days=1)).timestamp() * 1000)
        all_in_window = e[(e["reserved_until_ms"] > left_ms) & (e["ready_ms"] < right_ms)].copy()
        max_rows = st.select_slider("Máximo de reservas en pantalla", options=[20, 40, 80, 160, 400],
                                    value=80, key="competition_max_timeline_v2")
        if len(all_in_window) > max_rows:
            st.info(f"Mostrando {max_rows} de {len(all_in_window)} reservas. Acotá fechas para inspección completa.")
        display = all_in_window.sort_values("ready_ms").head(max_rows)
        busy = d[d["decision"] == "SKIPPED_SLOT_BUSY"]
        if not display.empty:
            st.plotly_chart(_slot_figure(display, busy), use_container_width=True,
                            config={"displaylogo": False, "scrollZoom": True}, key="slot_gantt_v2")
            st.caption("Eje vertical = símbolo que ocupa el slot; cada barra es su reserva temporal. "
                       "Las cruces marcan señales elegibles omitidas porque el slot estaba ocupado. "
                       "Colores: verde TP, rojo SL, naranja TIMEOUT, gris NO_FILL.")
        else:
            st.info("No hay reservas para esas fechas")
        st.subheader("Inspeccionar una reserva y sus alternativas")
        candidates = display if not display.empty else e.sort_values("ready_ms")
        chosen_owner = st.selectbox("Orden que reservó el slot", candidates["owner_event_key"].tolist(),
                                    format_func=_label, key="competition_owner_v2")
        row = e[e["owner_event_key"] == chosen_owner].iloc[0]
        block = d[(d["decision"] == "SKIPPED_SLOT_BUSY") &
                  (d["owner_at_decision"] == chosen_owner)].sort_values("ready_ms")
        same = d[(d["ready_ms"] == row["ready_ms"]) &
                 (d["event_key"] != chosen_owner) &
                 (d["decision"] == "SKIPPED_SLOT_BUSY")]
        st.write({"seleccionada": chosen_owner, "resultado": row["order_outcome"],
                  "inicio": _clock(row["ready_ms"]), "liberación": _clock(row["reserved_until_ms"]),
                  "minutos reservado": float(row["reservation_minutes"]),
                  "bloqueadas": int(row["blocked_count"])})
        keys = list(dict.fromkeys([chosen_owner] + block["event_key"].tolist()))
        if block.empty:
            st.info("No hubo candidatas adicionales bloqueadas durante esta reserva")
        else:
            st.dataframe(block[["event_key", "ready_ms", "owner_stage", "room_1h", "departure",
                                "return_rank", "volume_rank"]], use_container_width=True, hide_index=True)
        if has_shadow:
            subset = s[s["event_key"].isin(keys)].copy()
            subset["__order"] = subset["event_key"].map({key: i for i, key in enumerate(keys)})
            subset = subset.sort_values("__order")
            st.markdown("**Outcomes individuales: seleccionada vs. bloqueadas**")
            st.dataframe(_outcome_table(subset), use_container_width=True, hide_index=True)
            chart_keys = st.multiselect("Candidatas a comparar en trayectoria 1m", keys,
                                       default=keys[:min(4, len(keys))], max_selections=12, format_func=_label,
                                       key="competition_selected_paths_v2")
            if chart_keys:
                _trajectory(root, chart_keys, s)

    with tab_cand:
        st.subheader("Seguimiento individual de todas las candidatas")
        if not has_shadow:
            st.info("Se requiere generación offline de Shadow Outcomes")
        else:
            _blocked_panel(s, d)
            st.divider()
            choice_types = sorted(s["shadow_class"].unique().tolist())
            selected_types = st.multiselect("Tipo de candidata", choice_types,
                                            default=choice_types, key="competition_shadow_classes_v2")
            subset = s[s["shadow_class"].isin(selected_types)].copy()
            symbols = sorted(subset["symbol"].unique().tolist())
            sym = st.selectbox("Símbolo", ["TODOS"] + symbols, key="competition_symbol_v2")
            if sym != "TODOS":
                subset = subset[subset["symbol"] == sym]
            outcomes = ["TODOS"] + sorted(subset["shadow_status"].unique().tolist())
            status = st.selectbox("Resultado aislado", outcomes, key="competition_outcome_v2")
            if status != "TODOS":
                subset = subset[subset["shadow_status"] == status]
            st.caption(f"{len(subset)} candidatas. Las fuera de filtros o vetadas por Guard son "
                       "hipotéticas; NO podían ocupar el slot con esa política congelada.")
            st.dataframe(_outcome_table(subset.sort_values("ready_ms")), use_container_width=True,
                         hide_index=True, height=410)
            if not subset.empty:
                inspected = st.selectbox("Inspeccionar candidata y su trayectoria", subset["event_key"].tolist(),
                                         format_func=_label, key="competition_single_shadow_v2")
                chosen = subset[subset["event_key"] == inspected].iloc[0]
                m = st.columns(4)
                m[0].metric("Resultado aislado", chosen["shadow_status"])
                value = chosen["shadow_net_pct"]
                m[1].metric("Net % aislado", f"{float(value):+.3f}%" if pd.notna(value) else "No observable")
                m[2].metric("MFE bruto %", f"{float(chosen['shadow_mfe_raw_pct']):.3f}%" if pd.notna(chosen["shadow_mfe_raw_pct"]) else "—")
                m[3].metric("MAE bruto %", f"{float(chosen['shadow_mae_raw_pct']):.3f}%" if pd.notna(chosen["shadow_mae_raw_pct"]) else "—")
                _trajectory(root, [inspected], s)
                st.json({k: str(chosen[k]) for k in ("actual_decision", "shadow_class", "shadow_observability",
                                                           "shadow_fill_ts", "shadow_exit_ts", "room_1h",
                                                           "departure", "volume_rank", "return_rank")})
            st.warning("El resultado aislado NO equivale a ganancias sumables: ignora competencia futura "
                       "por slot y capital. MFE/MAE son rangos OHLC; la secuencia intravela es desconocida.")

    with tab_groups:
        st.subheader("Competencias simultáneas al mismo instante")
        sim = g[g["competition_type"].astype(str).str.startswith("SIMULTANEOUS")].copy()
        if sim.empty:
            st.info("No se encontraron grupos con al menos dos candidatas elegibles simultáneas")
        else:
            sim["hora_ARG"] = sim["ready_ms"].map(_clock)
            st.dataframe(sim[["hora_ARG", "competition_type", "eligible_count",
                              "selected_event_key", "owner_at_group_start", "eligible_event_keys"]],
                         use_container_width=True, hide_index=True)
            grp = st.selectbox("Ver grupo simultáneo", sim["group_id"].tolist(),
                               key="competition_group_v2")
            mem = d[d["group_id"] == grp].sort_values("simultaneous_rank")
            st.dataframe(mem[["event_key", "decision", "reason", "room_1h", "departure",
                                  "return_rank", "volume_rank", "owner_at_decision"]],
                         use_container_width=True, hide_index=True)
            if has_shadow:
                linked = s[s["event_key"].isin(mem["event_key"])].copy()
                st.dataframe(_outcome_table(linked), use_container_width=True, hide_index=True)
                _trajectory(root, linked["event_key"].tolist(), s)

    with tab_select:
        render_selection_research(root, strategy)

    with tab_policy:
        render_policy_research(root, strategy)

    with st.expander("Proveniencia, supuestos y seguridad"):
        st.json({"competition_engine": manifest["engine"], "source_hashes": manifest["source_hashes"],
                 "coverage": manifest.get("coverage", {}),
                 "shadow_engine": shadow_manifest.get("engine") if shadow_manifest else None,
                 "selected_order_parity": shadow_manifest.get("selected_orders_parity_verified") if shadow_manifest else None,
                 "cohort": manifest.get("cohort"),
                 "restrictions": shadow_manifest.get("restrictions") if shadow_manifest else []})
