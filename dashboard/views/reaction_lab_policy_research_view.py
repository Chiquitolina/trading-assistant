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


@st.cache_data(show_spinner=False, max_entries=20)
def _policy_events_cached(folder, file_mtime_ns, strategy, policies, start_ms, end_ms):
    return service.load_policy_event_window(folder, strategy, policies, start_ms, end_ms)


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



def _slot_timeline_chart(segments, paired, policy, start_ms, end_ms):
    """Observed order/position occupancy. Source: policy_events.csv only."""
    fig = go.Figure()
    colors = {service.POLICY_BASE: "#38bdf8", policy: "#fb923c"}
    pretty = {service.POLICY_BASE: "Room original", policy: _label(policy)}
    seen = set()
    for _, r in segments.sort_values(["start_ms", "policy"]).iterrows():
        kind = str(r["stage"])
        title = f"{pretty[r['policy']]} · {kind}"
        show = title not in seen
        seen.add(title)
        fig.add_trace(go.Scatter(
            x=[_time(r["start_ms"]), _time(r["end_ms"])],
            y=[pretty[r["policy"]], pretty[r["policy"]]],
            mode="lines", name=title, showlegend=show,
            line={"color":colors[r["policy"]], "width":16 if kind == "Posición abierta" else 10,
                  "dash":"solid" if kind == "Posición abierta" else "dot"},
            customdata=[str(r["event_key"])]*2,
            hovertemplate="%{customdata}<br>%{x}<extra>"+title+"</extra>",
        ))
    # Mark skipped signals, not filtered/rejected events. These markers do not
    # imply missed executions or profitable trades.
    for label, col in (("Room original", "decision_baseline"), (_label(policy), "decision_policy")):
        skips = paired.loc[paired[col] == "SKIPPED_SLOT_BUSY"].copy()
        if len(skips) > 300:
            skips = skips.iloc[:300].copy()
        if not skips.empty:
            fig.add_trace(go.Scatter(
                x=skips["ready_ms"].map(_time), y=[label]*len(skips),
                mode="markers", name="Señal omitida por slot · "+label,
                marker={"color":"#f43f5e", "symbol":"x", "size":9,
                        "line":{"width":1}},
                customdata=skips["event_key"],
                hovertemplate="Omitida por slot<br>%{customdata}<br>%{x}<extra></extra>",
            ))
    fig.add_vline(x=_time(start_ms).to_pydatetime(), line_dash="dash", line_color="gray")
    fig.update_layout(height=310, margin=dict(l=8,r=10,t=30,b=22),
                      xaxis_title="Hora Argentina · tramo seleccionado",
                      yaxis_title="Cartera / slot", hovermode="closest",
                      legend=dict(orientation="h",y=-.3),
                      xaxis_range=[_time(start_ms),_time(end_ms)])
    return fig


def _consequences_panel(root, divergences, competitions, strategy, policy, start_ms):
    st.subheader("Slot Consequences · ventana de impacto observable")
    st.caption("Solo lectura. Separamos el cambio de selección, las reservas posteriores y una nueva "
               "competencia donde ambos slots vuelven a estar libres. Las diferencias no prueban "
               "causalidad aislada ni equivalen a operaciones perdidas.")
    if policy == service.POLICY_BASE:
        st.info("Elegí una política alternativa para comparar con Room original")
        return
    # One window, one metric scope. The previous version erroneously counted
    # every later divergence while the decisions table used a local slider.
    hours = st.slider("Horas posteriores", min_value=1, max_value=24, value=6,
                      key="policy_research_hours_v1")
    local, later, had_prior, end_ms, boundary = service.scoped_consequences(
        divergences, competitions, strategy, policy, int(start_ms), hours)
    if had_prior:
        original, _ = service.divergence_after(divergences, strategy, policy, 0)
        earlier = original[original["ready_ms"] < start_ms]
        if not earlier.empty:
            st.warning(f"Esta política ya había divergido el {_clock(int(earlier['ready_ms'].min()))}. "
                       "Sus posiciones y capital pueden arrastrar diferencias previas: "
                       "esta ventana es descriptiva, no atribución causal.")
    if boundary is not None and boundary < start_ms + hours*3_600_000:
        st.info(f"Ventana cortada el {_clock(boundary)}: comienza otro desempate con ambos slots "
                "libres y distinta candidata enviada. Esa nueva competencia se analiza por separado; "
                "no constituye un reinicio del capital ni demuestra independencia estadística.")
    st.caption(f"Intervalo de las métricas: {_clock(start_ms)} → {_clock(end_ms)} "
               "(extremo final excluido).")
    counts = local["change_type"].value_counts() if not local.empty else pd.Series(dtype="int64")
    x, y, z = st.columns(3)
    x.metric("Divergencias en esta ventana", len(local))
    y.metric("Decisión distinta", int(counts.get("Decisión distinta",0)))
    z.metric("Misma decisión, otro motivo/reserva", int(counts.get("Misma decisión / reserva o resultado distinto",0)))
    if not later.empty:
        with st.expander(f"Divergencias posteriores fuera de esta ventana ({len(later)}) · NO atribuidas"):
            future = later.copy()
            future["Hora ARG"] = future["ready_ms"].map(_clock)
            st.dataframe(future[["Hora ARG","event_key","baseline_decision","policy_decision", "change_type"]],
                         use_container_width=True, hide_index=True, height=240)
    file_dec = root / "policy_decisions.csv"
    try:
        events = _decisions_cached(str(root), file_dec.stat().st_mtime_ns, strategy,
                                   (service.POLICY_BASE, policy), int(start_ms),int(end_ms))
    except (OSError, KeyError, ValueError) as exc:
        st.error(f"No se pudieron recuperar decisiones: {exc}")
        return
    if events.empty:
        st.info("No hay decisiones en esta ventana")
        return
    try:
        paired = service.paired_window_decisions(events, alternative=policy)
    except (KeyError, ValueError) as exc:
        st.error(f"No se pueden emparejar decisiones por evento: {exc}")
        return
    observed_keys = set(zip(paired.loc[paired["differs"], "ready_ms"],
                            paired.loc[paired["differs"], "event_key"]))
    expected_keys = set(zip(local["ready_ms"], local["event_key"]))
    if observed_keys != expected_keys:
        st.error("Divergencias y decisiones del replay no coinciden dentro de esta ventana. "
                 "No se muestran consecuencias hasta auditar ambos CSV.")
        return
    st.markdown("**Decisiones de ambas carteras en la ventana**")
    only_diff = st.checkbox("Mostrar solo filas diferentes", value=True,
                            key="policy_research_only_diff_42")
    show = paired.loc[paired["differs"]] if only_diff else paired
    show = show.copy()
    show["Hora ARG"] = show["ready_ms"].map(_clock)
    st.dataframe(show[["Hora ARG","event_key","decision_baseline","reason_baseline",
                       "decision_policy","reason_policy","differs"]].rename(columns={
                       "decision_baseline":"Room", "reason_baseline":"Motivo Room",
                       "decision_policy":"Alternativa", "reason_policy":"Motivo alternativa",
                       "differs":"Difiere"}),
                 hide_index=True, use_container_width=True, height=310)
    # Occupancy from actual order event lifecycle, including pending LIMIT.
    st.markdown("**Timeline de ocupación real simulada del slot**")
    st.caption("Barra punteada = LIMIT pendiente · barra continua = posición abierta · "
               "cruz = señal bloqueada por slot. Se usan los timestamps de los eventos "
               "del motor, no se estima la duración por `slot_busy_through`.")
    event_file = root / "policy_events.csv"
    try:
        lifecycle = _policy_events_cached(str(root), event_file.stat().st_mtime_ns,
                                          strategy, (service.POLICY_BASE, policy),
                                          int(start_ms), int(end_ms))
        segments, incomplete = service.build_observed_slot_segments(lifecycle,start_ms,end_ms)
        if incomplete:
            st.warning(f"{incomplete} reservas sin ciclo completo verificable: se omiten del gráfico "
                       "en lugar de inferir su fecha de liberación.")
        if segments.empty:
            st.info("No hay reservas completas observables en el tramo.")
        else:
            st.plotly_chart(_slot_timeline_chart(segments,paired,policy,start_ms,end_ms),
                            use_container_width=True, config={"displaylogo":False,"scrollZoom":True},
                            key="policy_research_slot_timeline_42")
    except (OSError, KeyError, ValueError) as exc:
        st.warning(f"No es posible mostrar una línea temporal verificable: {exc}")
    st.markdown("**Órdenes realmente enviadas que cambiaron entre carteras**")
    st.caption("Una orden NO_FILL no es una operación con PnL. Una señal bloqueada tampoco. "
               "Los netos son de operaciones simuladas ejecutadas, no shadow outcomes.")
    changed = paired.loc[paired["decision_changed"]]
    keys = tuple(changed["event_key"].astype(str))
    try:
        path = root / "policy_trades.csv"
        trades = _trades_cached(str(root),path.stat().st_mtime_ns,strategy,
                                (service.POLICY_BASE,policy),keys) if keys else pd.DataFrame()
        actual = service.actual_order_consequences(paired,trades,policy)
        if actual.empty:
            st.info("Sin cambios en órdenes enviadas dentro de esta ventana")
        else:
            actual["Hora ARG"] = actual["ready_ms"].map(_clock)
            st.dataframe(actual[["Hora ARG","event_key","baseline","baseline_net_pct",
                                 "policy","policy_net_pct"]].rename(columns={
                                 "baseline":"Room · resultado", "baseline_net_pct":"Room net %",
                                 "policy":"Alternativa · resultado", "policy_net_pct":"Alternativa net %"}),
                         use_container_width=True, hide_index=True)
    except (OSError, KeyError, ValueError) as exc:
        st.error(f"No se pudieron auditar las operaciones enviadas: {exc}")

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
