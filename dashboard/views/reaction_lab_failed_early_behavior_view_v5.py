"""Dashboard: closed-candle early landmarks AFTER V4 second break, no PnL.

Reads the isolated V5 CSV outputs and archived 1m OHLC paths; no writes.
"""
from pathlib import Path


def render_failed_early_behavior_panel(project, *, symbol=None, side=None, origin=None):
    import pandas as pd
    import streamlit as st
    import plotly.graph_objects as go

    root=Path(project)
    out=root/'reports/reaction_lab/failed_early_behavior_v5'
    st.markdown('### 🔍 FAILED Early Behavior V5 · ¿continuación o agotamiento temprano?')
    st.caption('Solo comportamiento, NO PnL. t0 = segundo quiebre confirmado por cierre 1m. '
               't1/2/3/5 usan exclusivamente velas cerradas; los desenlaces empiezan en la siguiente.')
    ep_path=out/'second_break_episodes.csv'
    landmarks_path=out/'early_landmarks.csv'
    windows_path=out/'forward_windows.csv'
    if not all(x.is_file() for x in [ep_path,landmarks_path,windows_path]):
        st.info('Ejecutá `python3 reaction_lab_failed_early_behavior_v5.py --run` para generar V5.')
        return
    try:
        episodes=pd.read_csv(ep_path,low_memory=False)
        lm=pd.read_csv(landmarks_path,low_memory=False)
        fw=pd.read_csv(windows_path,low_memory=False)
    except (OSError,ValueError,pd.errors.EmptyDataError) as exc:
        st.warning(f'No se pudieron leer los CSV V5: {exc}');return
    cols={'touch_key','cohort_type','early_minute','threshold_pct','early_state','still_beyond_peak'}
    if not cols.issubset(lm.columns) or not {'forward_horizon','full_coverage','first_0p5'}.issubset(fw.columns):
        st.warning('CSV V5 desactualizados; repetí --run.');return
    th_col,mark_col,window_col,focus_col=st.columns([1,1,1,2])
    with th_col:
        th=st.selectbox('Impulso V4',options=[.5,1.0],format_func=lambda v:f'±{v:.2f}%',key='febv5_th')
    with mark_col:
        landmark=st.selectbox('Observar después de quiebre',options=[1,2,3,5],format_func=lambda v:f'T+{v} cierre(s)',key='febv5_landmark')
    with window_col:
        horizon=st.selectbox('Mirar desde ese cierre',options=[5,10,15,30],format_func=lambda v:f'{v}m futuros',key='febv5_horizon')
    with focus_col:
        focus=st.selectbox('Estructura',options=['Todos','LONG → BREAKOUT','SHORT → REVERSAL','Otros'],key='febv5_focus')
    stage=episodes[(episodes.cohort_type=='FAILED')&episodes.threshold_pct.eq(th)].copy()
    a=lm[lm.threshold_pct.eq(th)&lm.early_minute.eq(landmark)].copy()
    w=fw[fw.threshold_pct.eq(th)&fw.early_minute.eq(landmark)&fw.forward_horizon.eq(horizon)].copy()
    if symbol:
        stage=stage[stage.symbol.eq(symbol)]
        a=a[a.symbol.eq(symbol)]
        w=w[w.symbol.eq(symbol)]
    if side:
        stage=stage[stage.original_side.eq(side)]
        a=a[a.original_side.eq(side)]
        w=w[w.original_side.eq(side)]
    if origin:
        if 'observation_type' in stage:stage=stage[stage.observation_type.eq(origin)]
        a=a[a.observation_type.eq(origin)]
        w=w[w.observation_type.eq(origin)]
    def apply_focus(df):
        long=(df.original_side.eq('LONG')&df.impulse_direction.eq('BREAKOUT'))
        short=(df.original_side.eq('SHORT')&df.impulse_direction.eq('REVERSAL'))
        if focus=='LONG → BREAKOUT':return df[long]
        if focus=='SHORT → REVERSAL':return df[short]
        if focus=='Otros':return df[~(long|short)]
        return df
    stage=apply_focus(stage)
    a=apply_focus(a)
    w=apply_focus(w)
    if stage.empty:
        st.info('No hay episodios V5 para estos filtros.');return
    status_counts=stage.second_status.value_counts(dropna=False)
    st.markdown('#### Embudo de cobertura y estado conocido')
    metrics=st.columns(5)
    metrics[0].metric('Primeros retesteos',stage.touch_key.nunique())
    metrics[1].metric('Segundo quiebre',int(status_counts.get('SECOND_BREAKOUT',0)))
    metrics[2].metric(f'T+{landmark} disponible',a[a.cohort_type.eq('FAILED')].touch_key.nunique())
    wt=w[w.cohort_type.eq('FAILED')].copy()
    complete=wt[wt.full_coverage.eq(True)]
    metrics[3].metric(f'Futuro {horizon}m completo',f'{len(complete)}/{len(wt)}')
    live=complete[complete.still_beyond_peak.eq(True)]
    metrics[4].metric('Aún sostiene pico',len(live))
    st.caption('La etiqueta temprana solo describe lo que ya se sabe al cierre T+N. '
               'Los eventos que perdieron el pico no se cuentan como futuras invalidaciones. '
               'EXTENDED = avance ≥0,25%; FADED = retroceso ≥0,25%; STABLE = sin ambos; '
               'LOST = ahora bajo el pico; RECLAIMED = lo perdió y recuperó antes de T+N.')
    with st.expander('Estado de TODOS los segundos quiebres (incluidos sin T+N)',expanded=False):
        st.dataframe(status_counts.rename_axis('estado').reset_index(name='N'),hide_index=True,use_container_width=True)
    state_options=['Todos','STABLE_ABOVE','EXTENDED_0P25','FADED_0P25','LOST_PEAK','RECLAIMED_PEAK']
    state_filter=st.selectbox('Estado conocido al T+N',state_options,key='febv5_state')
    if state_filter!='Todos':
        a=a[a.early_state.eq(state_filter)]
        w=w[w.early_state.eq(state_filter)]
    full=w[w.full_coverage.eq(True)].copy()
    st.markdown('#### Desenlaces futuros desde el precio observado en T+N')
    if full.empty:
        st.info('No hay suficientes velas posteriores completas para los filtros.');return
    summary=full.groupby(['cohort_type','original_side','impulse_direction','early_state'],dropna=False).agg(
        N=('touch_key','nunique'),
        continua_0p25_pct=('first_0p25',lambda s:100*s.eq('CONTINUE').mean()),
        revierte_0p25_pct=('first_0p25',lambda s:100*s.eq('REVERSE').mean()),
        continua_0p5_pct=('first_0p5',lambda s:100*s.eq('CONTINUE').mean()),
        revierte_0p5_pct=('first_0p5',lambda s:100*s.eq('REVERSE').mean()),
        ninguno_0p5_pct=('first_0p5',lambda s:100*s.eq('NEITHER').mean()),
        continua_1p0_pct=('first_1p0',lambda s:100*s.eq('CONTINUE').mean()),
        mfe_mediano_pct=('future_mfe_wick_pct','median'),
        mae_mediano_pct=('future_mae_wick_pct','median'),
        cambio_final_mediano_pct=('future_close_change_pct','median'),
        riesgo_recross_n=('eligible_future_recross','sum'),
        recross_futuro_n=('future_peak_recross_status',lambda s:s.eq('RE_CROSSED').sum()),
    ).reset_index()
    st.dataframe(summary.round(3),hide_index=True,use_container_width=True)
    st.caption('Porcentajes calculados sobre N completos; MFE/MAE por mechas no establecen orden intravela. '
               'La comparación de controles está condicionada a que ambos hayan alcanzado las fases.')
    st.markdown('#### Curva mediana posterior a T+N')
    cur_path=out/'future_trajectories.csv'
    if cur_path.is_file():
        traces=pd.read_csv(cur_path,low_memory=False)
        traces=traces[(traces.threshold_pct.eq(th))&(traces.early_minute.eq(landmark))&
                      (traces.future_minute.le(horizon))&traces.completed_30m.eq(True)]
        if symbol:traces=traces[traces.symbol.eq(symbol)]
        if origin:traces=traces[traces.observation_type.eq(origin)]
        if side:traces=traces[traces.original_side.eq(side)]
        if focus=='LONG → BREAKOUT':traces=traces[traces.original_side.eq('LONG')&traces.impulse_direction.eq('BREAKOUT')]
        elif focus=='SHORT → REVERSAL':traces=traces[traces.original_side.eq('SHORT')&traces.impulse_direction.eq('REVERSAL')]
        elif focus=='Otros':traces=traces[~((traces.original_side.eq('LONG')&traces.impulse_direction.eq('BREAKOUT'))|
                                              (traces.original_side.eq('SHORT')&traces.impulse_direction.eq('REVERSAL')))]
        if state_filter!='Todos':traces=traces[traces.early_state.eq(state_filter)]
        if not traces.empty:
            fig=go.Figure()
            traces=traces[traces.cohort_type.isin(['FAILED','MATCHED_NONFAILED'])]
            for cohort,group in traces.groupby('cohort_type'):
                curve=group.groupby('future_minute').agg(
                     median=('forward_change_pct','median'),N=('touch_key','nunique')).reset_index()
                fig.add_trace(go.Scatter(x=curve.future_minute,y=curve['median'],name=f'{cohort} (N≈{curve.N.min()})',
                                         mode='lines+markers'))
            fig.add_hline(y=0,line_dash='dash')
            fig.update_layout(height=300,xaxis_title='Minutos posteriores al T+N',
                              yaxis_title='Cambio mediano firmado desde cierre T+N (%)')
            st.plotly_chart(fig,use_container_width=True,key='febv5_median_curve')
            st.caption('Solo trayectorias de 30m completos. Curvas mediana, no trayectorias de una misma moneda ni retornos ejecutables.')
    st.markdown('#### Comparación estricta de pares')
    paired_file=out/'strict_paired_early.csv'
    if paired_file.is_file() and symbol is None and origin is None:
        pairs=pd.read_csv(paired_file)
        if {'threshold_pct','landmark','forward_horizon','both_full','both_same_impulse','both_same_early_state'}.issubset(pairs):
            pairs=pairs[(pairs.threshold_pct.eq(th))&(pairs.landmark.eq(landmark))&
                        (pairs.forward_horizon.eq(horizon))]
            if side:pairs=pairs[pairs.original_side.eq(side)]
            if focus=='LONG → BREAKOUT':pairs=pairs[pairs.original_side.eq('LONG')&pairs.impulse_direction.eq('BREAKOUT')]
            elif focus=='SHORT → REVERSAL':pairs=pairs[pairs.original_side.eq('SHORT')&pairs.impulse_direction.eq('REVERSAL')]
            elif focus=='Otros':pairs=pairs[~((pairs.original_side.eq('LONG')&pairs.impulse_direction.eq('BREAKOUT'))|
                                           (pairs.original_side.eq('SHORT')&pairs.impulse_direction.eq('REVERSAL')))]
            p1=pairs[pairs.both_full.eq(True)]
            p2=p1[p1.both_same_impulse.eq(True)]
            p3=p2[p2.both_same_early_state.eq(True)]
            st.caption(f'{len(pairs)} pares con ambos segundos quiebres y T+N; '
                       f'{len(p1)} con ventana completa; {len(p2)} con dirección igual; '
                       f'{len(p3)} también con estado temprano igual. '
                       'No es una muestra causal ni independiente.')
            if len(p3):
                st.dataframe(pd.DataFrame([{'pares':len(p3),
                   'FAILED_continua_0p5_pct':100*p3.failed_race_0p5.eq('CONTINUE').mean(),
                   'CONTROL_continua_0p5_pct':100*p3.control_race_0p5.eq('CONTINUE').mean()}]).round(2),
                   hide_index=True,use_container_width=True)
    st.markdown('#### Tabla por evento y gráfico 1m')
    showcols=['symbol','original_side','impulse_direction','early_minute','early_state',
              'move_since_second_pct','distance_to_peak_return_pct','early_mfe_wick_pct',
              'early_mae_wick_pct','full_coverage','future_mfe_wick_pct','future_mae_wick_pct',
              'first_0p25','first_0p5','first_0p5_delay','first_1p0',
              'future_peak_recross_status','future_peak_recross_delay_min','touch_key']
    st.dataframe(w[w.cohort_type.eq('FAILED')][[x for x in showcols if x in w]].head(700).round(4),
                 hide_index=True,use_container_width=True)
    st.download_button('Exportar tabla V5 filtrada',w.to_csv(index=False).encode('utf-8-sig'),
       file_name='failed_early_v5.csv',mime='text/csv',key='febv5_download')
    candidates=a[a.cohort_type.eq('FAILED')].sort_values('symbol').head(400)
    if candidates.empty:return
    pretty={r.touch_key:f'{r.symbol} · {r.original_side}/{r.impulse_direction} · {r.early_state} · quiebre T{r.second_min}'
            for r in candidates.itertuples()}
    chosen=st.selectbox('Inspeccionar FAILED 1m',list(pretty),format_func=lambda k:pretty[k],key='febv5_choice')
    if st.checkbox('Mostrar gráfico 1m archivado',key='febv5_plot'):
        try:
            import sqlite3
            from contextlib import closing
            import reaction_lab_failed_behavior_v1 as v1
            path=root/'reports/reaction_lab/first_touch_paths_v2.sqlite3'
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=7)) as con:
                con.execute('PRAGMA query_only=ON')
                blob=con.execute('SELECT path_blob FROM paths WHERE touch_key=?',(chosen,)).fetchone()
            if not blob:
                st.info('No hay velas 1m archivadas para este evento.');return
            bars=v1.decode(blob[0]);r=candidates[candidates.touch_key.eq(chosen)].iloc[0]
            stage=episodes[(episodes.touch_key.eq(chosen))&episodes.threshold_pct.eq(th)].iloc[0]
            s=int(stage.second_min)
            baseline=bars[4][4]
            evsign=(-1 if r.original_side=='LONG' else 1)
            firstsign=(1 if r.impulse_direction=='BREAKOUT' else -1)
            froms=max(1,s-12);through=min(len(bars),s+landmark+horizon)
            xs=list(range(froms,through+1))
            values=[firstsign*evsign*(bars[i-1][4]-baseline)/baseline*100 for i in xs]
            fig=go.Figure(go.Scatter(x=xs,y=values,mode='lines+markers',name='Cierre 1m firmado'))
            fig.add_hline(y=0,line_dash='dash',annotation_text='Base minuto 5')
            if pd.notna(stage.get('frozen_peak_progress_pct')):
                fig.add_hline(y=float(stage.frozen_peak_progress_pct),line_dash='dot',annotation_text='Pico congelado')
            for x,label in [(int(stage.pullback_min),'Retroceso confirmado'),(s,'Segundo quiebre'),
                            (s+landmark,f'Cierre T+{landmark} conocido')]:
                fig.add_vline(x=x,line_dash='dot',annotation_text=label)
            fig.update_layout(height=405,xaxis_title='Minutos desde el cierre FAILED',
                              yaxis_title='Distancia firmada desde base del minuto 5 (%)')
            st.plotly_chart(fig,use_container_width=True,key='febv5_1m_chart')
            st.caption('Cierres archivados de 1m. El indicador del T+N fue conocido solamente al terminar esa vela.')
        except (OSError,sqlite3.Error,IndexError,KeyError,ValueError,TypeError) as exc:
            st.warning(f'Gráfico individual no disponible: {exc}')
