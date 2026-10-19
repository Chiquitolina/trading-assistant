"""Micro Flow V3-only Streamlit interface. No Legacy V1/V2 data is queried."""
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from dashboard.services.micro_flow_v3_dashboard_service import (
    V3DashboardError, list_v3_tapes, inspect_v3_tape, existing_replay,
    replay_paths, load_buckets_jsonl,
)
from dashboard.services.micro_flow_v3_research import detect_pressure_events


def _display_ts(ms):
    if not ms:
        return '—'
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%d/%m/%Y %H:%M:%S UTC')


@st.cache_data(ttl=12, show_spinner=False)
def _inspect_cached(path, mtime_ns, size):
    return inspect_v3_tape(path)


@st.cache_data(ttl=20, show_spinner=False)
def _replay_cached(path, mtime_ns, size):
    return load_buckets_jsonl(path)


def _details(path):
    stat = path.stat()
    return _inspect_cached(str(path), stat.st_mtime_ns, stat.st_size)


def _is_closed_fast(path):
    # No full count for every six-hour archive; only inspect the final row.
    import sqlite3
    con = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        last = con.execute('SELECT kind,reason FROM tape ORDER BY seq DESC LIMIT 1').fetchone()
        return bool(last and last == ('MARKER', 'PROCESS_STOP'))
    finally:
        con.close()


def _chart(frame, events, title):
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=.04,
                        row_heights=[.53,.24,.23],
                        subplot_titles=('Precio observado', 'Delta agresivo (ETH)', 'CVD (ETH)'))
    frame = frame.copy()
    frame['dt'] = pd.to_datetime(frame['second_start_ms'], unit='ms', utc=True)
    for seg, part in frame.groupby('segment_id', sort=True):
        fig.add_trace(go.Scatter(x=part['dt'], y=part['close'], mode='lines',
                                 name=f'Precio · tramo {seg}', line=dict(width=1.7)), row=1, col=1)
        fig.add_trace(go.Bar(x=part['dt'], y=part['delta_qty'],
                             name=f'Delta · tramo {seg}',
                             marker_color=['#16a34a' if v >= 0 else '#dc2626'
                                           for v in part['delta_qty']]), row=2, col=1)
        fig.add_trace(go.Scatter(x=part['dt'], y=part['cvd_qty'], mode='lines',
                                 name=f'CVD · tramo {seg}', line=dict(width=1.3)), row=3, col=1)
    if events:
        for side, color, sym in [('BUY_PRESSURE','#16a34a','triangle-up'),
                                 ('SELL_PRESSURE','#dc2626','triangle-down')]:
            selected = [e for e in events if e['side'] == side and
                        int(frame['second_start_ms'].min()) <= e['second_start_ms'] <=
                        int(frame['second_start_ms'].max())]
            if selected:
                fig.add_trace(go.Scatter(
                    x=pd.to_datetime([e['second_start_ms'] for e in selected], unit='ms', utc=True),
                    y=[e['signal_price'] for e in selected], mode='markers', name=side,
                    marker=dict(color=color, symbol=sym, size=9),
                    customdata=[[e['relative_volume'],e['imbalance']] for e in selected],
                    hovertemplate='%{x}<br>Precio %{y:.2f}<br>Vol x%{customdata[0]:.2f}'
                                  '<br>Imbalance %{customdata[1]:.2f}<extra></extra>'), row=1, col=1)
    fig.update_layout(title=title, height=680, hovermode='x unified',
                      barmode='relative', showlegend=True,
                      legend=dict(orientation='h',y=-.10), margin=dict(l=20,r=20,t=65,b=40))
    fig.update_xaxes(rangeslider_visible=False)
    return fig


def render_micro_flow_v3_research(project_root):
    root = Path(project_root)
    st.markdown('## ⚡ Micro Flow V3 · Research Lab')
    st.caption('Fuente exclusiva: AggTrades de V3 en SQLite y Replay causal 1s. '
               'V1/V2 no participan. Investigación sin órdenes.')
    tapes = list_v3_tapes(root)
    if not tapes:
        st.info('No se encontraron tapes V3 en reports/micro_flow_v3/tapes/. '
                'Revisá el servicio micro-flow-v3-ethusdt en PM2.')
        return
    monitor_tab, replay_tab = st.tabs(['📡 Recorder V3', '🧪 Replay & presión agresiva'])
    with monitor_tab:
        newest = tapes[0]
        try:
            info = _details(newest)
        except Exception as exc:
            st.error(f'No se pudo leer el tape V3: {exc}')
        else:
            freshness_ns = info['last_trade_wall_ns'] or info['mtime_ns']
            age = max(0, (datetime.now(timezone.utc).timestamp() * 1e9 - freshness_ns) / 1e9)
            st.caption(f'Último tape observado: `{newest.name}` · último trade recibido hace {age:.0f}s')
            if info['closed']:
                st.info('Último tape cerrado. Consultá PM2 si esperabas una sesión nueva.')
            elif age > 120:
                st.warning('Tape abierto pero sin modificaciones recientes (>120s). '
                           'Puede estar detenido; verificar PM2.')
            else:
                st.info('Tape sin marcador de cierre y con actualización reciente. '
                        'No equivale a confirmar el estado de PM2.')
            m1, m2, m3, m4 = st.columns(4)
            m1.metric('AggTrades persistidos', f"{info['trades']:,}")
            m2.metric('Anomalías de trades', f"{info['anomalies']:,}")
            m3.metric('Saltos de ID detectados', f"{info['id_jumps']:,}")
            m4.metric('Tamaño SQLite', f"{info['size_bytes']/1024**2:.1f} MB")
            st.caption(f"Último AggTrade: {_display_ts((info['last_trade_wall_ns'] or 0)//1_000_000)} "
                       f"· generación observada {info['last_connection'] or '—'} "
                       '· métricas obtenidas por lectura SQLite, no mediante PM2.')
            with st.expander('Marcadores y reconexiones del tape', expanded=False):
                st.dataframe(pd.DataFrame(info['recent_markers']),
                             use_container_width=True, hide_index=True)
            st.markdown('#### Últimas sesiones')
            st.dataframe(pd.DataFrame([{
                'Archivo': p.name, 'Modificación UTC': datetime.fromtimestamp(p.stat().st_mtime, timezone.utc),
                'MB': round(p.stat().st_size / 1024**2, 2),
            } for p in tapes[:30]]), use_container_width=True, hide_index=True)
            if st.button('Actualizar datos del recorder', key='mfv3_refresh'):
                _inspect_cached.clear()
                st.rerun()
    with replay_tab:
        closed = []
        for p in tapes:
            try:
                if _is_closed_fast(p):
                    closed.append(p)
            except Exception:
                continue
        if not closed:
            st.warning('Todavía no hay sesiones V3 cerradas. '
                       'El Replay nunca procesa SQLite activos.')
            return
        default_idx = next((i for i,p in enumerate(closed)
                            if p.name == 'aggtrades_raw_10m_02.sqlite3'), 0)
        selected = st.selectbox('Sesión cerrada', closed, index=default_idx,
                                format_func=lambda p: f'{p.parent.name} · {p.name}',
                                key='mfv3_closed_tape')
        try:
            info = _details(selected)
        except Exception as exc:
            st.error(f'No se pudo inspeccionar este tape: {exc}')
            return
        if not info['closed']:
            st.warning('El tape seleccionado dejó de parecer cerrado. No se genera Replay.')
            return
        st.caption(f"{info['trades']:,} AggTrades · {info['id_jumps']} saltos de ID · "
                   f"{info['anomalies']} anomalías · {_display_ts((info['last_trade_wall_ns'] or 0)//1_000_000)}")
        result = existing_replay(root, selected, info)
        if result is None:
            out_path, summary_path = replay_paths(root, selected)
            st.info('No hay Replay compatible guardado para esta sesión. '
                    'Podés generarlo sin modificar el tape.')
            if st.button('Generar Replay causal 1s', key='mfv3_generate_replay'):
                try:
                    from research.micro_flow_v3.replay import replay_tape
                    with st.spinner('Reproduciendo AggTrades del tape cerrado...'):
                        replay_tape(selected, out_path, summary_path=summary_path)
                    _replay_cached.clear()
                    st.rerun()
                except Exception as exc:
                    st.error(f'Replay no generado: {exc}')
            return
        out_path, summary_path, summary = result
        st.caption('Replay asociado inicialmente por nombre, símbolo y cantidad de filas. '
                   'Para verificar la identidad exacta del origen, usar el botón SHA256.')
        if st.button('Verificar origen del Replay (SHA256)', key='mfv3_check_source_hash'):
            try:
                from research.micro_flow_v3.tape import replay_digest
                with st.spinner('Calculando digest del tape cerrado; puede tardar...'):
                    rows_n, digest = replay_digest(selected)
                if (rows_n == summary['source_tape_rows'] and
                        digest == summary['source_tape_digest_sha256']):
                    st.success('SHA256 del origen coincide con el Replay.')
                else:
                    st.error('El digest del tape no coincide: descartar este Replay y auditar.')
                    return
            except Exception as exc:
                st.error(f'No se pudo verificar SHA256: {exc}')
        try:
            stat = out_path.stat()
            buckets = _replay_cached(str(out_path), stat.st_mtime_ns, stat.st_size)
        except (OSError, ValueError, V3DashboardError) as exc:
            st.error(f'No se pudo cargar el Replay: {exc}')
            return
        if not buckets:
            st.warning('Replay sin buckets completos. No se generan eventos.')
            return
        frame = pd.DataFrame(buckets)
        for field in ('open','high','low','close','buy_qty','sell_qty','delta_qty','cvd_qty'):
            frame[field] = pd.to_numeric(frame[field], errors='raise')
        x1,x2,x3,x4 = st.columns(4)
        x1.metric('Buckets observados', f'{len(frame):,}')
        x2.metric('Tramos causales', f"{len(summary.get('segments', [])):,}")
        x3.metric('Trades en buckets', f"{int(frame['trades'].sum()):,}")
        x4.metric('Saltos del tape', f"{info['id_jumps']:,}")
        st.markdown('#### Experimento · presión agresiva')
        a,b,c,d = st.columns(4)
        with a:
            lookback = st.number_input('Baseline: buckets previos', min_value=5,
                                       max_value=180, value=30, step=5, key='mfv3_lb')
        with b:
            relative = st.number_input('Volumen relativo mínimo (x)', min_value=1.0,
                                        max_value=20.0, value=2.0, step=.25,
                                        key='mfv3_relvol')
        with c:
            imbalance = st.slider('|Imbalance| mínimo', 0.5, 1.0, .70, .05,
                                   key='mfv3_imbalance')
        with d:
            min_trades = st.number_input('AggTrades mínimos/bucket', min_value=1,
                                         max_value=500, value=3, key='mfv3_mtrades')
        cooldown = st.slider('Cooldown entre eventos (s)', 0, 180, 20, 5,
                              key='mfv3_cooldown')
        events = detect_pressure_events(buckets, lookback=int(lookback),
                                        relative_volume=float(relative),
                                        min_imbalance=float(imbalance),
                                        min_trades=int(min_trades),
                                        cooldown_seconds=int(cooldown))
        event_frame = pd.DataFrame(events)
        k1,k2,k3,k4 = st.columns(4)
        k1.metric('Eventos detectados', f'{len(events):,}')
        if not event_frame.empty and 'return_60s_pct' in event_frame:
            valid = pd.to_numeric(event_frame['return_60s_pct'], errors='coerce').dropna()
        else:
            valid = pd.Series(dtype='float64')
        k2.metric('Outcomes válidos · 60s', f'{len(valid):,}')
        k3.metric('Retorno direccional medio · 60s',
                  f'{valid.mean():+.3f}%' if len(valid) else '—')
        k4.metric('Continuación favorable · 60s',
                  f'{(valid > 0).mean()*100:.1f}%' if len(valid) else '—')
        st.caption('Exploratorio: evento causal a `ready_ms`, entrada proxy al primer '
                   'bucket posterior; retornos/máximos/mínimos observados, no fills '
                   'ni PnL. Los horizontes sin datos se censuran. Eventos solapados.')
        duration = st.select_slider('Ventana de gráfico (últimos minutos)',
                                    options=[5,10,30,60,120,360], value=30,
                                    key='mfv3_window')
        start_ms = int(frame['second_start_ms'].max()) - int(duration)*60_000
        chart_frame = frame[frame['second_start_ms'] >= start_ms].copy()
        if chart_frame.empty:
            st.warning('No hay datos para esa ventana.')
        else:
            st.plotly_chart(_chart(chart_frame, events, 'ETHUSDT · Replay V3'),
                            use_container_width=True, key='mfv3_price_flow_chart',
                            config={'displaylogo':False,'scrollZoom':True})
        if events:
            event_frame['hora_utc'] = pd.to_datetime(event_frame['ready_ms'],
                                                      unit='ms', utc=True)
            visible = ['hora_utc','side','relative_volume','imbalance','delta_qty',
                       'entry_proxy','return_30s_pct','return_60s_pct','return_180s_pct',
                       'mfe_180s_pct','mae_180s_pct','event_id']
            st.markdown('#### Eventos para inspeccionar')
            st.dataframe(event_frame[visible].iloc[::-1],
                         use_container_width=True, hide_index=True,
                         key='mfv3_event_table')
            st.download_button('Descargar eventos V3 (CSV)',
                               event_frame.to_csv(index=False).encode('utf-8'),
                               file_name='micro_flow_v3_ethusdt_pressure_events.csv',
                               mime='text/csv', key='mfv3_download_events')
            selected_id = st.selectbox('Inspeccionar evento', [e['event_id'] for e in events[::-1]],
                                       key='mfv3_inspect_id')
            selected_event = next(e for e in events if e['event_id'] == selected_id)
            st.json(selected_event)
        else:
            st.info('No se detectaron eventos con estos umbrales. '
                    'Podés reducir el volumen relativo o el desequilibrio.')
        with st.expander('Fuente, hash y límites del Replay', expanded=False):
            st.json(summary)
            st.caption('No se agregan datos desde CSV V1/V2, endpoints de Binance ni Redis.')
