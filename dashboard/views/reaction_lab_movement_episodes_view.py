"""Compact Movement Episodes view — manual, read-only REACTION normalization."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from contextlib import closing


def render_movement_episodes(project_root):
    import pandas as pd
    import plotly.graph_objects as go
    import streamlit as st

    from reaction_lab_movement_episodes import build_episodes, readonly_uri

    project = Path(project_root).resolve()
    output = project / "reports" / "reaction_lab" / "movement_episodes_v1"
    manifest = output / "manifest.json"
    episodes_file = output / "episodes.csv"
    events_file = output / "events.csv"

    st.markdown("### 🧩 Movimientos, no señales duplicadas")
    st.caption(
        "Agrupamos REACTIONs del **mismo símbolo y dirección** cuando hay continuidad "
        "verificable hasta el momento de cada señal. No eliminamos eventos: cada REACTION "
        "conserva su `event_key` y pertenece a un episodio."
    )
    x, y = st.columns([3.5, 1.2], vertical_alignment="center")
    x.caption("Experimento independiente · SQLite de origen en solo lectura · sin modificar LIVE")
    if y.button("↻ Agrupar episodios", type="primary", use_container_width=True, key="me_refresh"):
        try:
            with st.spinner("Uniendo REACTIONs y verificando continuidad 1m..."):
                build_episodes(project)
            st.success("Episodios regenerados. No se modificaron bases ni collectors.")
        except Exception as exc:
            st.error(f"No se pudieron construir episodios: {exc}")
            return

    if not manifest.is_file() or not episodes_file.is_file() or not events_file.is_file():
        st.info("Presioná **Agrupar episodios** para generar el primer mapa de movimientos.")
        return

    @st.cache_data(show_spinner=False, max_entries=3)
    def read_outputs(ep_path, ep_stamp, event_path, event_stamp):
        return pd.read_csv(ep_path), pd.read_csv(event_path)

    try:
        config = json.loads(manifest.read_text(encoding="utf-8"))
        episodes, events = read_outputs(
            str(episodes_file), episodes_file.stat().st_mtime_ns,
            str(events_file), events_file.stat().st_mtime_ns,
        )
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        st.error(f"Error leyendo resultados de episodios: {exc}")
        return
    if episodes.empty:
        st.warning("El scanner no contiene REACTIONs.")
        return

    n_events = len(events)
    n_eps = len(episodes)
    multi = episodes.loc[episodes["reactions"] > 1]
    grouped = int(multi["reactions"].sum())
    missing = int(episodes["verification"].ne("VERIFIED_SEED_PATH").sum())

    a, b, c, d = st.columns(4, gap="small")
    a.metric("REACTIONs", f"{n_events:,}".replace(",", "."))
    b.metric("Episodios", f"{n_eps:,}".replace(",", "."))
    c.metric("Con varios retests", f"{len(multi):,}".replace(",", "."))
    d.metric("Señales en episodios múltiples", f"{grouped:,}".replace(",", "."))
    st.caption(
        f"{config.get('created_utc', '')[:16].replace('T', ' ')} UTC · "
        f"{missing:,} episodios sin trayectoria inicial verificable (se mantienen separados). "
        "Un episodio no es necesariamente una oportunidad independiente del resto del mercado."
    )

    tab_summary, tab_explore = st.tabs(["📊 Panorama", "🔎 Explorar movimientos"])
    with tab_summary:
        left, right = st.columns([1.25, 1], gap="large")
        with left:
            st.markdown("**¿Cuántas REACTIONs tiene cada movimiento?**")
            buckets = episodes["reactions"].map(lambda n: "1" if n == 1 else "2" if n == 2 else "3" if n == 3 else "4+")
            bar = buckets.value_counts().reindex(["1", "2", "3", "4+"], fill_value=0)
            fig = go.Figure(go.Bar(
                x=["1 señal", "2 señales", "3 señales", "4 o más"],
                y=bar.values, text=bar.values, textposition="outside",
                marker_color=["#64748b", "#3fb8a3", "#258fbc", "#907ad4"],
                hovertemplate="Episodios: %{y}<extra></extra>",
            ))
            fig.update_layout(height=295, margin=dict(l=8, r=5, t=25, b=5),
                              paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                              yaxis_title="Episodios", showlegend=False)
            st.plotly_chart(fig, use_container_width=True, config={"displaylogo": False})
        with right:
            st.markdown("**Por dirección**")
            by_side = episodes.groupby("side", as_index=False).agg(
                episodios=("episode_id", "size"),
                retests=("reactions", "sum"),
            )
            by_side.rename(columns={"side": "Dirección", "episodios": "Episodios", "retests": "REACTIONs"}, inplace=True)
            st.dataframe(by_side, hide_index=True, use_container_width=True)
            st.markdown("**Cómo interpretar**")
            st.caption(
                "Los episodios con varias REACTIONs nos muestran oportunidades de "
                "continuación dentro de un mismo movimiento. Su cantidad final "
                "solo se conoce retrospectivamente: no puede usarse como señal temprana."
            )
        with st.expander("Ver criterios de agrupación", expanded=False):
            settings = config.get("policy", {})
            st.write(
                f"Máximo entre señales: **{settings.get('max_gap_min', 90)} min**. "
                f"Edad máxima: **{settings.get('max_age_min', 360)} min**. "
                f"Invalidación del precio de origen: **{settings.get('anchor_invalidation_pct', 2.0)}%** "
                f"adverso al cierre. Retroceso máximo desde pico favorable: "
                f"**{settings.get('peak_giveback_pct', 2.5)} puntos**."
            )
            st.caption(
                "Solo se revisan velas 1m cerradas antes de que la nueva REACTION fuera conocida. "
                "Sin cobertura no se unen eventos. Señales RECOVERED_WINDOW usan tiempo "
                "histórico as-of y no representan observación LIVE. Se pueden recalcular "
                "asociaciones históricas al aparecer backfills."
            )

    with tab_explore:
        f1, f2, f3 = st.columns([1.25, 1.2, 1], gap="small")
        only_multi = f1.toggle("Solo movimientos con varios retests", value=True, key="me_only_multi")
        search = f2.text_input("Buscar símbolo", placeholder="Ej.: AGT, ARC...", key="me_search").upper().strip()
        direction = f3.selectbox("Dirección", ["Todas", "LONG", "SHORT"], key="me_side")
        scoped = episodes
        if only_multi:
            scoped = scoped.loc[scoped["reactions"] > 1]
        if search:
            scoped = scoped.loc[scoped["symbol"].str.contains(search, regex=False, na=False)]
        if direction != "Todas":
            scoped = scoped.loc[scoped["side"].eq(direction)]
        scoped = scoped.sort_values(["reactions", "start_ready_ms"], ascending=[False, False])
        st.caption(f"{len(scoped)} episodios coinciden con estos filtros")
        display = scoped.head(150).copy()
        display["Inicio UTC"] = pd.to_datetime(display["start_ready_ms"], unit="ms", utc=True).dt.strftime("%d/%m %H:%M")
        display.rename(columns={"symbol": "Símbolo", "side": "Lado", "reactions": "REACTIONs", "episode_id": "ID"}, inplace=True)
        st.dataframe(display[["Símbolo", "Lado", "REACTIONs", "Inicio UTC", "ID"]],
                     hide_index=True, use_container_width=True, height=250)
        if scoped.empty:
            return
        options = scoped["episode_id"].tolist()[:400]
        display_map = {
            str(x["episode_id"]): f"{x['symbol']} · {x['side']} · {int(x['reactions'])} señales · {x['episode_id']}"
            for _, x in scoped.head(400).iterrows()
        }
        ep_id = st.selectbox("Inspeccionar movimiento", options, format_func=lambda x: display_map[x], key="me_choice")
        selected = scoped.loc[scoped["episode_id"].eq(ep_id)].iloc[0]
        members = events.loc[events["episode_id"].eq(ep_id)].copy()
        members["Observación UTC"] = pd.to_datetime(members["ready_ms"], unit="ms", utc=True, errors="coerce").dt.strftime("%d/%m %H:%M")
        members.rename(columns={"position_in_episode": "N.º", "decision": "Motivo", "observation_type": "Origen"}, inplace=True)
        st.dataframe(members[["N.º", "Observación UTC", "event_key", "Origen", "Motivo"]],
                     hide_index=True, use_container_width=True,
                     height=min(330, 40 * (len(members) + 1)))
        if str(selected.get("origin_basis")) == "RECOVERED_WINDOW":
            st.caption("Este episodio es retrospectivo (RECOVERED_WINDOW); no equivale a señales observadas LIVE.")
        if st.toggle("Ver recorrido 1m normalizado", value=False, key="me_show_chart"):
            _episode_chart(project, selected, members)

    with st.expander("Descargar datos y trazabilidad", expanded=False):
        st.download_button("Descargar episodios CSV", episodes_file.read_bytes(),
                           file_name="movement_episodes_v1.csv", mime="text/csv", key="me_export_episodes")
        st.download_button("Descargar REACTIONs vinculadas CSV", events_file.read_bytes(),
                           file_name="movement_episode_members.csv", mime="text/csv", key="me_export_events")
        st.caption("Los archivos se generaron como snapshot: no actualizan ni sobrescriben las bases de origen.")


def _episode_chart(project, episode, members):
    import pandas as pd
    import plotly.graph_objects as go
    import streamlit as st
    from reaction_lab_auto_paths import decode_path
    from reaction_lab_movement_episodes import readonly_uri

    path = Path(project) / "reports" / "reaction_lab" / "auto_paths_v1.sqlite3"
    scanner = Path(project) / "reports" / "reaction_lab" / "scanner_events_v1.sqlite3"
    try:
        with closing(sqlite3.connect(readonly_uri(path), uri=True)) as con:
            row = con.execute("SELECT path_blob FROM events WHERE event_key=?",
                              (str(episode["first_event_key"]),)).fetchone()
        with closing(sqlite3.connect(readonly_uri(scanner), uri=True)) as con:
            seed = con.execute("SELECT causal_json FROM events WHERE event_key=?",
                               (str(episode["first_event_key"]),)).fetchone()
        bars = decode_path(row[0]) if row and row[0] else []
        reference = float(json.loads(seed[0] or "{}")["retest_close"]) if seed else 0
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        st.caption(f"No se puede reconstruir la trayectoria inicial: {type(exc).__name__}")
        return
    if not bars or reference <= 0:
        st.info("No hay trayectoria completa para graficar el primer retest.")
        return
    sign = 1 if episode["side"] == "LONG" else -1
    frame = pd.DataFrame(bars, columns=["ts", "open", "high", "low", "close"])
    frame["normalizado"] = 100 * sign * (frame["close"] / reference - 1)
    frame["UTC"] = pd.to_datetime(frame["ts"], unit="ms", utc=True)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=frame["UTC"], y=frame["normalizado"], mode="lines",
                             name="Precio normalizado", line=dict(color="#37bda9", width=2)))
    fig.add_hline(y=0, line_dash="dot", line_color="#94a3b8", opacity=.65)
    points_x, points_y, captions = [], [], []
    for _, m in members.iterrows():
        ready = pd.to_numeric(m["ready_ms"], errors="coerce")
        if pd.isna(ready):
            continue
        # Previous fully closed bar only — no future price used for marker.
        eligible = frame.loc[frame["ts"] + 60000 <= int(ready)]
        if eligible.empty:
            continue
        candle = eligible.iloc[-1]
        points_x.append(candle["UTC"])
        points_y.append(candle["normalizado"])
        captions.append(f"REACTION {int(m['N.º'])}: {m['event_key']}")
    if points_x:
        fig.add_trace(go.Scatter(x=points_x, y=points_y, mode="markers+text",
            marker=dict(color="#f5b354", size=10, line=dict(color="#1e293b", width=1)),
            text=[str(i+1) for i in range(len(points_x))], textposition="top center",
            hovertext=captions, hovertemplate="%{hovertext}<br>%{y:.2f}%<extra></extra>",
            name="REACTIONs"))
    fig.update_layout(height=350, margin=dict(l=5, r=10, t=10, b=5),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        xaxis_title="Tiempo (UTC)", yaxis_title="Movimiento favorable firmado (%)",
        showlegend=False, hovermode="x unified")
    st.plotly_chart(fig, use_container_width=True, config={"displaylogo":False})
    st.caption("Trayectoria histórica desde la primera REACTION; LONG/SHORT normalizados para que subir sea favorable. "
               "El gráfico completo es retrospectivo: solo la asignación de miembros usa velas cerradas hasta cada observación.")
