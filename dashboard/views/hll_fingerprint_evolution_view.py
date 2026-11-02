"""Subvista opcional: Huella Estructural DINAMICA de un episodio HLL.

NO altera seleccion, criterios, universo, ni los graficos originales.
Requiere hll_structural_fingerprint_evolution_v1.py en la raiz del proyecto.
"""
from __future__ import annotations

import pandas as pd

import hll_structural_fingerprint_v1 as fingerprint
import hll_structural_fingerprint_evolution_v1 as evolution


FEATURES_BY_FAMILY = {
    fam: [fid for fid, definition in fingerprint.FEATURES.items() if definition[0] == fam]
    for fam in fingerprint.FAMILIES
}


def evolution_feature_table(evo, family):
    """Measured values and exact deltas; never a 0-100 quality representation."""
    if family not in fingerprint.FAMILIES:
        raise ValueError("Familia de huella no reconocida")
    rows = [r for r in evolution.compare_features(evo) if r["family_id"] == family]
    return pd.DataFrame([{
        "Variable":r["feature_id"], "#1":r["first_value"],
        "Anterior":r["previous_value"], "Seleccionado":r["selected_value"],
        "Δ vs #1":r["delta_vs_first"], "Δ vs anterior":r["delta_vs_previous"],
        "Unidad":r["unit"], "Definición":r["definition"]
    } for r in rows])


def evolution_plot(evo, feature):
    """UTC axis preserves unequal time spacing between CROSS_UP events."""
    import plotly.graph_objects as go
    if feature not in fingerprint.FEATURES:
        raise ValueError("Variable desconocida")
    snapshots = evo["snapshots"]
    selected_ordinal=evo["selected_ordinal"]
    fig=go.Figure()
    causal=[r for r in snapshots if r["episode_ordinal_report_only"]<=selected_ordinal]
    after=[r for r in snapshots if r["episode_ordinal_report_only"]>selected_ordinal]
    def trace(rows,name,dash,color):
        if not rows:return
        timestamps=[pd.Timestamp(int(r["pre_closed_ts"]),unit="ms",tz="UTC") for r in rows]
        values=[r[feature] for r in rows]
        labels=[f'#{r["episode_ordinal_report_only"]} · {r["attempt_id"]}' for r in rows]
        fig.add_trace(go.Scatter(x=timestamps,y=values,mode="lines+markers",
            line=dict(dash=dash,color=color,width=2),marker=dict(size=8),
            text=labels,hovertemplate="%{text}<br>UTC: %{x}<br>Valor: %{y}<extra></extra>",
            name=name,connectgaps=False))
    trace(causal,"Conocido hasta seleccionado","solid","#26ab91")
    if after:
        # Historical trace begins at the selected cut, so there is a visible
        # boundary and no implication that a later attempt was already known.
        trace(causal[-1:]+after,"Posteriores (retrospectivos)","dot","#d7a45c")
    unit=fingerprint.FEATURES[feature][1]
    fig.update_layout(height=270,xaxis_title="Corte PRE (UTC)",
        yaxis_title=f"{feature} · {unit}",margin=dict(l=6,r=8,t=8,b=25),
        legend=dict(orientation="h",y=-0.48))
    return fig


def render_fingerprint_evolution(members, selected_attempt_id, candles, scale_atr):
    """Place after the selected episode chart in the existing Discovery view.

    - members: ALL rows of the selected episode from validated overlap mapping.
    - selected_attempt_id: same selection used for the original candlestick chart.
    - candles: dict of original 30m OHLC bars (open timestamps).
    """
    import streamlit as st
    with st.expander("🧬 Huella Estructural Dinámica · evolución entre intentos PRE", expanded=False):
        st.caption("La huella se calcula para cada CROSS_UP con SUS propias 96 y 288 velas anteriores. "
            "Se comparan las 35 variables de seis familias; no se crea un puntaje global de calidad.")
        enabled=st.checkbox("Calcular y mostrar la evolución estructural de este episodio",
            value=False,key="hll_fingerprint_dynamic_enabled_v1")
        if not enabled:return
        mode_label=st.radio("Visibilidad temporal",[
            "Hasta el intento seleccionado · causal",
            "Episodio completo · exploración retrospectiva"
        ],horizontal=True,key="hll_fingerprint_dynamic_mode_v1")
        mode="CAUSAL" if mode_label.startswith("Hasta") else "HISTORICAL"
        try:
            # The engine sees future attempt IDs ONLY as catalog metadata; in
            # CAUSAL mode it truncates before calculating future fingerprints.
            seq=members.to_dict("records") if hasattr(members,"to_dict") else list(members)
            result=evolution.build_timeline(seq,candles,selected_attempt_id,scale_atr,mode=mode)
            comparison=evolution.get_comparison(result)
        except (ValueError,TypeError,KeyError,OverflowError,IndexError) as exc:
            st.error(f"No fue posible reconstruir la huella del episodio: {exc}")
            return
        selected=comparison["selected"]
        n_selected=result["selected_ordinal"]
        last_available=len(result["snapshots"])
        c1,c2,c3=st.columns(3)
        c1.metric("Corte seleccionado",f"#{n_selected}")
        c2.metric("Intentos visibles",str(last_available))
        c3.metric("Variables disponibles",f"{selected['observed_features']}/{fingerprint.NUM_FEATURES}")
        if mode=="HISTORICAL" and result["total_episode_attempts_report_only"]>n_selected:
            st.warning("Estás viendo intentos posteriores al corte seleccionado. "
                "Sus datos NO estaban disponibles en ese momento. "
                "Los cálculos de cada corte siguen siendo exclusivamente PRE.")
        else:
            st.caption("Modo causal: solo #1 hasta el intento seleccionado; "
                       "no hay huellas ni métricas futuras en esta vista.")
        st.caption("#1 es la línea de base; ‘Anterior’ es exactamente #N−1 (no se salta "
                   "un intento incompleto); ‘Seleccionado’ coincide con el gráfico de arriba.")
        t=pd.DataFrame(evolution.timeline_table(result))
        t["fecha_pre_utc"]=pd.to_datetime(t.pre_closed_ts,unit="ms",utc=True)
        columns={"attempt_number":"Intento","fecha_pre_utc":"Corte PRE UTC",
                 "attempt_id":"Attempt ID","hours_since_first":"Horas desde #1",
                 "features_available":"Medidas disponibles", "status":"Cobertura",
                 "future_vs_selected":"Posterior al seleccionado"}
        st.dataframe(t[list(columns)].rename(columns=columns),hide_index=True,use_container_width=True)
        if selected["status"]!="COMPLETE_PRE_96_288":
            st.warning(f"El corte seleccionado no tiene PRE 96 y 288 completo: {selected['status']}. "
                "No se imputan valores ni se interpreta como mala calidad.")
        st.markdown("**Evolución de las seis familias**")
        tabs=st.tabs(list(fingerprint.FAMILIES.values()))
        for i,(family,label) in enumerate(fingerprint.FAMILIES.items()):
            with tabs[i]:
                variables=FEATURES_BY_FAMILY[family]
                metric=st.selectbox("Variable para ver su evolución",variables,
                    format_func=lambda key: f"{key} ({fingerprint.FEATURES[key][1]})",
                    key=f"hll_fp_dyn_feature_{family}")
                st.caption(fingerprint.FEATURES[metric][2])
                fig=evolution_plot(result,metric)
                st.plotly_chart(fig,use_container_width=True,
                    key=f"hll_fp_dynamic_plot_{family}")
                df=evolution_feature_table(result,family)
                st.dataframe(df,hide_index=True,use_container_width=True)
        st.caption("Δ positivo o negativo indica diferencia numérica, NO mejora o empeoramiento "
                   "necesariamente. El ATR, la ventana y el estado del mercado pueden cambiar "
                   "entre cortes. Datos faltantes permanecen vacíos. "
                   "Agrupación por episodio: heurística 24/96, no estructuras independientes confirmadas.")
        # Detailed comparison can be exported for external audit, without modifying
        # any persistent HLL reports. Generated only on user interaction.
        export=pd.DataFrame(evolution.compare_features(result))
        st.download_button("Descargar comparación de 35 variables (CSV)",
            data=export.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"hll_fingerprint_evolution_{result['symbol']}_{selected_attempt_id}.csv",
            mime="text/csv",key="hll_fp_dyn_download_v1")
