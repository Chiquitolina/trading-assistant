"""Show candle-driven reconstruction hypotheses alongside the supplied chart images."""

from datetime import datetime, time, timezone
import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from finora_chart_reconstruction_v1 import BASE, HOUR, MODEL_VERSION, Model, read_source, reconstruct, stamp, utc


def figure(result, visible_days=None):
    bars = result['bars']
    right = pd.to_datetime(result['cutoff'], unit='ms', utc=True)
    left = pd.to_datetime(result['start'], unit='ms', utc=True)
    if visible_days is not None:
        left = max(left, right - pd.Timedelta(days=visible_days))
        bars = bars[pd.to_datetime(bars.available_ts, unit='ms', utc=True) > left]
    chart = go.Figure(go.Candlestick(
        x=pd.to_datetime(bars.available_ts, unit='ms', utc=True),
        open=bars.open, high=bars.high, low=bars.low, close=bars.close,
        increasing_line_color='#47baa3', decreasing_line_color='#ed7d8b', name='Velas cerradas 1h'))
    for line in result['lines']:
        chart.add_scatter(x=[left, right], y=[line['price']] * 2, mode='lines',
                          name=f"Línea {line['price']:g}", line=dict(color='#c7d1df', width=1),
                          hovertemplate=f"{line['role']}<br>{line['price']:g}<extra></extra>")
    for zone in result['zones']:
        origin = pd.to_datetime(zone['origin_ts'] + HOUR, unit='ms', utc=True)
        color = '#4bbda7' if zone['side'] == 'BUY' else '#e88194'
        name = 'Zona de compra candidata' if zone['side'] == 'BUY' else 'Zona de venta candidata'
        chart.add_shape(type='rect', x0=max(left, origin), x1=right, y0=zone['low'], y1=zone['high'],
                        fillcolor=color, opacity=.23, line=dict(color=color, width=1), layer='below')
        chart.add_scatter(x=[right], y=[zone['entry_edge']], mode='markers', name=name,
                          marker=dict(color=color, size=9, symbol='triangle-up' if zone['side'] == 'BUY'
                                      else 'triangle-down'),
                          hovertemplate=f"Borde del modelo {zone['entry_edge']:g}<br>{zone['rule']}<extra></extra>")
        if result.get('supply_rule') != 'structure_v11' and zone['gap_low'] is not None:
            chart.add_shape(type='rect', x0=max(left, origin), x1=right,
                            y0=zone['gap_low'], y1=zone['gap_high'], fillcolor='#a5afbd', opacity=.20,
                            line=dict(width=0), layer='below')
    for gap in result.get('gaps', []):
        chart.add_shape(type='rect', x0=max(left, pd.to_datetime(gap['origin_ts'] + HOUR, unit='ms', utc=True)),
                        x1=right, y0=gap['low'], y1=gap['high'], fillcolor='#a5afbd', opacity=.25,
                        line=dict(width=0), layer='below')
        chart.add_scatter(x=[right, right], y=[gap['low'], gap['high']], mode='lines',
                          name=f"Hueco sin llenar {gap['low']:g}–{gap['high']:g}",
                          line=dict(color='#a5afbd', width=3), showlegend=False,
                          hovertemplate=f"Hueco bajista · {gap['low']:g}–{gap['high']:g}<br>"
                          f"Disponible con el contexto {utc(gap['known_ts'])}<extra></extra>")
    chart.update_layout(template='plotly_dark', height=560, margin=dict(l=40, r=30, t=50, b=45),
                        xaxis_title='Cierre de vela · UTC', yaxis_title='USDT',
                        yaxis_type='log',
                        xaxis_rangeslider_visible=False, legend=dict(orientation='h', y=1.08, font=dict(size=10)))
    chart.update_xaxes(range=[left, right + pd.Timedelta(hours=5)])
    return chart


def render_reconstruction(project):
    st.subheader('Reconstrucción desde las velas')
    st.caption(f'Hipótesis V{MODEL_VERSION}. Calcula líneas y bandas desde las velas cerradas y el corte. '
               'Las capturas de Finora sirven para revisar los resultados.')
    folder = Path(project) / BASE
    try:
        manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
        if manifest['version'] != 'finora-reconstruction-v1':
            raise ValueError('Versión de datos desconocida')
        sources = {source['key']: source for source in manifest['datasets']}
        options = list(sources)
        key = st.selectbox('Símbolo y mercado', options,
                           index=options.index('NVDAUSDT_perpetual') if 'NVDAUSDT_perpetual' in options else 0,
                           format_func=lambda k: sources[k]['symbol'] + ' · Binance ' +
                           ('spot' if sources[k]['market'] == 'spot' else 'perpetuo'),
                           key='finora_rebuild_symbol')
        hourly, source = read_source(project, key)
        default = pd.Timestamp(source['default_cutoff_utc'])
        lower = pd.to_datetime(int(hourly.timestamp.iloc[0]) + 32 * HOUR, unit='ms', utc=True)
        upper = pd.to_datetime(int(hourly.available_ts.iloc[-1]), unit='ms', utc=True)
        a, b, c = st.columns(3)
        day = a.date_input('Fecha del corte UTC', value=default.date(), min_value=lower.date(),
                            max_value=upper.date(), key=f'finora_rebuild_day_{key}')
        clock = b.time_input('Hora del corte UTC', value=time(default.hour), step=3600,
                              key=f'finora_rebuild_time_{key}')
        days = c.selectbox('Historia visible (días)', [7, 10, 12], index=2, key='finora_rebuild_days')
        supply_rule = st.radio('Selección de oferta', ['structure_v11', 'compact_v1'],
                              format_func=lambda value: 'V1.1 · bases y tramos de 4h' if value == 'structure_v11'
                              else 'V1 anterior · bandas más angostas', horizontal=True,
                              key='finora_rebuild_supply_rule')
        at = stamp(datetime.combine(day, clock, timezone.utc))
        result = reconstruct(hourly, at, Model(supply_rule=supply_rule))
        st.caption(f"{source['symbol']} · última vela cerrada {utc(int(result['bars'].available_ts.iloc[-1]))}"
                   f" · último cierre {result['last_price']:g}")
        st.plotly_chart(figure(result, visible_days=days), use_container_width=True, key='finora_rebuild_chart')
        st.caption('Cálculo sobre una ventana de 12 días; Historia visible cambia el encuadre. '
                   'Los rectángulos parten del origen. Las tablas separan la formación de la geometría y la '
                   'confirmación del contexto usado al seleccionar. Los triángulos marcan el borde candidato del modelo.')

        st.markdown('**Líneas generadas**')
        lines = pd.DataFrame([
            {'Precio': row['price'], 'Origen UTC': utc(row['anchor_ts']),
             'Conocido UTC': utc(row['known_ts']), 'Regla': row['role']}
            for row in result['lines']])
        st.dataframe(lines, hide_index=True, use_container_width=True)
        st.markdown('**Bandas y bordes candidatos**')
        zones = pd.DataFrame([
            {'Lado': 'Compra' if row['side'] == 'BUY' else 'Venta',
             'Inferior': row['low'], 'Superior': row['high'], 'Borde candidato': row['entry_edge'],
             'Vela de origen UTC': utc(row['origin_ts']),
             'Geometría formada UTC': utc(row.get('formed_ts', row['known_ts'])),
             'Disponible con contexto UTC': utc(row['known_ts']),
             'Regla': row['rule']} for row in result['zones']])
        st.dataframe(zones, hide_index=True, use_container_width=True)
        st.download_button('Descargar líneas del modelo', lines.to_csv(index=False), f'{key}_lineas.csv',
                           mime='text/csv', key='finora_rebuild_download_lines')
        st.download_button('Descargar bandas del modelo', zones.to_csv(index=False), f'{key}_bandas.csv',
                           mime='text/csv', key='finora_rebuild_download_zones')
        if supply_rule == 'structure_v11':
            with st.expander('Oferta considerada por tramo de 4h', expanded=False):
                candidates = pd.DataFrame([
                    {'Máximo 4h': row['peak_price'], 'Vela del máximo UTC': utc(row['peak_ts']),
                     'Inferior': row['low'], 'Superior': row['high'],
                     'Vela de origen UTC': utc(row['origin_ts']),
                     'Geometría formada UTC': utc(row['formed_ts']),
                     'Disponible con contexto UTC': utc(row['known_ts']),
                     'Selección': 'Mostrada' if row['selected'] else 'Otro tramo', 'Regla': row['rule']}
                    for row in result['supply_candidates']])
                if candidates.empty:
                    st.info('Este corte no tiene oferta compacta válida en los tramos confirmados.')
                else:
                    st.dataframe(candidates, hide_index=True, use_container_width=True)
        with st.expander('Captura original de Finora', expanded=False):
            st.image(str(folder / source['image']), use_container_width=True)
            st.caption('La hora UTC exacta de la captura está pendiente. El corte inicial del modelo usa las 14:00 UTC del 24 de noviembre.')
        with st.expander('Reglas de la hipótesis y alcance', expanded=False):
            st.write('La estructura principal usa pivotes 3×3 de 4h y combina líneas cercanas al 0,20 %. '
                     'La demanda usa la vela horaria que contiene el último mínimo principal válido. '
                     'V1.1 agrupa la oferta por el tramo entre un máximo 4h confirmado y el siguiente mínimo '
                     '4h confirmado, o el corte si aún no lo hay. Prioriza la primera base neutra anterior a un '
                     'hueco bajista: cuerpo de hasta 25 % del rango. Como alternativas usa un máximo horario '
                     'compacto cercano al máximo 4h, o el origen alcista del primer hueco válido del tramo. '
                     'El ancho máximo de oferta es 3 % de su borde superior. Conserva hasta dos contextos: '
                     'el máximo de mayor precio y el más reciente elegibles; completa por recencia sin '
                     'solapamientos. Un cierre sobre el borde superior invalida la oferta desde su formación. '
                     'Los huecos grises se muestran cerca de las bandas seleccionadas mientras una mecha '
                     'posterior no haya llenado todo el hueco. La selección se evalúa al corte.')
            st.caption('La hipótesis se ajustó con estas tres capturas. Aún falta validarla en otros cortes. '
                       'Las horizontales conservan la regla V1 y en BTC siguen presentando diferencias. '
                       'Los bordes candidatos requieren contraste con las entradas publicadas por Finora.')
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        st.error(f'No se pudo reconstruir el gráfico. {exc}')
