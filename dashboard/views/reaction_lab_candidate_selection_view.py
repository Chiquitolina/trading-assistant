"""Read-only Candidate Selection Research UI attached to Competition Lab Phase 2.

Only opens separate, hash-linked create-only offline exports. Never recomputes
selection on outcomes and never writes to the running portfolio/replay.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as src:
        for block in iter(lambda: src.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def find_export(competition_root):
    competition_root = Path(competition_root)
    shadow = competition_root / "shadow_manifest.json"
    if not shadow.is_file():
        return None
    expected = _sha(shadow)
    found = []
    custom = os.getenv("CANDIDATE_SELECTION_LAB_DIR", "").strip()
    search_dirs = [competition_root.parent]
    possible = [Path(custom)] if custom else []
    for directory in search_dirs:
        try:
            possible.extend(directory.glob("runner_competition_*selection*"))
            possible.extend(directory.glob("candidate_selection_*"))
        except (PermissionError, OSError):
            pass
    for folder in possible:
        manifest = folder / "selection_manifest.json"
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if (data.get("source_shadow_manifest_sha256") == expected and
                all((folder / n).is_file() for n in (
                    "selection_candidates.csv", "selection_groups.csv",
                    "selection_policy_choices.csv", "candidate_factor_snapshots.csv"))):
                found.append(folder)
        except (ValueError, OSError):
            continue
    return max(found, key=lambda x: (x / "selection_manifest.json").stat().st_mtime_ns) if found else None


@st.cache_data(show_spinner=False)
def _load_selection(path, mtime):
    folder = Path(path)
    manifest = json.loads((folder / "selection_manifest.json").read_text())
    return (
        manifest,
        pd.read_csv(folder / "selection_candidates.csv", keep_default_na=False),
        pd.read_csv(folder / "selection_groups.csv", keep_default_na=False),
        pd.read_csv(folder / "selection_policy_choices.csv", keep_default_na=False),
    )


def _time(ms):
    return pd.to_datetime(int(ms), unit="ms", utc=True).tz_convert("America/Argentina/Buenos_Aires").strftime("%d/%m %H:%M")


def render_selection_research(competition_root, strategy):
    st.subheader("🔬 Candidate Selection Research")
    st.caption("Solo investigación · el desempate original permanece congelado · "
               "el outcome shadow se consulta DESPUÉS de elegir por factores disponibles as-of.")
    folder = find_export(competition_root)
    if folder is None:
        st.info("Todavía no existe un Selection Research export enlazado a este Shadow Ledger. "
                "Ejecutá candidate_selection_lab.py (archivo y comandos en docs/CANDIDATE_SELECTION_LAB.md).")
        return
    src = folder / "selection_manifest.json"
    manifest, candidates, groups, choices = _load_selection(str(folder), src.stat().st_mtime_ns)
    if not manifest.get("baseline_room_parity_verified"):
        st.error("No se verificó paridad del ordenamiento original; se cancela visualización.")
        return
    c = candidates[candidates["strategy"] == strategy].copy()
    g = groups[groups["strategy"] == strategy].copy()
    ch = choices[choices["strategy"] == strategy].copy()
    free = g[g["competition_type"] == "SIMULTANEOUS_FREE_SLOT"]
    a, b, d = st.columns(3)
    a.metric("Eventos con factores", len(c))
    b.metric("Grupos simultáneos", len(g))
    d.metric("Desempates con slot libre", len(free))
    st.caption(f"Archivo: `{folder}` · ",)
    status = manifest.get("feature_status", {})
    st.caption("Strength 4h vs BTC: " + " | ".join(f"{k}: {v}" for k, v in status.items()))
    st.warning("El retorno relativo 4h es un **proxy** de Strength, no equivalencia certificada con "
               "Candidate V2. Las comparaciones son individuales: NO constituyen nueva equity ni "
               "validación fuera de muestra.")
    if g.empty:
        st.info("Este portfolio no contiene competiciones simultáneas.")
        return
    policy_labels = {
        "BASELINE_ROOM": "Baseline · Mayor Room",
        "DIRECTIONAL_STRENGTH_VS_BTC_4H_PROXY": "Mayor Strength direccional vs BTC (proxy)",
        "STRENGTH_THEN_ROOM": "Strength → Room",
        "ROOM_THEN_STRENGTH": "Room → Strength",
        "DIRECTIONAL_RETURN_RANK_4H": "Return Rank direccional",
        "VOLUME_RANK_4H": "Volume Rank",
        "RELATIVE_VOLUME_4H": "Volumen relativo 4h",
    }
    st.markdown("#### Desempates por política (solo grupos con slot libre)")
    if not ch.empty:
        ch["hora ARG"] = ch["ready_ms"].map(_time)
        ch["política"] = ch["policy"].map(lambda k: policy_labels.get(k, k))
        st.dataframe(ch[["hora ARG", "política", "candidate_count", "baseline_event_key",
                         "choice_event_key", "decision_status", "changed_from_baseline",
                         "baseline_shadow_status", "choice_shadow_status",
                         "isolated_delta_net_pct_both_filled"]],
                     use_container_width=True, hide_index=True, height=300)
    else:
        st.info("No hubo desempates posibles porque el slot no estaba libre.")
    st.markdown("#### Inspeccionar una competencia")
    group_ids = g.sort_values("ready_ms")["group_id"].tolist()
    selection = st.selectbox("Grupo simultáneo", group_ids,
                             format_func=lambda k: f"{_time(g.loc[g['group_id']==k,'ready_ms'].iloc[0])} · {k}",
                             key="selection_research_group_v1")
    group = g[g["group_id"] == selection].iloc[0]
    part = c[(c["group_id"] == selection) &
             c["decision"].isin(("ORDER_SUBMITTED", "SKIPPED_SLOT_BUSY"))].copy()
    part["Elegida"] = part["event_key"] == group["baseline_selected_event_key"]
    cols = ["event_key", "Elegida", "decision", "room_1h_pct", "departure_pct",
            "directional_strength_vs_btc_4h_proxy_pct", "strength_status",
            "directional_return_rank_4h", "volume_rank_pct_4h", "relative_volume_4h",
            "shadow_status", "shadow_net_pct"]
    st.dataframe(part[[x for x in cols if x in part]], use_container_width=True, hide_index=True)
    if group["competition_type"] != "SIMULTANEOUS_FREE_SLOT":
        st.info("Este grupo apareció con un slot YA ocupado. No es un desempate ejecutable "
                "sin cambiar previamente la trayectoria de cartera.")
    if not part.empty:
        r = part.copy()
        r["room_1h_pct"] = pd.to_numeric(r["room_1h_pct"], errors="coerce")
        r["directional_strength_vs_btc_4h_proxy_pct"] = pd.to_numeric(
            r["directional_strength_vs_btc_4h_proxy_pct"], errors="coerce")
        r = r.dropna(subset=["room_1h_pct", "directional_strength_vs_btc_4h_proxy_pct"])
        if not r.empty:
            fig = go.Figure()
            for _, v in r.iterrows():
                fig.add_trace(go.Scatter(
                    x=[v["room_1h_pct"]], y=[v["directional_strength_vs_btc_4h_proxy_pct"]],
                    mode="markers+text", text=[v["symbol"]+" "+v["side"]], textposition="top center",
                    marker={"size": 14, "symbol": "star" if v["Elegida"] else "circle"},
                    name=f'{v["symbol"]} · {v["shadow_status"]}',
                    hovertemplate=f'{v["event_key"]}<br>Room: %{{x:.3f}}%<br>Strength proxy: %{{y:.3f}}%<extra></extra>',
                ))
            fig.update_layout(xaxis_title="Room 1h (%)", yaxis_title="Strength direccional 4h vs BTC (%) (proxy)",
                              height=370, margin=dict(l=10, r=10, t=20, b=10))
            st.plotly_chart(fig, use_container_width=True, key="selection_research_scatter_v1")
    st.caption("Resultado TP/SL/NO_FILL mostrado exclusivamente para evaluación retrospectiva. "
               "Para medir capital, comisiones, ocupación posterior y DD de una política nueva, "
               "falta Portfolio Policy Replay con slot único.")
    with st.expander("Fuente, cobertura y supuestos"):
        st.json(manifest)
