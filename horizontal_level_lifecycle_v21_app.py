#!/usr/bin/env python3
"""Read-only Streamlit inspector for Horizontal Level Lifecycle V2.1.

Run with streamlit run horizontal_level_lifecycle_v21_app.py --server.port 8521.
Can also import render_hll_v21(root) inside an existing Streamlit dashboard.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from horizontal_breakout_lab import Archive, TF
from horizontal_level_lifecycle_v21 import DEFAULT_OUTPUT, load_30m, _overlap_ratio


# External screenshots supplied by the user. These are comparison anchors,
# never detector signals and never used to construct or filter price levels.
REFERENCE_CASES = {
    'INITUSDT': {
        'entry_local': '2026-10-22 15:30',
        'entry_price': 0.1304,
        'tp4': 0.1486,
        'description': 'Captura LuxQuant: TP4 +14.0% anunciado',
    },
    'TREEUSDT': {
        'entry_local': '2026-10-23 10:50',
        'entry_price': 0.0396,
        'tp4': 0.0438,
        'description': 'Captura LuxQuant: TP4 +10.6% anunciado',
    },
}


def reference_clock(case, timezone='UTC'):
    """Return reference instant and last completed 30m candle close (ms).

    LuxQuant screenshots do not specify a timezone. The operator explicitly
    chooses an interpretation; no lookahead is introduced by this annotation.
    """
    t = pd.Timestamp(case['entry_local']).tz_localize(timezone).tz_convert('UTC')
    event_ms = int(t.timestamp() * 1000)
    return event_ms, (event_ms // TF) * TF


def active_zones_at_reference(levels, ends, events, cutoff, price):
    """Rank only levels ALREADY formed and still active at a known cutoff.

    The ranking is for visual comparison, not a success-based detector filter.
    Only TEST records that were observable at/before cutoff are counted.
    """
    if levels.empty or ends.empty:
        return pd.DataFrame()
    relevant = levels.merge(
        ends[['level_id', 'end_closed_ts']], on='level_id',
        validate='one_to_one', how='left')
    relevant = relevant[
        (relevant.formation_closed_ts <= cutoff) &
        (relevant.end_closed_ts > cutoff)
    ].copy()
    if relevant.empty:
        return relevant
    price = float(price)
    relevant['gap_price_pct'] = (
        100 * (relevant.zone_low.sub(price).clip(lower=0) +
               (price - relevant.zone_high).clip(lower=0)) / price
    )
    relevant['center_distance_pct'] = 100 * abs(relevant.center - price) / price
    known = pd.Series(dtype='int64')
    if not events.empty:
        e = events[(events.observed_closed_ts <= cutoff) &
                   (events.event_type.isin(('TEST_FROM_ABOVE', 'TEST_FROM_BELOW')))]
        known = e.groupby('level_id').size()
    relevant['tests_known_pre'] = relevant.level_id.map(known).fillna(0).astype(int)
    return relevant.sort_values(['gap_price_pct', 'center_distance_pct',
                                 'formation_closed_ts']).reset_index(drop=True)


def _read(p):
    if not p.is_file():
        return pd.DataFrame()
    try:
        return pd.read_csv(p)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _time(ms):
    return pd.Timestamp(int(ms), unit='ms', tz='UTC')


def _overlap_stats(levels, ends):
    if levels.empty or ends.empty:
        return 0, 0
    f = levels.merge(ends[['level_id', 'end_closed_ts']], on='level_id', how='left', validate='one_to_one')
    touched = set()
    pairs = 0
    for (_, origin), group in f.groupby(['symbol','origin']):
        rows = group.to_dict('records')
        for ix, a in enumerate(rows):
            for b in rows[ix+1:]:
                if (max(int(a['formation_closed_ts']),int(b['formation_closed_ts'])) >
                    min(int(a['end_closed_ts']),int(b['end_closed_ts']))):
                    continue
                if _overlap_ratio(float(a['zone_low']),float(a['zone_high']),
                                  float(b['zone_low']),float(b['zone_high'])) > 0:
                    pairs += 1
                    touched.update([a['level_id'],b['level_id']])
    return pairs, len(touched)


def _chart(candles, lv, events, others, cutoff, mode, pre_bars=90, post_bars=100,
           reference_ms=None, reference_cutoff=None, reference_price=None):
    freeze = int(lv['formation_closed_ts'])
    last = int(candles.timestamp.max())+TF
    if mode == 'FORMACION':
        end = freeze
    elif mode == 'EVENTO':
        end = int(cutoff)
    elif mode == 'REFERENCIA':
        end = min(last, int(reference_cutoff))
    else:
        end = min(last, (int(reference_cutoff) if reference_cutoff is not None else freeze)
                  + post_bars*TF)
    left = ((int(reference_cutoff) if reference_cutoff is not None and
             mode in ('REFERENCIA','COMPLETO') else freeze) - pre_bars*TF)
    if end < freeze:
        return None
    shown = candles[(candles.timestamp>=left)&(candles.timestamp+TF<=end)]
    if shown.empty:
        return None
    x = pd.to_datetime(shown.timestamp,unit='ms',utc=True)
    fig = make_subplots(rows=2,cols=1,shared_xaxes=True,vertical_spacing=.035,
                        row_heights=[.80,.20])
    fig.add_trace(go.Candlestick(x=x,open=shown.open,high=shown.high,
        low=shown.low,close=shown.close,name='OHLC 30m',
        increasing_line_color='#10b981',decreasing_line_color='#f87171',
        increasing_fillcolor='#10b981',decreasing_fillcolor='#f87171'),row=1,col=1)
    fig.add_trace(go.Bar(x=x,y=shown.volume,name='Volumen',
        marker_color='#64748b',opacity=.60),row=2,col=1)
    lo, hi = float(lv['zone_low']),float(lv['zone_high'])
    center=float(lv['center'])
    p1=int(lv['first_pivot_open_ts'])
    # Before freeze the candidate zone exists only as a retrospective drawing.
    if p1 < freeze and left <= freeze:
        fig.add_trace(go.Scatter(x=[_time(max(left,p1)),_time(freeze)],
            y=[center,center],mode='lines',name='Reconstrucción (NO operable)',
            line=dict(color='#a37f4f',dash='dot',width=1.25)),row=1,col=1)
    if end >= freeze:
        fig.add_shape(type='rect',x0=_time(max(freeze,left)),x1=_time(end),
            y0=lo,y1=hi,xref='x',yref='y',
            fillcolor='rgba(245,158,11,0.12)',
            line=dict(color='rgba(245,158,11,.35)',width=1),row=1,col=1)
        fig.add_trace(go.Scatter(x=[_time(max(freeze,left)),_time(end)],y=[center,center],
            mode='lines',name='Nivel congelado',line=dict(color='#fbbf24',width=2)),
            row=1,col=1)
    if left <= freeze <= end:
        fig.add_vline(x=_time(freeze),line_color='#94a3b8',line_dash='dot',row=1,col=1)
    pivots=shown[shown.timestamp.isin([int(lv['first_pivot_open_ts']),
                                        int(lv['second_pivot_open_ts'])])]
    if len(pivots):
        fig.add_trace(go.Scatter(x=pd.to_datetime(pivots.timestamp,unit='ms',utc=True),
            y=pivots.high if lv['origin']=='HIGH' else pivots.low,
            mode='markers',name='Pivotes fundadores',
            marker=dict(size=11,symbol='circle-open',color='#fbbf24',line=dict(width=2))),
            row=1,col=1)
    if mode!='FORMACION' and not events.empty:
        visible = events[events.observed_closed_ts<=end]
        for types, title, symbol, color in [
            (('TEST_FROM_ABOVE','TEST_FROM_BELOW'), 'Retests armados', 'diamond', '#38bdf8'),
            (('DEFENSE_CONFIRMED',), 'Defensa confirmada POST', 'triangle-up', '#34d399'),
            (('LOSS_CONFIRMED',), 'Nivel perdido POST', 'x', '#fb7185'),
            (('CROSS_UP','CROSS_DOWN'), 'Cruces al cierre', 'circle-open', '#a78bfa'),
        ]:
            subset=visible[visible.event_type.isin(types)]
            if subset.empty:
                continue
            labels = [f"{q.event_type} · #{int(q.visit_number)}" for q in subset.itertuples()]
            fig.add_trace(go.Scatter(
                x=pd.to_datetime(subset.observed_closed_ts,unit='ms',utc=True),
                y=subset.close,mode='markers',name=title,text=labels,
                hovertemplate='%{text}<br>%{x}<br>Close %{y:.8f}<extra></extra>',
                marker=dict(size=11,symbol=symbol,color=color)),row=1,col=1)
    # Other zones are shown ONLY if they already existed at the selected cutoff;
    # never use their eventual test counts as formation-time features.
    if others is not None and not others.empty:
        for z in others.itertuples():
            if int(z.formation_closed_ts)>end:continue
            end_other = min(end,int(z.end_closed_ts))
            if end_other < max(left,int(z.formation_closed_ts)):
                continue
            fig.add_trace(go.Scatter(x=[_time(max(left,int(z.formation_closed_ts))),_time(end_other)],
                y=[float(z.center)]*2,mode='lines',
                name=f"Otro {z.origin} · {str(z.level_id)[:6]}",
                line=dict(color='rgba(148,163,184,.42)',width=1,dash='dot'),
                showlegend=False,hoverinfo='skip'),row=1,col=1)
    if (reference_ms is not None and reference_price is not None and
            left <= int(reference_ms) <= end + TF):
        # An EXTERNAL marker, not a signal produced by our causal detector.
        fig.add_vline(x=_time(reference_ms),line_color='#38bdf8',
                      line_dash='dash',row=1,col=1)
        fig.add_trace(go.Scatter(
            x=[_time(reference_ms)],y=[float(reference_price)],mode='markers',
            marker=dict(size=13,color='#38bdf8',symbol='star',
                        line=dict(color='#ffffff',width=1)),
            name='Entrada de captura (EXTERNA)',
            hovertemplate='Referencia visual externa<br>%{x}<br>Precio %{y:.8f}<extra></extra>'
        ),row=1,col=1)
    fig.update_layout(template='plotly_dark',height=670,margin=dict(l=5,r=10,t=15,b=5),
        xaxis_rangeslider_visible=False,legend=dict(orientation='h',y=1.04),
        hovermode='x unified')
    fig.update_xaxes(range=[_time(int(shown.timestamp.min())-TF),_time(end+TF)])
    return fig


def render_hll_v21(root):
    root=Path(root)
    st.header('🧱 Horizontal Level Lifecycle V2.1')
    st.caption('Investigación histórica OFFLINE · soportes + resistencias · sin señales LIVE ni órdenes')
    report_root=root/DEFAULT_OUTPUT
    runs=sorted((p for p in report_root.iterdir() if (p/'audit.json').is_file()),
                key=lambda p:p.stat().st_mtime,reverse=True) if report_root.is_dir() else []
    if not runs:
        st.info('No hay corridas V2.1. Ejecutá el piloto con --run --max-symbols 15 --run-tag pilot_15_v21.')
        st.code('python3 horizontal_level_lifecycle_v21.py --plan --max-symbols 15\n'
                'python3 -u horizontal_level_lifecycle_v21.py --run --max-symbols 15 --run-tag pilot_15_v21')
        return
    run=st.selectbox('Corrida congelada V2.1',runs,format_func=lambda p:p.name)
    manifest=json.loads((run/'run_config.json').read_text())
    audit=json.loads((run/'audit.json').read_text())
    c=st.columns(5)
    for box,(key,title) in zip(c, [('levels','Zonas congeladas'),('visits','Retests reales'),
                                   ('defenses','Defensas POST'),('losses','Pérdidas POST'),
                                   ('suppressed_overlaps','Candidatas duplicadas')]):
        box.metric(title,audit.get(key,'—'))
    st.caption(f"Símbolos: {audit.get('symbols')} · Cierres 30m: {audit.get('bars30')} · "
               f"Cruces: {audit.get('cross_up',0)+audit.get('cross_down',0)} · "
               f"Zonas anchas para revisar: {audit.get('wide_zones',0)} · "
               'Sin rentabilidad ni TP/SL. Cada defensa se conoce únicamente al cierre POST.')
    coverage=_read(run/'coverage.csv')
    with st.expander('Cobertura / metodología / congelamiento',expanded=False):
        st.dataframe(coverage,use_container_width=True,hide_index=True)
        st.json({'audit':audit,'config':manifest.get('config')})
    if coverage.empty:return
    symbols=sorted(coverage.loc[coverage.status=='COMPLETE','symbol'].unique())
    if not symbols:
        st.warning('No hay símbolos con cobertura completa.')
        return
    sym=st.selectbox('Símbolo',symbols)
    shard=run/'shards'
    levels=_read(shard/f'{sym}.formation.csv')
    events=_read(shard/f'{sym}.events.csv')
    ends=_read(shard/f'{sym}.ends.csv')
    if levels.empty:
        st.info('Sin zonas detectadas en este símbolo.');return
    origin=st.radio('Tipo',('AMBOS','HIGH · resistencias','LOW · soportes'),horizontal=True)
    quality=st.checkbox('Incluir zonas muy anchas (revisar)',value=True)
    filtered=levels.copy()
    if origin!='AMBOS': filtered=filtered[filtered.origin==origin.split(' ')[0]]
    if not quality:filtered=filtered[filtered.quality_flag!='WIDE_ZONE_REVIEW']
    if filtered.empty:
        st.warning('No hay niveles con esos filtros.');return
    joined=filtered.merge(ends,on=['level_id','symbol','origin','formation_closed_ts'],
                          how='left',validate='one_to_one')
    joined['width_pct']=100*(joined.zone_high-joined.zone_low)/joined.center
    st.subheader('Inventario del símbolo')
    tablecols=['origin','formation_utc','center','width_pct','quality_flag',
               'n_tests','n_defended','n_lost','n_timed_out','n_reconfirmations','end_state']
    st.dataframe(joined[tablecols].sort_values('formation_utc',ascending=False).round(4),
                 use_container_width=True,hide_index=True,height=250)
    pairs,nlevels=_overlap_stats(levels,ends)
    st.caption(f'Superposición residual del símbolo: {pairs} pares / {nlevels} niveles involucrados. '
               'Se cuentan TODAS las intersecciones, incluso parciales; no equivale a duplicaciones.')

    ref_case=REFERENCE_CASES.get(sym)
    ref_ms=ref_cutoff=None
    comparison=pd.DataFrame()
    if ref_case:
        with st.expander('📸 Comparar con la captura externa (INIT / TREE)', expanded=True):
            reference_tz=st.selectbox(
                'Interpretar hora escrita en la captura como',
                ['UTC','America/Argentina/Buenos_Aires'],
                key='hll-ref-timezone')
            ref_ms,ref_cutoff=reference_clock(ref_case, reference_tz)
            st.write(f"**Entrada mostrada:** {ref_case['entry_local']} ({reference_tz}), "
                     f"precio {ref_case['entry_price']:.6f} USDT · "
                     f"TP4 {ref_case['tp4']:.6f} USDT")
            st.caption(ref_case['description'] +
                       '. Los horarios de las capturas NO indican explícitamente '
                       'zona horaria. La marca azul es externa y NO es una señal '
                       'de nuestro detector.')
            if ref_cutoff != ref_ms:
                st.caption('La referencia cae dentro de una vela 30m. '
                           f'El último cierre utilizable es {_time(ref_cutoff)} UTC; '
                           'no utilizamos el cierre futuro de esa vela.')
            comparison=active_zones_at_reference(
                levels,ends,events,ref_cutoff,ref_case['entry_price'])
            if comparison.empty:
                st.warning('No hay niveles confirmados y vigentes antes de esa '
                           'referencia, según el histórico de este run.')
            else:
                st.caption(f'{len(comparison)} niveles confirmados y vigentes '
                           'a ese corte. Ordenados por distancia a la entrada, '
                           'sin consultar resultados posteriores.')
                st.dataframe(comparison[[
                    'origin','formation_utc','center','zone_low','zone_high',
                    'gap_price_pct','center_distance_pct','tests_known_pre',
                    'quality_flag'
                ]].head(15).round(5),hide_index=True,use_container_width=True)
            if not events.empty:
                last_crosses=events[
                    events.event_type.isin(['CROSS_UP','CROSS_DOWN']) &
                    events.observed_closed_ts.le(ref_cutoff) &
                    events.observed_closed_ts.ge(ref_cutoff-4*TF)
                ].copy().sort_values('observed_closed_ts',ascending=False)
                st.caption('Cruces de cierre 30m conocidos en las 2 horas '
                           'anteriores a la referencia (descriptivos, no '
                           'rupturas confirmadas).')
                if not last_crosses.empty:
                    st.dataframe(last_crosses[
                        ['observed_utc','origin','event_type','center',
                         'close','zone_low','zone_high']
                    ].head(30).round(6),hide_index=True,use_container_width=True)
                else:
                    st.caption('Ningún CROSS_UP/DOWN de 30m registrado en esa ventana.')
    ids=list(joined.sort_values('formation_closed_ts',ascending=False).level_id)
    lookup=joined.set_index('level_id')
    recommended_id=(str(comparison.iloc[0].level_id)
                    if not comparison.empty else None)
    selected=st.selectbox('Zona para inspeccionar',ids,
        index=ids.index(recommended_id) if recommended_id in ids else 0,
        format_func=lambda k: (f"{lookup.loc[k,'origin']} · "
                               f"{float(lookup.loc[k,'center']):.7g} · "
                               f"{_time(lookup.loc[k,'formation_closed_ts']).strftime('%d/%m %H:%M')} · "
                               f"tests {int(lookup.loc[k,'n_tests'])}"))
    lv=lookup.loc[selected]
    lv_events=events[events.level_id==selected].sort_values('observed_closed_ts') if not events.empty else pd.DataFrame()
    if ref_ms is not None:
        if int(lv.formation_closed_ts)>ref_cutoff:
            st.warning('El nivel seleccionado se confirmó DESPUÉS de la '
                       'entrada de la captura: no existía causalmente en ese momento.')
        elif not comparison.empty and selected in set(comparison.level_id):
            st.success('El nivel seleccionado ya estaba confirmado y vigente '
                       'en la referencia. Eso NO prueba equivalencia con LuxQuant.')
        else:
            st.info('Este nivel estaba formado pero no figuraba como vigente '
                    'en la referencia seleccionada.')
    modes=(('FORMACION','EVENTO','REFERENCIA','COMPLETO') if ref_ms is not None
           else ('FORMACION','EVENTO','COMPLETO'))
    mode=st.radio('Corte temporal del gráfico',modes,horizontal=True,
                  index=modes.index('REFERENCIA') if ref_ms is not None else 1)
    selected_ts=None
    if mode=='EVENTO':
        choices=(list(dict.fromkeys(lv_events.observed_closed_ts.astype('int64').tolist()))
                 if not lv_events.empty else [])
        if choices:
            selected_ts=st.selectbox('Cierre conocido a inspeccionar',choices,index=len(choices)-1,
                         format_func=lambda ts: str(_time(ts))+' · '+', '.join(
                             lv_events.loc[lv_events.observed_closed_ts==ts,'event_type'].tolist()))
        else:
            selected_ts=int(lv.formation_closed_ts)
    pre_bars=st.select_slider('Velas PRE de contexto',options=[40,80,120,160,240],value=120)
    post_bars=st.select_slider('Velas POST (solo COMPLETO)',options=[40,80,120,160,240],value=160)
    overlay=st.checkbox('Mostrar centros de otras zonas',value=ref_ms is not None)
    try:
        candles_dir=manifest.get('candles_dir')
        with Archive(root,Path(candles_dir) if candles_dir else None) as arc:
            bars, provenance=load_30m(arc,sym)
        if ref_cutoff is not None and (bars.empty or
                                      int(bars.timestamp.max())+TF < ref_cutoff):
            st.warning('Este archivo no alcanza la hora de la captura. '
                       'No podemos afirmar si existía un nivel en esa entrada.')
        if overlay:
            overlays=levels[levels.level_id!=selected].merge(
                ends[['level_id','end_closed_ts']],on='level_id',
                validate='one_to_one')
            if ref_ms is not None and not comparison.empty:
                overlays=overlays[overlays.level_id.isin(
                    comparison.head(12).level_id)]
        else:
            overlays=None
        fig=_chart(bars,lv,lv_events,overlays,
                   selected_ts,mode,pre_bars,post_bars,
                   reference_ms=ref_ms,reference_cutoff=ref_cutoff,
                   reference_price=ref_case['entry_price'] if ref_case else None)
        if fig is None: st.warning('Sin velas suficientes para ese corte temporal.')
        else: st.plotly_chart(fig,use_container_width=True)
        st.caption(f'Fuente de velas: {provenance}. Marca de formación = primer cierre en que '
                   'la zona era utilizable. Puntos POST nunca son condiciones PRE. '
                   'No inferir entrada en mitad de vela de 30m.')
    except Exception as exc:
        st.error(f'No se pudieron mostrar velas para {sym}: {exc}')
    st.subheader('Eventos del nivel')
    if lv_events.empty:
        st.info('Este nivel aún no tiene eventos posteriores registrados.')
    else:
        if mode=='COMPLETO':
            visible=lv_events
        else:
            cutoff=(int(lv.formation_closed_ts) if mode=='FORMACION' else
                    int(ref_cutoff) if mode=='REFERENCIA' else int(selected_ts))
            visible=lv_events[lv_events.observed_closed_ts <= cutoff]
        cols=[c for c in ['observed_utc','event_type','visit_number',
                           'test_from','test_result','previous_relation',
                           'close_relation','close'] if c in visible]
        st.dataframe(visible[cols],hide_index=True,use_container_width=True,height=260)
        st.download_button('Exportar eventos de este nivel',lv_events.to_csv(index=False).encode(),
                           file_name=f'{sym}_{selected}_events.csv',mime='text/csv')
    st.info('FASE GEOMÉTRICA: sin TP/SL, sin 1m, sin PnL. Una defensa se confirma '
            'solo después del TEST; los TEST_TIMEOUT y las censuras no son pérdidas.')


if __name__=='__main__':
    render_hll_v21(Path(__file__).resolve().parent)
