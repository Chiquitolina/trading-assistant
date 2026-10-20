"""Compact, opt-in Strategy Context panel for the existing Explore workspace."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from research.micro_flow_v3.swing_sweep_discovery import detect_causal_swings
from research.micro_flow_v3.volume_aggression_confluence import observed_volume_candles
from research.micro_flow_v3.exhaustion_entry_research import evaluate_exhaustion_entries
from research.micro_flow_v3.microstructure_feature_outcomes import evaluate_feature_outcome_horizons
from research.micro_flow_v3.microstructure_context_features import (
    HORIZONS_SECONDS, build_pre_impulse_context, evaluate_context_cohorts,
    save_verified_context_run,
)

LABELS = {
 'AGGRESSION_WITH_PRIOR_TREND':'Agresión a favor del movimiento previo',
 'COUNTER_TREND_AGGRESSION':'Agresión contra movimiento previo (recuperación fallida candidata)',
 'MIXED_PRIOR_CONTEXT':'Contexto mixto',
 'CONTEXT_UNAVAILABLE':'Contexto insuficiente',
}
COHORT_LABELS = {
 'ALL_IMPULSES':'Todos los impulsos',
 'DIVERGENCE_ONLY':'Solo divergencia flujo/precio',
 'DIVERGENCE_AND_REJECTION':'Divergencia + rechazo previo',
}


def _flat_cases(study, horizon):
    out=[]
    for case in study['cases']:
        res=case['horizons'][f'{horizon}s']
        out.append({
            'event_id':case['event_id'], 'variant':case['variant'],
            'pressure_side':case['pressure_side'],'context_group':case['context_group'],
            'feature_group':case['feature_group'],'rejection':case['feature_rejection'],
            'prior_return_5m_pct':case['prior_return_5m_pct'],
            'prior_return_10m_pct':case['prior_return_10m_pct'],
            'prior_cvd_delta_5m':case['prior_cvd_delta_5m'],
            'prior_range_position_10m':case['prior_range_position_10m'],
            'known_1m_high_distance_pct':case['known_1m_high_distance_pct'],
            'known_1m_low_distance_pct':case['known_1m_low_distance_pct'],
            'episode_id':case['episode_id'],'episode_first':case['episode_first'],
            'confirmation_state':case['confirmation_state'],
            'entry_state':case['reference_state'],
            'outcome_state':res['state'],
            'return_directional_pct':res.get('return_directional_pct'),
            'mfe_pct':res.get('mfe_directional_pct'),
            'mae_pct':res.get('mae_directional_pct'),
        })
    return pd.DataFrame(out)


def render_micro_flow_v3_context(root, session, explore_study, selected_event, identity):
    """Never recompute on selector changes. Verification and persistence opt-in."""
    st.divider()
    st.subheader('🧭 Contexto previo · ¿se repite el agotamiento?')
    st.caption('Investigación complementaria: 5m/10m de precio y CVD ANTES de la ventana '
      'de agresión; swings 1m/5m confirmados antes del impulso. No son señales ni fills.')
    st.caption('Hipótesis: agresión en extremos de una subida/bajada vs. intento de '
      'recuperación contra el movimiento previo. Presión BUY y SELL se estudian por separado.')
    if st.button('Calcular contexto previo + comparar A/B (sin cambiar detectores)',
                 key='mfv3_ctx_compute'):
        try:
            with st.spinner('Construyendo contextos causales y outcomes A/B existentes...'):
                buckets=session['buckets']
                swings={}
                for tf in (1,5):
                    candles=observed_volume_candles(buckets,tf)
                    swings[tf]=detect_causal_swings(candles,explore_study['parameters']['swing_bars'])
                context=build_pre_impulse_context(buckets,explore_study['aggression'],swings)
                entries=evaluate_exhaustion_entries(buckets,explore_study['aggression'],explore_study['swings'])
                outcome=evaluate_feature_outcome_horizons(buckets,explore_study['aggression'],
                    explore_study['features'],entries,
                    feature_window_seconds=explore_study['parameters']['feature_window_seconds'])
                result=evaluate_context_cohorts(context,outcome)
            st.session_state['mfv3_context_study']=(identity,context,outcome,result)
        except (ValueError,KeyError,TypeError,RuntimeError,OSError) as exc:
            st.error(f'No se pudo estudiar el contexto: {exc}')
            return
    saved=st.session_state.get('mfv3_context_study')
    if not saved or saved[0]!=identity:
        st.info('Presioná el botón para calcular este estudio sobre todos los impulsos de la sesión.')
        return
    _,context,outcomes,study=saved
    cnt=context['summary']['by_group']
    a,b,c=st.columns(3)
    a.metric('Impulsos retenidos',context['summary']['impulses'])
    b.metric('A favor del movimiento previo',cnt['AGGRESSION_WITH_PRIOR_TREND'])
    c.metric('Contra movimiento previo',cnt['COUNTER_TREND_AGGRESSION'])
    lookup={c['event_id']:c for c in context['rows']}
    if selected_event['event_id'] in lookup:
        ev=lookup[selected_event['event_id']]
        with st.expander('Contexto del evento seleccionado · solo pasado',expanded=True):
            st.write('**Clasificación descriptiva:** '+LABELS.get(ev['context_group'],ev['context_group']))
            data=[]
            for w in ('5m','10m'):
                x=ev['windows'].get(w,{})
                data.append({'Ventana':w,'Estado':x.get('state'),
                    'Retorno previo %':x.get('return_pct'),
                    'Cambio CVD (ETH)':x.get('cvd_delta_qty'),
                    'Posición rango 0–1':x.get('range_position'),
                    'Cobertura':x.get('coverage_fraction'),
                    'Máximo gap observado (s)':x.get('max_observed_gap_seconds')})
            st.dataframe(pd.DataFrame(data),use_container_width=True,hide_index=True)
            st.caption('La ventana previa termina antes del inicio del impulso. '
                       'Ningún gap fue rellenado; cobertura previa no acredita salud del WebSocket.')
            with st.expander('Swings causales y procedencia',expanded=False):
                st.json({'swings':ev['known_swings'],
                         'pre_event_last_observed_second_ms':ev['pre_event_last_observed_second_ms'],
                         'pre_event_available_wall_ns':ev['pre_event_available_wall_ns'],
                         'pre_event_last_seq':ev['pre_event_last_seq']})
    st.markdown('**¿Se repite? · Comparación descriptiva por cohorte**')
    a,b,c,d=st.columns(4)
    with a:
        cohort=st.selectbox('Conjunto',list(COHORT_LABELS),index=1,
                            format_func=lambda x:COHORT_LABELS[x],key='mfv3_ctx_cohort')
    with b:
        side=st.selectbox('Presión',['BUY','SELL'],key='mfv3_ctx_side')
    with c:
        variant=st.selectbox('Entrada',['FAILURE','FAILURE_SWING_BREAK'],
               format_func=lambda x:'A · Fracaso' if x=='FAILURE' else 'B · Fracaso + swing',
               key='mfv3_ctx_variant')
    with d:
        h=st.selectbox('Horizonte',HORIZONS_SECONDS,index=3,
               format_func=lambda x:f'{x//60} min',key='mfv3_ctx_horizon')
    summary=pd.DataFrame([r for r in study['summary'] if r['cohort']==cohort and
        r['pressure_side']==side and r['variant']==variant and r['horizon_seconds']==h])
    summary['Contexto']=summary['context_group'].map(LABELS)
    st.dataframe(summary[['Contexto','impulses','trigger_episodes_180s',
        'entry_references','strict_observed','strict_coverage_pct',
        'mean_gross_pct','median_gross_pct','mean_net_12bps_illustrative_pct',
        'first_in_episode_strict_observed','first_in_episode_mean_gross_pct']].rename(columns={
            'impulses':'Impulsos','trigger_episodes_180s':'Episodios disparadores 180s',
            'entry_references':'Entradas proxy','strict_observed':'Outcomes completos',
            'strict_coverage_pct':'Cobertura estricta %','mean_gross_pct':'Bruto medio %',
            'median_gross_pct':'Mediana %','mean_net_12bps_illustrative_pct':'Neto 12bps ilustrativo %',
            'first_in_episode_strict_observed':'Primeros de episodio observados',
            'first_in_episode_mean_gross_pct':'Media primeros de episodio %'}),
        use_container_width=True,hide_index=True)
    st.caption('El estudio compara subgrupos exploratorios. Los episodios 180s '
       'reducen solapamiento de triggers, pero NO independizan outcomes más largos. '
       'Una media positiva no demuestra edge, y con pocos casos no permite inferencia fiable.')
    frame=_flat_cases(study,h)
    st.download_button('Exportar casos individuales de este horizonte (CSV)',
        frame.to_csv(index=False),file_name=f'mfv3_context_cases_{h}s.csv',
        mime='text/csv',key='mfv3_ctx_cases_export')
    st.download_button('Exportar TODAS las cohortes y horizontes (CSV)',
        pd.DataFrame(study['summary']).to_csv(index=False),
        file_name='mfv3_context_all_cohorts.csv',mime='text/csv',
        key='mfv3_ctx_summary_export')
    if st.button('Guardar corrida verificada (requiere RAW íntegro)',key='mfv3_ctx_save'):
        try:
            where=save_verified_context_run(root,session,context,outcomes,study)
            st.success(f'Corrida persistida sin sobrescrituras: {where}')
        except (ValueError,OSError,KeyError,TypeError) as exc:
            st.error(f'No se guardó: {exc}')
