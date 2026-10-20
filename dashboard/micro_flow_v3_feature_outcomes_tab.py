"""Streamlit UI for exploratory long-horizon feature/outcome study, not trades."""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from research.micro_flow_v3.microstructure_feature_outcomes import (
    HORIZONS_SECONDS, evaluate_feature_outcome_horizons, save_feature_outcome_run,
)


def _case_frame(study, horizon_s):
    out=[]
    key=f'{horizon_s}s'
    for case in study['cases']:
        o=case['horizons'][key]
        ref=case['entry_reference'] or {}
        diagnostic=o.get('endpoint_only_diagnostic') or {}
        out.append({
          'ID':case['event_id'], 'Variante':case['variant'],
          'Presión':case['pressure_side'],'Hipótesis':case['hypothesis_direction'],
          'Feature grupo':case['feature_group'],
          'Rechazo previo':case['feature_rejection'],
          'Feature estado':case['feature_state'],
          'Señal estado':case['confirmation_state'],
          'Entrada estado':case['reference_state'],
          'Precio referencia NO fill':ref.get('entry_reference_price'),
          'Hora referencia disponible ns':ref.get('entry_reference_available_wall_ns'),
          'Outcome estricto':o['state'],
          'Retorno bruto %':o.get('return_directional_pct'),
          'Neto 12bps ilustrativo %':o.get('net_illustrative_pct'),
          'MFE %':o.get('mfe_directional_pct'),
          'MAE %':o.get('mae_directional_pct'),
          'Cobertura trayectoria':o.get('coverage_fraction'),
          'Diagnóstico endpoint estado':diagnostic.get('state'),
          'Endpoint-only % NO operable':diagnostic.get('directional_endpoint_pct'),
          'Segundos faltantes':diagnostic.get('missing_seconds'),
          'Máximo intervalo sin datos (s)':diagnostic.get('max_unobserved_interval_seconds'),
        })
    return pd.DataFrame(out)


def _summary_frame(study, horizon_s, dimension):
    return pd.DataFrame([r for r in study['summary'] if
        r['horizon_seconds']==horizon_s and r['group_dimension']==dimension])


def render_microstructure_feature_outcomes(root,session,aggression,catalog,entry_study,
                                           *,window_seconds=15):
    st.divider()
    st.subheader('🔬 Microstructure Feature → Outcome Research · hasta 60 min')
    st.caption('Solo análisis exploratorio. Une features conocidas al publicarse el impulso '
       'con las dos entradas A/B ya definidas. Sin modificar detectores, sin fills, '
       'sin filtros retrospectivos de ganadoras. Todas las señales ausentes se conservan.')
    revealed=st.checkbox('Revelar resultados futuros de features (solo investigación)',
                         value=False,key='mfv3_micro_outcome_reveal')
    if not revealed:
        st.info('Features disponibles sin futuro. Activá Revelar para analizar outcomes posteriores.')
        return
    st.write('Horizontes congelados: **1, 3, 5, 10, 15, 30 y 60 minutos**.')
    st.caption('Los outcomes ESTRICTOS requieren cobertura continua (máximo 3s entre '
       'segundos observados), mismo segmento y precio fresco. Los diagnósticos '
       'endpoint-only con huecos NO equivalen a outcomes ni permiten MFE/MAE/TP/SL.')
    config=(str(session['replay_path']),aggression['source_buckets_sha256'],
            entry_study['source_preknown_swings_sha256'],
            catalog['parameters']['min_coverage'],int(window_seconds))
    if st.button('Calcular análisis causal de features + horizontes',key='mfv3_micro_outcome_compute'):
        try:
            with st.spinner('Calculando 1–60 minutos; censura estricta, sin modificar datos fuente...'):
                study=evaluate_feature_outcome_horizons(session['buckets'],aggression,
                    catalog,entry_study,feature_window_seconds=int(window_seconds))
            st.session_state['mfv3_micro_outcome_data']=(config,study)
        except Exception as exc:
            st.error(f'Análisis no disponible: {exc}')
            return
    saved=st.session_state.get('mfv3_micro_outcome_data')
    if not saved or saved[0]!=config:
        st.info('Calculá el análisis para la ventana de features y la sesión seleccionadas.')
        return
    study=saved[1]
    a,b,c=st.columns(3)
    a.metric('Impulsos originales',len(study['cases'])//2)
    b.metric('Hipótesis por impulso','2 · A / B')
    c.metric('Horizonte máximo','60 min')
    horizons=study['parameters']['horizons_seconds']
    h=st.selectbox('Horizonte desde la referencia (minutos)',horizons,
           format_func=lambda x:f'{x//60} min',index=3,key='mfv3_micro_outcome_horizon')
    dimension=st.radio('Comparar grupos retrospectivos',
            ['Relación flujo-precio','Rechazo observable'],horizontal=True,
            key='mfv3_micro_outcome_group')
    dim='alignment' if dimension=='Relación flujo-precio' else 'rejection'
    bygroup=_summary_frame(study,h,dim)
    labels={
     'AGGRESSIVE_FLOW_PRICE_ALIGNED':'Flujo/precio alineados',
     'AGGRESSIVE_FLOW_LOW_PRICE_PROGRESS':'Poco desplazamiento',
     'AGGRESSIVE_FLOW_PRICE_DISAGREEMENT':'Divergencia flujo/precio',
     'FLOW_NOT_ALIGNED_WITH_IMPULSE_SIDE':'Delta contrario al impulso',
     'FEATURE_UNAVAILABLE':'Feature sin cobertura',
     'TRUE':'Con rechazo','FALSE':'Sin rechazo',
    }
    bygroup['Grupo']=bygroup['group'].map(lambda v:labels.get(v,v))
    bygroup['Cobertura %']=bygroup.apply(lambda r:
        (100*r['strict_observed']/r['entry_references']) if r['entry_references'] else None,axis=1)
    shown=bygroup.rename(columns={
        'variant':'Variante','pressure_side':'Presión','impulses':'Impulsos',
        'entry_references':'Referencias','strict_observed':'Outcomes completos',
        'not_strict_observed':'Sin outcome completo',
        'endpoint_only_diagnostics':'Solo endpoint con huecos',
        'mean_gross_pct':'Bruto medio %','mean_net_illustrative_pct':'Neto ilustrativo %',
        'mean_mfe_pct':'MFE medio %','mean_mae_pct':'MAE medio %',
        'gross_positive':'Positivos brutos', 'net_positive_illustrative':'Positivos netos',
    })
    columns=['Variante','Presión','Grupo','Impulsos','Referencias',
             'Outcomes completos','Cobertura %','Solo endpoint con huecos',
             'Bruto medio %','Neto ilustrativo %','MFE medio %','MAE medio %']
    st.dataframe(shown[columns],use_container_width=True,hide_index=True)
    st.caption('Medias calculadas SOLO con recorridos completos. '
       'Los grupos tienen tamaños diferentes; no son controles emparejados ni resultados independientes.')
    positive=bygroup[(bygroup['strict_observed']>=5)&bygroup['mean_gross_pct'].notna()]
    if not positive.empty:
        fig=go.Figure()
        for variant in ('FAILURE','FAILURE_SWING_BREAK'):
            rows=positive[positive['variant']==variant]
            fig.add_trace(go.Bar(x=[f"{r['pressure_side']} · {r['Grupo']}" for _,r in rows.iterrows()],
                         y=rows['mean_gross_pct'].tolist(),name=variant,
                         text=[f"n={n}" for n in rows['strict_observed']]))
        fig.update_layout(barmode='group',height=340,title=f'Retorno bruto estrictamente observado a {h//60} min (n≥5)',
           yaxis_title='% (NO rentabilidad de trading)',margin=dict(l=10,r=10,t=50,b=110))
        st.plotly_chart(fig,use_container_width=True,key='mfv3_micro_outcome_group_chart')
    if (bygroup['strict_observed']<10).any():
        st.warning('Varios grupos tienen menos de 10 outcomes estrictos. '
                   'No se deben optimizar thresholds ni afirmar edge sobre estas muestras.')
    frame=_case_frame(study,h)
    st.markdown('**Todas las filas: incluidos NO_SIGNAL y censurados**')
    st.dataframe(frame,use_container_width=True,hide_index=True)
    st.download_button('Exportar casos A/B del horizonte seleccionado (CSV)',
          frame.to_csv(index=False),file_name=f'microstructure_outcomes_{h}s.csv',
          mime='text/csv',key='mfv3_micro_outcome_csv')
    st.download_button('Exportar resumen de TODAS las cohortes/horizontes (CSV)',
          pd.DataFrame(study['summary']).to_csv(index=False),
          file_name='microstructure_outcomes_all_horizons.csv',
          mime='text/csv',key='mfv3_micro_outcome_all_csv')
    st.markdown('**Comparación emparejada A/B: mismos impulsos, dos tiempos de entrada**')
    st.dataframe(pd.DataFrame(study['paired']),use_container_width=True,hide_index=True)
    st.caption('La comparación A/B es condicional y no aleatoria. '
               'El neto usa 12 bps ilustrativos, no costos medidos ni fills ejecutables.')
    if st.button('Guardar corrida inmutable Features → Outcomes (verificar RAW + Replay)',
                 key='mfv3_micro_outcome_save'):
        try:
            with st.spinner('Verificando RAW lógico, Replay y hashes antes de persistir...'):
                path=save_feature_outcome_run(root,session,aggression,catalog,entry_study,study)
            st.success(f'Investigación guardada: {path}')
        except Exception as exc:
            st.error(f'No se guardó la corrida: {exc}')
