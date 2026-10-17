"""Streamlit read-only view of completed Runner Portfolio V1 research runs.

This file never imports or invokes the simulator, Binance, Redis or LIVE scanner.
All metrics, fills and OHLC are read from immutable portfolio_ledger_v1.sqlite3.
"""

from __future__ import annotations

import json
import sqlite3

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard.services.reaction_lab_portfolio_reader import (
    as_float, as_ms, discover_runs, explain_skipped_signals, load_run_metadata,
    read_table, table_counts, verify_consistency,
)

TZ = 'America/Argentina/Buenos_Aires'
VIEW_VERSION = 'portfolio-dashboard-v2.0'


@st.cache_data(ttl=120, show_spinner=False, max_entries=12)
def _cached_table(project, run_id, db_mtime_ns, table):
    return read_table(project, run_id, table)


@st.cache_data(ttl=120, show_spinner=False, max_entries=12)
def _cached_metadata(project, run_id, db_mtime_ns):
    return load_run_metadata(project, run_id)


def _time(ms):
    m = as_ms(ms)
    if m is None:
        return pd.NaT
    return pd.to_datetime(m, unit='ms', utc=True).tz_convert(TZ).tz_localize(None)


def _fmt_time(ms):
    t = _time(ms)
    return '—' if pd.isna(t) else t.strftime('%d/%m/%Y %H:%M:%S')


def _df(rows):
    return pd.DataFrame(rows)


def _annotate_timestamps(df, cols):
    df = df.copy()
    for col in cols:
        if col in df:
            df[f'{col}_ARG'] = pd.to_datetime(pd.to_numeric(df[col], errors='coerce'),
                                              unit='ms', utc=True, errors='coerce').dt.tz_convert(TZ).dt.tz_localize(None)
    return df


def _download(df, title, filename, key):
    if not df.empty:
        st.download_button(title, df.to_csv(index=False).encode('utf-8'),
                           file_name=filename, mime='text/csv', key=key)


def _metrics(summary):
    strategies = list(summary)
    if not strategies:
        st.warning('El run no contiene estrategias.'); return
    cols = st.columns(len(strategies))
    for col, strategy in zip(cols, strategies):
        data = summary[strategy]
        with col:
            st.markdown(f'#### {strategy}')
            r1, r2 = st.columns(2)
            r1.metric('Equity final (USDT)', f"${as_float(data.get('equity_final_realized_usdt')):,.2f}")
            r2.metric('Retorno realizado', f"{as_float(data.get('return_realized_pct')):+.2f}%")
            r3, r4 = st.columns(2)
            r3.metric('Max. drawdown marcado', f"{as_float(data.get('max_marked_drawdown_pct')):.2f}%")
            r4.metric('Trades cerrados', int(data.get('resolved_trades', 0)))
            st.caption(f"Señales elegibles: {data.get('filter_eligible')} · "
                       f"órdenes: {data.get('orders_placed')} · "
                       f"slot ocupado: {data.get('skipped_slot_busy')} · "
                       f"auditoría: {data.get('audit_status', 'UNKNOWN')}")


def _equity_charts(equity_rows, summary):
    eq = _df(equity_rows)
    if eq.empty:
        st.warning('No hay marcas de equity guardadas.'); return
    eq = _annotate_timestamps(eq, ['timestamp_ms'])
    for c in ('wallet_usdt', 'marked_equity_usdt','drawdown_pct'):
        eq[c] = pd.to_numeric(eq[c], errors='coerce')
    eq = eq.sort_values(['strategy','timestamp_ms'], kind='stable')
    display = st.radio('Escala', ['USDT', 'Variación % desde equity inicial'],
                       horizontal=True, key='runner_portfolio_equity_scale_v2')
    fig = go.Figure()
    dd = go.Figure()
    for strategy, chunk in eq.groupby('strategy',sort=False):
        initial = as_float(summary.get(strategy, {}).get('equity_initial_usdt'), 200.)
        y_real = chunk['wallet_usdt'] if display=='USDT' else (chunk['wallet_usdt']/initial-1)*100
        y_mark = chunk['marked_equity_usdt'] if display=='USDT' else (chunk['marked_equity_usdt']/initial-1)*100
        fig.add_trace(go.Scatter(x=chunk['timestamp_ms_ARG'], y=y_real, mode='lines',
                                 line_shape='hv', name=f'{strategy} · realizada'))
        fig.add_trace(go.Scatter(x=chunk['timestamp_ms_ARG'], y=y_mark, mode='lines',
                                 line=dict(dash='dot'), name=f'{strategy} · marcada'))
        dd.add_trace(go.Scatter(x=chunk['timestamp_ms_ARG'], y=chunk['drawdown_pct'],
                                mode='lines', name=strategy))
    fig.update_layout(title='Equity realizada y marcada a mercado (1m)',
                      yaxis_title='USDT' if display=='USDT' else '% de equity inicial',
                      xaxis_title=f'Fecha y hora · {TZ}', height=480,
                      hovermode='x unified', legend=dict(orientation='h', y=-0.25))
    dd.update_layout(title='Drawdown de equity marcada', xaxis_title=f'Fecha · {TZ}',
                     yaxis_title='Drawdown (%)', hovermode='x unified', height=300,
                     legend=dict(orientation='h', y=-0.35))
    st.plotly_chart(fig, use_container_width=True, key='runner_portfolio_equity_chart_v2',
                    config={'scrollZoom': True, 'displaylogo': False})
    st.plotly_chart(dd, use_container_width=True, key='runner_portfolio_dd_chart_v2',
                    config={'scrollZoom': True, 'displaylogo': False})
    st.caption('La curva realizada conserva el capital entre cierres; la marcada usa cierres de velas 1m. '
               'El drawdown intravela podría ser mayor. Ambas son simulaciones, no fills reales de Binance.')
    with st.expander('Marcas originales de equity y descarga', expanded=False):
        st.dataframe(eq, use_container_width=True, hide_index=True)
        _download(eq, '⬇ Equity CSV', 'runner_portfolio_equity_v2.csv', 'runner_portfolio_equity_dl_v2')


def _trades_table(trades_rows):
    trades = _df(trades_rows)
    if trades.empty:
        st.info('Todavía no hay trades resueltos.'); return
    strategies = sorted(trades['strategy'].dropna().unique())
    selected = st.multiselect('Estrategias', strategies, default=strategies,
                              key='runner_portfolio_trades_strategies_v2')
    trades = trades[trades['strategy'].isin(selected)]
    if trades.empty:
        st.info('No hay operaciones con estos filtros.'); return
    statuses = sorted(trades['status'].dropna().unique())
    selected_status = st.multiselect('Resultado', statuses, default=statuses,
                                     key='runner_portfolio_trades_status_v2')
    trades = trades[trades['status'].isin(selected_status)]
    trades = _annotate_timestamps(trades, ['ready_ms', 'fill_ts', 'exit_ts', 'retest_ts'])
    for col in ('net_pnl_usdt','gross_pnl_usdt','entry_cost_usdt','exit_cost_usdt',
                'margin_usdt','notional_usdt','net_pct'):
        trades[col] = pd.to_numeric(trades[col], errors='coerce')
    trades['duration_min'] = (pd.to_numeric(trades['exit_ts'], errors='coerce') -
                              pd.to_numeric(trades['fill_ts'], errors='coerce')) / 60000
    trades = trades.sort_values('fill_ts')
    main_cols = ['strategy','symbol','side','status','ready_ms_ARG','fill_ts_ARG','exit_ts_ARG',
                 'duration_min','margin_usdt','notional_usdt','fill_price','exit_price',
                 'entry_liquidity','exit_liquidity','entry_cost_usdt','exit_cost_usdt',
                 'net_pct','net_pnl_usdt','wallet_before_usdt','wallet_after_usdt',
                 'room_1h','departure','volume_rank','return_rank','context_status',
                 'regime_status','capture_method','event_key']
    st.dataframe(trades[[c for c in main_cols if c in trades]], hide_index=True,
                 use_container_width=True, height=420)
    _download(trades, '⬇ Trades completos CSV', 'runner_portfolio_trades_v2.csv',
              'runner_portfolio_trades_dl_v2')
    st.caption('exit_ts_ARG identifica la vela de salida; POSITION_EXIT registra el cierre de esa vela. '
               'PnL en USDT ya incorpora los costos estimados del simulador.')


def _event_list(events, strategy, event_key):
    return sorted([r for r in events if r.get('strategy')==strategy and
                   r.get('event_key')==event_key], key=lambda r:as_ms(r.get('timestamp_ms')) or 0)


def _path_fig(path, events, trade, decision, title):
    """Plot 1m OHLC persisted at the time of simulation, no Binance or Redis requests."""
    df = _df(path)
    if df.empty:
        return None
    df = _annotate_timestamps(df, ['timestamp_ms'])
    df = df.sort_values('timestamp_ms', kind='stable')
    for c in ('open','high','low','close','limit_price','tp_price','sl_price'):
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df = df.dropna(subset=['timestamp_ms_ARG','open','high','low','close'])
    if df.empty:
        return None
    fig = go.Figure(go.Candlestick(x=df['timestamp_ms_ARG'], open=df['open'],
                                  high=df['high'], low=df['low'],close=df['close'],
                                  name='OHLC 1m · persistido'))
    xs = df['timestamp_ms_ARG']
    x_start, x_end = xs.min(), xs.max() + pd.Timedelta(minutes=1)
    low, high = float(df['low'].min()), float(df['high'].max())
    pdelta = (high-low) or max(abs(high)*.02,1e-8)
    min_y, max_y = low - .10*pdelta, high + .10*pdelta
    # Levels come from the persisted path; no independent recalculation of TP/SL.
    levels = [('limit_price','LIMIT','dash'),('tp_price','TP','dot'),('sl_price','SL','dot')]
    for field, label, style in levels:
        nums = df[field].dropna()
        if len(nums)==0:
            continue
        price = float(nums.iloc[0])
        start = x_start
        if field in ('tp_price','sl_price'):
            fills = [e for e in events if e.get('kind')=='ORDER_FILLED']
            if fills:
                start = max(x_start,_time(fills[0]['timestamp_ms']))
        fig.add_trace(go.Scatter(x=[start,x_end],y=[price,price], mode='lines',
                                 name=f'{label}: {price:.8g}',line=dict(dash=style, width=1.3)))
    markers = [('ORDER_PLACED','Orden','square'),('ORDER_FILLED','Fill','triangle-up'),
               ('POSITION_EXIT','Salida','x'),('ORDER_EXPIRED','Expirada','x')]
    for kind, name, shape in markers:
        event = next((e for e in events if e.get('kind')==kind), None)
        if not event:
            continue
        t = _time(event.get('timestamp_ms'))
        if pd.isna(t):
            continue
        # Keep actual millisecond timestamps, no rounding to bar start.
        if kind=='ORDER_PLACED':
            price = as_float(event.get('limit_price'),float(df['open'].iloc[0]))
        elif kind=='ORDER_FILLED':
            price = as_float(event.get('fill_price'),float(df['open'].iloc[0]))
        elif kind=='POSITION_EXIT':
            price = as_float(event.get('exit_price'),float(df['close'].iloc[-1]))
        else:
            price = float(df['close'].iloc[-1])
        if x_start - pd.Timedelta(minutes=1) <= t <= x_end:
            fig.add_trace(go.Scatter(x=[t],y=[price],name=name,mode='markers',
                                     marker=dict(symbol=shape,size=12)))
    for label, stamp in [('Retest',decision.get('retest_ts')),
                         ('Visto LIVE',decision.get('scanner_first_seen_ms')),
                         ('Factores listos',decision.get('ready_ms'))]:
        t = _time(stamp)
        if pd.notna(t) and x_start <= t <= x_end:
            fig.add_shape(type='line',x0=t,x1=t,y0=min_y,y1=max_y,
                          line=dict(dash='dot',width=1))
            # The precise timestamp is in the event timeline below.
    fig.update_layout(title=title, height=580, xaxis_title=f'Fecha y hora · {TZ}',
                      yaxis_title='Precio',xaxis_rangeslider_visible=False,
                      hovermode='x unified', legend=dict(orientation='h', y=-.20),
                      margin=dict(t=55,b=95))
    return fig


def _inspect_order(trades, decisions, events, paths, strategy, key, widget_id='inspector'):
    ev = _event_list(events, strategy, key)
    td = next((t for t in trades if t.get('strategy')==strategy and t.get('event_key')==key), None)
    dec = next((d for d in decisions if d.get('strategy')==strategy and d.get('event_key')==key), {})
    path = [p for p in paths if p.get('strategy')==strategy and p.get('event_key')==key]
    info = key.split('|')
    st.markdown(f"#### {' · '.join(info[:2])} · {'trade cerrado' if td else 'orden sin fill'}")
    cols = st.columns(4)
    cols[0].metric('Resultado', td.get('status','—') if td else 'NO_FILL / pendiente')
    cols[1].metric('PnL neto', f"{as_float(td.get('net_pnl_usdt')):+.4f} USDT" if td else '—')
    cols[2].metric('Entrada', f"{as_float(td.get('fill_price')):.8g}" if td else '—')
    cols[3].metric('Salida', f"{as_float(td.get('exit_price')):.8g}" if td else '—')
    fig = _path_fig(path, ev, td, dec, f'{strategy} · {key}')
    if fig is None:
        st.warning('Sin velas persistidas para este evento. No se descargan históricos externos.')
    else:
        st.plotly_chart(fig,use_container_width=True,key=f'runner_portfolio_path_{widget_id}_v2',
                        config={'scrollZoom':True,'displaylogo':False})
        path_df = _annotate_timestamps(_df(path),['timestamp_ms'])
        if not path_df.empty:
            m1,m2,m3=st.columns(3)
            peaks=path_df.loc[path_df['stage'].isin(['IN_POSITION','EXIT_BAR'])]
            if not peaks.empty:
                mfes=pd.to_numeric(peaks.get('running_mfe_pct'),errors='coerce')
                maes=pd.to_numeric(peaks.get('running_mae_pct'),errors='coerce')
                m1.metric('MFE máximo mostrado',f'{mfes.max():.3f}%' if mfes.notna().any() else '—')
                m2.metric('MAE máximo mostrado',f'{maes.max():.3f}%' if maes.notna().any() else '—')
            m3.metric('Velas persistidas', len(path_df))
            with st.expander('Velas 1m, MFE/MAE y factores originales', expanded=False):
                st.dataframe(path_df,use_container_width=True,hide_index=True,height=320)
                _download(path_df,'⬇ Recorrido CSV',f'runner_path_{widget_id}_v2.csv',
                          f'runner_path_dl_{widget_id}_v2')
    if ev:
        st.markdown('##### Cronología de eventos')
        timeline=[]
        for e in ev:
            timeline.append({'Hora ARG':_fmt_time(e.get('timestamp_ms')),'Evento':e.get('kind'),
                             'Precio LIMIT':e.get('limit_price'), 'Fill':e.get('fill_price'),
                             'Exit':e.get('exit_price'), 'Liquidez':e.get('liquidity'),
                             'PnL neto (USDT)':e.get('net_pnl_usdt')})
        st.dataframe(pd.DataFrame(timeline),use_container_width=True,hide_index=True)
    st.markdown('##### Causalidad de la señal')
    st.json({'event_key':key,'retest_ARG':_fmt_time(dec.get('retest_ts')),
             'scanner_first_seen_ARG':_fmt_time(dec.get('scanner_first_seen_ms')),
             'factors_ready_ARG':_fmt_time(dec.get('ready_ms')),
             'departure_pct':dec.get('departure'),'room_1h_pct':dec.get('room_1h'),
             'return_rank_4h':dec.get('return_rank'),
             'volume_rank_4h':dec.get('volume_rank'),
             'context_status':dec.get('context_status'),
             'regime_status':dec.get('regime_status'),
             'capture_method':dec.get('capture_method'),
             'execution_model':'OHLC 1m estimado; no se conoce secuencia intravela exacta'})


def _inspector(trades, decisions, events, paths, summary):
    strategies=list(summary)
    strategy=st.selectbox('Estrategia a inspeccionar',strategies,
                          key='runner_portfolio_inspector_strategy_v2')
    placed = [e for e in events if e.get('strategy')==strategy and e.get('kind')=='ORDER_PLACED']
    placed.sort(key=lambda e:as_ms(e.get('timestamp_ms')) or 0)
    if not placed:
        st.info('No hay órdenes colocadas para inspeccionar.'); return
    statuses={t['event_key']:t.get('status') for t in trades if t.get('strategy')==strategy}
    options=[e['event_key'] for e in placed]
    choice=st.selectbox('Orden · incluye NO_FILL',options,
                        format_func=lambda key:f'{key} · {statuses.get(key,"NO_FILL")}',
                        key='runner_portfolio_inspector_key_v2')
    _inspect_order(trades,decisions,events,paths,strategy,choice,'selected')


def _skipped(decisions,events,trades,paths,summary):
    strategy=st.selectbox('Estrategia · slot único',list(summary),
                          key='runner_portfolio_busy_strategy_v2')
    skipped=explain_skipped_signals(decisions,events,strategy)
    matched=sum(row['blocker_matches']==1 for row in skipped)
    col1,col2,col3=st.columns(3)
    col1.metric('Señales omitidas',len(skipped))
    col2.metric('Bloqueo identificado',matched)
    col3.metric('Sin atribución inequívoca',len(skipped)-matched)
    if not skipped:
        st.info('No hubo señales omitidas por slot ocupado.'); return
    df=_annotate_timestamps(_df(skipped), ['ready_ms','blocker_until_ms','retest_ts'])
    useful=['ready_ms_ARG','symbol','side','departure','room_1h','volume_rank','return_rank',
            'blocker_symbol','blocker_stage','blocker_until_ms_ARG',
            'blocker_release_matches_reason','context_status','event_key','blocker_event_key']
    st.dataframe(df[[c for c in useful if c in df]],hide_index=True,use_container_width=True,height=420)
    _download(df,'⬇ Señales bloqueadas CSV',f'runner_skipped_{strategy.replace(" ","_")}_v2.csv',
              'runner_portfolio_skipped_dl_v2')
    labels=list(range(len(skipped)))
    idx=st.selectbox('Inspeccionar un bloqueo',labels,
                     format_func=lambda i:f"{_fmt_time(skipped[i].get('ready_ms'))} · "
                            f"{skipped[i].get('symbol')} {skipped[i].get('side')} · "
                            f"bloqueado por {skipped[i].get('blocker_symbol') or 'desconocido'}",
                     key='runner_portfolio_skipped_selected_v2')
    row=skipped[idx]
    st.write('Señal descartada:',row.get('event_key'))
    st.write('Slot reservado por:',row.get('blocker_event_key','—'),
             '| Etapa:',row.get('blocker_stage','UNKNOWN'),
             '| Liberado:',_fmt_time(row.get('blocker_until_ms')))
    if row.get('blocker_event_key') and row.get('blocker_matches')==1:
        _inspect_order(trades,decisions,events,paths,strategy,row['blocker_event_key'],'blocker')
    else:
        st.warning('No se encontró una única orden bloqueante. No se asigna arbitrariamente.')
    st.caption('La atribución se deriva del intervalo ORDER_PLACED → ORDER_EXPIRED/POSITION_EXIT. '
               'POSITION_EXIT marca el cierre de la vela 1m, posterior al exit_ts del trade.')


def _integrity(project, run_id, summary, manifest, tables):
    quality=(manifest.get('provenance') or {}).get('data_quality') or {}
    source=int(quality.get('live') or 0)
    usable=int(quality.get('usable') or manifest.get('source_records') or 0)
    m1,m2,m3=st.columns(3)
    m1.metric('Eventos LIVE de referencia',source)
    m2.metric('Eventos utilizables',usable)
    m3.metric('Cobertura utilizable', f'{100*usable/source:.1f}%' if source else '—')
    st.caption('Los conteos de faltantes pueden representar causas superpuestas; no sumarlos como categorías independientes.')
    q=_df([quality]).T.rename(columns={0:'Cantidad'})
    st.dataframe(q,use_container_width=True)
    status=verify_consistency(summary,tables)
    if status['ok']:
        st.success('Consistencia contable, eventos de cierre y atribución de slots: sin diferencias detectadas.')
    else:
        for issue in status['issues']:
            st.error(issue)
    st.dataframe(_df([{'strategy':k,**v} for k,v in status['by_strategy'].items()]),
                 use_container_width=True,hide_index=True)
    st.caption('Esta validación no prueba paridad con LIVE, llenado real de LIMIT, secuencia intravela, '
               'liquidaciones ni funding. Los ranks de régimen tienen captura recomputada si así indica el manifest.')
    st.markdown('##### Parámetros y procedencia guardados')
    st.json({'engine_version':manifest.get('engine_version'),
             'created_at_utc':manifest.get('created_at_utc'),
             'source_digest_sha256':manifest.get('source_digest_sha256'),
             'cohort':manifest.get('cohort'),
             'profiles':manifest.get('profiles'),
             'portfolio_settings':manifest.get('portfolio_settings'),
             'cost_assumptions':manifest.get('cost_assumptions')})
    st.markdown('##### Tabla SQLite (modo solo lectura)')
    st.json(table_counts(project,run_id))
    with st.expander('Exportación completa de tablas · desde SQLite', expanded=False):
        st.caption('Incluye todas las decisiones, órdenes, operaciones, marcas de equity '
                   'y velas persistidas, sin consultar las fuentes originales.')
        for table in ('decisions','events','trades','equity','path_traces'):
            _download(_df(tables.get(table, [])),f'⬇ {table}.csv',
                      f'runner_portfolio_{table}_{run_id}.csv',
                      f'runner_portfolio_raw_{table}_v2')


def _experimental_guard_dashboard(summary, manifest, tables):
    """Inspect only persisted outcomes of the separately simulated guard variant."""
    name = 'Runner Challenger 220 · Extreme Strength Guard'
    parent = 'Runner Challenger 220'
    if name not in summary:
        st.info('Este run no incluye Extreme Strength Guard. Generá un nuevo run con '
                '`--extreme-strength-guard-experiment`. Los runs anteriores no se modifican.')
        return
    st.markdown('#### 🧪 Challenger #220 · Extreme Strength Guard')
    st.warning('Variante experimental IN-SAMPLE: el umbral 95/90 se formuló al observar '
               'resultados de septiembre. No es una validación independiente ni una regla LIVE.')
    guard = (manifest.get('experimental_variants') or {}).get(name, {})
    st.caption('Solo SHORT: rechazar si Return Rank 4h ≥ 95 Y Volume Rank 4h ≥ 90. '
               'TP/SL, LIMIT, TTL, costos, capital y slot iguales a Challenger #220. '
               'Ambas carteras se simulan completas desde cero.')
    if guard:
        with st.expander('Definición congelada del experimento', expanded=False):
            st.json(guard)
    table = []
    for strategy in (parent, name):
        if strategy not in summary:
            continue
        m = summary[strategy]
        table.append({
            'Estrategia': strategy,
            'Equity final USDT': m.get('equity_final_realized_usdt'),
            'Retorno %': m.get('return_realized_pct'),
            'Max DD MTM %': m.get('max_marked_drawdown_pct'),
            'Trades': m.get('resolved_trades'),
            'Profit Factor': m.get('profit_factor'),
            'Órdenes': m.get('orders_placed'),
            'Slot ocupado': m.get('skipped_slot_busy'),
            'Guard rechazadas': m.get('experimental_guard_rejected', 0),
        })
    st.dataframe(pd.DataFrame(table), use_container_width=True, hide_index=True)
    dec = tables.get('decisions', [])
    blocked = [r for r in dec if r.get('strategy') == name
               and r.get('decision') == 'FILTER_REJECTED_EXTREME_STRENGTH']
    st.metric('SHORTs vetados por fuerza extrema (antes del slot)', len(blocked))
    if blocked:
        blocked_df = _annotate_timestamps(_df(blocked), ['ready_ms'])
        cols = ['ready_ms_ARG', 'symbol', 'side', 'return_rank', 'volume_rank',
                'room_1h', 'departure', 'guard_id', 'event_key']
        st.dataframe(blocked_df[[c for c in cols if c in blocked_df]],
                     use_container_width=True, hide_index=True)
        _download(blocked_df, '⬇ Vetos experimentales CSV',
                  'runner_extreme_strength_guard_vetos.csv',
                  'runner_extreme_strength_guard_rejects_dl')
    all_trades = tables.get('trades', [])
    parent_by_key = {r['event_key']: r for r in all_trades
                     if r.get('strategy') == parent}
    variant_by_key = {r['event_key']: r for r in all_trades
                      if r.get('strategy') == name}
    # This is not a trade-to-trade PnL subtraction: slots, wallet and notional
    # are recalculated separately and different events may get executed.
    new_keys = sorted(variant_by_key.keys() - parent_by_key.keys())
    old_keys = sorted(parent_by_key.keys() - variant_by_key.keys())
    c1, c2 = st.columns(2)
    c1.metric('Trades nuevos por reasignación de slot', len(new_keys))
    c2.metric('Trades ausentes vs. Challenger', len(old_keys))
    changed = ([{'tipo': 'NUEVO_EN_GUARD', **variant_by_key[k]} for k in new_keys]
               + [{'tipo': 'SOLO_EN_CHALLENGER', **parent_by_key[k]} for k in old_keys])
    if changed:
        st.dataframe(_annotate_timestamps(_df(changed), ['fill_ts', 'exit_ts']),
                     use_container_width=True, hide_index=True)
        _download(_df(changed), '⬇ Diferencias de operaciones CSV',
                  'runner_extreme_strength_guard_trade_deltas.csv',
                  'runner_extreme_strength_guard_trade_deltas_dl')
    st.caption('Las diferencias de PnL requieren comparar las carteras simuladas completas, '
               'no sumar las pérdidas históricas evitadas. El gráfico Equity & Drawdown '
               'incluye ambas curvas independientes.')


def render_runner_portfolio_dashboard(project):
    """Entry point called only when the Portfolio Lab research tab is selected."""
    project=str(project)
    st.markdown('### 📈 Runner Portfolio Lab · Fase 2')
    st.caption('Solo lectura de runs persistidos. Equity, decisiones, fills y velas desde SQLite; '
               'NO recalcula ni ejecuta trades, NO usa Redis/Binance ni modifica journals.')
    runs=discover_runs(project)
    if not runs:
        st.info('Sin runs. Ejecutá `python3 runner_portfolio_lab.py --project /home/multiservices-1m` '
                'sin --dry-run; luego recargá esta vista.')
        return
    by_id={x['run_id']:x for x in runs}
    selected=st.selectbox('Ejecución guardada (la más reciente primero)',list(by_id),
                          format_func=lambda key:f"{key} · {by_id[key].get('source_records','?')} eventos",
                          key='runner_portfolio_run_id_v2')
    current=by_id[selected]
    try:
        manifest,summary=_cached_metadata(project,selected,current['db_mtime_ns'])
        tables={name:_cached_table(project,selected,current['db_mtime_ns'],name)
                for name in ('decisions','events','trades','equity','path_traces')}
    except (ValueError, OSError, sqlite3.Error, KeyError) as exc:
        st.error(f'No se pudo leer el ledger seleccionado: {exc}'); return
    st.caption(f"Run: `{selected}` · Engine: {manifest.get('engine_version')} · "
               f"Creado UTC: {manifest.get('created_at_utc')} · "
               f"Fuente: {manifest.get('cohort')}")
    _metrics(summary)
    has_guard = 'Runner Challenger 220 · Extreme Strength Guard' in summary
    tab_names = ['📉 Equity & Drawdown','📋 Trades','🔎 Trade Inspector',
                 '🚦 Skipped Signals','🛡️ Data Integrity']
    if has_guard:
        tab_names.append('🧪 Extreme Strength Guard')
    tabs=st.tabs(tab_names)
    with tabs[0]:
        _equity_charts(tables['equity'],summary)
    with tabs[1]:
        _trades_table(tables['trades'])
    with tabs[2]:
        _inspector(tables['trades'],tables['decisions'],tables['events'],
                   tables['path_traces'],summary)
    with tabs[3]:
        _skipped(tables['decisions'],tables['events'],tables['trades'],
                 tables['path_traces'],summary)
    with tabs[4]:
        _integrity(project,selected,summary,manifest,tables)
    if has_guard:
        with tabs[5]:
            _experimental_guard_dashboard(summary, manifest, tables)
    st.warning('Investigación retrospectiva. Datos de régimen 4h pueden ser RECOMPUTED_ASOF_CLOSED_4H_REDIS; '
               'equity estimada OHLC 1m, sin funding ni liquidaciones. No constituye validación forward independiente.')
