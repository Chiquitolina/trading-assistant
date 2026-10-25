"""Streamlit research display for FAILED Geometry Lab V3 (CSV + read-only 1m)."""
from pathlib import Path


def _metric_text(v, suffix='%'):
    import pandas as pd
    return f'{float(v):.2f}{suffix}' if pd.notna(v) else '—'


def render_failed_geometry_panel(project, *, symbol=None, side=None, origin=None):
    import pandas as pd
    import streamlit as st
    import plotly.graph_objects as go
    import sqlite3
    from contextlib import closing

    root=Path(project)
    out=root/'reports/reaction_lab/failed_geometry_v3'
    st.markdown('### 📐 FAILED Geometry Lab V3 · cronología desde el retroceso confirmado')
    st.caption('SIN PnL. Primera señal por CIERRE, pico congelado al retroceso ≥50%, '
               'ventanas futuras desde la vela SIGUIENTE. MFE/MAE son extremos 1m y no prueban orden intrabar.')
    event_file=out/'geometry_events.csv'
    window_file=out/'post_pullback_windows.csv'
    pairs_file=out/'paired_summary.csv'
    if not event_file.is_file() or not window_file.is_file():
        st.info('Falta generar V3: `python3 reaction_lab_failed_geometry_v3.py --run`')
        return
    try:
        events=pd.read_csv(event_file,low_memory=False)
        windows=pd.read_csv(window_file,low_memory=False)
    except (OSError, ValueError, pd.errors.EmptyDataError) as exc:
        st.warning(f'No se pueden leer los resultados V3: {exc}')
        return
    required={'touch_key','symbol','cohort_type','original_side','impulse_direction','threshold_pct',
              'pullback_min','room_to_frozen_peak_pct','room_to_m5_baseline_pct'}
    needed={'touch_key','cohort_type','original_side','impulse_direction','threshold_pct',
            'post_pb_horizon','full_coverage','post_pb_mfe_pct','post_pb_mae_pct',
            'close_first_0p5','frozen_peak_vs_baseline_race'}
    if not required.issubset(events) or not needed.issubset(windows):
        st.warning('Esquema V3 desactualizado, regenerá los informes.');return
    ev=events[events.cohort_type.eq('FAILED')].copy()
    if symbol: ev=ev[ev.symbol.eq(symbol)]
    if side: ev=ev[ev.original_side.eq(side)]
    if origin: ev=ev[ev.observation_type.eq(origin)]
    if ev.empty:
        st.info('Sin FAILED para los filtros elegidos.');return
    left,right,third,fourth=st.columns([1,1,2,1])
    with left:
        th=st.selectbox('Impulso confirmado por cierre',options=[0.5,1.0],format_func=lambda x:f'±{x:.2f}%',key='fgv3_th')
    with right:
        h=st.selectbox('Minutos desde retroceso',options=[5,10,15,30],key='fgv3_h')
    with third:
        focus=st.selectbox('Subgrupo',options=['Todos','FAILED LONG → BREAKOUT',
              'FAILED SHORT → REVERSAL','Otras secuencias'],key='fgv3_focus')
    with fourth:
        race_th=st.selectbox('Primera excursión',options=[0.25,0.5,1.0],key='fgv3_race',
                format_func=lambda x:f'±{x:.2f}%')
    token={0.25:'0p25',0.5:'0p5',1.0:'1p0'}[race_th]
    first_col=f'close_first_{token}'
    delay_col=f'first_{token}_delay_min'
    ev=ev[ev.threshold_pct.eq(th)]
    if focus=='FAILED LONG → BREAKOUT':ev=ev[ev.original_side.eq('LONG')&ev.impulse_direction.eq('BREAKOUT')]
    elif focus=='FAILED SHORT → REVERSAL':ev=ev[ev.original_side.eq('SHORT')&ev.impulse_direction.eq('REVERSAL')]
    elif focus=='Otras secuencias':ev=ev[~((ev.original_side.eq('LONG')&ev.impulse_direction.eq('BREAKOUT'))|
                         (ev.original_side.eq('SHORT')&ev.impulse_direction.eq('REVERSAL')))]
    w=windows[(windows.cohort_type=='FAILED')&(windows.threshold_pct==th)&
              (windows.post_pb_horizon==h)&windows.touch_key.isin(ev.touch_key)].copy()
    full=w[w.full_coverage.eq(True)]
    risk=full[full.risk_set_future_competition.eq(True)]
    a,b,c,d=st.columns(4)
    a.metric('FAILED en subgrupo',ev.touch_key.nunique())
    b.metric('Retroceso confirmado',int(ev.pullback_min.notna().sum()))
    c.metric(f'Cobertura post-PB {h}m',f'{len(full)}/{len(w)}')
    d.metric('Aún en riesgo al confirmar',len(risk))
    st.caption('Cobertura calculada DESDE el retroceso confirmado. Los eventos sin PB no '
               'entran al denominador de esta etapa. Si ya cruzó la base en ese cierre, '
               'no entra en la carrera futura contra el pico.')

    if full.empty:
        st.info('No hay ventanas completas para este recorte.');return
    st.markdown('#### Geometría disponible al confirmar el retroceso')
    geom_cols=['symbol','original_side','impulse_direction','impulse_min','peak_min','pullback_min',
        'minutes_impulse_to_pullback','minutes_peak_to_pullback','impulse_peak_pct',
        'pb_retrace_fraction_pct','room_to_frozen_peak_pct','room_to_m5_baseline_pct',
        'pb_distance_to_original_swing_pct','pb_already_crossed_m5','flow_asof_available','touch_key']
    candidate=ev[ev.pullback_min.notna()].copy()
    st.dataframe(candidate[[x for x in geom_cols if x in candidate]].sort_values('pullback_min').head(500).round(4),
                 hide_index=True,use_container_width=True)

    st.markdown('#### Qué ocurre DESPUÉS del retroceso confirmado')
    summary=(full.groupby(['original_side','impulse_direction'],dropna=False).agg(
       N=('touch_key','nunique'),
       peak_room_med_pct=('distance_to_peak_at_pb_pct','median'),
       baseline_room_med_pct=('distance_to_baseline_at_pb_pct','median'),
       mfe_med_pct=('post_pb_mfe_pct','median'),mae_med_pct=('post_pb_mae_pct','median'),
       close_end_med_pct=('post_pb_close_change_pct','median'),
       first_continue_pct=(first_col,lambda x:100*(x=='CONTINUE').mean()),
       first_reverse_pct=(first_col,lambda x:100*(x=='REVERSE').mean()),
       no_touch_pct=(first_col,lambda x:100*(x=='NEITHER').mean()),
       delay_med_min=(delay_col,'median')).reset_index())
    st.dataframe(summary.round(3),hide_index=True,use_container_width=True)
    st.caption(f'Primera carrera simétrica ±{race_th:.2f}% por CIERRE desde precio del retroceso confirmado. '
               'La mediana del MFE y la del MAE son distribuciones diferentes; no implican capturabilidad.')
    if not risk.empty:
        race=(risk.groupby(['original_side','impulse_direction'],dropna=False)
           .agg(N=('touch_key','nunique'),
             segundo_pct=('frozen_peak_vs_baseline_race',lambda x:100*(x=='SECOND_BREAKOUT').mean()),
             base_pct=('frozen_peak_vs_baseline_race',lambda x:100*(x=='BASELINE_CROSSED').mean()),
             ninguno_pct=('frozen_peak_vs_baseline_race',lambda x:100*(x=='NEITHER').mean()),
             demora_med_min=('frozen_peak_race_delay_min','median')).reset_index())
        st.markdown('**Pico congelado vs regreso al precio del minuto 5**')
        st.dataframe(race.round(2),hide_index=True,use_container_width=True)
    st.markdown('#### Controles estrictamente emparejados')
    if pairs_file.is_file():
        pairs=pd.read_csv(pairs_file)
        pairs=pairs[pairs.threshold_pct==th]
        if side:pairs=pairs[pairs.original_side==side]
        st.dataframe(pairs.round(3),hide_index=True,use_container_width=True)
        st.caption('Solo pares con cobertura en AMBOS lados para comparar el primer impulso. '
                   'Para comparar geometría post-PB se requiere retroceso y cobertura en ambos: '
                   'selección condicionada, NO inferencia causal ni prueba fuera de muestra.')
    st.markdown('#### Contexto 4h observado ANTES del FAILED')
    flow_ev=ev[ev.flow_asof_available.eq(True)]
    st.metric('FAILED con Return/Volume Rank causal disponible',f'{flow_ev.touch_key.nunique()}/{ev.touch_key.nunique()}')
    if not flow_ev.empty:
        fcols=['symbol','original_side','impulse_direction','flow_group','return_rank_4h_pct',
               'volume_rank_4h_pct','relative_volume_4h','pullback_min','room_to_frozen_peak_pct']
        st.dataframe(flow_ev[[x for x in fcols if x in flow_ev]].round(3).head(300),
                     hide_index=True,use_container_width=True)
        flows=root/'reports/reaction_lab/failed_geometry_v3/flow_geometry_asof.csv'
        if flows.is_file():
            fs=pd.read_csv(flows)
            if {'threshold_pct','post_pb_horizon'}.issubset(fs):
                fs=fs[(fs.threshold_pct==th)&(fs.post_pb_horizon==h)]
                if not fs.empty:st.dataframe(fs.round(3),hide_index=True,use_container_width=True)
    else:
        st.caption('Sin snapshots causales para el recorte: no se usan rankings del presente para explicar FAILED anteriores.')
    st.markdown('#### Eventos individuales · desde la confirmación observable')
    cols=['symbol','original_side','impulse_direction','pullback_min','distance_to_peak_at_pb_pct',
          'distance_to_baseline_at_pb_pct','post_pb_mfe_pct','post_pb_mae_pct',first_col,
          delay_col,'frozen_peak_vs_baseline_race','frozen_peak_race_delay_min','touch_key']
    st.dataframe(full[[x for x in cols if x in full]].head(500).round(4),hide_index=True,use_container_width=True)
    st.download_button('Descargar ventanas post-PB filtradas',w.to_csv(index=False).encode('utf-8-sig'),
                       file_name='failed_geometry_v3_windows.csv',mime='text/csv',key='fgv3_download')
    if not len(candidate):return
    options=candidate.sort_values('retest_ts',ascending=False).head(300)
    choice=st.selectbox('Evento para gráfico 1m',list(options.touch_key),
         format_func=lambda k:next((f'{r.symbol} {r.original_side} {r.impulse_direction} · {r.pullback_min:.0f}m'
                                   for r in options.itertuples() if r.touch_key==k),k),key='fgv3_event')
    if st.checkbox('Ver trayectoria normalizada 1m',key='fgv3_chart'):
        try:
            import reaction_lab_failed_behavior_v1 as base
            src=root/'reports/reaction_lab/first_touch_paths_v2.sqlite3'
            with closing(base.ro_db(src)) as con:
                row=con.execute('SELECT path_blob FROM paths WHERE touch_key=?',(choice,)).fetchone()
            if not row: st.info('Sin 1m archivado.');return
            bars=base.decode(row[0])
            selected=options.loc[options.touch_key==choice].iloc[0]
            baseline=bars[4][4];mul=(-1 if selected.original_side=='LONG' else 1)
            y=[100*mul*(b[4]-baseline)/baseline for b in bars[:min(len(bars),int(selected.pullback_min)+h)]]
            fig=go.Figure(go.Scatter(x=list(range(1,len(y)+1)),y=y,mode='lines',name='Cierre 1m firmado'))
            fig.add_hline(y=0,line_dash='dash',annotation_text='Precio minuto 5')
            fig.add_vline(x=5,line_dash='dash',annotation_text='t5')
            for field,name,color in [('impulse_min','Impulso','#DCA74A'),('peak_min','Pico','#818CF8'),
                                     ('pullback_min','Retroceso confirmado','#36AB79')]:
                t=selected[field]
                if pd.notna(t):fig.add_vline(x=int(t),line_dash='dot',line_color=color,annotation_text=name)
            fig.update_layout(height=410,xaxis_title='Minutos desde first touch',
                   yaxis_title='Movimiento firmado en dirección FAILED (%)',margin=dict(l=10,r=10,t=20,b=10))
            st.plotly_chart(fig,use_container_width=True)
        except (ValueError,OSError,KeyError,sqlite3.Error,TypeError) as exc:
            st.warning(f'No se pudo abrir el gráfico 1m: {exc}')
    st.caption('Flow 4h: solo snapshots archivados antes del evento. Los rankings ACTUALES no se unen retrospectivamente.')
