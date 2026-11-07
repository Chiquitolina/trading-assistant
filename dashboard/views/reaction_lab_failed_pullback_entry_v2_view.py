"""Embedded FAILED Pullback/Sweep V2 viewer under Runner Discovery ALL.

The offline data source is immutable until an explicit recompute of DERIVED
reports.  The 1m chart always renders for the chosen episode and uses verified
First Touch path records; source SQLite remains read-only.
"""
from __future__ import annotations

import json
from pathlib import Path

from reaction_lab_failed_pullback_entry_v2 import (
    OUTPUT, MODES, TARGETS, DEFAULT_DEPTHS, DEFAULT_SL_PCT, SUGGESTED_SL_PCTS,
    output_dir_for_sl, build,
)
from dashboard.views.reaction_lab_failed_reclaim_audit_v1_view import _read_selected_path

MODE_LABELS={
    'IMMEDIATE_ORIGINAL':'Control · OPEN inmediato en dirección original',
    'SWEEP_LIMIT':'Control · LIMIT en el sweep (sin confirmar giro)',
    'SWEEP_REBOUND':'Sweep → vela de rechazo/rebote → OPEN siguiente',
    'SWEEP_RECLAIM_LEVEL':'Sweep → recuperar nivel perdido → OPEN siguiente',
    'SWEEP_RECLAIM_2_CLOSES':'Sweep → dos cierres recuperando nivel → OPEN siguiente',
}


def _number(x):
    try:
        a=float(x)
        if a!=a:return None
        return a
    except (TypeError,ValueError):
        return None


def plot_sweep_episode(bars,event,outcome,visible):
    """Return Plotly figure; no Streamlit state or writes, testable offline."""
    import pandas as pd
    import plotly.graph_objects as go

    seq=bars[:min(visible,len(bars))]
    if not seq:raise ValueError('No verified POST bars')
    times=pd.to_datetime([b[0] for b in seq],unit='ms',utc=True)
    fig=go.Figure(go.Candlestick(x=times,open=[b[1] for b in seq],
            high=[b[2] for b in seq],low=[b[3] for b in seq],close=[b[4] for b in seq],
            increasing_line_color='#16856a',decreasing_line_color='#dc5b4d',
            name='POST · OHLC 1m'))
    level=_number(event.get('swing_price'))
    close=_number(event.get('retest_close'))
    sweep=_number(outcome.get('sweep_price'))
    if level is not None:
        fig.add_hline(y=level,line_dash='dash',line_color='#75859a',
                      annotation_text='Nivel original')
    if close is not None:
        fig.add_hline(y=close,line_dash='dot',line_color='#c6ab71',
                      annotation_text='Cierre del FAILED')
    if sweep is not None:
        fig.add_hline(y=sweep,line_dash='dot',line_color='#b77ed8',
                      annotation_text='Sweep adverso requerido')
    sweep_idx=_number(outcome.get('sweep_bar_index'))
    if sweep_idx is not None and int(sweep_idx)<len(seq):
        idx=int(sweep_idx)
        wick=seq[idx][3] if event['side']=='LONG' else seq[idx][2]
        fig.add_trace(go.Scatter(x=[times[idx]],y=[wick],mode='markers+text',
            marker=dict(symbol='x',size=12,color='#b77ed8'),name='Sweep alcanzado',
            text=['SWEEP'],textposition='bottom center'))
    if outcome.get('signal_state')=='SIGNAL':
        idx_num=_number(outcome.get('entry_bar_index'))
        price=_number(outcome.get('entry_price'))
        if idx_num is not None and price is not None:
            idx=int(idx_num)
            if idx<len(seq):
                fig.add_trace(go.Scatter(x=[times[idx]],y=[price],
                    mode='markers+text',marker=dict(symbol='diamond',size=13,color='#16856a'),
                    text=['Entrada (OPEN)' if outcome['entry_resolution']!='INTRABAR_LIMIT'
                         else 'LIMIT intravela'],textposition='top center',name='Entrada hipotética'))
            tp=_number(outcome.get('target_pct'))
            sl=_number(outcome.get('sl_pct'))
            if tp and sl:
                side=event['side']
                tp_price=price*(1+tp/100 if side=='LONG' else 1-tp/100)
                sl_price=price*(1-sl/100 if side=='LONG' else 1+sl/100)
                fig.add_hline(y=tp_price,line_dash='dash',line_color='#16856a',
                              annotation_text=f'TP +{tp:g}%')
                fig.add_hline(y=sl_price,line_dash='dash',line_color='#cd544b',
                              annotation_text=f'SL −{sl:g}%')
    fig.update_layout(height=520,margin=dict(l=15,r=15,t=34,b=20),
                      xaxis_rangeslider_visible=False,hovermode='x unified',
                      xaxis_title='POST desde la apertura posterior al FAILED (UTC)',
                      yaxis_title='Precio',legend=dict(orientation='h',y=-.2))
    return fig


def load_filtered(path,mode,depth,tp):
    """Only materialize selected experiment, not the entire 400k+ row CSV."""
    import pandas as pd
    frames=[]
    for chunk in pd.read_csv(path,low_memory=False,chunksize=50000):
        keep=(chunk['mode'].eq(mode)&chunk['target_pct'].eq(tp)&
              (pd.to_numeric(chunk['depth_pct'],errors='coerce').sub(depth).abs()<1e-7))
        if keep.any():frames.append(chunk.loc[keep].copy())
    return pd.concat(frames,ignore_index=True) if frames else pd.DataFrame()


def selectable_sl_values():
    """Explicit UI presets, while preserving arbitrary stop sizes via custom input."""
    return (*SUGGESTED_SL_PCTS, 'OTRO')


def render_failed_pullback_entry(project):
    import pandas as pd
    import streamlit as st

    project=Path(project).resolve()
    st.markdown('#### 🌊 FAILED · Sweep & Pullback Entry V2')
    st.caption('El sweep adverso ocurre DESPUÉS del FAILED. La entrada recupera la dirección '
               'ORIGINAL del swing; el SL y la profundidad son controles INDEPENDIENTES.')
    c1,c2,c3,c4=st.columns([1.25,1.2,1.15,1.35])
    desired_depth=float(c1.number_input('Profundidad SWEEP (%)',min_value=0.25,max_value=10.,
        value=1.,step=0.25,format='%.2f',key='fsweep_depth'))
    sl_choice=c2.selectbox('Stop Loss (%)',selectable_sl_values(),index=0,
        format_func=lambda x: 'Otro valor…' if x=='OTRO' else f'−{x:g}%',key='fsweep_sl_preset')
    if sl_choice=='OTRO':
        desired_sl=float(c2.number_input('SL personalizado (%)',min_value=0.25,max_value=10.,
            value=2.,step=0.25,format='%.2f',key='fsweep_sl_custom'))
    else:
        desired_sl=float(sl_choice)
    tp=int(c3.selectbox('Take Profit (%)',list(TARGETS),key='fsweep_tp'))
    mode=c4.selectbox('Regla',MODES,index=2,format_func=lambda k:MODE_LABELS[k],
                      key='fsweep_mode')
    # Reports with a different SL never overwrite this one's results.
    root=output_dir_for_sl(project,desired_sl)
    manifest_path=root/'manifest.json'; events_path=root/'events.csv'
    summary_path=root/'summary.csv'; outcomes_path=root/'event_outcomes.csv'
    manifest={}
    if manifest_path.is_file():
        try:manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        except (ValueError,OSError):st.warning('Manifiesto ilegible; regenerar informe')
    settings=manifest.get('settings',{})
    available=list(settings.get('depths_pct',DEFAULT_DEPTHS))
    if not available:available=list(DEFAULT_DEPTHS)
    saved_sl=float(settings.get('sl_pct',DEFAULT_SL_PCT))
    depth_available=any(abs(float(x)-desired_depth)<1e-7 for x in available)
    differs_sl=abs(saved_sl-desired_sl)>1e-7
    if (not depth_available or differs_sl) and manifest:
        st.info('La configuración seleccionada difiere del informe guardado. '
                'Usá «Calcular esta configuración» para regenerar únicamente reportes derivados.')
    left,right=st.columns([3,1.6],vertical_alignment='center')
    if manifest:
        left.caption(f"Informe {str(manifest.get('created_utc',''))[:16]} UTC · "
                     f"cohorte {manifest.get('cohort_total','?')} · "
                     f"elegibles {manifest.get('coverage_common','?')} · "
                     f"SL guardado −{saved_sl:g}% · profundidades: "
                     +', '.join(f'{x:g}%' for x in available))
        if manifest.get('scope')!='FULL_VPS_VERIFIED_ONLINE_AND_HISTORY':
            st.warning('Informe PILOTO: todavía no se evaluó todo el universo del VPS.')
    else:
        left.caption(f'No hay informe para SL −{desired_sl:g}%. Se guarda separado del SL −1% y de los otros SL.')
    if right.button('↻ Calcular esta configuración',key='fsweep_recalc',use_container_width=True):
        try:
            with st.spinner('Evaluando todas las trayectorias verificadas del FAILED...'):
                # Include the desired depth plus default 0.5–2.5% grid to compare
                depths=tuple(sorted(set([round(desired_depth,6),*DEFAULT_DEPTHS])))
                build(project,sl_pct=desired_sl,depths=depths,out=root)
            st.success('Informes V2 actualizados. SQLite y colectores intactos.')
            manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
            settings=manifest.get('settings',{});saved_sl=float(settings.get('sl_pct',1.))
            available=list(settings.get('depths_pct',[]))
            differs_sl=False;depth_available=True
        except Exception as e:
            st.error(f'Error al calcular: {type(e).__name__}: {e}')
            return
    if not all(p.is_file() for p in (manifest_path,events_path,summary_path,outcomes_path)):
        st.info(f'No hay reporte para SL −{desired_sl:g}%. Presioná «Calcular esta configuración» '
            'o ejecutá en VPS: `python3 reaction_lab_failed_pullback_entry_v2.py '
            f'--project /home/multiservices-1m --sl-pct {desired_sl:g} --run`.')
        return
    if differs_sl or not depth_available:
        st.warning('No se muestran resultados de una configuración distinta: primero recalculá el informe.')
        return
    @st.cache_data(show_spinner=False,max_entries=4)
    def csv_simple(path,mtime):return pd.read_csv(path,low_memory=False)
    @st.cache_data(show_spinner=False,max_entries=5)
    def filtered(path,mtime,mode,depth,tp):return load_filtered(path,mode,depth,tp)
    try:
        summary=csv_simple(str(summary_path),summary_path.stat().st_mtime_ns)
        events=csv_simple(str(events_path),events_path.stat().st_mtime_ns)
        selected=filtered(str(outcomes_path),outcomes_path.stat().st_mtime_ns,
                          mode,float(desired_depth),tp)
    except (OSError,ValueError,pd.errors.ParserError) as e:
        st.error(f'Error leyendo informe: {e}');return
    if selected.empty:
        st.warning('No hay resultados para la profundidad y TP elegidos.');return
    s1,s2=st.columns(2)
    side=s1.selectbox('Dirección original',['TODAS','LONG','SHORT'],key='fsweep_side')
    observed=s2.selectbox('Origen',['TODOS','LIVE_OBSERVED','RECOVERED_WINDOW'],key='fsweep_origin')
    if side!='TODAS':selected=selected.loc[selected['side'].eq(side)]
    if observed!='TODOS':selected=selected.loc[selected['observation_type'].eq(observed)]
    eligible=selected.loc[selected['cohort_eligible'].astype(bool)]
    traded=eligible.loc[eligible['signal_state'].eq('SIGNAL')]
    safe_tp=int(traded['outcome'].isin(['TP_FIRST','TP_GAP']).sum())
    ambiguous=int(traded['outcome'].isin(['SL_AMBIGUOUS','FILL_BAR_TP_UNORDERED']).sum())
    no_signal=int(eligible['signal_state'].eq('NO_SIGNAL').sum())
    metrics=st.columns(5)
    for col,name,val in zip(metrics,['Evaluables','Entradas','Sin señal','TP primero seguro','Ambiguos'],
                            [len(eligible),len(traded),no_signal,safe_tp,ambiguous]):
        col.metric(name,f'{val:,}')
    st.caption('TP y SL medidos DESDE CADA ENTRADA, con SL −'+f'{desired_sl:g}%. '
        'La media neta conservadora resta 0,20 puntos porcentuales y trata las '
        'ambigüedades como SL. Sin deslizamiento, comisiones calibradas ni cartera.')
    all_modes=summary.loc[(summary['group_type'].eq('ALL')) &
        (summary['target_pct'].eq(tp)) &
        (pd.to_numeric(summary['depth_pct'],errors='coerce').sub(desired_depth).abs()<1e-7)]
    cols=['mode','eligible_common','signal_n','no_signal_n','tp_first_n','sl_first_n',
          'ambiguous_n','time_exit_n','tp_rate_signalled_pct','net_mean_per_signal_pct',
          'net_best_mean_per_signal_pct','net_mean_per_eligible_pct']
    st.markdown('**Comparación: misma profundidad de sweep y mismo universo**')
    st.dataframe(all_modes.loc[:,cols],use_container_width=True,hide_index=True)
    search=st.text_input('Buscar símbolo o touch_key',key='fsweep_search').strip().upper()
    if search:
        selected=selected.loc[selected['symbol'].astype(str).str.upper().str.contains(search,regex=False)|
            selected['touch_key'].astype(str).str.upper().str.contains(search,regex=False)]
    state=st.selectbox('Estado',['TODOS','SIGNAL','NO_SIGNAL','CENSORED_COMMON_HORIZON'],
                       key='fsweep_state')
    if state!='TODOS':selected=selected.loc[selected['signal_state'].eq(state)]
    fields=['date_utc','symbol','side','mode','depth_pct','signal_state','sweep_bar_index',
            'entry_bar_index','entry_price','entry_resolution','outcome','net_pct','touch_key']
    st.dataframe(selected.loc[:,fields].head(600),use_container_width=True,hide_index=True)
    st.download_button('Descargar selección',selected.to_csv(index=False).encode('utf-8-sig'),
                       file_name='failed_sweep_v2_filtrado.csv',mime='text/csv',key='fsweep_export')
    if selected.empty:return
    st.markdown('**Gráfico del sweep y la entrada (se muestra al seleccionar un episodio)**')
    filtered_cases=selected.sort_values(['date_utc','symbol'],ascending=[False,True]).head(250)
    keys=filtered_cases['touch_key'].astype(str).tolist()
    descriptions={r['touch_key']:f"{r['date_utc']} · {r['symbol']} {r['side']} · "
                  f"{r['signal_state']} · {r['outcome']}" for _,r in filtered_cases.iterrows()}
    key=st.selectbox('Episodio (primeros 250 del filtro)',keys,
                     format_func=lambda k:descriptions.get(k,k),key='fsweep_case')
    width=st.select_slider('Velas POST visibles',[60,120,180,240,360],value=240,key='fsweep_window')
    try:
        row=filtered_cases.loc[filtered_cases['touch_key'].eq(key)].iloc[0]
        meta=events.loc[events['touch_key'].eq(key)]
        if len(meta)!=1:raise ValueError('Metadatos duplicados o faltantes')
        src,bars=_read_selected_path(project,key)
        fig=plot_sweep_episode(bars,meta.iloc[0],row,width)
        st.plotly_chart(fig,use_container_width=True,key='fsweep_visible_chart',
                        config={'displaylogo':False})
        st.caption(f"{src['path_source']} · Nivel {meta.iloc[0]['swing_price']} · "
            f"FAILED close {meta.iloc[0]['retest_close']} · "
            'Sweep medido desde el cierre FAILED; no hay información futura en la entrada.')
    except Exception as e:
        st.error(f'No se pudo graficar el episodio: {type(e).__name__}: {e}')
