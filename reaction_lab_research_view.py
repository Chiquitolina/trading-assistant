"""Read-only cohort-wide research explorer built from scanner/context/path SQLite.

Legacy research engines remain separate.  Context values are frozen as-of known_ts;
followups are later observations, never entry-time features.  This viewer does
not impersonate the historical Fast Candidate portfolio/materialization engine.
"""
from __future__ import annotations
import json
from contextlib import closing
import sqlite3
from pathlib import Path


def _readonly(path):
    return sqlite3.connect(f'file:{Path(path).resolve().as_posix()}?mode=ro',uri=True,timeout=10)


def continuous_research_frame(project, mode='Continuous V1', max_events=15000):
    import pandas as pd
    from reaction_lab_cohort_view import load_reaction_cohort
    root=Path(project)
    cohort=load_reaction_cohort(root,mode=mode)
    rows=cohort['rows'][:max_events]
    if not rows:return pd.DataFrame(),cohort
    ctx_db=root/'reports/reaction_lab/causal_context_v1.sqlite3'
    contexts={};followups={};v1_regimes={}
    if ctx_db.is_file():
        with closing(_readonly(ctx_db)) as con:
            contexts={k:(state,notes,json.loads(payload)) for k,state,notes,payload in
                      con.execute('SELECT event_key,status,notes_json,context_json FROM contexts')}
            followups={k:(state,json.loads(payload)) for k,state,payload in
                       con.execute('SELECT event_key,path_state,followup_json FROM followups')}
            # Optional, append-only canonical V1 enrichment. Existing DBs remain readable.
            tables={name for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'v1_regime_events' in tables:
                v1_regimes={k:(state,json.loads(payload)) for k,state,payload in
                    con.execute('SELECT event_key,status,metrics_json FROM v1_regime_events')}
    all_rows=[]
    protected={'event_key','symbol','signal','known_ts','retest_timestamp','observation_type','cohort'}
    for row in rows:
        record=dict(row);key=row['event_key']
        state,notes,ctx=contexts.get(key,('NO_CONTEXT','[]',{}))
        record['context_state']=state
        record['context_notes']=notes
        record.update({field:value for field,value in ctx.items() if field not in protected})
        regime_status,regime=v1_regimes.get(key,('PENDING',{}))
        record['v1_regime_status']=regime_status
        # Keep precision/lineage of already captured core fields; fill only
        # missing optional V1 metrics. No future outcomes enter causal context.
        for field,value in regime.items():
            if field not in protected and (field not in record or record[field] is None):
                record[field]=value
        if regime_status in ('READY','SECTOR_UNAVAILABLE'):
            record['sector_context_available']=regime_status=='READY'
            record['sector_context_status']='READY' if regime_status=='READY' else 'SECTOR_UNAVAILABLE'
        _state,followup=followups.get(key,(None,{}))
        record.update(followup)
        all_rows.append(record)
    frame=pd.DataFrame(all_rows)
    return frame,cohort


def selected_event_path(project,key):
    from reaction_lab_auto_paths import read_auto_paths_dataframe
    path=Path(project)/'reports/reaction_lab/auto_paths_v1.sqlite3'
    return read_auto_paths_dataframe(path,event_keys={key})


def render_cohort_research(project,cohort_mode,view_name,symbols):
    import pandas as pd
    import streamlit as st
    import plotly.graph_objects as go
    df,cohort=continuous_research_frame(project,cohort_mode)
    st.subheader(f'{view_name} · {cohort_mode}')
    st.caption('Un mismo conjunto de Event IDs; sin mezclar el contexto causal con los resultados posteriores.')
    for error in cohort['errors']:
        st.warning('Cohort audit: '+str(error))
    if df.empty:
        st.info('No hay eventos persistidos en esta cohorte.');return
    col1,col2,col3,col4=st.columns(4)
    col1.metric('REACTIONs',len(df))
    col2.metric('Contexto capturado',int(df['context_state'].isin(['READY','PARTIAL']).sum()))
    col3.metric('Paths completos',int(df['path_state'].eq('COMPLETE').sum()))
    col4.metric('Live observed',int(df['observation_type'].eq('LIVE_OBSERVED').sum()))
    st.caption(f"Régimen V1 canónico: {int(df['v1_regime_status'].eq('READY').sum())} completo · "
               f"{int(df['v1_regime_status'].eq('SECTOR_UNAVAILABLE').sum())} sin sector · "
               f"{int(df['v1_regime_status'].eq('PENDING').sum())} pendiente. "
               'Valores de rango/sector calculados as-of con velas 4h cerradas, no observados en vivo.')
    a,b,c=st.columns(3)
    with a: sel_symbol=st.selectbox('Símbolo',['Todos']+sorted(df['symbol'].dropna().unique().tolist()),key='cohort_global_symbol')
    with b:sel_side=st.selectbox('Dirección',['Todos','LONG','SHORT'],key='cohort_global_side')
    with c:sel_status=st.selectbox('Contexto',['Todos','READY','PARTIAL','NO_CONTEXT','ERROR'],key='cohort_global_context_status')
    if sel_symbol!='Todos':df=df[df['symbol'].eq(sel_symbol)]
    if sel_side!='Todos':df=df[df['signal'].eq(sel_side)]
    if sel_status!='Todos':df=df[df['context_state'].eq(sel_status)]
    if df.empty:
        st.info('Sin coincidencias.');return

    if 'Candidate Research' in view_name:
        st.markdown('#### Candidate V1/V2 — contexto y variantes')
        st.caption('Elegibilidad de investigación basada en campos disponibles; NO constituye ejecución ni portfolio congelado.')
        if 'side_adjusted_strength_vs_btc_4h' in df:
            threshold=st.number_input('Strong Strength 4h ≥ %',value=0.50,step=0.10,key='cohort_strength_threshold')
            strength=pd.to_numeric(df['side_adjusted_strength_vs_btc_4h'],errors='coerce')
            flow=df.get('Market alignment',pd.Series('UNKNOWN',index=df.index)).astype(str)
            variants=pd.DataFrame({'Variant':['Base','Strength > 0','Strong Strength','Strength + Flow'],
                                   'Events':[len(df),int((strength>0).sum()),
                                             int((strength>=threshold).sum()),
                                             int(((strength>0)&flow.eq('TAILWIND')).sum())]})
            st.dataframe(variants,hide_index=True,use_container_width=True)
        else:st.warning('Strength todavía no fue capturada para los eventos seleccionados.')
        for name,col in [('HTF Room','nearest_opposing_room_pct'),('Strength 4h','side_adjusted_strength_vs_btc_4h'),
                         ('Breadth 4h','market_breadth_4h'),('Breadth 1h','market_breadth_1h')]:
            if col in df:
                vals=pd.to_numeric(df[col],errors='coerce')
                if vals.notna().any():st.caption(f'{name}: {vals.notna().sum()} con dato · mediana {vals.median():.3f}')
    elif 'REACTION / Retests' in view_name:
        st.markdown('#### Retests persistidos')
        st.caption('Scanner continuo fijo: 15m 3x3, retest 0.10%, departure 0.20%. Otros detectores siguen siendo estudios independientes.')
    elif 'Pivots / Confirmations' in view_name:
        st.markdown('#### Swing → confirmación → retest')
        st.caption('Timestamps causales congelados por Event ID. No incluye todos los pivots sin retest.')
    elif 'Event swing structure' in view_name:
        st.markdown('#### Estructura y HTF Room causal')
    elif 'Volume' in view_name:
        st.markdown('#### Relación con volumen y RSI')
        st.caption('El universo Volume Exhaustion independiente no se redefine: se estudia su relación con REACTIONs de esta cohorte.')
    else:st.markdown('#### Registro auditado de REACTIONs')

    def columns_for_mode():
        base=['cohort','symbol','signal','retest_timestamp','observation_type','path_state','path_bars','context_state','v1_regime_status']
        if 'Candidate Research' in view_name:
            extra=['nearest_opposing_room_pct','side_adjusted_strength_vs_btc_4h','market_breadth_4h',
                   'relative_volume_4h','return_rank_pct_4h','volume_rank_pct_4h',
                   'sector_strength_vs_btc_4h','sector_relative_volume_4h',
                   'market_breadth_1h','Market alignment','mfe_180m_pct','tp_0_5_sl_3_h180_outcome']
        elif 'Pivots' in view_name: extra=['pivot_timestamp','actionable_timestamp','pivot_price','departure_timestamp','retest_price']
        elif 'Event swing' in view_name: extra=['nearest_opposing_room_pct','opposing_room_30m_pct','opposing_room_1h_pct','opposing_room_4h_pct','htf_confluence_count_0_50']
        elif 'Volume' in view_name: extra=['rel_volume_1m','rel_volume_5m','rel_volume_15m','rsi14_1m','rsi14_15m','aligned_rsi_extreme_count']
        else:extra=['pivot_timestamp','swing_price','max_departure_pct','retest_distance_pct','mfe_180m_pct']
        return [c for c in base+extra if c in df.columns]
    st.dataframe(df[columns_for_mode()].head(2500),hide_index=True,use_container_width=True)
    keys=df['event_key'].astype(str).tolist()
    selected=st.selectbox('Inspeccionar Event ID',keys,key='cohort_global_event_inspector')
    row=df.loc[df['event_key'].eq(selected)].iloc[0]
    with st.expander('Contexto completo, calidad y outcomes',expanded=True):
        value=row.to_dict()
        st.json({str(k):None if not isinstance(v,(list,dict)) and pd.isna(v) else v
                 for k,v in value.items() if not isinstance(v,(list,dict))})
        if row.get('context_state')!='READY':
            st.warning('Contexto no completamente validado. Las métricas ausentes no deben tratarse como ceros.')
    # Exact as-of candle evidence saved at first enrichment, independent of Redis.
    context_db=Path(project)/'reports/reaction_lab/causal_context_v1.sqlite3'
    if context_db.is_file():
        with closing(_readonly(context_db)) as con:
            candle_row=con.execute('SELECT candles_json FROM contexts WHERE event_key=?',(selected,)).fetchone()
        if candle_row:
            recorded=json.loads(candle_row[0])
            available=[tf for tf in ('15m','30m','1h','4h','5m','1m') if recorded.get(tf)]
            if available:
                active_tf=st.selectbox('Velas históricas congeladas · contexto as-of',available,key='cohort_global_htf_chart')
                historic=pd.DataFrame(recorded[active_tf])
                if {'timestamp','open','high','low','close'}.issubset(historic.columns):
                    times=pd.to_datetime(historic['timestamp'],unit='ms',utc=True)
                    chart=go.Figure(data=go.Candlestick(x=times,open=historic['open'],high=historic['high'],
                               low=historic['low'],close=historic['close'],name=active_tf))
                    for label,field in [('Pivot','pivot_timestamp'),('Confirmación','actionable_timestamp'),
                                        ('Retest','retest_timestamp')]:
                        v=pd.to_numeric(row.get(field),errors='coerce')
                        if pd.notna(v):
                            dt=pd.to_datetime(int(v),unit='ms',utc=True)
                            if not historic.empty and int(historic['timestamp'].min())<=int(v)<=int(historic['timestamp'].max())+4*3600000:
                                chart.add_shape(type='line',x0=dt.isoformat(),x1=dt.isoformat(),yref='paper',y0=0,y1=1,line=dict(dash='dot',width=1))
                    chart.update_layout(height=430,xaxis_rangeslider_visible=False)
                    st.plotly_chart(chart,use_container_width=True,key='cohort_global_htf_plot')
                    st.caption(f'Imagen causal: {len(historic)} velas {active_tf}, solo cierres anteriores al conocimiento de la REACTION.')

    path=selected_event_path(project,selected)
    if not path.empty:
        path=path.sort_values('timestamp')
        plot=go.Figure(data=go.Candlestick(x=pd.to_datetime(path['timestamp'],unit='ms',utc=True),
                        open=path['open'],high=path['high'],low=path['low'],close=path['close'],name='Path 1m'))
        plot.update_layout(height=430,xaxis_rangeslider_visible=False)
        st.plotly_chart(plot,use_container_width=True)
        st.caption(f'{len(path)} velas 1m persistidas de este Event ID.')
    else:st.info('Este evento todavía no tiene velas 1m persistidas.')
