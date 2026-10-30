"""Read-only Streamlit panel for causal 30m structural research V2.8."""
from __future__ import annotations

import json
from pathlib import Path
import pandas as pd

RUNS=Path('reports/horizontal_level_structure_quality_v28/runs')
CANDLES=Path('reports/horizontal_level_lifecycle_v21/candles_aug20_oct25_core15_btc')
PATTERN_RUNS=Path('reports/horizontal_level_structure_quality_v281/runs')
PATTERN_LABELS={
 'ASCENDING_CLEAN':'A · Mínimos ascendentes limpios',
 'ASCENDING_CHOPPY':'B · Mínimos ascendentes con cruces desordenados',
 'HORIZONTAL_BASE':'C · Base horizontal bajo resistencia',
 'MULTI_CROSS_RANGE':'D · Múltiples cruces / rango errático',
 'IMPULSIVE_APPROACH':'E · Aproximación impulsiva al techo',
 'DESCENDING_LOW_SEQUENCE':'F · Mínimos descendentes',
 'OTHER':'G · Mixta / otra estructura',
 'UNKNOWN':'Sin contexto suficiente',
}
DEFENSE_LABELS={
 'CLEAN_DEFENSE':'Retest limpio', 'CHOPPY_DEFENSE':'Defensa desordenada',
 'DEEP_RETEST':'Retest profundo', 'EXTENDED_CONFIRMATION':'Confirmación extendida',
 'OTHER_DEFENSE':'Defensa mixta', 'UNKNOWN':'Indeterminado',
}

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


def _plot_episode(row,bars,show_future,lookback=48,pivot_field=None):
    import plotly.graph_objects as go
    known=int(row.known_closed_ts);cross=int(row.breakout_closed_ts)
    i=bars.index[bars.timestamp.eq(cross-TF)]
    j=bars.index[bars.timestamp.eq(known-TF)]
    if len(i)!=1 or len(j)!=1:raise ValueError('Falta la vela 30m de breakout o de FLIP')
    start=max(0,int(i[0])-max(48,int(lookback)))
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
    pivots=json.loads(row[pivot_field]) if pivot_field and isinstance(row.get(pivot_field),str) else (json.loads(row.known_pivots_signal48_json) if isinstance(row.known_pivots_signal48_json,str) else [])
    if pivots:
        pivot_dates=[];values=[];hover=[]
        for p in pivots:
            if int(p.get('known_closed_ts',p.get('confirmed_close_ts',0)))>known:continue
            pivot_dates.append(pd.Timestamp(int(p['open_ts']),unit='ms',tz='UTC'))
            values.append(float(p['low']))
            hover.append('Pivote conocido desde '+pd.Timestamp(int(p.get('known_closed_ts',p.get('confirmed_close_ts',0))),unit='ms',tz='UTC').strftime('%Y-%m-%d %H:%M UTC'))
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
    from dashboard.views.hll_phase_batch_view import render_hll_phase_batch
    integral=root/'reports/horizontal_level_structure_quality_v282/runs/sep290_pre_all_frozen_v282/pre_cohort.csv'
    if integral.is_file():
        st.markdown('### 🧱 Horizontal Level Lab · Inspector unificado')
        mode=st.radio('Universo del inspector',
            ['290 símbolos · análisis integral','15 símbolos · histórico V2.8'],
            horizontal=True,key='hll_unified_universe')
        # Panel adicional CORE15; visible también cuando se inspecciona el universo 290.
        render_hll_phase_batch(root)
        if mode=='290 símbolos · análisis integral':
            st.caption('Corrida congelada de 290 símbolos; todas las estructuras PRE, flip, '
                       'continuación, reclaim y, cuando haya datos 1m, matrices TP/SL. '
                       'Los resultados históricos de 15 símbolos no se mezclan con los nuevos.')
            blind=st.checkbox('Auditoría ciega: ocultar resultados POST',value=False,
                              key='hll_unified_blind')
            from dashboard.views.hll_v282_pre_followup_view import render_hll_v282_pre_followup
            render_hll_v282_pre_followup(root,blind=blind)
            return
    st.markdown('### 🧱 Calidad estructural · V2.8 (histórico 15 símbolos)')
    if not integral.is_file():
        render_hll_phase_batch(root)
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
    st.caption('Las métricas V2.8 de arriba cuentan SOLO los FLIP_CONFIRMED (muestra condicionada). El inspector V2.8.2, si está disponible, incluye TODOS los intentos PRE de V2.6.')
    st.caption('Un valor UNKNOWN significa que faltan 3 pivotes confirmados o contexto completo; no equivale a NO.')
    st.warning('Las agrupaciones son exploratorias. Ago–sep y octubre pertenecen a un período YA inspeccionado; octubre no es una validación fuera de muestra. ' 
               'Las señales comparten símbolos, zonas y condiciones del mercado.')
    st.markdown('#### Comparación de características y resultados')
    blind=st.checkbox('Auditoría ciega: ocultar estadísticas de resultado y velas posteriores',value=True,key='hll28_blind')
    if not blind:
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
        if not blind and p.is_file():
            cw=pd.read_csv(p)
            st.dataframe(cw[cw.factor.eq(factor)&cw.horizon_min.eq(horizon)].head(200),
                         hide_index=True,use_container_width=True)
    # V2.8.2: prefer a full, frozen PRE cohort over the conditioned V2.8.1 flips-only inspector.
    # Does not alter LIVE nor the V2.8/V2.8.1 result files.
    from dashboard.views.hll_v282_pre_followup_view import render_hll_v282_pre_followup
    if render_hll_v282_pre_followup(root, blind=blind):
        return
    st.markdown('#### Inspector de estructura por episodio')
    pattern_runs=root/PATTERN_RUNS
    available_patterns=sorted([p for p in pattern_runs.iterdir() if (p/'taxonomy_entries.csv').is_file() and (p/'manifest.json').is_file()],
                              key=lambda p:p.stat().st_mtime,reverse=True) if pattern_runs.is_dir() else []
    patterns_available=False
    phase='pre'; window=48; taxonomy_field=None
    if available_patterns:
        pat_run=st.selectbox('Clasificador geométrico V2.8.1',available_patterns,format_func=lambda p:p.name,key='hll281_run')
        try:
            tax=pd.read_csv(pat_run/'taxonomy_entries.csv',low_memory=False)
            if tax.duplicated(['attempt_id','entry_type']).any():raise ValueError('IDs duplicados en V2.8.1')
            confirms=confirms.merge(tax.drop(columns=['known_closed_ts','breakout_closed_ts'],errors='ignore'),
                  on=['attempt_id','symbol','entry_type'],how='left',validate='one_to_one')
            patterns_available=confirms.pre_pattern_48.notna().any()
        except (OSError,ValueError,KeyError) as exc:
            st.error(f'No se pudo leer V2.8.1: {exc}')
    else:
        st.info('Para separar estructuras geométricas ejecutá V2.8.1 --run. V2.8 continúa disponible.')
    if patterns_available:
        w1,w2,w3=st.columns([1,1,2])
        with w1:phase=st.selectbox('Estructura conocida en',['Antes del breakout','Al confirmar el flip'],key='hll281_phase')
        phase='pre' if phase=='Antes del breakout' else 'flip'
        with w2:window=st.selectbox('Ventana 30m',[24,48,96],index=1,key='hll281_window')
        taxonomy_field=f'{phase}_pattern_{window}'
        with w3:
            pattern=st.selectbox('Tipo de estructura',['TODAS']+list(PATTERN_LABELS),
                                 format_func=lambda x:'Todas las estructuras' if x=='TODAS' else PATTERN_LABELS[x],
                                 key='hll281_type')
        d1,d2=st.columns(2)
        with d1:defense=st.selectbox('Calidad del retest / defensa',['TODAS']+list(DEFENSE_LABELS),
                                     format_func=lambda x:'Todas las defensas' if x=='TODAS' else DEFENSE_LABELS[x],
                                     key='hll281_defense',disabled=blind and phase=='pre')
        if blind and phase=='pre':defense='TODAS'
        with d2:st.caption('Tipos morfológicos, NO señales rentables. PRE se calcula sin breakout; la defensa solo se conoce al FLIP.')
        if phase=='pre':st.caption('Atención: el universo aquí está condicionado a que luego existió FLIP_CONFIRMED. No es una prueba predictiva sobre TODAS las rupturas.')
        pattern_summary=confirms[taxonomy_field].fillna('UNKNOWN').value_counts().rename_axis('Tipo').reset_index(name='Episodios')
        pattern_summary['Tipo']=pattern_summary['Tipo'].map(PATTERN_LABELS).fillna(pattern_summary['Tipo'])
        with st.expander('Cantidad de casos por estructura',expanded=False):
            st.dataframe(pattern_summary,hide_index=True,use_container_width=True)
        if not blind and (pat_run/'taxonomy_summary.csv').is_file():
            with st.expander('Resultados históricos por tipo (solo análisis POST)',expanded=False):
                results=pd.read_csv(pat_run/'taxonomy_summary.csv')
                rr=results[results.factor.eq(taxonomy_field)].copy()
                if not rr.empty:
                    rr['Tipo']=rr.pattern.map(PATTERN_LABELS).fillna(rr.pattern)
                    st.dataframe(rr[['period','horizon_min','Tipo','n','symbols','symbol_weeks',
                                     'tp_pct','sl_pct','mean_net_pct','equal_symbol_net_pct']].round(4),
                                 hide_index=True,use_container_width=True)
                st.caption('Estadística exploratoria de señales correlacionadas; no hubo validación fuera de muestra.')
    else:pattern='TODAS';defense='TODAS'
    s1,s2=st.columns(2)
    with s1:symbol=st.selectbox('Símbolo',['Todos']+sorted(confirms.symbol.unique()),key='hll28_symbol')
    with s2:hl=st.selectbox('Higher lows al FLIP',['Todos','Sí','No','Desconocido'],key='hll28_hl')
    choices=confirms.copy()
    if patterns_available and pattern!='TODAS':choices=choices[choices[taxonomy_field].eq(pattern)]
    if patterns_available and defense!='TODAS':choices=choices[choices.defense_pattern.eq(defense)]
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
    post=st.checkbox('Mostrar velas posteriores a la entrada (POST)',False,disabled=blind,key='hll28_show_future') if not blind else False
    p=root/CANDLES/f'{row.symbol}_30m.csv'
    if not p.exists():st.warning(f'Falta {p}');return
    try:
        candles=pd.read_csv(p,usecols=['timestamp','open','high','low','close']).sort_values('timestamp').reset_index(drop=True)
        st.plotly_chart(_plot_episode(row,candles,post,lookback=window,
             pivot_field=(f'{phase}{window}_pivot_lows_json' if patterns_available else None)),
             use_container_width=True,key='hll28_chart')
    except (ValueError,KeyError,TypeError) as exc:
        st.error(f'No se pudo dibujar el evento: {exc}');return
    if patterns_available:
        actual=row.get(taxonomy_field,'UNKNOWN')
        st.info('Clasificación automática: '+PATTERN_LABELS.get(actual,str(actual))+
                ' · Defensa: '+DEFENSE_LABELS.get(row.defense_pattern,row.defense_pattern))
        with st.expander('¿Detectó correctamente esta estructura?',expanded=False):
            judgment=st.selectbox('Tu evaluación',['CORRECTA','DUDOSA','INCORRECTA'],key='hll281_judgment')
            proposed=st.selectbox('Tipo visual correcto',['SIN CAMBIO']+list(PATTERN_LABELS),
                 format_func=lambda x:'Sin propuesta' if x=='SIN CAMBIO' else PATTERN_LABELS[x],key='hll281_proposed')
            reason=st.text_input('Motivo breve',key='hll281_reason',placeholder='Ej.: hay mínimos crecientes pero demasiados cruces')
            if 'hll281_reviews' not in st.session_state:st.session_state['hll281_reviews']={}
            if st.button('Registrar revisión en esta sesión',key='hll281_record'):
                st.session_state['hll281_reviews'][f'{row.attempt_id}:{phase}:{window}']={
                    'attempt_id':row.attempt_id,'symbol':row.symbol,'known_closed_ts':int(row.known_closed_ts),
                    'phase':phase,'window':window,'detected_type':actual,'judgment':judgment,
                    'proposed_type':proposed,'reason':reason}
                st.success('Revisión agregada. Exportalas antes de cerrar la sesión.')
            reviews=st.session_state['hll281_reviews']
            st.caption(f'Revisiones registradas en esta sesión: {len(reviews)}')
            if reviews:
                st.download_button('Descargar revisiones CSV',pd.DataFrame(reviews.values()).to_csv(index=False).encode('utf8'),
                   file_name='auditoria_manual_v281.csv',mime='text/csv',key='hll281_review_export')
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
    st.dataframe(pd.DataFrame([{'variable':k,'valor':row[k] if k in row.index else 'NO DISPONIBLE'} for k in known_fields]),hide_index=True,use_container_width=True)
    if post:
        perf=evaluations[evaluations.attempt_id.eq(row.attempt_id)].copy()
        st.markdown('**Resultados V2.7 de este intento, solo después de revelar POST**')
        st.dataframe(perf[['horizon_min','path_state','outcome','return_after_fee_pct',
                           'mfe_to_exit_pct','mae_to_exit_pct']],hide_index=True,use_container_width=True)
    st.caption('Los triángulos muestran el precio del pivote en su vela original, pero solo se dibujan si dos velas posteriores ' 
               'ya lo habían CONFIRMADO al cerrar FLIP_CONFIRMED. El gráfico histórico no debe interpretarse como disponibilidad inmediata en el pivote.')
