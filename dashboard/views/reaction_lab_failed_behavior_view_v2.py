"""Streamlit read-only research panel for FAILED Behavior Lab V2.

Historical rankings: archived observed-at snapshots only. Current Market Flow
can be shown alongside recent symbols but never treated as event-time context.
"""
from __future__ import annotations
from pathlib import Path


def _numbers(df,columns):
    import pandas as pd
    for c in columns:
        if c in df:df[c]=pd.to_numeric(df[c],errors='coerce')
    return df


def get_live_flow_table():
    """Only currently available Market Flow; never join historically as causal."""
    import os
    from dashboard.services.market_flow_dashboard_service import MarketFlowDashboardService
    service=MarketFlowDashboardService(host=os.getenv('REDIS_HOST','127.0.0.1'),
         port=int(os.getenv('REDIS_PORT','6379')),db=int(os.getenv('REDIS_DB','0')))
    snapshot=service.get_snapshot('4h')
    if snapshot is None:raise RuntimeError('Snapshot 4h no disponible: '+str(service.last_error))
    frame=service.build_symbol_table(snapshot)
    if frame is None or frame.empty:raise ValueError('Tabla Market Flow vacía')
    mapping={'symbol':'Symbol','return_pct_4h':'Return 4h %',
        'return_rank_pct_4h':'Return Rank %','relative_volume_4h':'Relative Volume',
        'volume_rank_pct_4h':'Volume Rank %','flow_group':'Flow Group',
        'primary_sector':'primary_sector','sector_return_rank_pct_4h':'sector_return_rank_pct_4h'}
    columns=[c for c in mapping if c in frame]
    return frame[columns].rename(columns=mapping), snapshot.get('market_flow_close_timestamp')


def render_failed_behavior_panel(project, *, symbol=None, side=None, origin=None):
    """Place underneath Continuous V1 metrics; no source data writes."""
    import streamlit as st
    import pandas as pd
    import plotly.express as px
    from pathlib import Path
    project=Path(project)
    out=project/'reports/reaction_lab/failed_behavior_v2'
    st.markdown('### 🔬 FAILED Behavior Lab V2 · Impulso → retroceso → segundo movimiento')
    st.caption('Estudio descriptivo de cierres 1m. t=5 es el punto de observación; primero se detecta un impulso por cierre, '
               'después el primer retroceso del 50% y por último un segundo avance o regreso al precio inicial. Sin PnL ni órdenes.')
    path=out/'sequence_events.csv'
    if not path.is_file():
        st.info('Todavía no está generado el informe V2. Ejecutá `python3 reaction_lab_failed_behavior_v2.py --run` en el VPS.')
        return
    try:
        data=pd.read_csv(path,low_memory=False)
    except (OSError,ValueError) as exc:
        st.error(f'No se pudo abrir el informe: {exc}');return
    needed={'symbol','status','cohort_type','horizon','threshold_pct','resolution','full_coverage',
            'impulse_min','pullback_min','second_resolution_min'}
    if not needed.issubset(data.columns):
        st.warning('El informe V2 tiene un esquema distinto. Regenerá con la versión nueva.');return
    data=_numbers(data,['horizon','threshold_pct','impulse_min','pullback_min','second_resolution_min',
        'return_rank_4h_pct','volume_rank_4h_pct','relative_volume_4h','flow_age_min'])
    f=data[data.cohort_type.eq('FAILED')].copy()
    if symbol:f=f[f.symbol.eq(symbol)]
    if side:f=f[f.original_side.eq(side)]
    if origin:f=f[f.observation_type.eq(origin)]
    if f.empty:
        st.info('Sin FAILED en este recorte.');return
    left,right=st.columns(2)
    with left:
        h=st.selectbox('Horizonte desde minuto 5',[30,60],key='fb2_h')
    with right:
        threshold=st.selectbox('Impulso mínimo por cierre',[0.5,1.0],key='fb2_threshold',format_func=lambda x:f'{x:.2f}%')
    chosen=f[(f.horizon==h)&(f.threshold_pct==threshold)].copy()
    evaluable=chosen[chosen.full_coverage.eq(True)].copy()
    unique=chosen.touch_key.nunique()
    a,b,c,d=st.columns(4)
    a.metric('FAILED en recorte',unique)
    b.metric(f'Completos (5+{h}m)',len(evaluable))
    c.metric('Impulso detectado',int(evaluable.impulse_min.notna().sum()))
    d.metric('Segundo avance',int((evaluable.resolution=='SECOND_BREAKOUT').sum()))
    st.caption('Los incompletos quedan censurados. La tasa de segundo avance usa como denominador '
               'todos los evaluables, no solamente los que ya tuvieron impulso/retroceso.')
    if not evaluable.empty:
        order=['NO_IMPULSE','IMPULSE_NO_PULLBACK','PULLBACK_UNRESOLVED','SECOND_BREAKOUT','BASELINE_CROSSED']
        tally=evaluable.resolution.value_counts().reindex(order,fill_value=0)
        df_counts=pd.DataFrame({'estado':tally.index,'eventos':tally.values,'porcentaje':(100*tally.values/len(evaluable)).round(2)})
        st.markdown('**Secuencias del primer impulso al segundo movimiento**')
        st.dataframe(df_counts,hide_index=True,use_container_width=True)
        stat=(evaluable.groupby(['original_side','impulse_direction','resolution'],dropna=False)
            .agg(N=('touch_key','nunique'),
                 pico_mediano_pct=('impulse_peak_pct','median'),
                 t_impulso_med=('impulse_min','median'),t_retroceso_med=('pullback_min','median'),
                 t_segundo_med=('second_resolution_min','median')).reset_index())
        st.dataframe(stat.round(3),hide_index=True,use_container_width=True)
    # Context exists only if it was OBSERVED prior to historical event.
    st.markdown('#### 🌊 Market Flow 4h · Histórico causal')
    real=chosen[chosen.flow_asof_available.eq(True)]
    st.caption('Solo se unen snapshots que ya estaban archivados al cierre del FAILED y con antigüedad ≤4h10m. '
               'NO se asignan rankings actuales a eventos históricos.')
    st.metric('FAILED con contexto histórico disponible',f'{real.touch_key.nunique()}/{unique}')
    if not real.empty:
        cats=real[real.full_coverage.eq(True)].copy()
        if not cats.empty:
            g=(cats.groupby(['rank_bucket','flow_group','original_side'],dropna=False)
               .agg(N=('touch_key','nunique'),
                    segundo_avance_pct=('resolution',lambda x:round(100*(x=='SECOND_BREAKOUT').mean(),1)),
                    retorno_a_base_pct=('resolution',lambda x:round(100*(x=='BASELINE_CROSSED').mean(),1)),
                    return_rank_med=('return_rank_4h_pct','median'),
                    volume_rank_med=('volume_rank_4h_pct','median'),
                    rel_volume_med=('relative_volume_4h','median')).reset_index())
            st.dataframe(g.round(3),hide_index=True,use_container_width=True)
            st.caption('N pequeño en varios grupos: exploratorio, no umbrales de trading validados.')
    else:
        st.info('Todavía no existen snapshots 4h contemporáneos a estos eventos. '
                'El archivo Market Flow comienza a construir historial desde la primera captura; no rellena el pasado.')
    st.markdown('#### 🧪 Grupo de control')
    matchpath=out/'matched_controls.csv'
    if matchpath.is_file():
        pairs=pd.read_csv(matchpath)
        st.caption('Controles: otros primeros retesteos REACTION/INDECISIVE, con mismo día UTC, lado y '
                   'movimiento/rango 5m comparable. No son velas aleatorias y pueden no ser equivalentes en otros factores.')
        a,b=st.columns(2)
        a.metric('Pares emparejados',len(pairs))
        b.metric('FAILED con par',pairs.failed_key.nunique() if not pairs.empty else 0)
        if not pairs.empty:
            selected_pairs=set(pairs.failed_key)
            control_keys=set(pairs.control_key)
            combo=data[(data.horizon==h)&(data.threshold_pct==threshold)&(data.full_coverage.eq(True))].copy()
            combo=combo[((combo.cohort_type=='FAILED')&combo.touch_key.isin(selected_pairs)) |
                        ((combo.cohort_type=='MATCHED_NONFAILED')&combo.touch_key.isin(control_keys))]
            if not combo.empty:
                stats=combo.groupby('cohort_type').agg(
                    N=('touch_key','nunique'),
                    pct_impulso=('impulse_min',lambda x:round(100*x.notna().mean(),2)),
                    pct_segundo=('resolution',lambda x:round(100*(x=='SECOND_BREAKOUT').mean(),2)),
                    pct_retorno=('resolution',lambda x:round(100*(x=='BASELINE_CROSSED').mean(),2))).reset_index()
                st.dataframe(stats,hide_index=True,use_container_width=True)
        else:
            st.info('No hay pares que satisfagan los límites actuales; no se fuerzan coincidencias.')
    st.markdown('#### 🔎 Inspección individual')
    cols=['symbol','original_side','observation_type','retest_ts','initial_penetration_pct',
       'early_move_pct','impulse_direction','impulse_min','impulse_peak_pct','pullback_min',
       'second_resolution_min','resolution','full_coverage','touch_key']
    present=[x for x in cols if x in chosen]
    st.dataframe(chosen[present].sort_values('retest_ts',ascending=False).head(300).round(4),
        hide_index=True,use_container_width=True)
    st.download_button('⬇ CSV FAILED Behavior (filtrado)',chosen.to_csv(index=False).encode('utf-8-sig'),
                       file_name='failed_behavior_v2_filtered.csv',mime='text/csv',key='fb2_dl')
    # Inspect 1m close paths from the separately archived SQL records (read only).
    choices=chosen.sort_values('retest_ts',ascending=False).head(300)
    keys=list(choices.touch_key)
    selected=st.selectbox('Evento para gráfico 1m',keys,format_func=lambda k:next(
        (f"{r.symbol} {r.original_side} · {r.resolution} · {r.retest_ts}" for r in choices.itertuples() if r.touch_key==k),k),key='fb2_event')
    if selected and st.checkbox('Mostrar secuencia 1m guardada',key='fb2_show_graph'):
        try:
            import reaction_lab_failed_behavior_v1 as b
            import sqlite3
            from contextlib import closing
            db=project/'reports/reaction_lab/first_touch_paths_v2.sqlite3'
            with closing(b.ro_db(db)) as con:
                row=con.execute('SELECT path_blob FROM paths WHERE touch_key=?',(selected,)).fetchone()
            if not row:st.info('Sin camino guardado.');return
            bars=b.decode(row[0]);target=choices.loc[choices.touch_key==selected].iloc[0]
            if len(bars)<5:st.info('Faltan las primeras cinco velas.');return
            baseline=bars[4][4]
            direction=-1 if target.original_side=='LONG' else 1
            y=[direction*(x[4]-baseline)/baseline*100 for x in bars[:5+h]]
            xx=list(range(1,len(y)+1))
            fig=px.line(x=xx,y=y,labels={'x':'Minuto posterior al first touch','y':'Movimiento firmado desde precio minuto 5 (%)'})
            fig.add_hline(y=0,line_dash='dash')
            fig.add_vline(x=5,line_dash='dash',annotation_text='Referencia t5')
            for field,color,label in [('impulse_min','#dca647','Impulso'),
                                      ('pullback_min','#b780cc','Retroceso'),
                                      ('second_resolution_min','#27a778','Segundo desenlace')]:
                v=target.get(field)
                if pd.notna(v):fig.add_vline(x=float(v),line_dash='dot',line_color=color,annotation_text=label)
            fig.update_layout(height=430,margin={'l':10,'r':10,'t':25,'b':10})
            st.plotly_chart(fig,use_container_width=True)
        except Exception as exc:st.warning(f'No se pudo graficar el archivo 1m: {exc}')
    st.markdown('#### 📍 Contexto Market Flow ACTUAL (no histórico)')
    st.caption('Esta lectura proviene del explorador 4h en este momento. Sirve para inspeccionar símbolos y '
               '**no** debe interpretarse como Return Rank al momento de los FAILED antiguos.')
    if st.button('Consultar Market Flow 4h actual',key='fb2_get_live_market_flow'):
        try:
            flow,close=get_live_flow_table()
            st.caption(f'Cierre del snapshot fuente: {close} ms UTC · símbolos: {len(flow)}')
            impacted=flow[flow.Symbol.isin(set(chosen.symbol))].copy()
            st.dataframe(impacted.sort_values('Return Rank %',ascending=False),hide_index=True,use_container_width=True)
        except Exception as exc:st.warning(f'Market Flow actual no disponible: {exc}')
