"""Embedded FAILED Reclaim Audit V1 Streamlit view, inside Runner Discovery ALL.

Read-only by default; opt-in derived rebuild. Does not modify original SQLite.
"""
from __future__ import annotations

from pathlib import Path
import json

from reaction_lab_failed_reclaim_audit_v1 import (
    OUTPUT, build, read_journal, MODES, TARGETS,
)
from reaction_lab_runner_discovery_all_touches_v1 import (
    iter_events, recovery_index,
)

LABELS={
    'IMMEDIATE_ORIGINAL':'OPEN tras fallo · dirección original',
    'IMMEDIATE_INVERSE':'OPEN tras fallo · dirección inversa',
    'BAND_REENTRY':'Reingreso a banda ±0,10% (cierre)',
    'ORIGINAL_RECLAIM':'Reclaim +0,05% (un cierre)',
    'ORIGINAL_RECLAIM_2_CLOSES':'Reclaim +0,05% (dos cierres)',
    'ORIGINAL_BREAK_FAIL_EXTREME':'Ruptura del extremo del fallo +0,05%',
    'INVERSE_2_CLOSES':'Dos cierres fuera · dirección inversa',
    'INVERSE_BREAK_FAIL_EXTREME':'Ruptura adversa · dirección inversa',
    'INVERSE_RETEST_REJECTION':'Retest rechazado · dirección inversa',
}


def _read_selected_path(project,key):
    # The exact same SHA256/identity/prefix-verifying reader as Runner Discovery.
    root=Path(project)/'reports/reaction_lab/first_touch_historical_recovery_v11'
    hist,_=recovery_index(root)
    it=iter_events(project,recovery=hist,include_bars=True,
                   only_status='TOUCH_FAILED',only_touch_key=key)
    items=list(it)
    if len(items)!=1:
        raise ValueError('Episode not uniquely found in First Touch V2')
    row,bars=items[0]
    if row['path_quality']!='CONTIGUOUS_VALID':
        raise ValueError('Selected episode does not have a validated path')
    return row,bars


def _plot_episode(bars,event,outcome,window):
    import plotly.graph_objects as go
    import pandas as pd
    valid=bars[:min(window,len(bars))]
    ts=pd.to_datetime([x[0] for x in valid],unit='ms',utc=True)
    fig=go.Figure()
    fig.add_trace(go.Candlestick(x=ts,open=[b[1] for b in valid],high=[b[2] for b in valid],
        low=[b[3] for b in valid],close=[b[4] for b in valid],name='POST 1m'))
    level=float(event['swing_price'])
    fig.add_hline(y=level,line_color='#64748b',line_dash='dash',annotation_text='Swing original')
    for mult,label in ((.999,'Banda −0,10%'),(1.001,'Banda +0,10%')):
        fig.add_hline(y=level*mult,line_color='#a49b84',line_width=1,line_dash='dot',
                      annotation_text=label,annotation_position='bottom right')
    if outcome.get('signal_state')=='SIGNAL':
        entry_ix=int(outcome['entry_min_after_fail'])
        if entry_ix<len(valid):
            fig.add_trace(go.Scatter(x=[ts[entry_ix]],y=[float(outcome['entry_open'])],
                mode='markers+text',text=['OPEN entrada'],textposition='top center',
                marker={'symbol':'diamond','size':12,'color':'#16856a'},name='Entrada causal'))
    fig.update_layout(height=480,margin=dict(l=20,r=20,t=40,b=30),
        xaxis_title='Velas POST de 1m, desde el OPEN siguiente al retest',
        yaxis_title='Precio',xaxis_rangeslider_visible=False,
        legend=dict(orientation='h'))
    return fig


def render_failed_reclaim_audit(project):
    import pandas as pd
    import streamlit as st

    project=Path(project).resolve();base=project/OUTPUT
    manifest_path=base/'manifest.json'; events_path=base/'events.csv'
    outcomes_path=base/'event_outcomes.csv'; summary_path=base/'summary.csv'
    st.markdown('#### 🔁 FAILED Reclaim Audit V1')
    st.caption('Auditoría offline del primer fallo de nivel: decisiones solo con velas cerradas; '
               'entrada en el OPEN posterior. Denominador común 60m de búsqueda + 180m de resultado. '
               'Esto NO equivale a una señal operativa LIVE.')
    info,act=st.columns([4,1.5],vertical_alignment='center')
    if manifest_path.is_file():
        try:
            m=json.loads(manifest_path.read_text(encoding='utf-8'))
            info.caption(f"Informe: {str(m.get('created_utc',''))[:16].replace('T',' ')} UTC · "
                         f"cohorte: {m.get('cohort_total','?')} · "
                         f"cobertura común: {m.get('coverage_common','?')}")
            if m.get('scope')!='FULL_VPS_VERIFIED_ONLINE_AND_HISTORY':
                st.warning('Este informe es PILOTO; no contiene toda la cohorte histórica.')
        except (ValueError,OSError):
            info.caption('Informe disponible, metadatos ilegibles')
    else:
        info.caption('Todavía no se generó la auditoría causal.')
    if act.button('↻ Recalcular FAILED',key='failed_reclaim_refresh',use_container_width=True):
        try:
            with st.spinner('Analizando todas las trayectorias verificadas de TOUCH_FAILED...'):
                build(project)
            st.success('Informe offline actualizado; SQLite y PM2 sin modificaciones.')
        except Exception as exc:
            st.error(f'Fallo al recalcular: {type(exc).__name__}: {exc}')
            return
    if not all(p.is_file() for p in (events_path,outcomes_path,summary_path)):
        st.info('Generá el informe offline con: '
                '`python3 -u reaction_lab_failed_reclaim_audit_v1.py --project /home/multiservices-1m --run`')
        return

    @st.cache_data(show_spinner=False,max_entries=4)
    def _load_csv(path,mtime):
        return pd.read_csv(path,low_memory=False)
    try:
        events=_load_csv(str(events_path),events_path.stat().st_mtime_ns)
        outcomes=_load_csv(str(outcomes_path),outcomes_path.stat().st_mtime_ns)
        summaries=_load_csv(str(summary_path),summary_path.stat().st_mtime_ns)
    except (OSError,ValueError,pd.errors.ParserError) as exc:
        st.error(f'Error leyendo reportes: {exc}')
        return
    if events.empty:
        st.warning('Cohorte vacía.');return
    a,b,c,d=st.columns([2,1,1,1.1])
    mode=a.selectbox('Condición de entrada',MODES,index=4,format_func=lambda x:LABELS.get(x,x),key='fr_mode')
    target=int(b.selectbox('TP',['2','3','5'],key='fr_tp'))
    side=c.selectbox('Dirección',['Todas','LONG','SHORT'],key='fr_side')
    observation=d.selectbox('Origen',['Todos','LIVE_OBSERVED','RECOVERED_WINDOW'],key='fr_observation')
    selected=outcomes.loc[outcomes['mode'].eq(mode)&outcomes['target_pct'].eq(target)].copy()
    if side!='Todas':selected=selected.loc[selected['side'].eq(side)]
    if observation!='Todos':selected=selected.loc[selected['observation_type'].eq(observation)]
    n=int(selected['cohort_eligible'].fillna(False).astype(bool).sum())
    traded=selected.loc[selected['signal_state'].eq('SIGNAL')]
    tp=int(traded['outcome'].isin(['TP_FIRST','TP_GAP']).sum())
    sl=int(traded['outcome'].isin(['SL_FIRST','SL_GAP','SL_AMBIGUOUS']).sum())
    nosig=int(selected['signal_state'].eq('NO_SIGNAL').sum())
    m1,m2,m3,m4,m5=st.columns(5)
    m1.metric('Cohorte filtrada',f'{len(selected):,}')
    m2.metric('Cobertura común',f'{n:,}')
    m3.metric('Señales',f'{len(traded):,}')
    m4.metric('TP primero',f'{tp:,}',f'{tp/n*100:.1f}% del elegible' if n else '—')
    m5.metric('Sin señal',f'{nosig:,}')
    st.caption(f'SL primero, gaps y ambigüedades conservadoras: {sl:,}. '
               'TP/SL +2/+3/+5 vs −1%, desde el OPEN de cada condición. '
               'MFE/MAE del horizonte completo incluso después de una salida simulada; '
               'simulación idealizada sin latencia, slippage ni funding.')
    main=summaries.loc[(summaries['group_type']=='ALL') & summaries['target_pct'].eq(target)]
    cols=['mode','eligible_common','signal_n','no_signal_n','tp_first_n','sl_first_n',
          'ambiguous_n','time_exit_n','tp_rate_eligible_pct','net_mean_per_eligible_pct']
    st.markdown('**Comparación de todas las condiciones (universo completo)**')
    st.dataframe(main.loc[:,cols],use_container_width=True,hide_index=True)
    st.caption('El comparativo general utiliza siempre la cohorte completa. '
               'Los filtros superiores afectan solamente los casos y métricas seleccionadas.')
    search=st.text_input('Filtrar símbolo / touch key',placeholder='Ej. WAXP o AINUSDT',key='fr_search').strip().upper()
    if search:
        selected=selected.loc[selected['symbol'].astype(str).str.upper().str.contains(search,regex=False) |
                              selected['touch_key'].astype(str).str.upper().str.contains(search,regex=False)]
    visible=['date_utc','symbol','side','mode','signal_state','entry_min_after_fail',
             'entry_open','outcome','exit_min_after_entry','net_pct','touch_key']
    st.dataframe(selected.loc[:,visible].head(600),use_container_width=True,hide_index=True)
    st.caption(f'Mostrando hasta 600 de {len(selected):,} casos. NO_SIGNAL y CENSORED se conservan en la exportación.')
    st.download_button('Descargar episodios filtrados',data=selected.to_csv(index=False).encode('utf-8-sig'),
                       file_name='failed_reclaim_audit_filtered.csv',mime='text/csv',key='fr_download')
    if selected.empty:return
    st.markdown('**Inspeccionar episodio POST con nivel original**')
    cases=selected.sort_values(['date_utc','symbol'],ascending=[False,True]).head(250)
    ids=cases['touch_key'].tolist()
    display={r['touch_key']:f"{r['date_utc']} · {r['symbol']} {r['side']} · {r['signal_state']} · {r['outcome']}" for _,r in cases.iterrows()}
    chosen=st.selectbox('Primeros 250 episodios del filtro',ids,format_func=lambda k:display.get(k,k),key='fr_episode')
    width=st.select_slider('Velas POST visibles',options=[60,120,180,240,360],value=180,key='fr_bars')
    if st.button('Ver recorrido 1m verificado',key='fr_inspect'):
        try:
            ev=events.loc[events['touch_key'].eq(chosen)]
            if len(ev)!=1:raise ValueError('Missing unique event metadata')
            detail=selected.loc[selected['touch_key'].eq(chosen)]
            if len(detail)!=1:raise ValueError('Missing unique outcome')
            source,bars=_read_selected_path(project,chosen)
            st.plotly_chart(_plot_episode(bars,ev.iloc[0],detail.iloc[0],width),use_container_width=True)
            st.caption(f"Nivel: {ev.iloc[0]['swing_price']} · "
                       f"profundidad cierre fallo: {ev.iloc[0]['fail_depth_close_pct']}% · "
                       f"fuente: {source['path_source']} · "
                       'El gráfico comienza en la primera vela POST; la clasificación pertenece al retest anterior.')
        except Exception as exc:
            st.error(f'No se pudo cargar el episodio: {type(exc).__name__}: {exc}')
