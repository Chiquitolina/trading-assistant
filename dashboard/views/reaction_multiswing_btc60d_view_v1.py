"""Visor read-only BTC 60D Multi-Swing dentro de Reaction & Swing Lab.

No reconstruye pivotes al abrir el dashboard. Solo lee freeze y catálogo 6TF
verificados, agrupando identidades físicas *as-of* antes de cada corte.
POST es exclusivamente visual: nunca incorpora swings confirmados después del
corte seleccionado. No altera event journal, HLL, scores ni LIVE.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from btc_multiswing_60d_explorer_v1 import (
    DAY, M, TF_COLORS, TF_MINUTES, aggregate_closed, asof_rows,
    chart_window, group_asof, iso, load_snapshot, selected_inspection_window,
)


def build_figure(candles, swings, *, display_tf: str, selected=None, cutoff=None,
                 chart_end=None):
    import pandas as pd
    import plotly.graph_objects as go
    if candles.empty:
        raise ValueError('NO_CANDLES_TO_DRAW')
    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=pd.to_datetime(candles.timestamp,unit='ms',utc=True),
        open=candles.open, high=candles.high, low=candles.low,close=candles.close,
        name=f'BTCUSDT {display_tf}', increasing_line_color='#43ae90',
        decreasing_line_color='#df7070', showlegend=False,
    ))
    for tf in TF_MINUTES:
        for side in ('HIGH','LOW'):
            subset = swings[(swings.timeframe == tf) & (swings.side == side)]
            if subset.empty:
                continue
            xpos = pd.to_datetime(subset['plot_ts'],unit='ms',utc=True)
            labels = [
                f"{tf} {side} · {float(row.price):,.2f} USD"
                f"<br>Extremo: {iso(int(row.plot_ts))}"
                f"<br>Primera confirmación conocida: {iso(int(row.first_available_asof))}"
                f"<br>Última confirmación conocida: {iso(int(row.last_available_asof))}"
                f"<br>Temporalidades PRE: {row.tf_set_asof}"
                f"<br>Identidad: {row.physical_identity_state}"
                for row in subset.itertuples(index=False)
            ]
            fig.add_trace(go.Scattergl(
                x=xpos,y=subset.price, mode='markers',
                marker={'symbol':'triangle-down' if side=='HIGH' else 'triangle-up',
                        'size':8 if tf in ('1m','5m') else 10,
                        'color':TF_COLORS[tf], 'opacity':0.78},
                hovertext=labels, hoverinfo='text',
                name=f'{tf} {side}',
            ))
    if selected is not None:
        stamp=int(selected['plot_ts'])
        fig.add_trace(go.Scatter(
            x=[pd.to_datetime(stamp,unit='ms',utc=True)],
            y=[float(selected['price'])],mode='markers',
            marker={'symbol':'star','size':18,'color':'#f3ce50',
                    'line':{'width':1,'color':'#f8f8f8'}},
            name='Pivote seleccionado',
            hovertext=f"Seleccionado · {iso(stamp)} · {selected['tf_set_asof']}",
            hoverinfo='text',
        ))
    if cutoff is not None and chart_end is not None and int(chart_end)>int(cutoff):
        dt=pd.to_datetime(int(cutoff),unit='ms',utc=True)
        fig.add_shape(type='line',xref='x',yref='paper',x0=dt,x1=dt,
                      y0=0,y1=1,line={'color':'#f2c66d','width':2,'dash':'dash'})
        fig.add_annotation(x=dt,y=1,xref='x',yref='paper',text='Corte PRE',showarrow=False,
                           yanchor='bottom',font={'color':'#f2c66d','size':11})
    fig.update_layout(
        title=f'BTCUSDT · Multi-Swing histórico · velas {display_tf}',
        height=690, hovermode='closest', xaxis_rangeslider_visible=False,
        margin={'l':25,'r':25,'t':48,'b':25},legend={'orientation':'h','y':-0.12},
        xaxis_title='UTC',yaxis_title='Precio USD',
    )
    return fig


def _selected_cutoff_date(last_exclusive: int):
    return datetime.fromtimestamp((last_exclusive-M)/1000,timezone.utc).date()


def render_btc_multiswing_60d_view(project):
    import pandas as pd
    import streamlit as st

    st.markdown('### 🧭 Multi-Swing Structural Lab · BTC 60D · V1')
    st.caption('Catálogo congelado 1m / 5m / 15m / 30m / 1h / 4h. '
               'Los pivotes solo son elegibles cuando su confirmación está cerrada. '
               'Sin nuevos detectores, sin consultas de red y sin modificar LIVE.')
    try:
        full, catalog, source = load_snapshot(Path(project))
    except (FileNotFoundError, KeyError, ValueError, OSError) as exc:
        st.error(f'No se pudo abrir el BTC 60D congelado: {exc}')
        st.info('Comprobá el freeze y el catálogo 6TF; no se sustituyen por datos LIVE.')
        return

    start, end = source['start'], source['end']
    d1,d2,d3,d4=st.columns([2,1,2,1])
    with d1:
        selected_date=st.date_input(
            'Corte de exploración (UTC)',
            value=_selected_cutoff_date(end),
            min_value=datetime.fromtimestamp(start/1000,timezone.utc).date(),
            max_value=_selected_cutoff_date(end),
            key='ms60_cutoff_date')
    with d2:
        span_days=st.selectbox('Ventana (días)',[1,3,7,14,30,60],index=2,key='ms60_days')
    with d3:
        selected_tfs=tuple(st.multiselect('Temporalidades',list(TF_MINUTES),
              default=['15m','30m','1h','4h'],key='ms60_tfs'))
    with d4:
        side=st.selectbox('Tipo', ['BOTH','LOW','HIGH'],
                          format_func=lambda s: {'BOTH':'Ambos','LOW':'LOW','HIGH':'HIGH'}[s],
                          key='ms60_side')
    a,b,c=st.columns([2,2,2])
    with a:
        group_mode=st.selectbox('Identidad física',
                ['Una marca por extremo','Todas las representaciones TF'],
                key='ms60_group_mode')
    with b:
        multi_only=st.checkbox('Solo extremos con ≥2 TF confirmados',value=False,key='ms60_multi')
    with c:
        display_tf=st.selectbox('Velas del gráfico',list(TF_MINUTES),
                                index=3,key='ms60_display_tf')
    if not selected_tfs:
        st.info('Seleccioná una o más temporalidades para continuar.')
        return
    # Date picker selects a DAY INCLUSIVE in UTC. 8-Nov data ends at 01:00.
    midnight_after=datetime.combine(selected_date+timedelta(days=1),time.min,tzinfo=timezone.utc)
    exploration_cutoff=min(end,int(midnight_after.timestamp()*1000))
    explore_start,_=chart_window(exploration_cutoff,start,int(span_days))
    known=asof_rows(catalog,exploration_cutoff,selected_tfs,side)
    display=group_asof(known,collapse=group_mode.startswith('Una'),multi_only=multi_only)
    pool=display[(display['plot_ts']>=explore_start) & (display['plot_ts']<exploration_cutoff)].copy()
    pool=pool.sort_values(['plot_ts','timeframe'],ascending=[False,True]).reset_index(drop=True)
    verified=known.loc[known['physical_identity_state']=='VERIFIED','physical_id'].nunique()
    shared=known[known.physical_identity_state=='VERIFIED'].groupby('physical_id').size()
    x1,x2,x3,x4=st.columns(4)
    x1.metric('Representaciones PRE',len(known))
    x2.metric('Extremos físicos verificados PRE',verified)
    x3.metric('Extremos ≥2 TF PRE',int((shared>=2).sum()))
    x4.metric('Identidad ambigua PRE',int((known.physical_identity_state!='VERIFIED').sum()))
    st.caption(f"Corte: {iso(exploration_cutoff)} · Ventana desde {iso(explore_start)}. "
               'Las cantidades anteriores son PRE al corte y consideran únicamente los TF seleccionados.')
    if pool.empty:
        st.info('No hay pivotes confirmados para estos filtros y esta ventana.')
        return
    selector_count=500
    subset=pool.head(selector_count)
    keys=['']+subset['group_key'].astype(str).tolist() if group_mode.startswith('Una') else ['']+subset['pivot_id'].astype(str).tolist()
    keys=list(dict.fromkeys(keys))
    by_key={
        (str(row.group_key) if group_mode.startswith('Una') else str(row.pivot_id)):row
        for row in subset.itertuples(index=False)
    }
    chosen=st.selectbox('Inspeccionar un pivote (los 500 más recientes de la ventana)',keys,
        format_func=lambda k: 'Exploración general · sin seleccionar' if not k else (
            f"{iso(int(by_key[k].plot_ts))} · {by_key[k].side} · "
            f"{float(by_key[k].price):,.2f} · TF PRE {by_key[k].tf_set_asof} · "
            f"{by_key[k].physical_identity_state}"), key='ms60_selected')
    if len(pool)>selector_count:
        st.caption(f'Selector: últimos {selector_count} de {len(pool)} pivotes visibles. '
                   'Para inspeccionar anteriores, cambiá la fecha del corte y/o temporalidades. '
                   'El gráfico general sigue mostrando todos los pivotes que admite su límite visual.')
    chosen_row=None
    cutoff=exploration_cutoff
    chart_start=explore_start
    chart_end=exploration_cutoff
    if chosen:
        row=pool.loc[pool['group_key'].eq(chosen) if group_mode.startswith('Una')
                     else pool['pivot_id'].eq(chosen)].iloc[0]
        chosen_row=row
        r1,r2,r3=st.columns([2,2,2])
        with r1:
            before=int(st.selectbox('Horas PRE alrededor del pivote',[2,4,8,12,24,48],index=4,key='ms60_pre_hours'))
        with r2:
            reveal=st.checkbox('Revelar POST · solo velas',value=False,key='ms60_reveal_post')
        with r3:
            after=int(st.selectbox('Horas POST',[2,4,8,12,24],index=2,key='ms60_post_hours',
                                   disabled=not reveal))
        chart_start,chart_end,cutoff=selected_inspection_window(
            row,before,after,start,end,reveal)
        st.caption(f'Pivote: {iso(int(row.plot_ts))} · Corte PRE: {iso(cutoff)} · '
                   'Toda confirmación POST permanece oculta incluso al revelar velas futuras.')

    candles=aggregate_closed(full,display_tf,chart_start,chart_end)
    if candles.empty:
        st.warning('No hay velas completas en la temporalidad elegida hasta ese corte. '
                   'Elegí un timeframe menor o una ventana mayor.')
        return
    max_candles=4000
    if len(candles)>max_candles:
        st.warning(f'El gráfico contiene {len(candles):,} velas {display_tf} (límite visual {max_candles:,}). '
                   'Elegí un timeframe mayor o menos días; no reducimos datos silenciosamente.')
        return
    # Recompute as-of evidence at selected pivot's first/last confirmation.
    # Even with POST candles visible, never add future swing markers.
    asof = asof_rows(catalog,cutoff,selected_tfs,side)
    markers = group_asof(asof,collapse=group_mode.startswith('Una'),multi_only=multi_only)
    markers = markers[(markers['plot_ts']>=chart_start) & (markers['plot_ts']<cutoff)].copy()
    max_markers=3500
    if len(markers)>max_markers:
        st.warning(f'{len(markers):,} marcadores, sobre el límite visual {max_markers:,}. '
                   'Reducí ventana o temporalidades; no ocultamos pivotes sin avisar.')
        return
    fig=build_figure(candles,markers,display_tf=display_tf,
                     selected=chosen_row,cutoff=cutoff if chosen_row is not None else None,
                     chart_end=chart_end)
    st.plotly_chart(fig,use_container_width=True,key='ms60_chart',
                    config={'displaylogo':False,'scrollZoom':True})
    with st.expander('🔎 Pivotes confirmados en la ventana · evidencia PRE',expanded=chosen_row is not None):
        view=markers.copy().sort_values('plot_ts',ascending=False)
        if not view.empty:
            view['pivot_utc']=pd.to_datetime(view['plot_ts'],unit='ms',utc=True)
            view['available_first_utc']=pd.to_datetime(view['first_available_asof'],unit='ms',utc=True)
            view['available_last_utc']=pd.to_datetime(view['last_available_asof'],unit='ms',utc=True)
            st.dataframe(view[['pivot_utc','side','price','timeframe','tf_set_asof',
                  'representations_asof','available_first_utc','available_last_utc',
                  'physical_identity_state','pivot_id']].head(1000),
                  hide_index=True,use_container_width=True)
            if len(view)>1000:
                st.caption(f'Tabla: últimas 1000 de {len(view)} filas; el gráfico no se recortó.')
    with st.expander('📦 Procedencia y límites de interpretación',expanded=False):
        st.json({'symbol':'BTCUSDT','catalog':'6TF v1.1','source_sha256':source['source_sha256'],
                 'catalog_sha256':source['catalog_sha256'],
                 'HLL_30m_parity':source['source_hll_parity'],
                 'start':iso(start),'end_exclusive':iso(end),
                 'plot_end':iso(chart_end),'evidence_cutoff':iso(cutoff),
                 'confirmed_records_asof':len(asof),'rendered_markers':len(markers),
                 'rendered_closed_candles':len(candles)})
        st.caption('Las marcas se posicionan en el extremo/pivote histórico; '
                   'su disponibilidad causal se determina por available_ts. '
                   'TIED_EXTREMA no se agrupa en una falsa identidad física. '
                   'Departure, cruces, HLL y outcomes aún NO se calculan en este visor V1.')
