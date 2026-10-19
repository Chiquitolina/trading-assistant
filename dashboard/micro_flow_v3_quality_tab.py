"""Opt-in persisted quality + matched-controls panel for V3 multi-session view."""
import json
from pathlib import Path

import pandas as pd
import streamlit as st

from dashboard.services.micro_flow_v3_quality_baseline import (
    QualityError, persist_quality_run, verify_saved_run,
)


def render_quality_baseline(root, sessions, result, *, parameters):
    """No side effects until user explicitly requests a source-verified save."""
    st.markdown('---')
    st.markdown('### 🧾 Quality & Matched Baseline · evidencia persistida')
    st.caption('Usa los mismos episodios/FAILED y los mismos SQLite cerrados que acabás de analizar. '
               'Los controles se emparejan dentro de la misma sesión y del mismo lado candidato, '
               'según tendencia/volatilidad disponibles ANTES de decidir. El horizonte 300s del control '
               'debe haber terminado antes del evento; no filtra por resultados futuros.')
    col1,col2 = st.columns(2)
    with col1:
        estimated_cost_bps = st.number_input(
            'Escenario de costos ida y vuelta (bps, no reales)',
            min_value=0, max_value=100, value=12, step=2,
            key='mfv3_quality_cost_bps')
    with col2:
        quiet = st.select_slider('Sin señal previa durante (s)',
            options=[10,20,30,60],value=30,key='mfv3_quality_quiet')
    st.caption('Los controles se generan cada 30 segundos, sin reutilización; '
               'se comparan hasta 90 min hacia atrás, con tolerancias de tendencia/volatilidad de 0,20 puntos porcentuales. '
               'No se fuerza un match si no existe control comparable. '
               'Una ventana sin señales anteriores no equivale a un mercado sin señales futuras.')
    params=dict(parameters)
    control_settings={'control_spacing_seconds':30,
                      'signal_quiet_seconds':int(quiet),
                      'match_window_minutes':90,
                      'max_trend_difference_pct':.20,
                      'max_vol_difference_pct':.20,
                      'estimated_round_trip_cost_bps':float(estimated_cost_bps)}
    analysis_signature = (tuple((s['tape'],s['summary']['replay_jsonl_sha256']) for s in sessions),
                          json.dumps(params,sort_keys=True),json.dumps(control_settings,sort_keys=True))
    if st.button('Auditar fuentes y guardar corrida inmutable',
                 key='mfv3_quality_save',type='primary'):
        st.session_state.pop('mfv3_quality_saved', None)
        try:
            with st.spinner('Calculando controles y verificando SHA256 completos de cada tape...'):
                path,manifest=persist_quality_run(root,sessions,result,
                                                  parameters=params,control_settings=control_settings)
            st.session_state['mfv3_quality_saved']=(analysis_signature,str(path))
            st.success(f"Snapshot verificado: {manifest['run_id']}")
        except (OSError, ValueError, QualityError, KeyError, TypeError) as exc:
            st.error(f'No se guardó ninguna corrida: {exc}')
            return
    saved=st.session_state.get('mfv3_quality_saved')
    if not saved or saved[0] != analysis_signature:
        st.info('El guardado es explícito: no se modifican SQLite ni Replay. '
                'Aún no hay snapshot persistido para esta combinación de sesiones y parámetros.')
        return
    folder=Path(saved[1])
    try:
        manifest=verify_saved_run(folder)
        overview=json.loads((folder/'quality_summary.json').read_text(encoding='utf-8'))
        per_session=json.loads((folder/'quality_per_session.json').read_text(encoding='utf-8'))
        comparison=json.loads((folder/'quality_comparison.json').read_text(encoding='utf-8'))
    except (OSError, ValueError, KeyError) as exc:
        st.error(f'Snapshot persistido inválido: {exc}')
        return
    st.code(str(folder), language='text')
    a,b,c=st.columns(3)
    a.metric('Episodios auditados',overview['episodes_total'])
    b.metric('Casos con control',overview['matched_cases'])
    c.metric('Casos evaluables',overview['eligible_cases'])
    st.markdown('#### Calidad por sesión')
    st.dataframe(pd.DataFrame(per_session),hide_index=True,use_container_width=True)
    st.markdown('#### Comparación por estado, lado y horizonte')
    st.dataframe(pd.DataFrame(comparison),hide_index=True,use_container_width=True)
    st.caption('La diferencia es candidato menos control, en puntos porcentuales. '
               'Solo pares con ambos resultados OBSERVED entran en la media. '
               'Sin fills ejecutables, spread real ni validación fuera de muestra.')
    with st.expander('Manifiesto y verificación de procedencia'):
        st.json({'run_id':manifest['run_id'],
                 'sources':manifest['identity']['sources'],
                 'parameters':manifest['identity']['parameters'],
                 'code_sha256':manifest['identity']['software_sha256'],
                 'artifact_sha256':manifest['artifact_sha256']})
        if st.button('Revalidar SQLite + Replay originales (lectura intensa)',
                     key='mfv3_quality_reverify'):
            try:
                verify_saved_run(folder, verify_sources=True, project_root=root)
                st.success('Fuentes originales, Replay y resultados persistidos: hashes correctos')
            except (OSError, ValueError) as exc:
                st.error(f'Integridad comprometida: {exc}')
