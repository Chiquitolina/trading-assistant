"""Read-only Streamlit view of FAILED Sweep -> Reversal -> Retest V3 reports.

Embedded under Runner Discovery > All first retests. No live actions.
"""
from __future__ import annotations

import json
from pathlib import Path

from reaction_lab_failed_reversal_retest_v3 import (
    OUTPUT, SCHEMA, MODES, TARGETS, DEFAULT_DEPTHS,
    config_dir, build,
)
from dashboard.views.reaction_lab_failed_reclaim_audit_v1_view import _read_selected_path

MODE_LABELS={
  'IMMEDIATE_ORIGINAL': 'Control · OPEN tras FAILED',
  'V2_SWEEP_REBOUND': 'Control V2 · sweep → rebote confirmado',
  'V2_RECLAIM_2_CLOSES': 'Control V2 · sweep → reclaim 2 cierres',
  'REVERSAL_OPEN': 'V3 · sweep → giro 1m → OPEN posterior',
  'RETEST_25': 'V3 · giro → primer retroceso 25% (LIMIT)',
  'RETEST_50': 'V3 · giro → primer retroceso 50% (LIMIT)',
  'RETEST_75': 'V3 · giro → primer retroceso 75% (LIMIT)',
}


def to_float(x):
    try:
        a=float(x)
        return a if a==a and abs(a)!=float('inf') else None
    except (ValueError,TypeError,OverflowError):
        return None


def plot_reversal_retest(bars,event,outcome,visible=240):
    import pandas as pd
    import plotly.graph_objects as go
    seq=bars[:min(visible,len(bars))]
    if not seq:raise ValueError('No verified POST 1m candles')
    times=pd.to_datetime([b[0] for b in seq],unit='ms',utc=True)
    fig=go.Figure(go.Candlestick(x=times,open=[b[1] for b in seq],
        high=[b[2] for b in seq],low=[b[3] for b in seq],close=[b[4] for b in seq],
        increasing_line_color='#16856a',decreasing_line_color='#d85e54',name='POST 1m'))
    def hline(value,label,color,dash='dash'):
        x=to_float(value)
        if x is not None:fig.add_hline(y=x,line_color=color,line_dash=dash,
            annotation_text=label,annotation_position='top left')
    hline(event.get('swing_price'),'Nivel original','#70859b')
    hline(event.get('retest_close'),'FAILED close','#c5aa6f','dot')
    hline(outcome.get('sweep_price'),'Profundidad sweep','#a47cce','dot')
    if to_float(outcome.get('limit_price')) is not None:
        hline(outcome['limit_price'],'LIMIT post-giro','#d7aa52','dot')
    for name,index,color,symbol in (
        ('Sweep',outcome.get('sweep_bar_index'),'#a47cce','x'),
        ('Giro confirmado',outcome.get('reversal_bar_index'),'#d7aa52','diamond'),
        ('Entrada',outcome.get('entry_bar_index'),'#16856a','circle')):
        i=to_float(index)
        if i is None:continue
        i=int(i)
        if i<0 or i>=len(seq):continue
        if name=='Entrada':
            y=to_float(outcome.get('entry_price'))
        elif name=='Sweep':
            y=seq[i][3] if event['side']=='LONG' else seq[i][2]
        else:y=seq[i][4]
        fig.add_trace(go.Scatter(x=[times[i]],y=[y],mode='markers+text',
            marker={'size':12,'symbol':symbol,'color':color},text=[name],
            textposition='top center',name=name))
    if outcome.get('signal_state')=='SIGNAL':
        price=to_float(outcome.get('entry_price'))
        target=to_float(outcome.get('target_pct'))
        if price is not None and target is not None:
            tp=price*(1+target/100 if event['side']=='LONG' else 1-target/100)
            hline(tp,f'TP +{target:g}%','#16856a')
        hline(outcome.get('stop_price'),'SL real de esta configuración','#d85e54')
    fig.update_layout(height=530,margin=dict(l=12,r=12,t=34,b=15),
        xaxis_rangeslider_visible=False,hovermode='x unified',
        xaxis_title='Velas POST verificadas (UTC)',yaxis_title='Precio',
        legend=dict(orientation='h',y=-.20))
    return fig


def _filtered_rows(path,mode,depth,tp):
    import pandas as pd
    frames=[]
    for part in pd.read_csv(path,low_memory=False,chunksize=60000):
        filtered=part.loc[part['mode'].eq(mode) &
            pd.to_numeric(part['depth_pct'],errors='coerce').sub(depth).abs().lt(1e-6) &
            pd.to_numeric(part['target_pct'],errors='coerce').eq(tp)]
        if not filtered.empty:frames.append(filtered)
    return pd.concat(frames,ignore_index=True) if frames else pd.DataFrame()


def render_failed_reversal_retest(project):
    import pandas as pd
    import streamlit as st
    project=Path(project).resolve()
    st.markdown('#### ↩ FAILED · Sweep → Giro → Segundo Pullback V3')
    st.caption('Investigación causal, no señal LIVE. Giro: cierre 1m que rompe el extremo '
        'de la vela anterior tras un sweep; LIMIT fijada después del cierre y válida '
        'hasta 60m desde FAILED. Las entradas sin fill NO se convierten en perdedoras.')
    c1,c2,c3,c4=st.columns(4)
    depth=float(c1.selectbox('Sweep adverso',['1%','2%'],index=0,key='fv3_depth')[0])
    stop_type=c2.selectbox('Stop',['Porcentaje','Estructural'],key='fv3_stop_type')
    structural=(stop_type=='Estructural')
    if structural:
        buffer=float(c3.number_input('Margen sobre extremo (%)',min_value=.01,max_value=2.,
            value=.1,step=.05,key='fv3_stop_struct'))
        sl=1.
    else:
        preset=c3.selectbox('SL (%)',[1.,3.,4.,5.,'Otro'],index=0,
            format_func=lambda x: 'Personalizado' if x=='Otro' else f'−{x:g}%',key='fv3_sl')
        sl=float(c3.number_input('SL personalizado (%)',min_value=.1,max_value=10.,
            value=2.,step=.25,key='fv3_sl_custom')) if preset=='Otro' else float(preset)
        buffer=.1
    tp=int(c4.selectbox('TP (%)',list(TARGETS),key='fv3_tp'))
    mode=st.selectbox('Regla de entrada',MODES,index=4,
        format_func=lambda x:MODE_LABELS[x],key='fv3_mode')
    folder=config_dir(project,'structural' if structural else 'pct',sl,buffer)
    manifest_file=folder/'manifest.json';events_file=folder/'events.csv'
    summary_file=folder/'summary.csv';outcomes_file=folder/'event_outcomes.csv'
    manifest={}
    if manifest_file.is_file():
        try:manifest=json.loads(manifest_file.read_text(encoding='utf-8'))
        except (OSError,ValueError):st.warning('Manifest V3 ilegible')
    settings=manifest.get('settings',{})
    valid=(manifest.get('schema')==SCHEMA and
       settings.get('stop_mode')==('structural' if structural else 'pct') and
       depth in settings.get('depths_pct',[]) and
       int(settings.get('max_wait_min',-1))==60 and
       int(settings.get('hold_min',-1))==180 and
       float(settings.get('reversal_min_leg_pct',-1))==.25 and
       float(settings.get('reversal_break_buffer_pct',-1))==0. and
       (abs(float(settings.get('struct_buffer_pct') or 0)-buffer)<1e-7 if structural
        else abs(float(settings.get('sl_pct') or 0)-sl)<1e-7))
    info,run=st.columns([3,1.2],vertical_alignment='center')
    info.caption('Informe: '+(str(manifest.get('created_utc'))[:19]+' UTC' if manifest
       else 'No generado para este SL')+'. Esta selección no altera reportes V1/V2.')
    if run.button('↻ Recalcular V3',key='fv3_recalc',use_container_width=True):
        try:
            with st.spinner('Validando históricos y calculando todas las condiciones…'):
                build(project,stop_mode='structural' if structural else 'pct',
                    sl_pct=sl,struct_buffer_pct=buffer,depths=DEFAULT_DEPTHS)
            st.success('Informe V3 recalculado; fuentes intactas.')
            st.rerun()
        except Exception as exc:
            st.error(f'No se pudo recalcular V3: {type(exc).__name__}: {exc}')
            return
    if not all(x.is_file() for x in (manifest_file,events_file,summary_file,outcomes_file)):
        st.info('Primero generá V3 para esta configuración con «Recalcular V3» o el CLI.')
        return
    if not valid:
        st.warning('El informe guardado tiene parámetros distintos; no se reutilizan sus estadísticas.')
        return
    @st.cache_data(show_spinner=False,max_entries=4)
    def simple_csv(path,mtime):return pd.read_csv(path,low_memory=False)
    @st.cache_data(show_spinner=False,max_entries=6)
    def selected_csv(path,mtime,mode,depth,tp):return _filtered_rows(path,mode,depth,tp)
    try:
        summaries=simple_csv(str(summary_file),summary_file.stat().st_mtime_ns)
        events=simple_csv(str(events_file),events_file.stat().st_mtime_ns)
        selected=selected_csv(str(outcomes_file),outcomes_file.stat().st_mtime_ns,
            mode,depth,tp)
    except (OSError,ValueError,pd.errors.ParserError) as exc:
        st.error(f'Error leyendo V3: {exc}');return
    if selected.empty:
        st.warning('Sin filas de esa configuración.');return
    group=summaries.loc[summaries['group_type'].eq('ALL') &
        pd.to_numeric(summaries['depth_pct'],errors='coerce').sub(depth).abs().lt(1e-7) &
        pd.to_numeric(summaries['target_pct'],errors='coerce').eq(tp)]
    cols=['mode','eligible_common','signal_n','no_sweep_n','no_reversal_n',
          'no_fill_n','tp_first_n','sl_first_n','ambiguous_n',
          'net_mean_per_signal_pct','net_worst_mean_per_signal_pct',
          'net_best_mean_per_signal_pct','net_mean_per_eligible_pct']
    st.markdown('**Control vs giro y segundo retroceso · mismo universo**')
    st.dataframe(group.loc[:,cols],hide_index=True,use_container_width=True)
    c1,c2=st.columns(2)
    side=c1.selectbox('Dirección',['TODAS','LONG','SHORT'],key='fv3_side')
    source=c2.selectbox('Origen',['TODOS','LIVE_OBSERVED','RECOVERED_WINDOW'],key='fv3_origin')
    if side!='TODAS':selected=selected.loc[selected['side'].eq(side)]
    if source!='TODOS':selected=selected.loc[selected['observation_type'].eq(source)]
    eligible=selected.loc[selected['cohort_eligible'].astype(bool)]
    traded=eligible.loc[eligible['signal_state'].eq('SIGNAL')]
    counts=st.columns(5)
    for target,value,col in zip(['Evaluables','Entradas','Sin señal','TP seguro','Inciertas'],
        [len(eligible),len(traded),int(eligible['signal_state'].eq('NO_SIGNAL').sum()),
         int(traded['outcome'].isin(['TP_FIRST','TP_GAP']).sum()),
         int(traded['entry_bar_uncertain'].astype(bool).sum())],counts):
        col.metric(target,f'{value:,}')
    st.caption('Los LIMIT intravela se muestran con su peor y mejor resultado posibles. '
       'La media neta es exploratoria (costo 0,20 pp), sin órdenes LIVE ni cartera.')
    search=st.text_input('Buscar símbolo o touch_key',key='fv3_search').upper().strip()
    if search:selected=selected.loc[selected['symbol'].astype(str).str.upper().str.contains(search,regex=False)|
       selected['touch_key'].astype(str).str.upper().str.contains(search,regex=False)]
    state=st.selectbox('Estado',['TODOS','SIGNAL','NO_SIGNAL','CENSORED_COMMON_HORIZON','NOT_APPLICABLE'],
                       key='fv3_state')
    if state!='TODOS':selected=selected.loc[selected['signal_state'].eq(state)]
    show=['date_utc','symbol','side','mode','depth_pct','signal_state',
          'no_signal_reason','sweep_bar_index','reversal_bar_index','entry_bar_index',
          'entry_price','stop_distance_pct','outcome','net_pct','net_best_pct','touch_key']
    st.dataframe(selected.loc[:,show].head(500),hide_index=True,use_container_width=True)
    st.download_button('Exportar eventos V3 filtrados',selected.to_csv(index=False).encode('utf-8-sig'),
        file_name='failed_reversal_retest_v3.csv',mime='text/csv',key='fv3_export')
    if selected.empty:return
    st.markdown('**Velas POST verificadas: sweep, giro y entrada**')
    cases=selected.sort_values(['date_utc','symbol'],ascending=[False,True]).head(250)
    keys=cases['touch_key'].astype(str).tolist()
    labels={r['touch_key']:f"{r['date_utc']} · {r['symbol']} {r['side']} · "
       f"{r['signal_state']} · {r['outcome']}" for _,r in cases.iterrows()}
    key=st.selectbox('Episodio',keys,format_func=lambda x:labels.get(x,x),key='fv3_case')
    window=st.select_slider('Velas visibles',[60,120,180,240,360],value=240,key='fv3_window')
    try:
        case=cases.loc[cases['touch_key'].eq(key)].iloc[0]
        meta=events.loc[events['touch_key'].eq(key)]
        if len(meta)!=1:raise ValueError('No hay metadatos únicos')
        source_row,bars=_read_selected_path(project,key)
        chart=plot_reversal_retest(bars,meta.iloc[0],case,window)
        st.plotly_chart(chart,use_container_width=True,key='fv3_candles',
            config={'displaylogo':False})
        st.caption(f"{source_row['path_source']} · velas verificadas: {len(bars)} · "
            'Los marcadores se calculan sin usar velas futuras al generar la señal.')
    except Exception as exc:
        st.error(f'No se pudo abrir episodio V3: {type(exc).__name__}: {exc}')
