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



# Continuous inspector uses the original, immutable event and OHLC snapshots.
# It does not run Redis backtests or change the Legacy inspector.
def _valid_number(value):
    import math
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def persisted_room_fields(row):
    """Use frozen causally available HTF fields; never recompute from live Redis."""
    price = _valid_number(row.get('retest_close'))
    if price is None:
        price = _valid_number(row.get('retest_price'))
    swing_tf = row.get('nearest_opposing_swing_tf')
    if not isinstance(swing_tf, str) or not swing_tf.strip():
        swing_tf = None
    rsi_ready = all(_valid_number(row.get('rsi14_' + tf)) is not None
                    for tf in ('1m', '5m', '15m', '1h'))
    return {
        'reaction_price': price,
        'room_pct': _valid_number(row.get('nearest_opposing_room_pct')),
        'swing_tf': swing_tf,
        'swing_price': _valid_number(row.get('nearest_opposing_swing_price')),
        'swing_confirmed': _valid_number(row.get('nearest_opposing_swing_confirmed_timestamp')),
        'swing_actionable': _valid_number(row.get('nearest_opposing_swing_actionable_timestamp')),
        'aligned_rsi_count': (_valid_number(row.get('aligned_rsi_extreme_count')) if rsi_ready else None),
    }


def frozen_closed_15m(candles, known_ts, limit):
    """Only snapshot 15m candles that were FULLY closed at event-known time."""
    import pandas as pd
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(candles)
    required = {'timestamp', 'open', 'high', 'low', 'close'}
    if not required.issubset(df.columns):
        return pd.DataFrame()
    for col in required:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    known = _valid_number(known_ts)
    if known is None:
        return pd.DataFrame()
    return (
        df.dropna(subset=list(required))
        .loc[lambda x: x['timestamp'] + 900_000 <= known]
        .sort_values('timestamp')
        .drop_duplicates(subset=['timestamp'], keep='last')
        .tail(max(1, int(limit)))
        .reset_index(drop=True)
    )


def persisted_retest_markers(row):
    """Markers saved by the canonical structural scanner (no synthetic pivots)."""
    result = []
    for label, timestamp_field, price_fields in (
        ('Pivot', 'pivot_timestamp', ('swing_price', 'pivot_price')),
        ('Confirmed', 'actionable_timestamp', ('entry_price', 'swing_price')),
        ('Departure', 'departure_timestamp', ('departure_price',)),
        ('REACTION', 'retest_timestamp', ('retest_price', 'retest_close')),
    ):
        stamp = _valid_number(row.get(timestamp_field))
        price = next((_valid_number(row.get(k)) for k in price_fields
                      if _valid_number(row.get(k)) is not None), None)
        if stamp is not None and price is not None:
            result.append((label, int(stamp), price))
    return result


def render_persisted_retest_inspector(row, recorded, view_name):
    """Show the Legacy-style inspection controls on persisted Continuous events.

    Unlike the live Legacy inspector, the 15m plot contains ONLY the as-of
    snapshot. The post-event 1m path is rendered separately below.
    """
    import pandas as pd
    import plotly.graph_objects as go
    import streamlit as st

    room = persisted_room_fields(row)
    show_room = st.checkbox(
        'Show causal HTF room on chart', value=True,
        key='continuous_retest_show_htf_room',
        help='HTF room frozen at known_ts, not recalculated with future swings.',
    )
    m1, m2, m3, m4 = st.columns(4)
    m1.metric('Detected HTF room', f"{room['room_pct']:.2f}%" if room['room_pct'] is not None else '—')
    m2.metric('Opposing swing TF', str(room['swing_tf']) if room['swing_tf'] else '—')
    m3.metric('Opposing swing price', f"{room['swing_price']:.8g}" if room['swing_price'] is not None else '—')
    m4.metric('Aligned RSI TFs', str(int(room['aligned_rsi_count'])) if room['aligned_rsi_count'] is not None else '—')
    known = _valid_number(row.get('known_ts'))
    # Existing raw fields are historically persisted and can be absent on older events.
    if room['room_pct'] is not None and room['reaction_price'] is not None and room['swing_price'] is not None:
        st.caption(
            f"Causal room snapshot: REACTION {room['reaction_price']:.8g} → "
            f"{room['swing_tf'] or 'HTF'} opposing swing {room['swing_price']:.8g} "
            f"({room['room_pct']:.2f}%). Frozen from already-confirmed HTF structures."
        )
    else:
        st.caption('No opposing HTF swing was available in this frozen context; room is not inferred.')

    count = st.slider(
        '15m candles in inspector', min_value=40, max_value=200, value=80,
        step=10, key='continuous_retest_15m_candle_limit',
        help='Number of PRE-event fully closed 15m candles from the saved snapshot; not a live Redis fetch.',
    )
    history = frozen_closed_15m(recorded.get('15m', []), known, count)
    if history.empty:
        st.warning('No fully closed 15m candles were archived for this Event ID.')
        return
    times = pd.to_datetime(history['timestamp'], unit='ms', utc=True)
    fig = go.Figure(data=[go.Candlestick(
        x=times, open=history['open'], high=history['high'],
        low=history['low'], close=history['close'], name='15m (as-of)',
    )])
    pivot_price = _valid_number(row.get('swing_price'))
    if pivot_price is not None:
        fig.add_hline(y=pivot_price, line_dash='dash', annotation_text='15m swing')

    markers = persisted_retest_markers(row)
    left_ms = int(history['timestamp'].min())
    last_closed_ms = int(history['timestamp'].max()) + 900_000
    retest_ms = int(_valid_number(row.get('retest_timestamp')) or last_closed_ms)
    # The retest may fall in a 15m candle not yet fully closed at known_ts.
    # Keep its marker visible to the right without fabricating that candle.
    right_ms = max(last_closed_ms, retest_ms + 900_000)
    marker_shapes = {'Pivot':'triangle-up' if row.get('signal')=='LONG' else 'triangle-down',
                     'Confirmed':'circle-open','Departure':'circle','REACTION':'diamond'}
    for label, ts, price in markers:
        if left_ms <= ts <= right_ms:
            fig.add_trace(go.Scatter(
                x=[pd.to_datetime(ts, unit='ms', utc=True)], y=[price],
                mode='markers+text', text=[label], textposition='top center',
                marker={'symbol':marker_shapes[label], 'size':11}, name=label,
            ))

    if show_room and room['room_pct'] is not None and room['reaction_price'] is not None and room['swing_price'] is not None:
        side = str(row.get('signal', '')).upper()
        verified = room['swing_actionable']
        if side in {'LONG','SHORT'} and (known is None or verified is None or verified <= known):
            end_ms = retest_ms + 3_600_000  # Visual projection, NOT future candles
            fig.add_shape(
                type='rect', x0=pd.to_datetime(retest_ms, unit='ms', utc=True),
                x1=pd.to_datetime(end_ms, unit='ms', utc=True),
                y0=min(room['reaction_price'], room['swing_price']),
                y1=max(room['reaction_price'], room['swing_price']),
                fillcolor='rgba(34,197,94,0.15)' if side=='LONG' else 'rgba(239,68,68,0.15)',
                line={'width':0}, layer='below',
            )
            fig.add_hline(y=room['swing_price'], line_dash='dot',
                          annotation_text=f"HTF target {room['swing_tf'] or ''} · {room['room_pct']:.2f}%")
            right_ms=max(right_ms,end_ms)
        elif verified is not None and known is not None and verified > known:
            st.warning('Stored HTF swing confirmation is later than known_ts: band hidden for causal safety.')
    fig.update_layout(
        height=600, xaxis_rangeslider_visible=False, hovermode='x unified',
        title=f"{row.get('symbol','')} · 15m confirmed swing retest · {row.get('signal','')} · {len(history)} × 15m",
    )
    fig.update_xaxes(range=[pd.to_datetime(left_ms,unit='ms',utc=True),
                            pd.to_datetime(right_ms,unit='ms',utc=True)])
    st.caption(
        f"Historical inspector: {len(history)} fully closed 15m candles archived as-of the signal "
        '(requested %d). Markers retain pivot, confirmation, departure and retest times. '
        'Blank space to the right represents no archived 15m candle; see the separate 1m path for outcomes.' % count
    )
    st.plotly_chart(fig, use_container_width=True, key='continuous_retest_detail_chart',
                    config={'displaylogo':False,'scrollZoom':True})


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
    is_retest_view = 'REACTION / Retests' in view_name
    if is_retest_view:
        # Same inspector controls as Legacy, but based on the persisted cohort.
        options_by_key = df.set_index('event_key', drop=False)
        def _label(key):
            event = options_by_key.loc[key]
            run = _valid_number(event.get('mfe_180m_pct'))
            run_text = f" · MFE +{run:.2f}%" if run is not None else ''
            swing = _valid_number(event.get('swing_price'))
            swing_text = f" · swing {swing:.8g}" if swing is not None else ''
            timestamp = _valid_number(event.get('retest_timestamp'))
            date_text = (pd.to_datetime(int(timestamp), unit='ms', utc=True)
                         .strftime('%Y-%m-%d %H:%M UTC') if timestamp is not None else key)
            return (f"{event.get('symbol', '?')} · 15m 3x3 · "
                    f"{event.get('signal', '?')} · REACTION{run_text}"
                    f"{swing_text} · {date_text}")
        selected = st.selectbox('Inspect confirmed swing retest', keys,
                                format_func=_label, key='cohort_global_event_inspector')
    else:
        selected = st.selectbox('Inspeccionar Event ID', keys,
                                key='cohort_global_event_inspector')
    row=df.loc[df['event_key'].eq(selected)].iloc[0]
    context_db=Path(project)/'reports/reaction_lab/causal_context_v1.sqlite3'
    recorded={}
    if context_db.is_file():
        with closing(_readonly(context_db)) as con:
            candle_row=con.execute('SELECT candles_json FROM contexts WHERE event_key=?',(selected,)).fetchone()
        if candle_row:
            recorded=json.loads(candle_row[0])
    if is_retest_view:
        render_persisted_retest_inspector(row.to_dict(), recorded, view_name)
    with st.expander('Contexto completo, calidad y outcomes',expanded=not is_retest_view):
        value=row.to_dict()
        st.json({str(k):None if not isinstance(v,(list,dict)) and pd.isna(v) else v
                 for k,v in value.items() if not isinstance(v,(list,dict))})
        if row.get('context_state')!='READY':
            st.warning('Contexto no completamente validado. Las métricas ausentes no deben tratarse como ceros.')
    # Exact as-of HTF evidence, independent of Redis. 15m uses the
    # dedicated inspector above in REACTION / Retests; other modes unchanged.
    if recorded:
            available=[tf for tf in ('15m','30m','1h','4h','5m','1m')
                       if recorded.get(tf) and not (is_retest_view and tf=='15m')]
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
