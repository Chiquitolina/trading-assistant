"""Read-only visual 1m/5m swing sweep discovery. Does not control LIVE or PM2."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from dashboard.services.micro_flow_v3_multi_session import load_replay_sessions
from research.micro_flow_v3.swing_sweep_discovery import (
    discover, save_immutable_run,
)


def _fmt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%d/%m %H:%M:%S UTC')


def _table(events, reveal):
    out = []
    for e in events:
        row = {
            'Evento':e['event_id'], 'UTC':_fmt(e['sweep']['available_ms']),
            'Swing':e['swing_side'], 'Sesgo hipotético':e['hypothesis_direction'],
            'Nivel':e['level'], 'Vol. relativo':e['aggressive_volume_relative'],
            'Imbalance':e['aggressive_imbalance'],
            'Penetración inicial %':e['initial_sweep_penetration_pct'],
        }
        if reveal:
            row.update({'Penetración máx. posterior %':e['max_penetration_before_reclaim_pct'],
                        'Estado':e['status'],
                        'Recuperación UTC':_fmt(e['reclaim']['available_ms']) if e['reclaim'] else None,
                        'Retest UTC':_fmt(e['retest']['available_ms']) if e['retest'] else None,
                        'Retorno dir. 180s %':e.get('outcome_180s_return_directional_pct')})
        out.append(row)
    return pd.DataFrame(out)


def _inspect_chart(session, result, event, before_minutes, after_minutes, reveal):
    sweep_ms = event['sweep']['available_ms']
    start = sweep_ms - before_minutes * 60_000
    finish = sweep_ms + after_minutes * 60_000 if reveal else sweep_ms
    seg = event['segment_id']
    candles = [c for c in result['candles'] if c['segment_id'] == seg and
               start <= c['timestamp'] <= finish and
               c['available_ms'] <= finish]
    buckets = [b for b in session['buckets'] if b['segment_id'] == seg and
               start <= int(b['second_start_ms']) <= finish and
               int(b['available_wall_ns']) <= finish * 1_000_000]
    if not buckets:
        return None
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=.035,
                        row_heights=[.57,.22,.21],
                        subplot_titles=(f"{session['summary']['symbol']} · velas observadas {event['timeframe_minutes']}m",
                                        'Delta agresivo · ETH · Replay 1s',
                                        'CVD del segmento · ETH'))
    if candles:
        d = pd.to_datetime([c['timestamp'] for c in candles], unit='ms', utc=True)
        fig.add_trace(go.Candlestick(
            x=d, open=[c['open'] for c in candles], high=[c['high'] for c in candles],
            low=[c['low'] for c in candles], close=[c['close'] for c in candles],
            increasing_line_color='#16a34a', decreasing_line_color='#dc2626',
            name='OHLC observado'),row=1,col=1)
    fig.add_hline(y=event['level'],line=dict(color='#f59e0b',dash='dash',width=1.3),row=1,col=1)
    swings = [s for s in result['swings'] if s['segment_id']==seg and
              s['confirmed_available_ms'] <= (finish if reveal else sweep_ms) and
              start <= s['pivot_ms'] <= finish]
    if swings:
        fig.add_trace(go.Scatter(x=pd.to_datetime([s['pivot_ms'] for s in swings],unit='ms',utc=True),
                     y=[s['level'] for s in swings],
                     mode='markers', name='Swings (solo confirmados)',
                     marker=dict(symbol=['triangle-down' if s['side']=='HIGH' else 'triangle-up'
                                         for s in swings],size=9,color='#a78bfa'),
                     customdata=[[s['confirmed_available_ms'],s['side']] for s in swings],
                     hovertemplate='Pivote %{x}<br>Precio %{y}<br>Confirmado (ms) %{customdata[0]}<extra></extra>'),
                     row=1,col=1)
    bdt=pd.to_datetime([b['second_start_ms'] for b in buckets],unit='ms',utc=True)
    fig.add_trace(go.Scatter(x=bdt, y=[float(b['close']) for b in buckets],
                             line=dict(color='#38bdf8',width=1.1),
                             name='Precio 1s observado'),row=1,col=1)
    deltas=[float(b['delta_qty']) for b in buckets]
    fig.add_trace(go.Bar(x=bdt,y=deltas,name='Delta BUY−SELL',
               marker_color=['#16a34a' if v>=0 else '#dc2626' for v in deltas]),row=2,col=1)
    fig.add_trace(go.Scatter(x=bdt,y=[float(b['cvd_qty']) for b in buckets],
               mode='lines',line=dict(color='#f472b6',width=1.3),name='CVD'),row=3,col=1)
    timeline=[('swing_confirm_available_ms','Swing conocido','#a78bfa'),
              ('sweep','Barrido publicado','#f59e0b')]
    if reveal:
        timeline += [('reclaim','Recuperación','#22c55e'),
                     ('departure','Alejamiento','#0ea5e9'),('retest','Retest','#e879f9')]
    for key,label,color in timeline:
        stage = event.get(key)
        stamp = stage if isinstance(stage,int) else (stage.get('available_ms') if stage else None)
        if stamp is None or stamp < start or stamp > finish:
            continue
        x=pd.to_datetime(stamp,unit='ms',utc=True)
        fig.add_vline(x=x,line_color=color,line_dash='dot',line_width=1.3,row=1,col=1)
        fig.add_annotation(x=x,y=1.025,xref='x',yref='paper',text=label,
                  font=dict(color=color,size=10),showarrow=False,
                  bgcolor='rgba(0,0,0,.6)')
    fig.update_layout(height=790,hovermode='x unified',showlegend=True,
                      legend=dict(orientation='h',y=-.1),
                      margin=dict(l=10,r=10,t=85,b=30))
    fig.update_xaxes(rangeslider_visible=False)
    return fig


def _is_closed(path):
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True, timeout=2) as db:
        last=db.execute('SELECT kind,reason FROM tape ORDER BY seq DESC LIMIT 1').fetchone()
        return last == ('MARKER','PROCESS_STOP')


def render_swing_sweep_discovery(root, tapes):
    root=Path(root)
    st.subheader('🧭 Swing Sweep Discovery · 1m / 5m')
    st.caption('Research-only: swings confirmados + barridos + presión AggTrades + '
               'reclaim + alejamiento + retest. No hay entradas ejecutables ni órdenes.')
    if not tapes:
        st.info('No hay tapes V3 para estudiar.'); return
    closed=[]
    for tape in tapes:
        try:
            if _is_closed(tape):
                closed.append(tape)
        except Exception:
            # Zero-byte/truncated/archive invalid explicitly unavailable.
            pass
    if not closed:
        st.warning('No hay tapes completos y cerrados con Replay causal.'); return
    with st.form('mfv3_sweep_form'):
        selected = st.selectbox('Tape cerrado de ETHUSDT', closed,
                               format_func=lambda p:p.name, key='mfv3_sweep_tape')
        a,b,c,d=st.columns(4)
        with a:
            tf=st.selectbox('Estructura', [1,5],format_func=lambda v:f'{v}m',index=0)
        with b:
            swing=st.selectbox('Swing L×R',[2,3,5],index=1,format_func=lambda n:f'{n}x{n}')
        with c:
            age=st.number_input('Edad máxima swing (min)',5,120,30,5)
        with d:
            reclaim=st.number_input('Ventana reclaim (s)',30,900,300,30)
        a,b,c=st.columns(3)
        with a:
            departure=st.number_input('Alejamiento mínimo %',.0,1.0,.03,.01,format='%.3f')
        with b:
            tolerance=st.number_input('Tolerancia retest %',.0,1.0,.02,.01,format='%.3f')
        with c:
            retest=st.number_input('Ventana retest (s)',60,1800,600,60)
        submitted=st.form_submit_button('Descubrir barridos en Replay completo',type='primary')
    settings=(str(selected),int(tf),int(swing),int(age),int(reclaim),float(departure),
              float(tolerance),int(retest))
    if submitted:
        st.session_state.pop('mfv3_sweep_discovery',None)
        try:
            with st.spinner('Verificando SHA256 del Replay y detectando estructuras causales...'):
                session=load_replay_sessions(root,[selected],max_sessions=1)[0]
                result=discover(session['buckets'],timeframe_minutes=tf,
                     swing_bars=swing,max_swing_age_minutes=age,
                     reclaim_seconds=reclaim,departure_pct=departure,
                     retest_tolerance_pct=tolerance,retest_seconds=retest)
                st.session_state['mfv3_sweep_discovery']=(settings,session,result)
        except Exception as exc:
            st.error(f'No fue posible completar el estudio: {exc}'); return
    data=st.session_state.get('mfv3_sweep_discovery')
    if not data:
        st.info('Seleccioná un tape cerrado y ejecutá el detector. Los 1s originales no se modifican.')
        return
    saved_settings,session,result=data
    if saved_settings != settings:
        st.warning('Cambiaron los parámetros o la sesión. Ejecutá nuevamente el análisis.'); return
    s=result['summary']
    columns=st.columns(5)
    for col,label,val in zip(columns,('Velas completas','Swings','Barridos','Recuperaciones','Retests'),
                                      (s['candles'],s['swings'],s['sweeps'],s['reclaimed'],s['retested'])):
        col.metric(label,f'{val:,}')
    st.caption('Cada swing puede producir su primer barrido: los eventos pueden solaparse, NO son trades independientes. '
               'Todos los barridos se conservan aunque NO recuperen el nivel o NO retesteen. '
               'Datos 1s observados, sin rellenar segundos/minutos vacíos; '
               'swings disponibles después del cierre real de la vela confirmadora.')
    if not result['events']:
        st.warning('No hay barridos con esta configuración.'); return
    reveal=st.checkbox('Revelar etapas posteriores y outcomes (modo exploratorio)',
                       value=False,key='mfv3_sweep_future')
    table=_table(result['events'],reveal)
    st.dataframe(table,use_container_width=True,hide_index=True)
    st.download_button('Exportar tabla de barridos (CSV)',table.to_csv(index=False),
                       file_name='swing_sweep_discovery.csv',mime='text/csv')
    ids=[e['event_id'] for e in result['events']]
    key=st.selectbox('Seleccionar barrido para inspeccionar',ids,
                      format_func=lambda k: k.split(':SWEEP:')[-1]+' · '+
                        next(e['hypothesis_direction'] for e in result['events'] if e['event_id']==k)+
                        ' · '+k.split(':')[1],key='mfv3_sweep_selected')
    ev=next(e for e in result['events'] if e['event_id']==key)
    before=st.slider('Minutos anteriores',5,90,20,5,key='mfv3_sweep_before')
    after=st.slider('Minutos posteriores (solo si se revelan)',5,30,15,5,key='mfv3_sweep_after')
    chart=_inspect_chart(session,result,ev,before,after,reveal)
    if chart is not None:
        st.plotly_chart(chart,use_container_width=True)
    st.caption(f"Swing conocido: {_fmt(ev['swing_confirm_available_ms'])} · "
               f"Barrido publicado: {_fmt(ev['sweep']['available_ms'])} · "
               f"Segmento causal: {ev['segment_id']} · "
               'Las líneas de nivel no implican órdenes ejecutadas.')
    if reveal:
        st.json(ev)
        st.warning('Los outcomes posteriores son descriptivos, sin fills, spread ni costos. '
                   'El retest requiere observación posterior al alejamiento.')
    st.divider()
    st.markdown('#### Persistencia reproducible')
    st.caption('Replay SHA256 verificado al cargar; guardar exige verificar también el '
               'digest lógico del SQLite original y genera un directorio nuevo, '
               'sin reemplazar RAW, Replay ni investigaciones anteriores.')
    if st.button('Guardar corrida nueva + hashes con verificación RAW',key='mfv3_sweep_save'):
        try:
            with st.spinner('Verificando digest lógico del tape original y guardando artefactos...'):
                path=save_immutable_run(root,session,result,verify_raw=True)
            st.success(f'Investigación guardada: {path}')
        except Exception as exc:
            st.error(f'No se guardó: {exc}')
