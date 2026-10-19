"""One opt-in multi-session V3 research view; no LIVE/PM2 actions."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from dashboard.services.micro_flow_v3_dashboard_service import V3DashboardError
from dashboard.services.micro_flow_v3_multi_session import (
    analyze_sessions, load_replay_sessions, MAX_SESSIONS,
)


def _closed(path):
    try:
        con = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
        try:
            return con.execute('SELECT kind,reason FROM tape ORDER BY seq DESC LIMIT 1').fetchone() == ('MARKER','PROCESS_STOP')
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return False


def _view_chart(rows, anchors, title, event=None):
    """Chart a selected bounded window; do not connect source segments."""
    frame = pd.DataFrame(rows).copy()
    frame['utc'] = pd.to_datetime(frame['second_start_ms'],unit='ms',utc=True)
    for key in ('close','delta_qty','cvd_qty'):
        frame[key] = pd.to_numeric(frame[key],errors='raise')
    fig = make_subplots(rows=3,cols=1,shared_xaxes=True,vertical_spacing=.04,
                        row_heights=[.52,.24,.24],
                        subplot_titles=('Precio','Delta agresivo (ETH)','CVD (ETH)'))
    for segment, part in frame.groupby('segment_id'):
        fig.add_trace(go.Scatter(x=part['utc'],y=part['close'],mode='lines',
                       name=f'Precio · segmento {segment}'),row=1,col=1)
        fig.add_trace(go.Bar(x=part['utc'],y=part['delta_qty'],
                       name=f'Delta · segmento {segment}'),row=2,col=1)
        fig.add_trace(go.Scatter(x=part['utc'],y=part['cvd_qty'],mode='lines',
                       name=f'CVD · segmento {segment}'),row=3,col=1)
    start,stop = int(frame['second_start_ms'].min()),int(frame['second_start_ms'].max())
    for side,symbol,color in (('BUY_PRESSURE','triangle-up','#16a34a'),
                              ('SELL_PRESSURE','triangle-down','#dc2626')):
        selected = [e for e in anchors if e['side'] == side and
                    start <= e['second_start_ms'] <= stop and
                    e['segment_id'] in frame['segment_id'].unique()]
        if selected:
            fig.add_trace(go.Scatter(
                x=pd.to_datetime([e['second_start_ms'] for e in selected],unit='ms',utc=True),
                y=[e['signal_price'] for e in selected],mode='markers',
                marker=dict(symbol=symbol,color=color,size=10),name=side),row=1,col=1)
    if event is not None:
        for timekey,color,label in (('ready_ms','#f59e0b','Disponible'),
                                    ('entry_ms','#22d3ee','Proxy')):
            value=event.get(timekey)
            if value is not None:
                timestamp = pd.to_datetime(int(value),unit='ms',utc=True)
                fig.add_shape(type='line', x0=timestamp, x1=timestamp, y0=0, y1=1,
                              xref='x', yref='paper',
                              line=dict(color=color,width=1.5,dash='dash'))
                fig.add_annotation(x=timestamp, y=1.03, xref='x', yref='paper',
                                   text=label, showarrow=False, font=dict(color=color,size=10))
    fig.update_layout(title=title,height=620,hovermode='x unified',
                      margin=dict(l=20,r=20,t=65,b=30))
    return fig


def _summary_table(rows):
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.rename(columns={'side':'Señal','horizon_s':'Horizonte (s)',
            'signals':'Episodios ancla','observed':'Observados','censored':'Censurados',
            'continuation':'Continúa','reversal':'Revierte','neutral':'Neutral',
            'continuation_pct':'Continuación %',
            'mean_directional_return_pct':'Retorno dir. medio %'})
    return df


def render_multi_session_research(project_root, tapes):
    root = Path(project_root)
    st.markdown('### 📊 Multi-Session Research · ETHUSDT')
    st.caption('Cada tape V3 cerrado se investiga como segmento independiente. '
               'Nunca se rellenan huecos, se arrastra baseline ni se comparte CVD '
               'entre sesiones. Investigación sin órdenes.')
    candidates = [p for p in tapes[:40] if p.name.startswith('aggtrades_raw_') and _closed(p)]
    if not candidates:
        st.warning('No se encontraron tapes cerrados V3.')
        return
    with st.form('mfv3_multi_form'):
        selected = st.multiselect(
            f'Sesiones cerradas (máximo {MAX_SESSIONS})',candidates,
            default=candidates[:min(2,len(candidates))],
            format_func=lambda p: f'{p.parent.name} · {p.name}',
        )
        st.caption('Se aceptan Replay *_1s.jsonl y *_buckets_1s.jsonl; '
                   'no regeneramos ni sobrescribimos archivos existentes.')
        a,b,c,d = st.columns(4)
        with a:
            lookback = st.number_input('Baseline (buckets)',5,180,30,5)
            relative = st.number_input('Volumen relativo x',1.,20.,2.,.25)
        with b:
            imbalance = st.slider('|Imbalance| mínimo',.50,1.,.70,.05)
            min_trades = st.number_input('Trades mínimos',1,500,3)
        with c:
            cooldown = st.select_slider('Cooldown (s)',options=[0,10,20,30,60,90],value=20)
            episode_seconds = st.select_slider('Episodio (s)',options=[30,60,90,120,180,240,300],value=180)
        with d:
            confirm = st.select_slider('Confirmación (s)',options=[10,20,30,60,90,120],value=30)
            opposite = st.number_input('Opuesto mínimo %',.005,1.,.03,.01,format='%.3f')
        with st.expander('Otros criterios (mismos defaults que V3 individual)',expanded=False):
            max_favorable = st.number_input('Pico favorable máximo %',0.,2.,.10,.025,format='%.3f')
            followthrough = st.number_input('Continuación mínima %',.005,1.,.03,.01,format='%.3f')
            freshness = st.select_slider('Precio fresco (s)',options=[1,2,3,5,10],value=3)
            neutral = st.select_slider('Neutral para outcomes %',options=[0.,.005,.01,.02,.05],value=.01)
        submitted = st.form_submit_button('Analizar sesiones cerradas',type='primary')

    inputs = (tuple(sorted(str(p.resolve()) for p in selected)),int(lookback),float(relative),
              float(imbalance),int(min_trades),int(cooldown),int(episode_seconds),
              int(confirm),float(opposite),float(max_favorable),float(followthrough),
              int(freshness),float(neutral))
    if submitted:
        st.session_state.pop('mfv3_multi_results',None)
        if not 1 <= len(selected) <= MAX_SESSIONS:
            st.error(f'Seleccioná entre 1 y {MAX_SESSIONS} sesiones')
            return
        try:
            with st.spinner('Verificando Replay y reconstruyendo episodios separados...'):
                sessions = load_replay_sessions(root,selected)
                result = analyze_sessions(
                    sessions,lookback=int(lookback),relative_volume=float(relative),
                    min_imbalance=float(imbalance),min_trades=int(min_trades),
                    cooldown_seconds=int(cooldown),episode_seconds=int(episode_seconds),
                    confirmation_seconds=int(confirm),min_opposite_pct=float(opposite),
                    max_favorable_pct=float(max_favorable),
                    min_followthrough_pct=float(followthrough),
                    freshness_seconds=int(freshness),neutral_band_pct=float(neutral))
            fingerprints = tuple((s['tape'],Path(s['replay_path']).stat().st_mtime_ns,
                                  Path(s['replay_path']).stat().st_size) for s in sessions)
            st.session_state['mfv3_multi_results']=(inputs,fingerprints,sessions,result)
        except (OSError, ValueError, sqlite3.Error, V3DashboardError,KeyError) as exc:
            st.error(f'No se procesaron las sesiones: {exc}')
            return

    active = st.session_state.get('mfv3_multi_results')
    if not active:
        st.info('Seleccioná las sesiones y presioná «Analizar sesiones cerradas».')
        return
    previous_inputs, fingerprints, sessions, result = active
    if inputs != previous_inputs:
        st.warning('Cambiaste sesiones o parámetros. Volvé a analizar para actualizar los resultados.')
        return
    if any(not Path(s['replay_path']).is_file() or
           (s['tape'],Path(s['replay_path']).stat().st_mtime_ns,
            Path(s['replay_path']).stat().st_size) != fp
           for s,fp in zip(sessions,fingerprints)):
        st.warning('Cambió un archivo de Replay. Ejecutá nuevamente el análisis.')
        return
    total_buckets = sum(len(s['buckets']) for s in sessions)
    cards = st.columns(4)
    cards[0].metric('Sesiones',len(sessions))
    cards[1].metric('Buckets',f'{total_buckets:,}')
    cards[2].metric('Señales',f"{len(result['events']):,}")
    cards[3].metric('Episodios independientes por ventana',f"{len(result['episodes']):,}")
    st.caption('Independientes por convención temporal de agrupamiento; '
               'NO implica observaciones estadísticamente independientes.')
    st.markdown('#### Diagnóstico por sesión')
    st.dataframe(pd.DataFrame(result['per_session']),use_container_width=True,hide_index=True)
    st.markdown('#### Momentum vs Exhaustion · primeras señales de episodios')
    st.dataframe(_summary_table(result['outcome_anchor']),use_container_width=True,hide_index=True)
    st.markdown('#### Failed Aggression · por estado, lado y horizonte posconfirmación')
    st.dataframe(pd.DataFrame(result['failed_summary']),use_container_width=True,hide_index=True)
    st.caption('Los resultados sin observación suficiente permanecen censurados; '
               'el retorno posterior utiliza una entrada proxy posterior a la decisión. '
               'No hay baseline aleatorio/tendencia ni costos todavía.')
    if st.button('Verificar SHA256 de los SQLite fuente (lectura intensa)',key='mfv3_verify_sources'):
        from research.micro_flow_v3.tape import replay_digest
        with st.spinner('Comprobando hashes de las fuentes cerradas...'):
            for s in sessions:
                rows_n,digest = replay_digest(Path(s['tape_path']))
                matched = (rows_n == s['summary']['source_tape_rows'] and
                           digest == s['summary']['source_tape_digest_sha256'])
                if matched:
                    st.success(f"Origen verificado: {s['tape']}")
                else:
                    st.error(f"Hash incorrecto: {s['tape']} — no utilizar este resultado")
    if result['events']:
        st.markdown('#### Episodios y casos')
        ep_frame = pd.DataFrame(result['episodes'])
        ep_frame['hora_utc'] = pd.to_datetime(ep_frame['anchor_ready_ms'],unit='ms',utc=True)
        ep_cols = ['source_session','hora_utc','episode_side','trigger_count',
                   'buy_triggers','sell_triggers','episode_id']
        st.dataframe(ep_frame[ep_cols].iloc[::-1],use_container_width=True,hide_index=True)
        st.download_button('Exportar episodios (CSV)',ep_frame.to_csv(index=False),
                           file_name='mfv3_multi_episodes.csv',mime='text/csv')
        ev_frame = pd.DataFrame(result['events'])
        st.download_button('Exportar señales (CSV)',ev_frame.to_csv(index=False),
                           file_name='mfv3_multi_signals.csv',mime='text/csv')
        fa_frame = pd.DataFrame(result['failed'])
        st.download_button('Exportar Failed Aggression y censurados (CSV)',
                           fa_frame.to_csv(index=False),
                           file_name='mfv3_multi_failed_aggression.csv',mime='text/csv')
        st.markdown('#### 🔎 Event Inspector multisesión')
        anchor_by_id={e['event_id']:e for e in result['anchors']}
        ids=list(anchor_by_id)
        if ids:
            selected_id = st.selectbox('Episodio ancla',ids[::-1],key='mfv3_multi_event',
                format_func=lambda key: (
                    f"{anchor_by_id[key]['source_session']} · "
                    f"{datetime.fromtimestamp(anchor_by_id[key]['ready_ms']/1000,timezone.utc):%H:%M:%S} UTC · "
                    f"{anchor_by_id[key]['side']}"))
            ev=anchor_by_id[selected_id]
            fa=next((r for r in result['failed'] if r['event_id']==selected_id),None)
            st.caption(f"Fuente: `{ev['source_session']}` · Segmento {ev['segment_id']} "
                       f"· Disponible {_utc(ev['ready_ms'])} · Entrada proxy {_utc(ev.get('entry_ms'))}")
            if fa:
                st.info(f"Evaluación posterior: {fa['state']} · {_utc(fa['decision_ms'])} · "
                        f"proxy {fa.get('entry_proxy') or '—'}")
            nearby = [r for r in result['buckets'] if
                      r['segment_id']==ev['segment_id'] and
                      ev['ready_ms']-60_000 <= int(r['second_start_ms']) <= ev['ready_ms']+360_000]
            if nearby:
                st.plotly_chart(_view_chart(nearby,[ev],f"Inspector · {ev['source_session']}",ev),
                                use_container_width=True)
            st.dataframe(pd.DataFrame([{
                'Horizonte':f'{h}s',
                'Estado':ev.get(f'outcome_state_{h}s'),
                'Retorno dir. %':ev.get(f'return_{h}s_pct'),
                'MFE dir. %':ev.get(f'directional_mfe_{h}s_pct'),
                'MAE dir. %':ev.get(f'directional_mae_{h}s_pct')
            } for h in (30,60,180)]),hide_index=True,use_container_width=True)
            with st.expander('Evento original y confirmación (datos crudos)'):
                st.json(ev)
                if fa: st.json(fa)
    st.caption('Se preservan V1/V2 y PM2; la vista solo lee Replay V3 y SQLite cerrados.')


def _utc(ms):
    if ms is None:
        return '—'
    return datetime.fromtimestamp(int(ms)/1000,timezone.utc).strftime('%d/%m/%Y %H:%M:%S UTC')
