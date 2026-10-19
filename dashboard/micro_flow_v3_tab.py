"""Micro Flow V3-only Streamlit interface. No Legacy V1/V2 data is queried."""
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from dashboard.micro_flow_v3_multi_session_tab import render_multi_session_research
from dashboard.micro_flow_v3_sweep_tab import render_swing_sweep_discovery

from dashboard.services.micro_flow_v3_dashboard_service import (
    V3DashboardError, list_v3_tapes, inspect_v3_tape, existing_replay,
    replay_paths, load_buckets_jsonl,
)
from dashboard.services.micro_flow_v3_research import (
    detect_pressure_events, cluster_pressure_events,
    outcome_comparison, classify_observed_outcome,
)
from dashboard.services.micro_flow_v3_failed_aggression import (
    analyze_failed_aggression, summarize_failed_aggression,
)


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
                    marker=dict(color=color, symbol=sym, size=10),
                    customdata=[[e['relative_volume'],e['imbalance']] for e in selected],
                    hovertemplate='%{x}<br>Precio %{y:.2f}<br>Vol x%{customdata[0]:.2f}'
                                  '<br>Imbalance %{customdata[1]:.2f}<extra></extra>'), row=1, col=1)
    fig.update_layout(title=title, height=680, hovermode='x unified',
                      barmode='relative', showlegend=True,
                      legend=dict(orientation='h',y=-.10), margin=dict(l=20,r=20,t=65,b=40))
    fig.update_xaxes(rangeslider_visible=False)
    return fig


def _inspector_chart(frame, event, before_seconds, after_seconds, failure_case=None):
    """Observed data only, restricted to the event causal segment."""
    ready = int(event['ready_ms'])
    subset = frame[
        frame['segment_id'].eq(event['segment_id'])
        & frame['second_start_ms'].between(
            ready - int(before_seconds) * 1000,
            ready + int(after_seconds) * 1000,
        )
    ].copy()
    if subset.empty:
        return None
    fig = _chart(subset, [event], f"Inspección · {event['event_id']}")
    color = '#16a34a' if event['direction'] > 0 else '#dc2626'
    for ts_ms, label, shade in (
        (event['ready_ms'], 'Señal disponible', '#f59e0b'),
        (event.get('entry_ms'), 'Entrada proxy', color),
    ):
        if ts_ms is None:
            continue
        x = pd.to_datetime(int(ts_ms), unit='ms', utc=True)
        fig.add_shape(type='line', x0=x, x1=x, y0=0, y1=1,
                      xref='x', yref='paper', line=dict(color=shade,width=2,dash='dash'))
        fig.add_annotation(x=x, y=1.04, xref='x', yref='paper', text=label,
                           showarrow=False, font=dict(color=shade,size=10),
                           bgcolor='rgba(0,0,0,.65)')
    # Second clock: the experimental confirmation is NOT the original signal.
    # Show it only when it belongs to the same selected causal event.
    if failure_case is not None:
        for ts_ms, label, shade in (
            (failure_case['decision_ms'], 'Evaluación posterior', '#a78bfa'),
            (failure_case.get('entry_ms'), 'Proxy posconfirmación', '#22d3ee'),
        ):
            if ts_ms is None:
                continue
            x = pd.to_datetime(int(ts_ms), unit='ms', utc=True)
            fig.add_shape(type='line', x0=x, x1=x, y0=0, y1=1,
                          xref='x', yref='paper',
                          line=dict(color=shade,width=1.6,dash='dot'))
            fig.add_annotation(x=x, y=1.10, xref='x', yref='paper',
                               text=label, showarrow=False,
                               font=dict(color=shade,size=9),
                               bgcolor='rgba(0,0,0,.7)')
    outcomes = [(h, event.get(f'exit_observed_ms_{h}s'),
                 event.get(f'exit_proxy_{h}s')) for h in (30,60,180)]
    available = [(h, ts, px) for h,ts,px in outcomes if ts is not None and px is not None
                 and (ready - before_seconds * 1000) <= ts <= (ready + after_seconds*1000)]
    if available:
        fig.add_trace(go.Scatter(
            x=pd.to_datetime([ts for _,ts,_ in available], unit='ms', utc=True),
            y=[px for _,_,px in available], mode='markers+text',
            text=[f'{h}s' for h,_,_ in available], textposition='top center',
            name='Horizontes observados', marker=dict(symbol='diamond',size=10,color='#eab308'),
            hovertemplate='Observado: %{x}<br>Precio proxy %{y:.2f}<extra></extra>'
        ), row=1, col=1)
    fig.update_layout(height=670, margin=dict(l=20,r=20,t=80,b=40))
    return fig


def _report_table(events, *, neutral_band_pct):
    summary = outcome_comparison(events, neutral_band_pct=neutral_band_pct)
    rows = []
    for r in summary:
        rows.append({
            'Dirección señal': 'BUY' if r['side'] == 'BUY_PRESSURE' else 'SELL',
            'Horizonte': f"{r['horizon_s']}s",
            'Señales': r['signals'], 'Válidos': r['observed'],
            'Censurados': r['censored'], 'Continúan': r['continuation'],
            'Revierten': r['reversal'], 'Neutrales': r['neutral'],
            'Continuación %': (round(r['continuation_pct'],1)
                                if r['continuation_pct'] is not None else None),
            'Ret. dir. medio %': (round(r['mean_directional_return_pct'],4)
                                   if r['mean_directional_return_pct'] is not None else None),
        })
    return pd.DataFrame(rows)


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
    monitor_tab, replay_tab, multi_tab, sweep_tab = st.tabs(['📡 Recorder V3', '🧪 Replay & presión agresiva', '📊 Multi-Session Research', '🧭 Swing Sweep Discovery'])
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
    with multi_tab:
        render_multi_session_research(root, tapes)
    with sweep_tab:
        render_swing_sweep_discovery(root, tapes)
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
        # Event clustering is ANALYTICAL; it never changes the raw signal set.
        a_episode, b_episode = st.columns([1, 2])
        with a_episode:
            episode_seconds = st.select_slider(
                'Agrupar señales dentro de (s)',
                options=[30,60,90,120,180,240,300], value=180,
                key='mfv3_episode_window',
            )
        with b_episode:
            population = st.radio(
                'Base de comparación',
                ['Episodios (primera señal)', 'Todas las señales (solapadas)'],
                horizontal=True, key='mfv3_population',
            )
        annotated_events, episodes = cluster_pressure_events(
            events, window_seconds=int(episode_seconds),
        )
        anchor_events = [event for event in annotated_events if event['is_episode_anchor']]
        compared = (anchor_events if population.startswith('Episodios')
                    else annotated_events)
        mixed_count = sum(ep['mixed_sides'] for ep in episodes)
        k1,k2,k3,k4 = st.columns(4)
        k1.metric('Señales detectadas', f'{len(events):,}')
        k2.metric('Episodios', f'{len(episodes):,}')
        k3.metric('Con BUY y SELL', f'{mixed_count:,}')
        valid_60 = [float(e['return_60s_pct']) for e in compared
                    if e.get('return_60s_pct') is not None]
        k4.metric('Retorno direccional medio · 60s',
                  f'{sum(valid_60)/len(valid_60):+.3f}%' if valid_60 else '—')
        st.caption(
            'Agrupación por ventanas FIJAS desde la primera señal observada en cada tramo; '
            'cada señal pertenece a un solo episodio. Los resultados por episodio usan '
            'solo su primera señal, aunque existan disparadores opuestos posteriores. '
            'Esta convención reduce duplicados, pero no demuestra independencia estadística.'
        )
        st.markdown('#### Momentum vs Exhaustion · resultados observados')
        neutral_band = st.select_slider(
            'Zona neutral del retorno direccional (%)',
            options=[0.0, 0.005, 0.01, 0.02, 0.05], value=0.01,
            key='mfv3_neutral_band',
        )
        st.dataframe(_report_table(compared,neutral_band_pct=float(neutral_band)),
                     use_container_width=True, hide_index=True,
                     key='mfv3_outcome_comparison')
        st.caption('Continuación: retorno direccional > banda neutral. Reversión: < -banda. '
                   'Es una etiqueta EX POST; no se conoce al detectar la señal. '
                   'Válidos excluye horizontes censurados; retornos sin costos/fills.')
        # Phase 2C: a SECOND, LATER observation time. These are not labels
        # inferred from the original 180s outcome or historical optimizations.
        st.markdown('#### 🧪 Failed Aggression · confirmación observable')
        st.caption(
            'Se evalúa DESPUÉS de la señal original. La decisión utiliza solo '
            'buckets publicados hasta el momento de confirmación (available_ms). '
            'Si confirma un fracaso, la hipótesis contraria empieza únicamente '
            'en una nueva entrada proxy posterior. No son órdenes, fills ni PnL.'
        )
        fa1, fa2 = st.columns(2)
        with fa1:
            confirm_seconds = st.select_slider(
                'Esperar para evaluar presión (s)',
                options=[10,20,30,60,90,120], value=30,
                key='mfv3_failed_confirmation_seconds',
            )
        with fa2:
            min_opposite_pct = st.number_input(
                'Desplazamiento contrario mínimo (%)',
                min_value=0.005, max_value=1.0, value=0.03, step=0.01,
                format='%.3f', key='mfv3_failed_opposite_pct',
            )
        with st.expander('Criterios adicionales de confirmación', expanded=False):
            fa3, fa4, fa5 = st.columns(3)
            with fa3:
                max_favorable_pct = st.number_input(
                    'Máxima excursión a favor original (%)',
                    min_value=0.0, max_value=2.0, value=0.10, step=0.025,
                    format='%.3f', key='mfv3_failed_max_favorable',
                )
            with fa4:
                min_followthrough_pct = st.number_input(
                    'Continuación mínima (%)',
                    min_value=0.005, max_value=1.0, value=0.03, step=0.01,
                    format='%.3f', key='mfv3_failed_followthrough',
                )
            with fa5:
                freshness_s = st.select_slider(
                    'Antigüedad máxima del precio (s)',
                    options=[1,2,3,5,10], value=3,
                    key='mfv3_failed_freshness',
                )
        failed_cases = analyze_failed_aggression(
            buckets, anchor_events,
            confirmation_seconds=int(confirm_seconds),
            min_opposite_pct=float(min_opposite_pct),
            max_favorable_pct=float(max_favorable_pct),
            min_followthrough_pct=float(min_followthrough_pct),
            freshness_seconds=int(freshness_s),
        )
        failed_by_id = {r['event_id']:r for r in failed_cases}
        failures = sum(r['state']=='FAILED_AGGRESSION' for r in failed_cases)
        continued = sum(r['state']=='PRESSURE_CONTINUED' for r in failed_cases)
        unresolved = sum(r['state']=='UNRESOLVED' for r in failed_cases)
        censored = sum(r['state'].startswith('CENSORED') for r in failed_cases)
        z1,z2,z3,z4 = st.columns(4)
        z1.metric('Fracaso confirmado', failures)
        z2.metric('Presión continuó', continued)
        z3.metric('Sin confirmación', unresolved)
        z4.metric('Observación censurada', censored)
        if failed_cases:
            st.dataframe(pd.DataFrame(summarize_failed_aggression(failed_cases)).rename(
                columns={
                    'state':'Grupo posconfirmación',
                    'original_side':'Presión original',
                    'horizon_s':'Horizonte desde proxy (s)',
                    'episodes':'Episodios', 'observed':'Observados',
                    'censored':'Censurados',
                    'mean_return_pct':'Retorno direccional medio %',
                    'positive_pct':'Positivos %',
                }), use_container_width=True, hide_index=True,
                key='mfv3_failed_summary')
            failed_df = pd.DataFrame(failed_cases)
            failed_df['decision_utc'] = pd.to_datetime(
                failed_df['decision_ms'],unit='ms',utc=True,
            )
            columns = [
                'decision_utc','original_side','state','candidate_side',
                'directional_progress_pct','peak_original_favorable_pct',
                'opposite_progress_pct','confirmation_net_delta_qty',
                'entry_state','entry_proxy','return_30s_pct',
                'return_60s_pct','return_180s_pct','return_300s_pct',
                'event_id',
            ]
            st.dataframe(failed_df[columns].iloc[::-1],
                         use_container_width=True, hide_index=True,
                         key='mfv3_failed_cases')
            st.download_button(
                'Descargar confirmaciones y casos censurados (CSV)',
                data=failed_df.to_csv(index=False).encode('utf-8'),
                file_name='micro_flow_v3_failed_aggression_research.csv',
                mime='text/csv', key='mfv3_failed_export',
            )
        st.caption(
            'FAILED: desplazamiento contrario suficiente Y pico favorable '
            'original acotado. CONTINUED: progreso en dirección original. '
            'UNRESOLVED: sin condición; CENSORED: información insuficiente. '
            'La comparación se basa en primeras señales de episodios. '
            'Rendimientos desde una entrada POSTERIOR a la confirmación; '
            'no incluyen costos, spread, ejecución ni controles de tendencia.'
        )
        duration = st.select_slider('Ventana del gráfico general (últimos minutos)',
                                    options=[5,10,30,60,120,360], value=30,
                                    key='mfv3_window')
        start_ms = int(frame['second_start_ms'].max()) - int(duration)*60_000
        chart_frame = frame[frame['second_start_ms'] >= start_ms].copy()
        if chart_frame.empty:
            st.warning('No hay datos para esa ventana.')
        else:
            st.plotly_chart(_chart(chart_frame, anchor_events,
                                  'ETHUSDT · Replay V3 · anclas de episodios'),
                            use_container_width=True, key='mfv3_price_flow_chart',
                            config={'displaylogo':False,'scrollZoom':True})
        if annotated_events:
            st.markdown('#### Episodios (sin duplicar disparadores)')
            episode_frame = pd.DataFrame(episodes)
            episode_frame['inicio_utc'] = pd.to_datetime(
                episode_frame['anchor_ready_ms'], unit='ms', utc=True,
            )
            display_eps = ['inicio_utc', 'episode_id', 'episode_side', 'trigger_count',
                           'buy_triggers', 'sell_triggers', 'anchor_entry_proxy']
            st.dataframe(episode_frame[display_eps].iloc[::-1],
                         use_container_width=True, hide_index=True,
                         key='mfv3_episode_table')
            st.markdown('#### Todas las señales y su episodio')
            event_frame = pd.DataFrame(annotated_events)
            event_frame['hora_utc'] = pd.to_datetime(event_frame['ready_ms'],
                                                      unit='ms', utc=True)
            visible = ['hora_utc','side','episode_id','is_episode_anchor',
                       'relative_volume','imbalance','delta_qty','entry_proxy',
                       'return_30s_pct','return_60s_pct','return_180s_pct',
                       'directional_mfe_180s_pct','directional_mae_180s_pct',
                       'outcome_state_180s','event_id']
            st.dataframe(event_frame[visible].iloc[::-1],
                         use_container_width=True, hide_index=True,
                         key='mfv3_event_table')
            st.download_button('Descargar eventos V3 (CSV, con episodios)',
                               event_frame.to_csv(index=False).encode('utf-8'),
                               file_name='micro_flow_v3_ethusdt_episodes_events.csv',
                               mime='text/csv', key='mfv3_download_events')
            st.download_button('Descargar episodios V3 (CSV)',
                               episode_frame.to_csv(index=False).encode('utf-8'),
                               file_name='micro_flow_v3_ethusdt_episodes.csv',
                               mime='text/csv', key='mfv3_download_episodes')
            st.markdown('#### 🔎 Event Inspector')
            inspect_all = st.checkbox('Incluir señales secundarias en el selector',
                                      value=False, key='mfv3_inspect_all')
            candidates = annotated_events if inspect_all else anchor_events
            event_by_id = {e['event_id']:e for e in candidates}
            options = [e['event_id'] for e in candidates[::-1]]
            selected_id = st.selectbox(
                'Seleccionar evento', options,
                format_func=lambda ident: (
                    f"{_display_ts(event_by_id[ident]['ready_ms'])} · "
                    f"{event_by_id[ident]['side']} · "
                    f"{'ANCLA' if event_by_id[ident]['is_episode_anchor'] else 'secundaria'} · "
                    f"{ident}"
                ), key='mfv3_inspect_id',
            )
            selected_event = event_by_id[selected_id]
            i1, i2, i3 = st.columns(3)
            i1.metric('Señal disponible',_display_ts(selected_event['ready_ms']))
            i2.metric('Entrada proxy',
                      f"{selected_event['entry_proxy']:.2f}"
                      if selected_event['entry_proxy'] is not None else 'CENSURADA')
            i3.metric('Episodio',selected_event['episode_id'])
            before_col, after_col = st.columns(2)
            with before_col:
                before_s = st.select_slider('Segundos previos',
                                            options=[15,30,60,120,180], value=60,
                                            key='mfv3_before')
            with after_col:
                after_s = st.select_slider('Segundos posteriores',
                                           options=[60,120,180,240,360], value=240,
                                           key='mfv3_after')
            failure_case = failed_by_id.get(selected_id)
            detail = _inspector_chart(frame,selected_event,before_s,after_s,
                                      failure_case=failure_case)
            if detail is not None:
                st.plotly_chart(detail, use_container_width=True,
                                key='mfv3_event_inspector_chart',
                                config={'displaylogo':False,'scrollZoom':True})
            else:
                st.info('No hay buckets de ese tramo en el rango seleccionado.')
            outcomes = []
            for h in (30,60,180):
                suffix=f'{h}s'
                result = selected_event.get(f'return_{suffix}_pct')
                outcomes.append({
                    'Horizonte': suffix,
                    'Estado': selected_event.get(f'outcome_state_{suffix}'),
                    'Clasificación posterior': classify_observed_outcome(
                        result, neutral_band_pct=neutral_band),
                    'Retorno direccional %':result,
                    'MFE favorable %': selected_event.get(f'directional_mfe_{suffix}_pct'),
                    'MAE adverso %': selected_event.get(f'directional_mae_{suffix}_pct'),
                    'Precio observado horizonte': selected_event.get(f'exit_proxy_{suffix}'),
                })
            st.dataframe(pd.DataFrame(outcomes),use_container_width=True,
                         hide_index=True,key='mfv3_inspected_outcomes')
            st.caption('Para BUY un alza favorece; para SELL una baja favorece. '
                       'MFE positivo = excursión favorable; MAE negativo = adversa. '
                       'Los extremos de precio sin ajustar siguen disponibles '
                       'como price_high_excursion/price_low_excursion en el JSON.')
            if failure_case is not None:
                st.markdown('##### Resultado de la confirmación posterior')
                ff1, ff2, ff3 = st.columns(3)
                ff1.metric('Estado a la evaluación', failure_case['state'])
                ff2.metric('Dirección evaluada', failure_case['candidate_side'] or '—')
                ff3.metric('Entrada proxy posterior',
                           f"{failure_case['entry_proxy']:.2f}"
                           if failure_case['entry_proxy'] is not None else '—')
                st.caption(
                    'La señal original y la evaluación posterior tienen relojes '
                    'diferentes (naranja y violeta). La posible entrada posterior '
                    'se marca en cian; ninguna línea representa un fill real.'
                )
                with st.expander('Detalles causales de Failed Aggression', expanded=False):
                    st.json(failure_case)
            else:
                st.caption('Failed Aggression analiza solo la primera señal de cada episodio.')
            with st.expander('Evento completo y datos crudos', expanded=False):
                st.json(selected_event)
        else:
            st.info('No se detectaron eventos con estos umbrales. '
                    'Podés reducir el volumen relativo o el desequilibrio.')
        with st.expander('Fuente, hash y límites del Replay', expanded=False):
            st.json(summary)
            st.caption('No se agregan datos desde CSV V1/V2, endpoints de Binance ni Redis.')

