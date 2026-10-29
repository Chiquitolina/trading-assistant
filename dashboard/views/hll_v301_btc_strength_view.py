"""Small read-only BTC-context panel integrated INSIDE the existing HLL inspector."""
from __future__ import annotations
from pathlib import Path
import pandas as pd


def render_btc_strength_panel(root):
    import streamlit as st
    import plotly.express as px
    root=Path(root)
    from dashboard.views.hll_v301_btc_strength import BASE
    with st.expander('₿ Fuerza e independencia frente a BTC · HLL histórico (causal)', expanded=False):
        st.caption('Investigación offline sobre entradas HLL congeladas. Solo velas 30m cerradas antes de cada entrada. '
                   'Correlación (96 retornos de 30m), beta, R² y residual de 2h/4h/24h. '
                   'Esto NO modifica la pestaña BTC Correlation de trades, las señales ni LIVE.')
        a,b=st.columns(2)
        with a:scope=st.radio('Universo de intentos',['FIRST_PER_LEVEL','ALL'],horizontal=True,key='hll_btc_scope')
        path=root/BASE/f'btc_strength_{scope}'
        f=path/'entry_features.csv';p=path/'paired_results.csv'
        if not (f.is_file() and p.is_file()):
            st.info('Aún no hay contexto BTC histórico para esta unidad. Ejecutá el análisis offline --run correspondiente.')
            return
        @st.cache_data(show_spinner=False,max_entries=4)
        def load_cached(file,mtime):
            return pd.read_csv(file,low_memory=False)
        d=load_cached(str(p),p.stat().st_mtime_ns)
        if not len(d):st.info('Sin resultados históricos.');return
        c1,c2,c3=st.columns(3)
        with c1:universe=st.selectbox('Grupo de símbolos',['ADDITIONAL_275','ORIGINAL_15','AMBOS'],key='hll_btc_u')
        with c2:route=st.selectbox('Ruta',['TODAS']+sorted(d.route.dropna().unique()),key='hll_btc_r')
        with c3:month=st.selectbox('Mes',['TODOS']+sorted(d.entry_month.dropna().astype(str).unique()),key='hll_btc_m')
        c4,c5,c6=st.columns(3)
        with c4:case=st.selectbox('Escenario',['5_5_48h','5_5_12h','2_2_48h','2_2_12h'],key='hll_btc_case')
        with c5:pattern=st.selectbox('Estructura PRE 48',['TODAS']+sorted(d.pre_pattern_48.dropna().unique()),key='hll_btc_pattern')
        with c6:groupcol=st.selectbox('Segmentar por',[
            'btc_strength_state_4h','btc_corr_bucket','btc_residual_bucket_4h','entry_month','pre_pattern_48'
        ],format_func=lambda x:{'btc_strength_state_4h':'Fuerza 4h vs BTC',
                                 'btc_corr_bucket':'Correlación 48h',
                                 'btc_residual_bucket_4h':'Fuerza residual 4h',
                                 'entry_month':'Mes','pre_pattern_48':'Estructura PRE 48'}.get(x,x),key='hll_btc_group')
        s=d[d.case.eq(case)].copy()
        if universe!='AMBOS':s=s[s.universe_group.eq(universe)]
        if route!='TODAS':s=s[s.route.eq(route)]
        if month!='TODOS':s=s[s.entry_month.astype(str).eq(month)]
        if pattern!='TODAS':s=s[s.pre_pattern_48.eq(pattern)]
        total=len(s)
        known=s[s.result.isin(['TP_FIRST','SL_FIRST','TIMEOUT'])].copy()
        avail=known[known.btc_context_status.eq('AVAILABLE')]
        a,b,c=st.columns(3)
        a.metric('Entradas seleccionadas',f'{total:,}'.replace(',','.'))
        b.metric('Contexto causal disponible',f'{int(s.btc_context_status.eq("AVAILABLE").sum()):,}'.replace(',','.'))
        c.metric('Neto base evaluable',f'{known.net_pct.mean():+.4f}%' if len(known) else '—')
        if len(avail)==0:st.warning('Sin suficientes velas previas para este filtro.');return
        g=avail.groupby(groupcol,dropna=False).agg(
            entradas=('entry_id','nunique'), conocidas=('net_pct','count'),
            neto_medio_pct=('net_pct','mean'),
            tp=('result',lambda x:int(x.eq('TP_FIRST').sum())),
            sl=('result',lambda x:int(x.eq('SL_FIRST').sum())),
            timeout=('result',lambda x:int(x.eq('TIMEOUT').sum())),
            simbolos=('symbol','nunique')
        ).reset_index()
        g['tp_pct']=100*g.tp/g.conocidas.replace(0,float('nan'))
        g['delta_vs_contexto_pp']=g.neto_medio_pct-avail.net_pct.mean()
        minimum=st.slider('Mínimo de entradas evaluables por grupo',min_value=1,max_value=200,value=30,
                          key='hll_btc_min_n')
        show=g[g.conocidas>=minimum].sort_values('neto_medio_pct',ascending=False)
        st.caption('Neto = media sobre entradas evaluables (no cartera). Δ = diferencia descriptiva frente a todas las '
                   'entradas con contexto disponible bajo los mismos filtros. Repeticiones entre símbolos/fechas '
                   'no son observaciones independientes. Buckets de investigación, no reglas operativas.')
        if show.empty:st.info('No hay grupos que alcancen el mínimo.');return
        st.dataframe(show.round(4),hide_index=True,use_container_width=True)
        fig=px.bar(show,x=groupcol,y='neto_medio_pct',hover_data=['conocidas','simbolos','tp','sl','timeout'],
                   title='Neto medio por grupo de contexto BTC')
        fig.add_hline(y=float(avail.net_pct.mean()),line_dash='dash',annotation_text='Base mismo filtro')
        fig.update_layout(yaxis_title='Neto medio por entrada (%)',xaxis_title=None)
        st.plotly_chart(fig,use_container_width=True,key='hll_btc_plot')
        if st.checkbox('Mostrar entradas individuales con métricas causales',value=False,key='hll_btc_rows'):
            columns=['entry_id','symbol','entry_utc','route','pre_pattern_48','btc_corr_30m_48h',
                     'btc_beta_30m_48h','btc_residual_4h_pct','btc_strength_state_4h',
                     'case','result','net_pct']
            st.dataframe(s[[x for x in columns if x in s]].head(1000).round(5),
                         use_container_width=True,hide_index=True)
        st.download_button('Exportar comparación filtrada (CSV)',
                           data=s.to_csv(index=False),mime='text/csv',
                           file_name=f'hll_btc_strength_{scope}.csv',key='hll_btc_export')
