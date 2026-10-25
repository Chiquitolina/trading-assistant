"""Streamlit explorer for frozen V6 geometry audit. Read-only on journals."""
from pathlib import Path


def render_failed_path_audit_v6(project, *, symbol=None, side=None, origin=None):
    import pandas as pd
    import streamlit as st
    import plotly.graph_objects as go

    root=Path(project)
    folder=root/'reports/reaction_lab/failed_path_audit_v6'
    report_path=folder/'report.json'
    paths_path=folder/'v6_paths.csv'
    summary_path=folder/'v6_summary.csv'
    st.markdown('### 🔬 V6 · auditoría final de caminos del segundo quiebre')
    st.caption('Hipótesis FIJA: FAILED LONG → BREAKOUT, impulso 0,5%, segundo quiebre confirmado, cierre T+1. '
               'A = aceleración inmediata; B = pico perdido. Sin PnL ni operación automática.')
    if not paths_path.is_file() or not summary_path.is_file() or not report_path.is_file():
        st.info('Primero ejecutá `python3 reaction_lab_failed_path_audit_v6.py --run`.')
        return
    try:
        paths=pd.read_csv(paths_path,low_memory=False)
        summaries=pd.read_csv(summary_path,low_memory=False)
        follow_path=folder/'v6_fresh_break_followthrough.csv'
        follow=pd.read_csv(follow_path,low_memory=False) if follow_path.is_file() else pd.DataFrame()
        pairs_path=folder/'v6_pair_audit.csv'
        pairs=pd.read_csv(pairs_path,low_memory=False) if pairs_path.is_file() else pd.DataFrame()
        daily_path=folder/'v6_daily.csv'
        daily=pd.read_csv(daily_path,low_memory=False) if daily_path.is_file() else pd.DataFrame()
    except (OSError,ValueError,pd.errors.EmptyDataError) as err:
        st.warning(f'No se pudieron cargar los resultados V6: {err}')
        return
    needed={'touch_key','pathway','cohort_type','horizon','full_coverage','competing_result','era','observation_type'}
    if not needed.issubset(paths.columns):
        st.warning('Formato V6 incompatible. Volvé a ejecutar `--run`.')
        return
    hcol,pcol,ecol,ccol=st.columns(4)
    with hcol:horizon=st.selectbox('Minutos después de T+1',[5,15,30],index=1,key='v6_h')
    with pcol:pathway=st.selectbox('Camino',['A_ACCELERATED','B_LOST_PEAK','Todos'],key='v6_path')
    with ecol:era=st.selectbox('Cohorte temporal',['Todos','HISTORICAL_EXPLORATORY','FORWARD_LIVE','BACKFILL_OR_BOUNDARY'],key='v6_era')
    with ccol:cohort=st.selectbox('Origen',['FAILED','MATCHED_NONFAILED','Ambos'],key='v6_cohort')
    x=paths[paths.horizon.eq(horizon)].copy()
    if pathway!='Todos':x=x[x.pathway.eq(pathway)]
    if era!='Todos':x=x[x.era.eq(era)]
    if cohort!='Ambos':x=x[x.cohort_type.eq(cohort)]
    if symbol:x=x[x.symbol.eq(symbol)]
    if side:x=x[x.original_side.eq(side)]
    if origin:x=x[x.observation_type.eq(origin)]
    if x.empty:
        st.info('No hay eventos con estos filtros, o todavía no llegaron eventos nuevos.');return
    full=x[x.full_coverage.eq(True)]
    counts=full.competing_result.value_counts()
    m=st.columns(5)
    m[0].metric('Eventos',len(x))
    m[1].metric('Cobertura completa',f'{len(full)}/{len(x)}')
    m[2].metric('Nuevo quiebre primero',int(counts.get('FRESH_BREAK_FIRST',0)))
    m[3].metric('Invalidación primero',int(counts.get('INVALIDATION_FIRST',0)))
    m[4].metric('Sin desenlace',int(counts.get('NEITHER',0)))
    st.caption('Otros: base ya cruzada al T+1, ambigüedad al cierre o ventana censurada. '
               'A: nuevo extremo vs pérdida del pico congelado. '
               'B: ruptura REAL del extremo congelado vs regreso al precio base m5.')

    st.markdown('#### Comparación geométrica por camino, sin cambiar reglas')
    src=summaries[summaries.horizon.eq(horizon)].copy()
    if pathway!='Todos':src=src[src.pathway.eq(pathway)]
    if era!='Todos':src=src[src.era.eq(era)]
    if cohort!='Ambos':src=src[src.cohort_type.eq(cohort)]
    if symbol or origin or side:
        # Aggregated CSV doesn't contain all individual filters; aggregate
        # only visible rows, not unfiltered population summary.
        if not full.empty:
            rows=[]
            for keys,g in full.groupby(['cohort_type','era','pathway'],dropna=False):
                rows.append(dict(zip(['cohort_type','era','pathway'],keys)) | dict(
                    N_full=len(g),pct_fresh_first=100*g.competing_result.eq('FRESH_BREAK_FIRST').mean(),
                    pct_invalidation_first=100*g.competing_result.eq('INVALIDATION_FIRST').mean(),
                    n_0p5_from_landmark_without_fresh=int(((g.landmark_first_0p5=='CONTINUE')&
                                                    g.first_fresh_break_delay_min.isna()).sum()),
                    med_next_open_mfe=g.next_open_mfe_wick_pct.median(),
                    med_next_open_mae=g.next_open_mae_wick_pct.median()))
            src=pd.DataFrame(rows)
    cols=['cohort_type','era','pathway','N','N_full','N_censored','n_fresh_first','n_invalidation_first',
          'n_baseline_already_t1','pct_fresh_first','pct_invalidation_first',
          'n_0p5_from_landmark_without_fresh','med_delay_fresh','med_next_open_mfe','med_next_open_mae']
    st.dataframe(src[[c for c in cols if c in src]].round(3),hide_index=True,use_container_width=True)
    st.caption('La apertura de la vela siguiente es solo una referencia para excursiones, no un fill. '
               'Las carreras se deciden por cierres; MFE/MAE usa mechas y no determina su orden.')

    if not full.empty:
        chart=full.groupby(['cohort_type','pathway','competing_result']).size().reset_index(name='N')
        fig=go.Figure()
        for label,group in chart.groupby(['cohort_type','pathway']):
            fig.add_trace(go.Bar(x=group.competing_result,y=group.N,name=' / '.join(label)))
        fig.update_layout(barmode='group',height=285,xaxis_title='Primer desenlace observado',yaxis_title='Número de eventos')
        st.plotly_chart(fig,use_container_width=True,key='v6_outcomes_chart')

    st.markdown('#### ¿Qué pasa después del NUEVO quiebre verdadero?')
    if not follow.empty and {'touch_key','horizon','follow_horizon','full_coverage'}.issubset(follow):
        f=follow[(follow.horizon.eq(15))&(follow.follow_horizon.isin([5,15]))].copy()
        f=f[f.touch_key.isin(set(x.touch_key))]
        if cohort!='Ambos':f=f[f.cohort_type.eq(cohort)]
        f=f[f.full_coverage.eq(True)]
        if not f.empty:
            table=f.groupby(['cohort_type','pathway','follow_horizon'],dropna=False).agg(
                N=('touch_key','nunique'),continua_0p5_pct=('first_0p5',lambda g:100*g.eq('CONTINUE').mean()),
                retrocede_0p5_pct=('first_0p5',lambda g:100*g.eq('REVERSE').mean()),
                MFE_mediano_pct=('future_mfe_wick_pct','median'),MAE_mediano_pct=('future_mae_wick_pct','median')).reset_index()
            st.dataframe(table.round(3),hide_index=True,use_container_width=True)
        else:st.caption('Todavía no hay quiebres con trayectorias posteriores completas para estos filtros.')
    st.caption('Condicionado a que el nuevo quiebre ya se confirmó: este panel es diagnóstico, '
               'NO prueba que fuera predecible en T+1.')

    st.markdown('#### Robustez y control')
    if not pairs.empty:
        st.caption(f'Pares de selección inicial con ambas trayectorias a 15m: '
                   f'{int(pairs.both_full.eq(True).sum())}; mismo camino y cobertura: '
                   f'{int(pairs.both_same_pathway.eq(True).sum())}. '
                   'No interpretes el emparejamiento condicionado a fase como prueba causal.')
    if not daily.empty:
        with st.expander('Resultados por día UTC',expanded=False):
            st.dataframe(daily[daily.horizon.eq(horizon)].round(3),hide_index=True,use_container_width=True)

    st.markdown('#### Casos individuales · velas 1m archivadas')
    display_cols=['symbol','date_utc','observation_type','era','pathway',
       'progress_at_t1_pct','progress_to_fresh_break_pp','progress_to_peak_pp',
       'first_fresh_break_delay_min','first_peak_recross_delay_min','first_baseline_cross_delay_min',
       'competing_result','landmark_first_0p5','first_0p5_from_next_open',
       'next_open_mfe_wick_pct','next_open_mae_wick_pct','touch_key']
    st.dataframe(x[[c for c in display_cols if c in x]].round(4),hide_index=True,use_container_width=True)
    st.download_button('Exportar auditoría V6 filtrada',x.to_csv(index=False).encode('utf-8-sig'),
                       file_name='failed_path_audit_v6.csv',mime='text/csv',key='v6_export')
    cases=x[x.cohort_type.eq('FAILED')].drop_duplicates('touch_key').sort_values(['date_utc','symbol'])
    if cases.empty:return
    labels={r.touch_key:f"{r.symbol} · {r.date_utc} · {r.pathway}" for r in cases.itertuples()}
    chosen=st.selectbox('Seleccionar FAILED',list(labels),format_func=lambda k:labels[k],key='v6_choice')
    if not st.checkbox('Ver gráfico 1m del caso',key='v6_plot'):return
    try:
        import sqlite3
        import reaction_lab_failed_behavior_v1 as v1
        db=root/'reports/reaction_lab/first_touch_paths_v2.sqlite3'
        with sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True,timeout=7) as con:
            con.execute('PRAGMA query_only=ON')
            r=con.execute('SELECT path_blob FROM paths WHERE touch_key=?',(chosen,)).fetchone()
        if not r:
            st.info('No hay archivo 1m disponible.');return
        bars=v1.decode(r[0])
        a=cases[cases.touch_key.eq(chosen)].iloc[0]
        t0=int(a.second_min);t1=int(a.known_min)
        first=max(1,t0-9);last=min(len(bars),t1+horizon)
        sec=bars[first-1:last]
        ticks=list(range(first,last+1))
        fig=go.Figure(go.Candlestick(x=ticks,open=[p[1] for p in sec],high=[p[2] for p in sec],
                  low=[p[3] for p in sec],close=[p[4] for p in sec],name='OHLC 1m'))
        for y,name in [(a.frozen_peak_price,'Pico congelado'),
                       (a.m5_baseline_price,'Base m5'),
                       (a.fresh_break_price_threshold,'Nuevo quiebre REAL')]:
            fig.add_hline(y=float(y),line_dash='dot',annotation_text=name)
        for minute,name in [(t0,'Segundo quiebre (cierre)'),(t1,'T+1 conocido (cierre)')]:
            fig.add_vline(x=minute,line_dash='dash',annotation_text=name)
        if pd.notna(a.first_fresh_break_delay_min):
            fig.add_vline(x=t1+int(a.first_fresh_break_delay_min),line_dash='dot',
                          annotation_text='Nuevo quiebre futuro')
        fig.update_layout(height=440,xaxis_title='Minuto de vela desde FAILED (cierre)',
                          yaxis_title='Precio',xaxis_rangeslider_visible=False)
        st.plotly_chart(fig,use_container_width=True,key='v6_candles')
        st.caption('Pico, base y umbral conocidos a T+1. El futuro se utiliza solamente para etiquetar desenlaces.')
    except (OSError,sqlite3.Error,IndexError,ValueError,KeyError,TypeError) as exc:
        st.warning(f'Gráfico individual no disponible: {exc}')
