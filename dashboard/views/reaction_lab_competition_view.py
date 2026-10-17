"""Independent Streamlit viewer for completed Phase-1 Competition Ledger exports.

This view only reads exported CSV/JSON; no connection to Binance/Redis/replay.
No shadow outcomes or claims about skipped candidates' PnL.
"""
from __future__ import annotations

import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

TZ = ZoneInfo("America/Argentina/Buenos_Aires")


@st.cache_data(show_spinner=False)
def _load(folder: str, manifest_mtime: int):
    root = Path(folder)
    manifest = json.loads((root / "competition_manifest.json").read_text(encoding="utf-8"))
    return (manifest,
            pd.read_csv(root / "competition_decisions.csv", keep_default_na=False),
            pd.read_csv(root / "slot_episodes.csv", keep_default_na=False),
            pd.read_csv(root / "ready_groups.csv", keep_default_na=False))


def _time(ms):
    return pd.to_datetime(int(ms), unit="ms", utc=True).tz_convert(TZ)


def render_competition_lab(output_dir):
    root = Path(output_dir)
    manifest_path = root / "competition_manifest.json"
    st.title("Candidate Competition Lab")
    st.caption("Fase 1 · Solo lectura · Competencia por un único slot · Sin Shadow Outcomes")
    if not manifest_path.is_file():
        st.error(f"No existe la exportación: {manifest_path}")
        return
    try:
        manifest, decisions, episodes, groups = _load(str(root), manifest_path.stat().st_mtime_ns)
    except (OSError, ValueError, KeyError) as exc:
        st.error(f"No se pudo leer el Competition Ledger: {exc}")
        return
    strategies = list(manifest.get("strategies", {}))
    if not strategies:
        st.warning("El ledger no contiene estrategias")
        return
    strategy = st.selectbox("Cartera congelada", strategies)
    stats = manifest["strategies"][strategy]
    cols = st.columns(4)
    cols[0].metric("Órdenes enviadas", stats["orders_submitted"])
    cols[1].metric("Slot ocupado", stats["skipped_slot_busy"])
    cols[2].metric("Atribución verificada", f'{stats["slot_busy_owner_verified"]}/{stats["skipped_slot_busy"]}')
    cols[3].metric("Competencias simultáneas", stats["simultaneous_eligible_groups"])
    st.caption("Todas las decisiones del run congelado. Los conflictos también incluyen "
               "órdenes LIMIT pendientes, aunque no se hayan ejecutado.")

    d = decisions[decisions["strategy"] == strategy].copy()
    e = episodes[episodes["strategy"] == strategy].copy()
    g = groups[groups["strategy"] == strategy].copy()
    e["ready_ms"] = pd.to_numeric(e["ready_ms"], errors="coerce")
    e["reserved_until_ms"] = pd.to_numeric(e["reserved_until_ms"], errors="coerce")
    e["blocked_count"] = pd.to_numeric(e["blocked_count"], errors="coerce").fillna(0).astype(int)
    dates = [x.date() for x in pd.to_datetime(e["ready_ms"], unit="ms", utc=True).dt.tz_convert(TZ)]
    if len(dates) == 0:
        st.warning("Sin episodios de ocupación")
        return
    first, last = min(dates), max(dates)
    day_range = st.date_input("Ventana del timeline", value=(first, last), min_value=first, max_value=last)
    if isinstance(day_range, (tuple, list)) and len(day_range) == 2:
        left, right = day_range
        left_ms = int(pd.Timestamp(left, tz=TZ).tz_convert("UTC").timestamp() * 1000)
        end_ms = int((pd.Timestamp(right, tz=TZ) + pd.Timedelta(days=1)).tz_convert("UTC").timestamp() * 1000)
        e = e[(e["reserved_until_ms"] >= left_ms) & (e["ready_ms"] <= end_ms)]
        d["ready_ms"] = pd.to_numeric(d["ready_ms"], errors="coerce")
        d = d[(d["ready_ms"] >= left_ms) & (d["ready_ms"] <= end_ms)]

    st.subheader("Timeline de ocupación del slot")
    fig = go.Figure()
    display_episodes = e.sort_values("ready_ms")
    for i, (_, row) in enumerate(display_episodes.iterrows()):
        left, right = _time(row["ready_ms"]), _time(row["reserved_until_ms"])
        fig.add_trace(go.Scatter(
            x=[left, right], y=[i, i], mode="lines+markers",
            line=dict(width=9, color="#64748b" if row["order_outcome"] == "NO_FILL" else "#3675ad"),
            marker=dict(size=3), showlegend=False,
            hovertemplate=(f'{row["owner_event_key"]}<br>{row["order_outcome"]}'
                           f'<br>Slot: {row["reservation_minutes"]:.1f} min'
                           f'<br>Candidatas bloqueadas: {row["blocked_count"]}<extra></extra>')))
    fig.update_layout(height=max(350, min(750, 140 + len(e) * 8)),
                      xaxis_title="Fecha y hora · Argentina", yaxis_title="Episodio de slot",
                      yaxis=dict(showticklabels=False), margin=dict(t=15))
    st.plotly_chart(fig, use_container_width=True, config={"displaylogo": False})
    st.caption("Cada segmento representa la reserva del slot desde ORDER_SUBMITTED hasta liberación. "
               "No representa una curva de precios ni el PnL de señales descartadas.")

    st.subheader("Competencias simultáneas")
    comp = g[g["competition_type"].str.startswith("SIMULTANEOUS")].copy()
    if not comp.empty:
        comp["hora_ARG"] = comp["ready_ms"].map(lambda v: _time(v).strftime("%d/%m %H:%M:%S"))
        columns = ["hora_ARG", "competition_type", "eligible_count", "selected_event_key",
                   "owner_at_group_start", "eligible_event_keys"]
        st.dataframe(comp[columns], hide_index=True, use_container_width=True)
    else:
        st.info("Sin dos o más candidatas elegibles al mismo instante en esta cartera")

    st.subheader("Inspección por dueño del slot")
    choices = list(display_episodes["owner_event_key"])
    if choices:
        selected = st.selectbox("Orden que reservó el slot", choices)
        sel = display_episodes[display_episodes["owner_event_key"] == selected].iloc[0]
        st.write({"resultado": sel["order_outcome"],
                  "duración de reserva (min)": float(sel["reservation_minutes"]),
                  "candidatas bloqueadas": int(sel["blocked_count"])})
        block = d[(d["decision"] == "SKIPPED_SLOT_BUSY") & (d["owner_at_decision"] == selected)].copy()
        if block.empty:
            st.info("No hubo candidatas rechazadas por slot ocupado durante esta reserva")
        else:
            block["hora_ARG"] = block["ready_ms"].map(lambda v: _time(v).strftime("%d/%m %H:%M:%S"))
            cols = ["hora_ARG", "event_key", "side", "room_1h", "departure",
                    "volume_rank", "return_rank", "owner_stage"]
            st.dataframe(block[cols], hide_index=True, use_container_width=True)
            st.caption("Estos son candidatos admitidos por filtros pero omitidos debido al slot. "
                       "Su resultado hipotético todavía NO fue calculado.")

    with st.expander("Proveniencia e integridad"):
        st.json({"engine": manifest["engine"], "cohort": manifest["cohort"],
                 "ttl_source": manifest["ttl_source"],
                 "source_hashes": manifest["source_hashes"],
                 "coverage": manifest.get("coverage", {}),
                 "shadow_outcomes_computed": manifest["shadow_outcomes_computed"]})
