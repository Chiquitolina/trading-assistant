"""FAILED Second Break V4 UI; read-only CSV + 1m chart anchored at second break."""
from pathlib import Path


def render_failed_second_break_panel(project, *, symbol=None, side=None, origin=None):
    import pandas as pd
    import streamlit as st
    import plotly.graph_objects as go
    from contextlib import closing

    root=Path(project)
    out=root/'reports/reaction_lab/failed_second_break_v4'
    st.markdown('### 🔎 FAILED Second Break V4 · ¿continúa después del segundo quiebre?')
    st.caption('SIN PnL. Segundo quiebre confirmado por CIERRE 1m; cada ventana futura empieza '
               'en la vela SIGUIENTE. Comparación con primeros retesteos de control, no barras aleatorias.')
    a=out/'second_break_events.csv';b=out/'post_second_windows.csv'
    if not a.is_file() or not b.is_file():
        st.info('Generá V4 primero: `python3 reaction_lab_failed_second_break_v4.py --run`.')
        return
    try:
        events=pd.read_csv(a,low_memory=False)
        windows=pd.read_csv(b,low_memory=False)
    except (OSError,ValueError,pd.errors.EmptyDataError) as exc:
        st.warning(f'No se pudieron abrir resultados V4: {exc}');return
    needed={'touch_key','cohort_type','threshold_pct','second_status','original_side','impulse_direction'}
    if not needed.issubset(events.columns):
        st.warning('CSV V4 desactualizado. Repetí --run.');return
    e=events[events.cohort_type.eq('FAILED')].copy()
    if symbol:e=e[e.symbol.eq(symbol)]
    if side:e=e[e.original_side.eq(side)]
    if origin:e=e[e.observation_type.eq(origin)]
    c1,c2,c3,c4=st.columns([1,1,2,1])
    with c1:
        th=st.selectbox('Impulso original',options=[.5,1.0],format_func=lambda v:f'±{v:.2f}%',key='fsbv4_threshold')
    with c2:
        horizon=st.selectbox('Después segundo quiebre',options=[5,10,15,30],key='fsbv4_horizon')
    with c3:
        focus=st.selectbox('Estructura',options=['Todos','SHORT → REVERSAL','LONG → BREAKOUT','Otros'],key='fsbv4_focus')
    with c4:
        excursion=st.selectbox('Primera excursión',options=[.25,.5,1.0],format_func=lambda v:f'±{v:.2f}%',key='fsbv4_exc')
    e=e[e.threshold_pct.eq(th)]
    if focus=='SHORT → REVERSAL':e=e[e.original_side.eq('SHORT')&e.impulse_direction.eq('REVERSAL')]
    elif focus=='LONG → BREAKOUT':e=e[e.original_side.eq('LONG')&e.impulse_direction.eq('BREAKOUT')]
    elif focus=='Otros':
        e=e[~((e.original_side.eq('SHORT')&e.impulse_direction.eq('REVERSAL'))|
              (e.original_side.eq('LONG')&e.impulse_direction.eq('BREAKOUT')))]
    if e.empty:st.info('No hay eventos con estos filtros.');return
    second=e[e.second_status.eq('SECOND_BREAKOUT')]
    w=windows[(windows.cohort_type=='FAILED')&(windows.threshold_pct.eq(th))&
              (windows.post_second_horizon.eq(horizon))&windows.touch_key.isin(second.touch_key)].copy() if not windows.empty else pd.DataFrame()
    full=w[w.full_coverage.eq(True)] if not w.empty else w
    m=st.columns(4)
    m[0].metric('FAILED en cohorte',e.touch_key.nunique())
    m[1].metric('Segundo quiebre confirmado',second.touch_key.nunique())
    m[2].metric(f'Cobertura post-quiebre {horizon}m',f'{len(full)}/{len(w)}')
    m[3].metric('Market Flow causal',f'{int(e.flow_asof_available.fillna(False).sum())}/{len(e)}' if 'flow_asof_available' in e else '—')
    st.caption('Los eventos sin segundo quiebre NO desaparecen: se cuentan en la cohorte original. '
               'Los resultados posteriores se calculan solamente donde hubo quiebre y ventana completa; '
               'los censurados no cuentan como negativos.')
    st.markdown('#### Frecuencia de alcanzar la etapa')
    stages=e.groupby(['original_side','impulse_direction','second_status'],dropna=False).size().rename('N').reset_index()
    st.dataframe(stages,hide_index=True,use_container_width=True)
    if full.empty:
        st.info('Todavía no hay segundos quiebres con ventana completa para este recorte.');return
    key={.25:'0p25',.5:'0p5',1.0:'1p0'}[excursion]
    field=f'first_{key}'
    st.markdown('#### Qué sucede DESPUÉS del segundo quiebre')
    grouped=full.groupby(['original_side','impulse_direction'],dropna=False).agg(
        N=('touch_key','nunique'),
        continuacion_pct=(field,lambda s:100*s.eq('CONTINUE').mean()),
        invalidacion_pct=(field,lambda s:100*s.eq('REVERSE').mean()),
        ninguno_pct=(field,lambda s:100*s.eq('NEITHER').mean()),
        vuelve_al_pico_pct=('recross_peak_status',lambda s:100*s.eq('YES').mean()),
        vuelve_al_retroceso_pct=('retouch_pb_status',lambda s:100*s.eq('YES').mean()),
        vuelve_al_precio_t5_pct=('retouch_m5_status',lambda s:100*s.eq('YES').mean()),
        continuidad_mediana=('fraction_closes_beyond_peak','median'),
        consecutivas_medianas=('longest_consecutive_beyond_peak','median'),
        mfe_med_pct=('post_second_mfe_pct','median'),
        mae_med_pct=('post_second_mae_pct','median'),
        demora_a_pico_mediana=('first_recross_peak_min','median')).reset_index()
    st.dataframe(grouped.round(3),hide_index=True,use_container_width=True)
    st.caption(f'Carrera de CIERRE ±{excursion:.2f}% desde el precio de confirmación del segundo quiebre. '
               'Retorno al pico, al retroceso o al precio t5 son desenlaces descriptivos separados.')
    st.markdown('#### Comparación contra controles emparejados')
    paired=out/'strict_paired_post_second.csv'
    if paired.is_file():
        p=pd.read_csv(paired)
        p=p[p.threshold_pct.eq(th)&p.horizon.eq(horizon)]
        if side:p=p[p.side.eq(side)]
        if not p.empty:
            cp=p[p.both_full.eq(True)].copy()
            st.metric('Pares con SEGUNDO QUIEBRE y cobertura completa en AMBOS',len(cp))
            if not cp.empty:
                summary=pd.DataFrame([{'pares':len(cp),
                    'failed_continue_0p5_pct':100*cp.failed_first_half.eq('CONTINUE').mean(),
                    'control_continue_0p5_pct':100*cp.control_first_half.eq('CONTINUE').mean(),
                    'failed_recross_peak_pct':100*cp.failed_recross.eq('YES').mean(),
                    'control_recross_peak_pct':100*cp.control_recross.eq('YES').mean()}])
                st.dataframe(summary.round(2),hide_index=True,use_container_width=True)
            st.caption('Comparación muy seleccionada: ambos pares debieron confirmar segundo quiebre; '
                       'NO prueba causalidad ni rentabilidad. Consultá la tabla de pares CSV para detalle.')
    st.markdown('#### Cada FAILED · medidas individuales')
    cols=['symbol','original_side','impulse_direction','second_min','second_overshoot_pct',
          'second_distance_to_swing_pct','full_coverage','post_second_mfe_pct',
          'post_second_mae_pct',field,'recross_peak_status','first_recross_peak_min',
          'retouch_pb_status','first_retouch_pb_min','longest_consecutive_beyond_peak','touch_key']
    st.dataframe(w[[c for c in cols if c in w.columns]].head(700).round(4),hide_index=True,use_container_width=True)
    st.download_button('Exportar tabla post-segundo-quiebre',w.to_csv(index=False).encode('utf-8-sig'),
         file_name='failed_second_break_v4_filtered.csv',mime='text/csv',key='fsbv4_dl')
    choices=second.sort_values('second_min').head(400)
    if choices.empty:return
    selected=st.selectbox('Ver evento 1m',choices.touch_key.tolist(),key='fsbv4_choose',
      format_func=lambda k:next((f'{r.symbol} · {r.original_side} / {r.impulse_direction} · segundo quiebre t{int(r.second_min)}'
                                 for r in choices.itertuples() if r.touch_key==k),k))
    if st.checkbox('Gráfico 1m desde FAIL hasta después del segundo quiebre',key='fsbv4_plot'):
        try:
            import reaction_lab_failed_behavior_v1 as base
            import sqlite3
            row=choices[choices.touch_key.eq(selected)].iloc[0]
            uri=(root/'reports/reaction_lab/first_touch_paths_v2.sqlite3').resolve().as_uri()+'?mode=ro'
            with closing(sqlite3.connect(uri,uri=True,timeout=7)) as con:
                con.execute('PRAGMA query_only=ON')
                blob=con.execute('SELECT path_blob FROM paths WHERE touch_key=?',(selected,)).fetchone()
            if not blob:st.info('Recorrido 1m no disponible.');return
            bars=base.decode(blob[0]);baseline=bars[4][4]
            mul=-1 if row.original_side=='LONG' else 1
            last=min(len(bars),int(row.second_min)+horizon)
            price=[mul*(bar[4]-baseline)/baseline*100 for bar in bars[:last]]
            fig=go.Figure(go.Scatter(x=list(range(1,last+1)),y=price,mode='lines',name='Cierre 1m firmado'))
            fig.add_hline(y=0,line_dash='dash',annotation_text='Base t5')
            for col,txt,color in [('impulse_min','Impulso','#EAB85C'),('peak_min','Pico','#91A0EF'),
                                  ('pullback_min','PB confirmado','#AA78D0'),('second_min','Segundo quiebre','#26A574')]:
                t=row[col]
                if pd.notna(t):fig.add_vline(x=int(t),line_color=color,line_dash='dot',annotation_text=txt)
            if pd.notna(row.frozen_peak_progress_pct):fig.add_hline(y=row.frozen_peak_progress_pct* (1 if row.impulse_direction=='BREAKOUT' else -1),line_dash='dot',annotation_text='Pico congelado')
            fig.update_layout(height=430,xaxis_title='Minuto desde FAILED',
                  yaxis_title='Movimiento hacia la ruptura del FAILED (%)',margin=dict(l=10,r=10,t=10,b=10))
            st.plotly_chart(fig,use_container_width=True)
        except (ValueError,TypeError,KeyError,OSError,sqlite3.Error) as exc:
            st.warning(f'No se pudo dibujar el evento: {exc}')
    st.caption('Market Flow: unión histórica por símbolo y primera observación anterior al evento. '
               'Snapshots actuales no se aplican a eventos anteriores.')
