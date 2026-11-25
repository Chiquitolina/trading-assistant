"""Show immutable band births and observed endings from the causal hourly journal."""
from datetime import datetime, time, timezone
from pathlib import Path
from math import log10

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from finora_chart_reconstruction_v1 import HOUR, stamp, utc
from finora_band_lifecycle_v12 import at_cutoff, load_sources, read_journal

VIEW_VERSION = '1.2.2'

EVENT_NAMES = {'DETECTED': 'Primera detección', 'CONTACT': 'Contacto', 'INVALIDATED': 'Invalidación',
               'FILLED': 'Hueco rellenado por mecha',
               'CONTEXT_UPDATED': 'Contexto actualizado', 'SELECTED': 'Elegida otra vez',
               'NOT_SELECTED': 'Fuera de la selección'}
STATUS_NAMES = {'ACTIVE': 'Vigente sin contacto', 'CONTACTED': 'Vigente con contacto',
                'INVALIDATED': 'Invalidada', 'FILLED': 'Hueco rellenado'}


def when(value):
    return '—' if value is None or pd.isna(value) else utc(int(value))


def band_table(rows):
    return pd.DataFrame([{'Banda': z['band_id'], 'Tipo': 'Hueco' if z['family'] == 'GAP' else
        ('Demanda' if z['side'] == 'BUY' else 'Oferta'),
        'Inferior': z['low'], 'Superior': z['high'], 'Origen UTC': when(z['origin_ts']),
        'Ancho (%)': round(100 * (z['high'] - z['low']) / z['low'], 3),
        'Geometría UTC': when(z['formed_ts']), 'Primera detección UTC': when(z['first_detected_ts']),
        'Primer contacto UTC': when(z['first_contact_ts']), 'Invalidada UTC': when(z['invalidated_ts']),
        'Hueco rellenado UTC': when(z['filled_ts']),
        'Contactos': z['contacts'], 'Estado al corte': STATUS_NAMES[z['status']],
        'Elegida al corte': bool(z['selected']), 'Cobertura inicial limitada': bool(z['coverage_limited'])}
        for z in rows])


def lifecycle_figure(result, identities, show_origin=True, show_contacts=True, include_distant=False):
    left = pd.to_datetime(result['start'], unit='ms', utc=True)
    right = pd.to_datetime(result['cutoff'], unit='ms', utc=True)
    bars = result['bars']
    candles = go.Candlestick(x=pd.to_datetime(bars.available_ts, unit='ms', utc=True),
        open=bars.open, high=bars.high, low=bars.low, close=bars.close,
        increasing_line_color='#47baa3', decreasing_line_color='#ed7d8b', name='Velas cerradas 1h', legendrank=0)
    fig = go.Figure()
    zones = {z['band_id']: z for z in result['bands'] if z['band_id'] in identities}
    sides = set()
    for z in zones.values():
        born = pd.to_datetime(z['first_detected_ts'], unit='ms', utc=True)
        end = min(right, pd.to_datetime(z['ended_ts'], unit='ms', utc=True)) if z['ended_ts'] is not None else right
        color = '#abb5c6' if z['family'] == 'GAP' else ('#47baa3' if z['side'] == 'BUY' else '#ed7d8b')
        fill = 'rgba(171,181,198,.17)' if z['family'] == 'GAP' else (
               'rgba(71,186,163,.17)' if z['side'] == 'BUY' else 'rgba(237,125,139,.17)')
        category = 'GAP' if z['family'] == 'GAP' else z['side']
        if show_origin:
            origin = max(left, pd.to_datetime(z['origin_ts'] + HOUR, unit='ms', utc=True))
            if origin < born:
                fig.add_shape(type='rect', x0=origin, x1=min(born, right), y0=z['low'], y1=z['high'],
                    line=dict(color='#94a0b3', width=1, dash='dot'), fillcolor='rgba(148,160,179,.05)', layer='below')
        if end >= left:
            start = max(left, born)
            kind = 'Hueco' if z['family'] == 'GAP' else ('Demanda' if z['side'] == 'BUY' else 'Oferta')
            text = (f"{z['band_id']}<br>{kind} · "
                    f"{z['low']:g}–{z['high']:g}<br>Primera detección: {when(z['first_detected_ts'])}"
                    f"<br>Fin observado: {when(z['ended_ts'])}<br>{STATUS_NAMES[z['status']]}")
            fig.add_scatter(x=[start, end, end, start, start],
                y=[z['low'], z['low'], z['high'], z['high'], z['low']], mode='lines', fill='toself',
                fillcolor=fill, line=dict(color=color, width=1),
                name='Hueco disponible' if z['family'] == 'GAP' else
                     ('Demanda confirmada' if z['side'] == 'BUY' else 'Oferta confirmada'),
                legendgroup=category, showlegend=category not in sides,
                hovertemplate=text + '<extra></extra>')
            sides.add(category)
            if z['ended_ts'] is not None and result['start'] <= z['ended_ts'] <= result['cutoff']:
                fig.add_scatter(x=[end, end], y=[z['low'], z['high']], mode='lines', showlegend=False,
                    line=dict(color='#bfc8d7' if z['family'] == 'GAP' else '#ff6074', width=2.5),
                    hovertemplate=f"Fin de {kind.lower()} {z['low']:g}–{z['high']:g}<br>"
                                  f"{when(z['ended_ts'])}<extra></extra>")
    # Price stays above the historical fills when many bands overlap.
    fig.add_trace(candles)
    events = result['events'][result['events'].band_id.isin(zones)]
    definitions = [('DETECTED', 'Primera detección', '#ffd166', 'diamond'),
                   ('CONTACT', 'Contacto de vela', '#8cc9ef', 'circle-open'),
                   ('INVALIDATED', 'Invalidación por cierre', '#ff6074', 'x'),
                   ('FILLED', 'Hueco rellenado', '#bfc8d7', 'square-open')]
    for kind, name, color, symbol in definitions:
        if kind == 'CONTACT' and not show_contacts:
            continue
        frame = events[(events.kind == kind) & (events.available_ts >= result['start'])]
        if frame.empty:
            continue
        text = [f"{EVENT_NAMES[kind]} · {row.band_id}<br>Franja {zones[row.band_id]['low']:g}–{zones[row.band_id]['high']:g}"
                f"<br>{when(row.available_ts)}"
                f"<br>Vela abierta: {when(row.bar_open_ts)}<br>Rango: {row.low:g}–{row.high:g}"
                f"<br>Cierre: {row.close:g}" for row in frame.itertuples(index=False)]
        fig.add_scatter(x=pd.to_datetime(frame.available_ts, unit='ms', utc=True), y=frame.price,
            mode='markers', name=name, marker=dict(color=color, symbol=symbol, size=10, line=dict(width=2)),
            text=text, hovertemplate='%{text}<extra></extra>')
    if show_origin:
        fig.add_scatter(x=[None], y=[None], mode='lines', name='Origen retrospectivo · aún no disponible',
                        line=dict(color='#94a0b3', dash='dot', width=1))
    fig.update_layout(template='plotly_dark', height=660, margin=dict(l=40, r=30, t=105, b=45),
        xaxis_title='Cierre de vela · UTC', yaxis_title='USDT', yaxis_type='log',
        xaxis_rangeslider_visible=False,
        legend=dict(orientation='h', y=1.16, font=dict(size=10)))
    fig.update_xaxes(range=[left, right + pd.Timedelta(hours=2)])
    if not include_distant and not bars.empty:
        fig.update_yaxes(range=[log10(float(bars.low.min()) * .985), log10(float(bars.high.max()) * 1.015)])
    return fig


def render_lifecycle(project):
    st.subheader(f'Bandas y ciclo de vida · V{VIEW_VERSION}')
    st.caption('Primera detección conservada por banda. El tramo punteado es su origen retrospectivo; '
               'la franja de color comienza cuando el detector pudo seleccionarla.')
    try:
        manifest = load_sources(project)
        sources = {s['key']: s for s in manifest['datasets']}
        options = list(sources)
        key = st.selectbox('Símbolo y mercado', options,
            index=options.index('SHELLUSDT_spot') if 'SHELLUSDT_spot' in options else 0,
            format_func=lambda k: sources[k]['symbol'] + ' · Binance ' +
            ('spot' if sources[k]['market'] == 'spot' else 'perpetuo'), key='finora_life_symbol')
        source, bands, events, meta = read_journal(project, key)
        default = pd.Timestamp(meta['default_cutoff_utc'])
        earliest = pd.to_datetime(int(source.timestamp.iloc[0]) + 28 * HOUR, unit='ms', utc=True)
        latest = pd.to_datetime(int(source.available_ts.iloc[-1]), unit='ms', utc=True)
        a, b, c = st.columns(3)
        day = a.date_input('Fecha del corte UTC', value=default.date(), min_value=earliest.date(),
            max_value=latest.date(), key=f'finora_life_day_{key}')
        clock = b.time_input('Hora del corte UTC', value=time(default.hour), step=3600,
            key=f'finora_life_time_{key}')
        days = c.selectbox('Historia visible (días)', [1, 3, 7, 12, 20, 30, 45, 60], index=3,
            key='finora_life_days')
        at = stamp(datetime.combine(day, clock, timezone.utc))
        result = at_cutoff(source, bands, events, at, days)
        st.caption(f"Fuente congelada: {when(int(source.timestamp.iloc[0]))} → {when(int(source.available_ts.iloc[-1]))}. "
                   f"Último cierre visible: {result['bars'].close.iloc[-1]:g}. "
                   'El selector conserva su ventana de cálculo de 12 días; el gráfico admite hasta 60.')
        if at - days * 24 * HOUR < int(source.timestamp.iloc[0]):
            st.info('El encuadre solicita más historia que la disponible antes de este corte; se muestra la cobertura real.')
        in_view = [z for z in result['bands'] if z['ended_ts'] is None or z['ended_ts'] >= result['start']]
        alive_only = st.checkbox('Solo franjas vigentes al corte', value=False, key='finora_life_alive_only_v122')
        if alive_only:
            in_view = [z for z in in_view if z['ended_ts'] is None]
        in_view.sort(key=lambda z: (z['first_detected_ts'], z['band_id']), reverse=True)
        mapping = {z['band_id']: z for z in in_view}
        def label(identity):
            z = mapping[identity]
            kind = 'Hueco' if z['family'] == 'GAP' else ('Compra' if z['side'] == 'BUY' else 'Venta')
            return f"{kind} {z['low']:g}–{z['high']:g} · " + \
                   f"{when(z['first_detected_ts'])} · {STATUS_NAMES[z['status']]}"
        scope = st.radio('Franjas visibles', ['ALL', 'MANUAL'], horizontal=True,
            format_func=lambda value: 'Todas las del período' if value == 'ALL' else 'Selección manual',
            key='finora_life_scope_v121')
        if scope == 'ALL':
            chosen = list(mapping)
        else:
            wanted = [z['band_id'] for z in in_view if z['selected']]
            wanted += [z['band_id'] for z in in_view if z['band_id'] not in wanted][:max(0, 8 - len(wanted))]
            chosen = st.multiselect('Bandas en el gráfico', list(mapping), default=wanted,
                format_func=label, key=f'finora_life_bands_manual_v121_{key}_{at}_{days}_{int(alive_only)}')
        st.caption(f'Mostrando {len(chosen)} de {len(mapping)} franjas del período. '
                   'Se incluyen las antiguas e invalidadas cuyo ciclo atraviesa el encuadre; '
                   'cada una conserva su primera detección y su final observado.')
        d, e = st.columns(2)
        origin = d.checkbox('Mostrar origen previo a la confirmación', value=True, key='finora_life_origin')
        contact = e.checkbox('Marcar contactos', value=True, key='finora_life_contacts')
        distant = st.checkbox('Incluir franjas lejanas en la escala de precio', value=False,
                              key='finora_life_distant_v122')
        st.plotly_chart(lifecycle_figure(result, chosen, origin, contact, distant),
            use_container_width=True, key='finora_life_chart')
        lower = float(result['bars'].low.min()) * .985
        upper = float(result['bars'].high.max()) * 1.015
        outside = sum(mapping[k]['high'] < lower or mapping[k]['low'] > upper for k in chosen)
        if outside and not distant:
            st.caption(f'{outside} franjas quedan fuera de la escala centrada en las velas. '
                       'Activá Incluir franjas lejanas para verlas. Permanecen en las tablas.')
        st.caption('Rombo amarillo: primera detección. Círculo azul: inicio de un contacto. Cruz roja: '
                   'primer cierre de invalidación. Cuadrado gris: hueco rellenado por mecha. '
                   'Las horas son cierres de velas de 1h; el orden intravela '
                   'y el precio de ejecución no se infieren. El contacto puede compartir vela con la invalidación.')
        shown = [mapping[k] for k in chosen]
        wide = [z for z in shown if z['family'] == 'BAND' and (z['high'] - z['low']) / z['low'] > .03]
        if wide:
            st.info(f'{len(wide)} bandas superan el 3 % de ancho. El modelo toma el rango completo de su vela '
                    'de origen; estas zonas aún requieren revisar su geometría. El ancho figura en la tabla.')
        table = band_table(shown)
        st.markdown('**Ciclo de las bandas mostradas**')
        st.dataframe(table, hide_index=True, use_container_width=True)
        observed = result['events'][result['events'].band_id.isin(chosen)].copy()
        event_table = pd.DataFrame([{'Banda': row.band_id, 'Evento': EVENT_NAMES[row.kind],
            'Disponible UTC': when(row.available_ts), 'Vela de origen UTC': when(row.bar_open_ts),
            'Cierre observado': row.close, 'Contexto UTC': when(row.context_ts)}
            for row in observed.itertuples(index=False)])
        with st.expander('Eventos conocidos al corte', expanded=False):
            st.dataframe(event_table, hide_index=True, use_container_width=True)
        with st.expander('Catálogo de bandas de esta ventana', expanded=False):
            st.dataframe(band_table(in_view), hide_index=True, use_container_width=True)
        st.download_button('Descargar ciclo de bandas al corte', table.to_csv(index=False),
            f'{key}_ciclo_al_corte.csv', mime='text/csv', key='finora_life_export_bands')
        st.download_button('Descargar eventos conocidos al corte', event_table.to_csv(index=False),
            f'{key}_eventos_al_corte.csv', mime='text/csv', key='finora_life_export_events')
        with st.expander('Cómo se conserva el ciclo', expanded=False):
            st.write('Cada cierre horario reproduce el selector V1.1 usando solo las velas disponibles. '
                     'La primera selección queda fija por identidad física de banda. Los cambios de contexto '
                     'posteriores se guardan como eventos separados. Una banda detectada continúa en el registro '
                     'aunque entre el precio en ella o deje de ser elegida. Los contactos consecutivos forman '
                     'un solo episodio. La oferta termina en el primer cierre horario sobre su borde superior; '
                     'la demanda, bajo su borde inferior. El dibujo sólido termina en ese cierre. '
                     'Los huecos grises terminan cuando una mecha posterior llena todo su rango. Las bandas '
                     'vigentes llegan al corte y su resultado posterior aún está sin observar. '
                     'La dirección de nacimiento se conserva; no se generan cambios automáticos de función.')
            st.caption('Confirmación significa primera detección por esta hipótesis. La coincidencia con Finora '
                       'y la utilidad de las bandas requieren validación independiente. El origen punteado '
                       'solo se reconstruye para bandas cuya detección ya es conocida al corte. Las bandas '
                       'nacidas durante la cobertura inicial limitada se identifican en las tablas.')
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        st.error(f'No se pudo abrir el ciclo de vida. {exc}')
