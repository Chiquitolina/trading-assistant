"""Streamlit read-only Portfolio Policy Research (phase 4.1).

CSV-only: no simulated trades or historical data are recomputed at render time.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard.services import reaction_lab_policy_dashboard as service
from dashboard.views.reaction_lab_candidate_selection_view import find_export as find_selection

TZ = "America/Argentina/Buenos_Aires"


def _clock(ms):
    return pd.to_datetime(int(ms), unit="ms", utc=True).tz_convert(TZ).strftime("%d/%m %H:%M")


def _time(ms):
    return pd.to_datetime(int(ms), unit="ms", utc=True).tz_convert(TZ)


def _label(policy):
    return service.POLICY_LABELS.get(policy, policy)


def _candidate_label(key):
    if not key or not str(key).strip():
        return "Sin orden (slot ocupado)"
    pieces = str(key).split("|")
    return f"{pieces[0]} {pieces[1]}" if len(pieces) > 1 else str(key)


@st.cache_data(show_spinner=False)
def _load_core_cached(folder, manifest_mtime_ns, selection_folder):
    manifest = service.validate_policy_run(folder, selection_folder)
    return manifest, service.load_core(folder, manifest)


@st.cache_data(show_spinner=False, max_entries=12)
def _curve_cached(folder, file_mtime_ns, strategy, policies, bucket_minutes):
    return service.equity_curve(folder, strategy, policies, bucket_minutes=bucket_minutes)


@st.cache_data(show_spinner=False, max_entries=20)
def _trades_cached(folder, file_mtime_ns, strategy, policies, event_keys):
    return service.load_trade_detail(folder, strategy, policies, event_keys)


@st.cache_data(show_spinner=False, max_entries=20)
def _decisions_cached(folder, file_mtime_ns, strategy, policies, start_ms, end_ms):
    return service.load_decisions_window(folder, strategy, policies, start_ms, end_ms)


@st.cache_data(show_spinner=False)
def _factors_cached(selection_folder, file_mtime_ns):
    filename = Path(selection_folder) / "candidate_factor_snapshots.csv"
    return pd.read_csv(filename, keep_default_na=False)


def _metrics_table(summary):
    cols = ["policy", "equity_final_realized_usdt", "return_realized_pct",
            "max_marked_drawdown_pct", "profit_factor", "win_rate_pct",
            "resolved_trades", "orders_placed", "skipped_slot_busy",
            "total_fees_and_slippage_usdt"]
    found = [x for x in cols if x in summary.columns]
    show = summary[found].copy()
    if "policy" in show:
        show["policy"] = show["policy"].map(_label)
    return show.rename(columns={
        "policy":"Política", "equity_final_realized_usdt":"Equity final (USDT)",
        "return_realized_pct":"Retorno %", "max_marked_drawdown_pct":"Max DD %",
        "profit_factor":"PF", "win_rate_pct":"WR %", "resolved_trades":"Trades resueltos",
        "orders_placed":"Órdenes", "skipped_slot_busy":"Slot busy",
        "total_fees_and_slippage_usdt":"Costos (USDT)",
    })


def _equity_panel(root, strategy, summary, policies):
    st.subheader("Equity Comparison · carteras simuladas")
    st.caption("Cada curva surge de un replay cronológico independiente, no de sumar Shadow Outcomes. "
               "DD y PF son métricas del motor; las curvas se reducen por hora preservando extremos.")
    selected = st.multiselect("Políticas visibles", policies, default=policies,
                              format_func=_label, key="policy_research_curves_v1")
    if not selected:
        st.info("Elegí al menos una política.")
        return
    rows = summary[summary["policy"].isin(selected)].copy()
    st.dataframe(_metrics_table(rows), use_container_width=True, hide_index=True)
    equity_file = root / "policy_equity.csv"
    try:
        curves = _curve_cached(str(root), equity_file.stat().st_mtime_ns,
                               strategy, tuple(selected), 60)
    except (OSError, ValueError, KeyError) as exc:
        st.error(f"No se pudo leer equity: {exc}")
        return
    if curves.empty:
        st.info("No hay puntos de equity para estas políticas")
        return
    mode = st.radio("Serie", ["Equity marcada (wallet + posición)", "Wallet realizada"],
                    horizontal=True, key="policy_research_equity_metric_v1")
    ycol = "marked_equity_usdt" if mode.startswith("Equity") else "wallet_usdt"
    fig = go.Figure()
    for policy in selected:
        frame = curves[curves["policy"] == policy]
        if frame.empty:
            continue
        fig.add_trace(go.Scatter(
            x=frame["timestamp_ms"].map(_time), y=frame[ycol],
            mode="lines", name=_label(policy),
            line=dict(width=2.8 if policy == service.POLICY_BASE else 1.5,
                      dash="solid" if policy == service.POLICY_BASE else "dot"),
            hovertemplate=f"%{{x}}<br>US$ %{{y:.2f}}<extra>{_label(policy)}</extra>",
        ))
    fig.update_layout(height=500, xaxis_title="Fecha y hora (Argentina)",
                      yaxis_title="USDT estimados", margin=dict(l=8, r=10, t=20, b=12),
                      legend=dict(orientation="h", y=-.2), hovermode="x unified")
    st.plotly_chart(fig, use_container_width=True, config={"scrollZoom":True, "displaylogo":False},
                    key="policy_research_equity_plot_v1")
    st.caption("Curvas muestreadas por intervalo conservando mínimo y máximo de equity marcado. "
               "Los resultados exactos son los del CSV completo y no se alteran al visualizar.")


def _comparison_table(pair):
    out = pair.copy()
    out["Momento ARG"] = out["ready_ms"].map(_clock)
    out["Baseline elige"] = out["order_submitted_event_key_baseline"].map(_candidate_label)
    out["Política elige"] = out["order_submitted_event_key_policy"].map(_candidate_label)
    out["Cambio"] = out["changed_pick"].map({True:"Sí",False:"No"})
    out["Slot libre baseline"] = out["baseline_slot_free"].map({True:"Sí",False:"No"})
    out["Slot libre política"] = out["policy_slot_free"].map({True:"Sí",False:"No"})
    return out[["Momento ARG", "Baseline elige", "Política elige", "Cambio",
                "Slot libre baseline", "Slot libre política", "eligible_count_baseline"]].rename(
                    columns={"eligible_count_baseline":"Elegibles simultáneas"})


def _competition_detail(root, selection_root, strategy, policy, pairs, all_competitions, policies):
    st.subheader("Policy Divergences · mismo ready_ms")
    st.caption("Los pares comparan órdenes efectivamente enviadas en cada cartera; cuando el slot "
               "ya estaba ocupado no se inventa una selección posible.")
    if pairs.empty:
        st.info("No hay competencias simultáneas para esta estrategia.")
        return None
    st.dataframe(_comparison_table(pairs), hide_index=True, use_container_width=True, height=260)
    options = pairs["ready_ms"].tolist()
    selected_ms = st.selectbox("Competencia", options,
                               format_func=lambda ms: f"{_clock(ms)} · " +
                               "/".join(sorted(set(str(pairs.loc[pairs['ready_ms']==ms, 'eligible_event_keys_baseline'].iloc[0]).split(';')))),
                               key="policy_research_group_v1")
    row = pairs[pairs["ready_ms"] == selected_ms].iloc[0]
    st.markdown("**Las siete políticas en el mismo instante**")
    all_choices = all_competitions[(all_competitions["strategy"] == strategy) &
                                   (all_competitions["ready_ms"] == int(selected_ms))].copy()
    all_choices["Política"] = all_choices["policy"].map(_label)
    all_choices["Candidata enviada"] = all_choices["order_submitted_event_key"].map(_candidate_label)
    all_choices["Slot disponible"] = all_choices["slot_was_available_under_policy"].map(
        lambda x: "Sí" if str(x).lower() in ("true", "1", "yes") else "No")
    all_keys = tuple(k for k in all_choices["order_submitted_event_key"].astype(str) if k.strip())
    trades_file = root / "policy_trades.csv"
    all_trades = _trades_cached(str(root), trades_file.stat().st_mtime_ns,
                                strategy, tuple(policies), all_keys)
    if not all_trades.empty:
        fields = [x for x in ("policy", "event_key", "status", "net_pct") if x in all_trades]
        trades_link = all_trades[fields].rename(columns={
            "event_key":"order_submitted_event_key", "status":"Resultado operado",
            "net_pct":"Net % operado"})
        if trades_link.duplicated(["policy", "order_submitted_event_key"]).any():
            st.error("Trades duplicados para una misma política y candidata")
            return int(selected_ms)
        all_choices = all_choices.merge(trades_link, on=["policy", "order_submitted_event_key"],
                                        how="left", validate="one_to_one")
    fields = ["Política", "Candidata enviada", "Slot disponible",
              "Resultado operado", "Net % operado"]
    st.dataframe(all_choices[[x for x in fields if x in all_choices]],
                 use_container_width=True, hide_index=True)
    st.caption("Resultado operado = cartera re-simulada. Si el slot no estaba disponible, "
               "no hay orden ni resultado ejecutado; NO_FILL no equivale a SL.")
    st.markdown("**Comparación enfocada: Room vs política elegida**")
    cols = st.columns(2)
    cols[0].metric("Room original", _candidate_label(row["order_submitted_event_key_baseline"]))
    cols[1].metric(_label(policy), _candidate_label(row["order_submitted_event_key_policy"]))
    if not row["baseline_slot_free"] or not row["policy_slot_free"]:
        st.info("Al menos una cartera ya tenía el slot ocupado. Las diferencias de elección en este "
                "grupo pueden depender de competencias anteriores.")
    keys = [k for k in str(row["eligible_event_keys_baseline"]).split(";") if k]
    facfile = selection_root / "candidate_factor_snapshots.csv"
    factors = _factors_cached(str(selection_root), facfile.stat().st_mtime_ns)
    factors = factors[factors["event_key"].isin(keys)].copy()
    wanted = ["event_key", "room_1h_pct", "departure_pct", "directional_strength_vs_btc_4h_proxy_pct",
              "return_rank_pct_4h", "directional_return_rank_4h", "volume_rank_pct_4h",
              "relative_volume_4h", "room_available_ms", "ranks_available_ms", "feature_snapshot_close_ms"]
    if not factors.empty:
        st.markdown("**Factores causales de todas las candidatas elegibles**")
        st.dataframe(factors[[x for x in wanted if x in factors]], hide_index=True,
                     use_container_width=True)
    selected_keys = [row["order_submitted_event_key_baseline"], row["order_submitted_event_key_policy"]]
    trades_file = root / "policy_trades.csv"
    trades = _trades_cached(str(root), trades_file.stat().st_mtime_ns,
                            strategy, (service.POLICY_BASE, policy), tuple(k for k in selected_keys if k))
    if not trades.empty:
        trades = trades[((trades["policy"] == service.POLICY_BASE) &
                         (trades["event_key"] == selected_keys[0])) |
                        ((trades["policy"] == policy) & (trades["event_key"] == selected_keys[1]))]
        show = ["policy", "event_key", "status", "fill_ts", "exit_ts", "net_pct",
                "net_pnl_usdt", "wallet_after_usdt"]
        st.markdown("**Resultados de las operaciones realmente simuladas**")
        st.dataframe(trades[[x for x in show if x in trades]], hide_index=True, use_container_width=True)
    else:
        st.caption("Sin trades con fill para esas candidatas; pueden existir órdenes NO_FILL o slot ocupado.")
    return int(selected_ms)


def _consequences_panel(root, divergences, competitions, strategy, policy, start_ms):
    st.subheader("Slot Consequences · efecto posterior en decisiones")
    st.caption("Se comparan decisiones de las carteras completas. Un cambio de `slot_busy_through` "
               "puede modificar futuras oportunidades incluso cuando la decisión sigue siendo SKIPPED_SLOT_BUSY. "
               "No atribuir automáticamente cada diferencia posterior a una sola competencia.")
    if policy == service.POLICY_BASE:
        st.info("Elegí una política alternativa para comparar con Room original")
        return
    earlier = divergences[(divergences["strategy"] == strategy) &
                          (divergences["policy"] == policy)]
    current, had_prior = service.divergence_after(divergences, strategy, policy, start_ms)
    if had_prior:
        first = int(earlier["ready_ms"].min())
        st.warning(f"Ya había divergencias desde {_clock(first)}, antes de esta competencia. "
                   "Los cambios posteriores NO se pueden atribuir únicamente al grupo seleccionado.")
    if current.empty:
        st.info("No se registraron diferencias de decisión ni motivo desde ese instante.")
        return
    st.metric("Eventos con divergencia desde la competencia", len(current))
    counts = current["change_type"].value_counts()
    a, b = st.columns(2)
    a.metric("Decisión distinta", int(counts.get("Decisión distinta", 0)))
    b.metric("Misma decisión, distinto resultado/reserva", int(counts.get("Misma decisión / reserva o resultado distinto", 0)))
    table = current.copy()
    table["Hora ARG"] = table["ready_ms"].map(_clock)
    st.dataframe(table[["Hora ARG", "event_key", "baseline_decision", "baseline_reason",
                        "policy_decision", "policy_reason", "change_type"]],
                 use_container_width=True, hide_index=True, height=330)
    st.markdown("**Ventana local de decisiones desde el desempate**")
    hours = st.slider("Horas posteriores", min_value=1, max_value=24, value=6,
                      key="policy_research_hours_v1")
    end_ms = int(start_ms + hours * 3600 * 1000)
    decisions_file = root / "policy_decisions.csv"
    try:
        events = _decisions_cached(str(root), decisions_file.stat().st_mtime_ns, strategy,
                                   (service.POLICY_BASE, policy),
                                   int(start_ms), end_ms)
    except (OSError, KeyError, ValueError) as exc:
        st.error(f"No se pueden consultar las decisiones: {exc}")
        return
    if events.empty:
        st.info("Sin decisiones en esta ventana")
        return
    events["ready_ms"] = pd.to_numeric(events["ready_ms"], errors="raise").astype("int64")
    baseline = events[events["policy"] == service.POLICY_BASE].set_index("event_key")
    variant = events[events["policy"] == policy].set_index("event_key")
    common_keys = baseline.index.intersection(variant.index)
    if not len(common_keys):
        st.warning("La ventana no contiene claves comunes; revisá el export")
        return
    local = pd.DataFrame({
        "event_key": common_keys,
        "ready_ms": baseline.loc[common_keys, "ready_ms"].to_numpy(),
        "baseline": baseline.loc[common_keys, "decision"].to_numpy(),
        "policy": variant.loc[common_keys, "decision"].to_numpy(),
        "baseline_reason": baseline.loc[common_keys, "reason"].to_numpy(),
        "policy_reason": variant.loc[common_keys, "reason"].to_numpy(),
    }).sort_values(["ready_ms", "event_key"])
    local["Hora ARG"] = local["ready_ms"].map(_clock)
    local["Difiere"] = ((local["baseline"] != local["policy"]) |
                        (local["baseline_reason"] != local["policy_reason"]))
    st.dataframe(local[["Hora ARG", "event_key", "baseline", "baseline_reason",
                        "policy", "policy_reason", "Difiere"]],
                 hide_index=True, use_container_width=True, height=320)
    st.caption("Se incluyen decisiones con el mismo `event_key` en ambas carteras. "
               "Las filas posteriores muestran consecuencias observadas, no un test causal independiente.")


def render_policy_research(competition_root, strategy):
    """App-compatible entry point. Called from existing Candidate Competition view."""
    st.subheader("📈 Portfolio Policy Research")
    st.caption("Fase 4.1 · Solo lectura · 1 slot · órdenes LIMIT/TP/SL/reinversión simuladas · "
               "desempates a igual ready_ms · NO ejecución LIVE.")
    selection_root = find_selection(competition_root)
    if selection_root is None:
        st.info("No existe Selection Research ligado al Shadow Ledger. Completá la Fase 3 antes.")
        return
    selection_root = Path(selection_root)
    runs = service.find_policy_runs(selection_root)
    if not runs:
        st.info("No encontré ningún Portfolio Policy Replay con hashes enlazados a este Selection Lab. "
                "Si los resultados están fuera de /root, definí CANDIDATE_POLICY_REPLAY_DIR apuntando al run.")
        return
    roots = [r for r, m in runs if strategy in m.get("strategies", [])]
    if not roots:
        st.info("Hay Policy Replays, pero ninguno incluye la cartera elegida.")
        return
    root = st.selectbox("Portfolio Policy run", roots, format_func=lambda r: r.name,
                        key="policy_research_run_v1")
    try:
        manifest, (summary_all, competitions_all, divergences_all) = _load_core_cached(
            str(root), (root / "manifest.json").stat().st_mtime_ns, str(selection_root))
    except (OSError, ValueError, KeyError) as exc:
        st.error(f"Inconsistencia en el Portfolio Policy Replay: {exc}")
        return
    policies = [p for p in manifest["policies"] if
                ((summary_all["strategy"] == strategy) & (summary_all["policy"] == p)).any()]
    if service.POLICY_BASE not in policies:
        st.error("Sin política BASELINE_ROOM; no se puede comparar.")
        return
    st.caption(f"Origen: `{root}` · baseline original auditado · Strength = proxy 4h (NO canónico). "
               "Resultados del mismo período de descubrimiento; no validación fuera de muestra.")
    if strategy.endswith("Extreme Strength Guard"):
        st.warning("Extreme Guard fue definido post-hoc en este período: su ventaja aún no está validada fuera de muestra.")
    summary = summary_all[summary_all["strategy"] == strategy].copy()
    baseline = summary[summary["policy"] == service.POLICY_BASE].iloc[0]
    x = st.columns(4)
    x[0].metric("Baseline final", f"US$ {float(baseline['equity_final_realized_usdt']):,.2f}")
    x[1].metric("Retorno baseline", f"{float(baseline['return_realized_pct']):+.2f}%")
    x[2].metric("Máx. DD baseline", f"{float(baseline['max_marked_drawdown_pct']):.2f}%")
    x[3].metric("Políticas ejecutadas", len(policies))
    section = st.radio(
        "Vista de investigación",
        ["📈 Equity Comparison", "⚔️ Policy Divergences", "🔗 Slot Consequences"],
        horizontal=True, key="policy_research_section_v1",
    )
    if section == "📈 Equity Comparison":
        _equity_panel(root, strategy, summary, policies)
    elif section == "⚔️ Policy Divergences":
        alt = st.selectbox("Comparar Room contra", [p for p in policies if p != service.POLICY_BASE],
                           format_func=_label, key="policy_research_choice_v1") if len(policies) > 1 else service.POLICY_BASE
        try:
            paired = service.competition_comparison(competitions_all, strategy, alt)
            _competition_detail(root, selection_root, strategy, alt, paired, competitions_all, policies)
        except (OSError, ValueError, KeyError) as exc:
            st.error(f"No se pueden comparar competencias: {exc}")
    else:
        alt2 = st.selectbox("Política alternativa", [p for p in policies if p != service.POLICY_BASE],
                            format_func=_label, key="policy_research_slot_policy_v1") if len(policies) > 1 else service.POLICY_BASE
        try:
            pairs2 = service.competition_comparison(competitions_all, strategy, alt2)
            if pairs2.empty:
                st.info("Sin competencias simultáneas para el análisis de slots")
            else:
                groups = pairs2["ready_ms"].tolist()
                ms = st.selectbox("Punto de comparación", groups, format_func=_clock,
                                  key="policy_research_slot_group_v1")
                _consequences_panel(root, divergences_all, competitions_all, strategy, alt2, int(ms))
        except (OSError, ValueError, KeyError) as exc:
            st.error(f"No se pueden reconstruir consecuencias: {exc}")
    with st.expander("Auditoría / proveniencia / límites de interpretación"):
        st.json({"engine":manifest["engine"], "research_status":manifest.get("research_status"),
                 "source_hashes":manifest["source_hashes"],
                 "reference_parity":manifest.get("reference_parity"),
                 "notices":manifest.get("notices", []),
                 "note":"Los outputs CSV no están firmados. Se verifica el enlace SHA de inputs y coherencia del resumen."})
