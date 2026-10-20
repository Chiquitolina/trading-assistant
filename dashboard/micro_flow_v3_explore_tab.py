"""Focused Micro Flow V3 visual discovery workspace. Read-only / research-only."""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from dashboard.services.micro_flow_v3_dashboard_service import list_v3_tapes
from dashboard.services.micro_flow_v3_multi_session import (
    find_replay_for_closed_tape, load_replay_sessions,
)
from dashboard.services.micro_flow_v3_explore_service import (
    FOCUS_OPTIONS, build_explore_catalog, filter_explore_events, observed_view,
)


def _utc(ms):
    return datetime.fromtimestamp(int(ms)/1000, timezone.utc).strftime('%d/%m %H:%M:%S UTC')


def _event_name(event):
    t = _utc(event['available_ms'])
    return f"{t} · {event['pressure_side']} · {event['label']} · {event['event_id']}"


def _chart(session, study, ev, view, *, show_swings=True, show_other_events=False):
    candles, buckets = view['candles'], view['buckets']
    if not buckets:
        return None
    fig = make_subplots(rows=4, cols=1, shared_xaxes=True, vertical_spacing=.028,
                        row_heights=[.54,.18,.14,.14],
                        subplot_titles=('Precio · OHLC observado', 'Volumen ejecutado BUY / SELL',
                                        'Delta ejecutado · 1s', 'CVD observado'))
    if candles:
        x = pd.to_datetime([c['timestamp'] for c in candles], unit='ms', utc=True)
        fig.add_trace(go.Candlestick(x=x,
                       open=[c['open'] for c in candles], high=[c['high'] for c in candles],
                       low=[c['low'] for c in candles], close=[c['close'] for c in candles],
                       name='Velas observadas', increasing_line_color='#16a34a',
                       decreasing_line_color='#dc2626'),row=1,col=1)
        fig.add_trace(go.Bar(x=x, y=[c['buy_qty'] for c in candles],
                       name='Volumen BUY', marker_color='#16a34a', opacity=.8),row=2,col=1)
        fig.add_trace(go.Bar(x=x, y=[c['sell_qty'] for c in candles],
                       name='Volumen SELL', marker_color='#dc2626', opacity=.8),row=2,col=1)
    ticks = pd.to_datetime([b['second_start_ms'] for b in buckets], unit='ms', utc=True)
    fig.add_trace(go.Scatter(x=ticks,y=[b['close'] for b in buckets],
                        name='Precio 1s observado',line=dict(color='#38bdf8',width=1)),row=1,col=1)
    fig.add_trace(go.Bar(x=ticks,y=[b['delta_qty'] for b in buckets],
                        name='Delta BUY−SELL', marker_color=[
                            '#16a34a' if float(b['delta_qty'])>=0 else '#dc2626'
                            for b in buckets]),row=3,col=1)
    fig.add_trace(go.Scatter(x=ticks,y=[b['cvd_qty'] for b in buckets],
                        name='CVD',line=dict(color='#f472b6',width=1.4)),row=4,col=1)
    segment=ev['segment_id']
    if show_swings:
        # Only levels confirmed as-of the selected event, not hindsight swings.
        eligible=[s for s in study['swings'] if s['segment_id']==segment
                  and s['confirmed_available_ms']<=ev['available_ms']]
        for side,color in (('LOW','#22c55e'),('HIGH','#f59e0b')):
            recent=[s for s in eligible if s['side']==side][-2:]
            for swing in recent:
                left=max(view['start_ms'],swing['confirmed_available_ms'])
                right=view['cutoff_ms']
                if left>right:
                    continue
                fig.add_trace(go.Scatter(
                    x=pd.to_datetime([left,right],unit='ms',utc=True),
                    y=[swing['level']]*2,mode='lines',
                    line=dict(color=color,dash='dash',width=1),
                    name=f'Swing {side} conocido · {swing["level"]:.2f}',
                    hovertemplate=(f'Swing {side} · {swing["level"]:.2f}'
                                   f'<br>Conocido {_utc(swing["confirmed_available_ms"])}'
                                   '<extra></extra>')),row=1,col=1)
    if show_other_events:
        nearby=[e for e in study['events'] if e['segment_id']==segment
                and view['start_ms']<=e['available_ms']<=view['cutoff_ms']
                and e['event_id']!=ev['event_id']]
        for e in nearby[:90]:
            t=pd.to_datetime(e['available_ms'],unit='ms',utc=True)
            fig.add_vline(x=t,line_dash='dot',line_width=.6,
                     line_color='#9ca3af' if e['kind']=='AE' else '#c084fc',row=1,col=1)
    t=pd.to_datetime(ev['available_ms'],unit='ms',utc=True)
    fig.add_vline(x=t,line_color='#f59e0b',line_width=2,line_dash='dot',row=1,col=1)
    fig.add_annotation(x=t,y=1.02,yref='paper',text='Evento disponible',
                       showarrow=False,font=dict(color='#f59e0b',size=11),
                       bgcolor='rgba(0,0,0,.6)')
    fig.update_layout(height=850,hovermode='x unified',barmode='stack',
                      legend=dict(orientation='h',y=-.085),
                      margin=dict(l=8,r=8,t=70,b=22))
    fig.update_xaxes(rangeslider_visible=False)
    return fig


def render_micro_flow_v3_explore(root):
    root=Path(root)
    st.subheader('🔎 Explore · Aggressive Exhaustion')
    st.caption('Una vista del precio y del flujo, sincronizados. La hipótesis prioritaria es '
               'agresión extrema con discrepancia flujo/precio y posible rechazo. '
               'Research-only: ninguna marca es una orden ni un fill.')
    tapes=list_v3_tapes(root)
    closed=[]
    for path in tapes:
        # A broken/truncated source is not considered a valid input.
        try:
            if find_replay_for_closed_tape(root,path) is not None:
                closed.append(path)
        except (ValueError,OSError,KeyError,TypeError,sqlite3.Error):
            continue
    if not closed:
        st.warning('No hay tapes cerrados con Replay reconocido. '
                   'La captura activa y los tapes dañados no se leen desde Explore.')
        return
    with st.form('mfv3_explore_config'):
        a,b,c,d=st.columns([3,1,1,1])
        with a:
            tape=st.selectbox('Sesión RAW cerrada',closed,
                              format_func=lambda p:p.name,key='mfv3_explore_tape')
        with b:
            tf=st.selectbox('Velas',[1,5],format_func=lambda n:f'{n}m',
                            key='mfv3_explore_tf')
        with c:
            feature_window=st.selectbox('Features',[5,15,30,60],index=1,
                                        format_func=lambda n:f'{n}s',
                                        key='mfv3_explore_feature_window')
        with d:
            swing=st.selectbox('Swing',[2,3,5],index=1,
                               format_func=lambda n:f'{n}x{n}',
                               key='mfv3_explore_swing')
        load=st.form_submit_button('Cargar investigación causal',type='primary')
    try:
        # A replaced/truncated source or Replay invalidates any cached workspace.
        replay_pair = find_replay_for_closed_tape(root, tape)
        if replay_pair is None:
            st.error('El tape dejó de estar cerrado o su Replay no es reconocido.')
            st.session_state.pop('mfv3_explore_run', None)
            return
        replay_path = Path(replay_pair[0])
        tape_stat, replay_stat = tape.stat(), replay_path.stat()
        identity=(str(tape.resolve()),tape_stat.st_mtime_ns,tape_stat.st_size,
                  str(replay_path.resolve()),replay_stat.st_mtime_ns,replay_stat.st_size,
                  int(tf),int(feature_window),int(swing))
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error):
        st.error('La sesión o el Replay dejaron de estar disponibles.')
        st.session_state.pop('mfv3_explore_run', None)
        return
    if load:
        try:
            with st.spinner('Verificando Replay y preparando velas, impulsos y features...'):
                session=load_replay_sessions(root,[tape],max_sessions=1)[0]
                study=build_explore_catalog(session['buckets'],
                         timeframe_minutes=tf,feature_window_seconds=feature_window,
                         swing_bars=swing)
            st.session_state['mfv3_explore_run']=(identity,session,study)
        except (ValueError,KeyError,OSError,TypeError,RuntimeError,sqlite3.Error) as exc:
            st.error(f'No se pudo cargar Explore: {exc}')
            return
    loaded=st.session_state.get('mfv3_explore_run')
    if not loaded or loaded[0]!=identity:
        st.info('Seleccioná una sesión y presioná **Cargar investigación causal**. '
                'No se ejecutan detectores automáticamente cada vez que cambiás un control.')
        return
    _,session,study=loaded
    st.caption(f"Replay SHA256 comprobado al cargar · {len(session['buckets']):,} buckets observados · "
               f"{len(study['candles']):,} velas {tf}m · segmento(s) conservados · "
               'el digest lógico completo del RAW se verifica en Data & Replay Quality.')
    a,b,c,d=st.columns(4)
    ae=study['aggression']['events']
    a.metric('Impulsos BUY/SELL',len(ae))
    b.metric('Divergencias',sum(e['label']=='Divergencia flujo/precio' for e in study['events']))
    c.metric('Rechazos observables',sum(e['rejection'] for e in study['events']))
    d.metric('Volume Exhaustion',len(study['ve']['events']))
    first,second=st.columns([2,1])
    with first:
        focus=st.selectbox('Qué queremos inspeccionar',FOCUS_OPTIONS,
                           index=0,key='mfv3_explore_focus')
    with second:
        pressure=st.selectbox('Presión',['Ambas','BUY','SELL'],
                              key='mfv3_explore_pressure')
    selected_events=filter_explore_events(study['events'],focus=focus,pressure=pressure)
    if not selected_events:
        st.info('No hay eventos en este grupo. Podés cambiar el foco o la dirección; '
                'no se recalculan los detectores.')
        return
    table=pd.DataFrame([{
        'Evento UTC':_utc(e['available_ms']), 'Origen':e['kind'],
        'Presión':e['pressure_side'],'Hipótesis':e['hypothesis_direction'],
        'Característica':e['label'], 'Vol. relativo':e['relative_volume'],
        'Imbalance':e['imbalance'], 'Movimiento %':e['signed_move_pct'],
        'Rechazo':e['rejection'],'Cobertura':e['coverage'], 'ID':e['event_id'],
    } for e in selected_events])
    st.dataframe(table,use_container_width=True,hide_index=True,height=230)
    st.download_button('Exportar catálogo visible (sin outcomes)',table.to_csv(index=False),
                       file_name='micro_flow_v3_explore_catalog.csv',mime='text/csv',
                       key='mfv3_explore_csv')
    options={e['event_id']:e for e in selected_events}
    chosen_id=st.selectbox('Evento para inspeccionar',list(options),
                        format_func=lambda e:_event_name(options[e]),
                        key='mfv3_explore_event')
    chosen=options[chosen_id]
    a,b,c,d=st.columns([1,1,1,1])
    with a:
        reveal=st.checkbox('Revelar futuro',value=False,key='mfv3_explore_reveal')
    with b:
        before=st.select_slider('Minutos previos',[5,10,20,30,45,60],value=20,
                                 key='mfv3_explore_before')
    with c:
        after=st.select_slider('Minutos posteriores',[5,10,15,30,45,60],value=15,
                               disabled=not reveal,key='mfv3_explore_after')
    with d:
        show_other=st.checkbox('Otros eventos',value=False,key='mfv3_explore_overlay')
    view=observed_view(session['buckets'],study['candles'],segment_id=chosen['segment_id'],
                       anchor_ms=chosen['available_ms'],before_minutes=before,
                       after_minutes=after,reveal=reveal)
    fig=_chart(session,study,chosen,view,show_other_events=show_other)
    if fig is not None:
        st.plotly_chart(fig,use_container_width=True,key='mfv3_explore_main_chart')
    else:
        st.warning('Esta ventana no contiene buckets publicados. Probá otra ventana.')
    st.caption(f"Evento conocido {_utc(chosen['available_ms'])} · segmento {chosen['segment_id']} · "
               f"modo {'retrospectivo, futuro revelado' if reveal else 'as-of, futuro oculto'}. "
               'Velas y marcadores solo se dibujan cuando quedaron publicados.')
    x,y,z=st.columns(3)
    x.metric('Volumen relativo',f"{chosen['relative_volume']:.2f}x")
    y.metric('Desplazamiento %',
             f"{chosen['signed_move_pct']:+.4f}%" if chosen['signed_move_pct'] is not None else 'N/D')
    z.metric('Rechazo observable','Sí' if chosen['rejection'] else 'No / no evaluado')
    if chosen['kind']=='AE':
        f=chosen['feature']
        with st.expander('Detalle microestructural as-of · sin outcomes',expanded=False):
            st.json(f)
    else:
        st.caption('Volume Exhaustion es un detector experimental de RSI/volumen '
                   'derivado de velas observadas: no está validada su paridad con el collector legado.')
    if reveal:
        st.info('El futuro se muestra para investigación visual, no para seleccionar '
                'eventos ganadores ni simular un fill. Los outcomes causales y sus censuras '
                'se analizan en Strategy Research.')
    st.caption('Explore no guarda ni modifica tapes, Replay, resultados ni parámetros '
               'congelados de las investigaciones anteriores.')
