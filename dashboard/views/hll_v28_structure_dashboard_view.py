"""Read-only Streamlit panel for causal 30m structural research V2.8."""
from __future__ import annotations

import json
from pathlib import Path
import pandas as pd

RUNS=Path('reports/horizontal_level_structure_quality_v28/runs')
CANDLES=Path('reports/horizontal_level_lifecycle_v21/candles_aug20_oct25_core15_btc')
TF=1800000
FACTOR_NAMES={
    'pre24_hl_last3':'Higher lows PRE · últimos 3 pivotes',
    'signal24_hl_last3':'Higher lows conocidos al FLIP',
    'pre24_low_progression_atr':'Progresión de mínimos PRE (ATR)',
    'signal24_low_progression_atr':'Progresión de mínimos al FLIP (ATR)',
    'pre24_close_center_crossings':'Cruces del centro PRE (24 velas)',
    'breakout_to_signal_center_crossings':'Cruces del centro entre breakout y señal',
    'pre24_directional_efficiency':'Eficiencia direccional PRE',
    'pre24_range_recent_earlier_ratio':'Compresión de rangos PRE',
    'pre24_pullback_depth_last_over_first':'¿Retrocesos se achican? PRE',
    'signal24_pullback_depth_last_over_first':'¿Retrocesos se achican? al FLIP',
    'pre24_zone_overlap_bars':'Velas PRE que tocan la zona',
    'pre24_higher_low_steps':'Cantidad de pasos hacia mínimos más altos PRE',
    'retest_to_signal_zone_overlaps':'Velas tocando zona durante defensa',
    'retest_to_signal_closes_inside_zone':'Cierres dentro de la zona durante defensa',
    'retest_low_below_zone_high_atr':'Profundidad del retest (ATR)',
    'breakout_volume_ratio':'Volumen en breakout / base',
    'signal_distance_from_zone_low_atr':'Distancia de la señal a soporte (ATR)',
}


def _load(folder):
    f=folder/'entry_structures.csv'
    tbl=folder/'factor_tables.csv'
    ev=folder/'evaluation_enriched.csv'
    if not all(p.is_file() for p in [f,tbl,ev,folder/'manifest.json']):
        return None
    meta=json.loads((folder/'manifest.json').read_text(encoding='utf8'))
    return (pd.read_csv(f,low_memory=False),pd.read_csv(tbl,low_memory=False),
            pd.read_csv(ev,low_memory=False),meta)


def _plot_episode(row,bars,show_future):
    import plotly.graph_objects as go
    known=int(row.known_closed_ts);cross=int(row.breakout_closed_ts)
    i=bars.index[bars.timestamp.eq(cross-TF)]
    j=bars.index[bars.timestamp.eq(known-TF)]
    if len(i)!=1 or len(j)!=1:raise ValueError('Falta la vela 30m de breakout o de FLIP')
    start=max(0,int(i[0])-48)
    stop=min(len(bars),int(j[0])+1+(24 if show_future else 0))
    view=bars.iloc[start:stop]
    ts=pd.to_datetime(view.timestamp,unit='ms',utc=True)
    fig=go.Figure(go.Candlestick(x=ts,open=view.open,high=view.high,
        low=view.low,close=view.close,name='30m',
        increasing_line_color='#159b70',decreasing_line_color='#d65b68'))
    lo=float(row.zone_low);hi=float(row.zone_high)
    fig.add_hrect(y0=lo,y1=hi,fillcolor='#d6a251',opacity=.18,line_width=0)
    for v in [lo,hi]:fig.add_hline(y=v,line_color='#bd9248',line_dash='dash')
    fig.add_vline(x=pd.Timestamp(cross,unit='ms',tz='UTC'),
                  line_color='#397be4',line_dash='dot')
    fig.add_vline(x=pd.Timestamp(known,unit='ms',tz='UTC'),
                  line_color='#20bb89',line_dash='dot')
    pivots=json.loads(row.known_pivots_signal48_json) if isinstance(row.known_pivots_signal48_json,str) else []
    if pivots:
        pivot_dates=[];values=[];hover=[]
        for p in pivots:
            if int(p['confirmed_close_ts'])>known:continue
            pivot_dates.append(pd.Timestamp(int(p['open_ts']),unit='ms',tz='UTC'))
            values.append(float(p['low']))
            hover.append('Pivote conocido desde '+pd.Timestamp(int(p['confirmed_close_ts']),unit='ms',tz='UTC').strftime('%Y-%m-%d %H:%M UTC'))
        if pivot_dates:
            fig.add_trace(go.Scatter(x=pivot_dates,y=values,mode='markers+lines',
                line=dict(color='#f0b55c',dash='dot',width=1.2),
                marker=dict(color='#e8ac4d',size=10,symbol='triangle-up'),
                name='Mínimos 2×2 confirmados',text=hover,hovertemplate='%{text}<br>Mínimo: %{y}<extra></extra>'))
    fig.update_layout(height=520,xaxis_rangeslider_visible=False,
        margin=dict(l=16,r=14,t=22,b=20),yaxis_title='Precio',xaxis_title='UTC',showlegend=True)
    return fig


def render_hll_v28_structure_dashboard(root):
    import streamlit as st
    root=Path(root)
    st.markdown('### 🧱 Calidad estructural · V2.8')
    st.caption('30m causal: mínimos ascendentes confirmados, calidad del retest y cruces de zona. ' 
               'Compara resultados de V2.7 sin recalcularlos ni modificar LIVE.')
    parent=root/RUNS
    available=sorted([p for p in parent.iterdir() if (p/'manifest.json').exists()],
                     key=lambda p:p.stat().st_mtime,reverse=True) if parent.is_dir() else []
    if not available:
        st.info('No hay corrida V2.8. Ejecutar `python3 hll_v28_structure_quality.py --plan` y luego `--run`.')
        return
    folder=st.selectbox('Corrida de estructura V2.8',available,format_func=lambda p:p.name,key='hll28_run')
    try:loaded=_load(folder)
    except (OSError,ValueError,KeyError) as exc:
        st.error(f'V2.8 incompleto: {exc}');return
    if loaded is None:st.error('Faltan archivos de salida V2.8.');return
    e,f,evaluations,manifest=loaded
    confirms=e[e.entry_type.eq('FLIP_CONFIRMED')].copy()
    cols=st.columns(4)
    cols[0].metric('FLIP_CONFIRMED',len(confirms))
    cols[1].metric('Símbolos',confirms.symbol.nunique())
    cols[2].metric('Con Higher Lows PRE',int(confirms.pre24_hl_last3.eq(1).sum()))
    cols[3].metric('Con Higher Lows al FLIP',int(confirms.signal24_hl_last3.eq(1).sum()))
    st.caption('Un valor UNKNOWN significa que faltan 3 pivotes confirmados o contexto completo; no equivale a NO.')
    st.warning('Las agrupaciones son exploratorias. Ago–sep y octubre pertenecen a un período YA inspeccionado; octubre no es una validación fuera de muestra. ' 
               'Las señales comparten símbolos, zonas y condiciones del mercado.')
    st.markdown('#### Comparación de características y resultados')
    aa,bb,cc=st.columns([1.6,1,1])
    with aa: factor=st.selectbox('Característica',list(FACTOR_NAMES),format_func=lambda x:FACTOR_NAMES[x],key='hll28_factor')
    with bb: horizon=st.selectbox('Horizonte (TP2 / SL2)',[60,180],index=1,key='hll28_h')
    with cc: period=st.selectbox('Período',['TODOS','AUG_SEP_DISCOVERY','OCT_REVIEW'],key='hll28_period')
    selection=f[f.factor.eq(factor)&f.horizon_min.eq(horizon)]
    if period!='TODOS':selection=selection[selection.period.eq(period)]
    if selection.empty:st.info('Sin muestras para este factor, período y horizonte.')
    else:
        table=selection[['period','bucket','n','symbols','clusters','tp_pct','sl_pct','mean_net_pct',
                         'equal_symbol_net_pct','positive_symbols']].copy()
        st.dataframe(table.round(4),hide_index=True,use_container_width=True)
        if (table.n<30).any():st.caption('Atención: uno o más grupos tienen menos de 30 episodios.')
        import plotly.express as px
        fig=px.bar(table,x='bucket',y='mean_net_pct',color='period',barmode='group',
                   hover_data=['n','symbols','tp_pct','sl_pct'],
                   labels={'bucket':'Grupo estructural','mean_net_pct':'Retorno neto medio (%)'})
        fig.add_hline(y=0,line_dash='dash',line_color='#8c939a')
        fig.update_layout(height=320,margin=dict(l=12,r=12,t=10,b=10))
        st.plotly_chart(fig,use_container_width=True,key='hll28_factor_plot')
        st.caption('Se excluyen de estos promedios los 1m incompletos. TP/SL siguen las mismas reglas de V2.7.')
    with st.expander('📄 Variables completas y grupos por símbolo/semana',expanded=False):
        fields=['symbol','known_closed_ts','pre24_hl_last3','signal24_hl_last3',
                'pre24_low_progression_atr','signal24_low_progression_atr',
                'pre24_close_center_crossings','breakout_to_signal_center_crossings',
                'retest_to_signal_zone_overlaps','retest_low_below_zone_high_atr',
                'signal_distance_from_zone_low_atr']
        st.dataframe(confirms[fields].head(200),hide_index=True,use_container_width=True)
        st.download_button('Exportar características completas',e.to_csv(index=False).encode('utf8'),
                           file_name='hll_v28_entry_structures.csv',mime='text/csv',key='hll28_export')
        p=folder/'symbol_week_groups.csv'
        if p.is_file():
            cw=pd.read_csv(p)
            st.dataframe(cw[cw.factor.eq(factor)&cw.horizon_min.eq(horizon)].head(200),
                         hide_index=True,use_container_width=True)
    st.markdown('#### Inspector de estructura por episodio')
    s1,s2=st.columns(2)
    with s1:symbol=st.selectbox('Símbolo',['Todos']+sorted(confirms.symbol.unique()),key='hll28_symbol')
    with s2:hl=st.selectbox('Higher lows al FLIP',['Todos','Sí','No','Desconocido'],key='hll28_hl')
    choices=confirms.copy()
    if symbol!='Todos':choices=choices[choices.symbol.eq(symbol)]
    if hl=='Sí':choices=choices[choices.signal24_hl_last3.eq(1)]
    elif hl=='No':choices=choices[choices.signal24_hl_last3.eq(0)]
    elif hl=='Desconocido':choices=choices[choices.signal24_hl_last3.isna()]
    st.caption(f'{len(choices)} intentos disponibles. Elegir ejemplos sin filtrar por TP o SL futuro.')
    if choices.empty:return
    choices=choices.sort_values('known_closed_ts',ascending=False).copy()
    choices['label']=(choices.symbol+' · '+pd.to_datetime(choices.known_closed_ts,unit='ms',utc=True).dt.strftime('%Y-%m-%d %H:%M UTC')+' · '+choices.attempt_id.str[:10])
    chosen=st.selectbox('Inspeccionar FLIP_CONFIRMED',choices.label.tolist(),key='hll28_episode')
    row=choices[choices.label.eq(chosen)].iloc[0]
    post=st.checkbox('Mostrar velas posteriores a la entrada (POST)',False,key='hll28_show_future')
    p=root/CANDLES/f'{row.symbol}_30m.csv'
    if not p.exists():st.warning(f'Falta {p}');return
    try:
        candles=pd.read_csv(p,usecols=['timestamp','open','high','low','close']).sort_values('timestamp').reset_index(drop=True)
        st.plotly_chart(_plot_episode(row,candles,post),use_container_width=True,key='hll28_chart')
    except (ValueError,KeyError,TypeError) as exc:
        st.error(f'No se pudo dibujar el evento: {exc}');return
    a,b,c,d=st.columns(4)
    a.metric('HL PRE', 'Sí' if row.pre24_hl_last3==1 else ('No' if row.pre24_hl_last3==0 else '?'))
    b.metric('HL al confirmar', 'Sí' if row.signal24_hl_last3==1 else ('No' if row.signal24_hl_last3==0 else '?'))
    c.metric('Cruces centro PRE',int(row.pre24_close_center_crossings) if pd.notna(row.pre24_close_center_crossings) else '—')
    d.metric('Contactos retest→flip',int(row.retest_to_signal_zone_overlaps))
    known_fields=['pre24_pivots','signal24_pivots','pre24_low_progression_atr',
           'signal24_low_progression_atr','pre24_pullback_depth_last_over_first',
           'signal24_pullback_depth_last_over_first','pre24_directional_efficiency',
           'breakout_to_signal_center_crossings','retest_low_below_zone_high_atr',
           'defense_close_above_zone_high_atr','signal_distance_from_zone_low_atr',
           'breakout_volume_ratio','next_resistance_at_breakout_atr']
    st.dataframe(pd.DataFrame([{'variable':k,'valor':row[k]} for k in known_fields]),hide_index=True,use_container_width=True)
    if post:
        perf=evaluations[evaluations.attempt_id.eq(row.attempt_id)].copy()
        st.markdown('**Resultados V2.7 de este intento, solo después de revelar POST**')
        st.dataframe(perf[['horizon_min','path_state','outcome','return_after_fee_pct',
                           'mfe_to_exit_pct','mae_to_exit_pct']],hide_index=True,use_container_width=True)
    st.caption('Los triángulos muestran el precio del pivote en su vela original, pero solo se dibujan si dos velas posteriores ' 
               'ya lo habían CONFIRMADO al cerrar FLIP_CONFIRMED. El gráfico histórico no debe interpretarse como disponibilidad inmediata en el pivote.')
