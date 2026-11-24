"""Offline comparison of Finora's dated public references in the existing replay."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from finora_band_comparison_v1 import BASE, DEFAULT_RUN, FAMILY_ORDER, M, TIMEFRAMES, VERSION, digest
from multisymbol_band_reaction_pilot_v1 import closed_bars


LABELS = {
    'SWING_5X5': 'Swing confirmado 5×5',
    'HLL': 'Centro histórico HLL 30m',
    'MULTI_SWING': 'Banda Multi-Swing',
    'FVG': 'Hueco de tres velas',
    'FVG_BOS': 'Hueco con ruptura por cierre',
    'OB_BOS': 'Bloque de origen con ruptura',
}
COLORS = {'SWING_5X5': '#b9c6d7', 'HLL': '#b29cec', 'MULTI_SWING': '#58bad2'}
SIDES = {'ABOVE': 'Arriba', 'BELOW': 'Abajo', 'INSIDE': 'Dentro'}


def date(ts):
    return pd.to_datetime(int(ts), unit='ms', utc=True)


def utc(ts):
    return '' if pd.isna(ts) else date(ts).strftime('%d %b %H:%M UTC')


def number(val):
    return f'{float(val):.8f}'.rstrip('0').rstrip('.')


def read_run_file(run, manifest, relative):
    path = run / relative
    if digest(path) != manifest['files_sha256'][relative]:
        raise ValueError(f'Archivo de corrida alterado: {relative}')
    return path


def comparison_plot(minutes, meta, top, tf, hours, visible_end, first_touch=None):
    at = meta['asof_ts']
    left = at - hours * 60 * M
    bars = closed_bars(minutes, TIMEFRAMES[tf], start=left, end=visible_end)
    fig = go.Figure(go.Candlestick(x=pd.to_datetime(bars.available_ts, unit='ms', utc=True),
                                  open=bars.open, high=bars.high, low=bars.low, close=bars.close,
                                  increasing_line_color='#4ac0a5', decreasing_line_color='#ef8084',
                                  name=f'Velas cerradas {tf}'))
    if visible_end > at:
        fig.add_vrect(x0=date(at), x1=date(visible_end), line_width=0, fillcolor='#92a7c7', opacity=.07,
                      layer='below')
    for row in top.itertuples(index=False):
        start = max(left, int(row.available_ts))
        end = at if pd.isna(row.invalidated_pre_ts) else min(at, int(row.invalidated_pre_ts))
        if end <= start:
            continue
        color = COLORS.get(row.family, '#58c99c' if row.side == 'UP' else '#e57c88')
        name = LABELS[row.family]
        if row.low == row.high:
            fig.add_scatter(x=[date(start), date(end)], y=[row.low, row.high], mode='lines', name=name,
                            line=dict(color=color, dash='dot' if row.family == 'HLL' else 'solid', width=1.4))
        else:
            fig.add_shape(type='rect', x0=date(start), x1=date(end), y0=row.low, y1=row.high,
                          line=dict(color=color, width=1), fillcolor=color, opacity=.23, layer='below')
            fig.add_scatter(x=[date(end)], y=[(row.low + row.high) / 2], mode='markers', name=name,
                            marker=dict(color=color, size=7), hovertemplate=name + '<extra></extra>')
    for i, level in enumerate(dict.fromkeys([meta['low'], meta['high']])):
        fig.add_scatter(x=[date(left), date(visible_end)], y=[level, level], mode='lines',
                        name='Precio publicado por Finora', legendgroup='published', showlegend=i == 0,
                        line=dict(color='#ffd17b', dash='dash', width=1.5),
                        hovertemplate=f'Finora · {number(level)}<extra></extra>')
    fig.add_vline(x=date(at), line_color='#ffd17b', line_width=1, line_dash='dot')
    fig.add_annotation(x=date(at), y=.985, yref='paper', text='Publicación', showarrow=False,
                       xanchor='right', xshift=-5, font=dict(color='#ffd17b', size=11))
    if first_touch is not None and at < first_touch <= visible_end:
        bar = minutes.loc[minutes.timestamp + M == first_touch].iloc[0]
        y = min(meta['high'], max(meta['low'], float(bar.close)))
        fig.add_scatter(x=[date(first_touch)], y=[y], mode='markers',
                        name='Primer contacto posterior · rango 1m',
                        marker=dict(symbol='diamond', size=12, color='#e9c8ff', line=dict(width=1)))
    fig.update_layout(template='plotly_dark', height=465, margin=dict(l=64, r=18, t=57, b=58),
                      yaxis_title='USDT', xaxis_title='Cierre de vela · UTC', xaxis_rangeslider_visible=False,
                      xaxis=dict(range=[date(left), date(visible_end) + pd.Timedelta(minutes=6)]),
                      legend=dict(orientation='h', y=1.02, x=0, xanchor='left', yanchor='bottom'))
    return fig


def ranked_table(rows, top, kind):
    records = []
    for row in top.itertuples(index=False):
        group = rows[rows.family.eq(row.family) & rows.timeframe.eq(row.timeframe)]
        result = {
            'Método': LABELS[row.family], 'TF': row.timeframe,
            'Inferior': number(row.low), 'Superior': number(row.high),
            'Error inferior': number(row.error_low), 'Error superior': number(row.error_high),
            'Origen UTC': '' if row.family == 'MULTI_SWING' else utc(row.origin_ts),
            'Conocido UTC': utc(row.available_ts), 'Invalidado PRE UTC': utc(row.invalidated_pre_ts),
            'Comparados': len(group), 'Exactos': int(group.exact.sum()),
        }
        if kind == 'ZONE':
            result['Intersección/unión'] = f'{float(row.iou):.1%}' if row.low != row.high else '—'
        else:
            result['Contiene el precio'] = 'Sí' if row.covers_level else 'No'
        records.append(result)
    return pd.DataFrame(records)


def render_finora_comparison(project):
    mode = st.radio('Modo de trabajo', ['Reconstrucción desde las velas', 'Referencias publicadas'],
                    horizontal=True, key='finora_work_mode')
    if mode == 'Reconstrucción desde las velas':
        from dashboard.views.reaction_multiswing_finora_reconstruction_v1_view import render_reconstruction
        render_reconstruction(project)
        return
    render_published_references(project)


def render_published_references(project):
    st.subheader('Comparación con Finora')
    base = Path(project) / BASE / 'runs'
    runs = sorted(p.parent.name for p in base.glob('*/manifest.json') if not p.parent.name.startswith('.'))
    if not runs:
        st.info('La comparación aparece al cargar una corrida en reports/reaction_lab/finora_comparison_v1/runs.')
        return
    run_name = st.selectbox('Corrida', runs, index=runs.index(DEFAULT_RUN) if DEFAULT_RUN in runs else 0,
                            key='finora_run')
    run = base / run_name
    try:
        manifest = json.loads((run / 'manifest.json').read_text(encoding='utf-8'))
        if manifest['version'] != VERSION or not manifest['cases']:
            raise ValueError('Corrida sin referencias o de otra versión')
        mapping = {}
        for case in manifest['cases']:
            if Path(case).name != case or not case.isidentifier():
                raise ValueError('Identificador de caso inválido')
            meta_file = read_run_file(run, manifest, f'{case}/referencia.json')
            mapping[case] = json.loads(meta_file.read_text(encoding='utf-8'))
        choice = st.selectbox('Referencia publicada', list(mapping),
                              format_func=lambda k: f"{mapping[k]['label']} · {mapping[k]['timeframe']}",
                              key='finora_case')
        meta = mapping[choice]
        minutes = pd.read_csv(read_run_file(run, manifest, f'{choice}/velas_1m.csv'), float_precision='round_trip')
        rows = pd.read_csv(read_run_file(run, manifest, f'{choice}/candidatos.csv'), keep_default_na=False,
                           na_values=[''], float_precision='round_trip')
        rows['timeframe'] = rows.timeframe.fillna('')
        a, b, c = st.columns((2, 2, 3))
        label = number(meta['low'])
        if meta['kind'] == 'ZONE':
            label += '–' + number(meta['high'])
        a.metric('Nivel o zona publicada', label)
        b.metric('Cierre 1m al publicar', number(meta['last_1m_close']))
        c.metric(f"Último cierre {meta['timeframe']} disponible", utc(meta['last_tf_close_ts']))
        st.caption(f"{utc(meta['asof_ts'])} · Binance {meta['symbol']}.P · {meta['context']} [Ficha de Finora]({meta['source_url']})")

        cut = meta['asof_ts']
        if st.session_state.get('finora_last_case') != (run_name, choice):
            st.session_state['finora_reveal_post'] = False
        st.session_state['finora_last_case'] = (run_name, choice)
        cols = st.columns(3)
        graph_options = list(dict.fromkeys([meta['timeframe'], '15m', '5m', '1h', '4h']))
        chart_tf = cols[0].selectbox('Velas del gráfico', graph_options, key=f'finora_tf_{choice}')
        hours = cols[1].selectbox('Historia PRE (horas)', [12, 24, 48, 96], index=2, key='finora_hours')
        reveal = cols[2].checkbox('Revelar POST', key='finora_reveal_post', disabled=cut >= meta['data_end_ts'])
        post = {}
        if reveal:
            path = read_run_file(run, manifest, f'{choice}/post.json')
            post = json.loads(path.read_text(encoding='utf-8'))
        end = post.get('checked_through_ts', cut)
        primary = rows[rows.timeframe.eq(meta['timeframe']) | rows.family.isin(('HLL', 'MULTI_SWING'))]
        choices = [f for f in FAMILY_ORDER if f in set(primary.family)]
        enabled = st.multiselect('Capas del gráfico', choices, default=choices,
                                 format_func=LABELS.get, key=f'finora_layers_{choice}')
        overlays = primary[primary['rank'].eq(1) & primary.family.isin(enabled)]
        fig = comparison_plot(minutes, meta, overlays, chart_tf, hours, end, post.get('first_contact_closed_ts'))
        st.plotly_chart(fig, use_container_width=True, key='finora_comparison_plot')
        st.caption('El nivel publicado se muestra como guía de precio. Las capas calculadas aparecen desde su confirmación y se cortan en el PRE.')
        if meta['exact_swing_at_expected_time'] is not None:
            text = 'Encontrado con la misma vela y el mismo precio' if meta['exact_swing_at_expected_time'] else 'Sin coincidencia de vela y precio'
            st.caption(f"Pivote publicado del {utc(meta['pivot_open_ts'])}. {text}.")
        if meta['hll_status'] != 'FORMATIONS_VERIFIED':
            st.caption('El caso carece de un atlas HLL asociado a estas velas.')
        st.markdown('**Geometría más cercana por regla**')
        more = st.checkbox('Ver también las temporalidades auxiliares', key='finora_diagnostic_tfs')
        displayed = rows if more else primary
        table = ranked_table(displayed, displayed[displayed['rank'].eq(1)], meta['kind'])
        st.dataframe(table, hide_index=True, use_container_width=True)
        if post:
            touch = post['first_contact_closed_ts']
            if touch is None:
                st.caption(f'Sin contacto posterior observado hasta {utc(end)}.')
            else:
                st.markdown(f'**Primer contacto posterior al publicar · {utc(touch)}**')
                if post['closes_5m']:
                    st.dataframe(pd.DataFrame([
                        {'Cierre UTC': utc(bar['closed_ts']), 'Precio': number(bar['close']),
                         'Respecto a la zona': SIDES[bar['relation']]}
                        for bar in post['closes_5m']]), hide_index=True, use_container_width=True)
    except (OSError, ValueError, KeyError) as exc:
        st.error(f'No se pudo abrir la corrida. {exc}')
