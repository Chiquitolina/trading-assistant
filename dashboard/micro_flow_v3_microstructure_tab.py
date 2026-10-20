"""Read-only UI for causal per-impulse shared V3 microstructure feature catalog."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard.micro_flow_v3_feature_outcomes_tab import render_microstructure_feature_outcomes

from research.micro_flow_v3.microstructure_exhaustion_features import (
    DEFAULT_WINDOWS_SECONDS, build_exhaustion_feature_catalog,
    read_footprint_from_closed_raw, save_feature_run,
)


def _dt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%d/%m %H:%M:%S UTC')


def _flat(catalog, seconds):
    rows = []
    for e in catalog['features']:
        feature = e['windows'][str(seconds) + 's']
        rows.append({
            'ID':e['event_id'], 'UTC disponible':_dt(e['signal_available_ms']),
            'Presión':e['pressure_side'], 'Segmento':e['segment_id'],
            'Estado cobertura':feature['state'],
            'Cobertura segundos':feature['observed_seconds'],
            'Cobertura %':feature['coverage_fraction']*100,
            'Delta base':feature.get('delta_qty'),
            'Volumen base':feature.get('gross_qty'),
            'Imbalance':feature.get('imbalance'),
            'Movimiento en dirección presión %':feature.get('pressure_signed_move_pct'),
            'Impacto bps/100 unidades base':feature.get('signed_impact_bps_per_100base_gross_qty'),
            'Notional USDT ejecutado':feature.get('gross_quote_usdt'),
            'Impacto bps/100k USDT':feature.get('signed_impact_bps_per_100k_usdt_executed'),
            'Impacto bps/100 unidades delta':feature.get('signed_impact_bps_per_100base_net_delta'),
            'CVD cambio base':feature.get('cvd_change_qty'),
            'Relación flujo-precio':feature.get('flow_price_alignment'),
            'Close location en dirección presión':feature.get('pressure_close_location'),
            'Retroceso desde extremo previo bps':feature.get('retreat_from_prior_extreme_bps'),
            'Rechazo tras extremo previamente observado':feature.get('rejection_after_prior_extreme_observed'),
            'Primer seq':feature.get('first_seq'),
            'Último seq':feature.get('last_seq'),
            'Disponible ns':feature.get('available_wall_ns'),
        })
    return pd.DataFrame(rows)


def render_microstructure_feature_study(root,session,aggression,selected_event=None,entry_study=None):
    st.divider()
    st.subheader('🧬 Microstructure Exhaustion Features · capa compartida')
    st.caption('Solo información que existía al publicarse cada impulso agresivo. '
       'Impacto del volumen ejecutado, discrepancia CVD/precio, retroceso de un extremo '
       'de un segundo anterior y, opcionalmente, Footprint desde los AggTrades RAW. '
       'NO etiqueta agotamientos ganadores ni mira outcomes posteriores.')
    win_choice = st.selectbox('Ventana de features (segundos)', DEFAULT_WINDOWS_SECONDS,
                   index=1,key='mfv3_micro_window')
    min_cov = st.selectbox('Cobertura mínima observada', [.50,.60,.80,1.0],
                      index=1,key='mfv3_micro_cov')
    config = (session['replay_path'],aggression['source_buckets_sha256'],
              tuple(DEFAULT_WINDOWS_SECONDS),min_cov)
    if st.button('Calcular features causales para todos los impulsos',
                 key='mfv3_micro_compute'):
        try:
            with st.spinner('Calculando ventanas observadas, sin alterar los eventos...'):
                catalog = build_exhaustion_feature_catalog(session['buckets'],aggression,
                                min_coverage=min_cov)
            st.session_state['mfv3_micro_data'] = (config,catalog)
            st.session_state.pop('mfv3_micro_footprint',None)
        except Exception as exc:
            st.error(f'Features no disponibles: {exc}')
            return
    saved=st.session_state.get('mfv3_micro_data')
    if not saved or saved[0]!=config:
        st.info('Calculá la capa compartida. Se generan features para TODOS los impulsos; '
                'no modifica los detectores existentes ni los SQLite RAW.')
        return
    cat=saved[1]
    frame=_flat(cat,win_choice)
    metrics=cat['summary']['by_window'][str(win_choice)+'s']
    a,b,c=st.columns(3)
    a.metric('Impulsos originales',cat['summary']['impulses'])
    b.metric('Ventanas observadas',metrics['observed'])
    c.metric('Ventanas insuficientes/censuradas',metrics['unavailable'])
    st.dataframe(frame,use_container_width=True,hide_index=True)
    st.download_button('Exportar features de todos los impulsos (CSV)',
        frame.to_csv(index=False),file_name=f'microstructure_features_{win_choice}s.csv',
        mime='text/csv',key='mfv3_micro_csv')
    if entry_study is not None:
        render_microstructure_feature_outcomes(root,session,aggression,cat,entry_study,
                                               window_seconds=win_choice)
    if not cat['features']:
        return
    ids=[e['event_id'] for e in cat['features']]
    default=ids.index(selected_event) if selected_event in ids else 0
    eid=st.selectbox('Inspeccionar impulso (features hasta su publicación)',ids,
                    index=default,key='mfv3_micro_selected')
    chosen=next(x for x in cat['features'] if x['event_id']==eid)
    st.caption(f"{chosen['pressure_side']} · {_dt(chosen['signal_available_ms'])} · "
       f"segmento {chosen['segment_id']} · seq del impulso "
       f"{chosen['signal_first_seq']}–{chosen['signal_last_seq']}")
    plot_rows=[]
    for key,feature in chosen['windows'].items():
        if feature['state']=='OBSERVED':
            plot_rows.append({'Ventana':key,'Movimiento %':feature['pressure_signed_move_pct'],
                              'Impacto bps/100 base':feature['signed_impact_bps_per_100base_gross_qty']})
    if plot_rows:
        fig=go.Figure()
        fig.add_trace(go.Bar(x=[r['Ventana'] for r in plot_rows],
                    y=[r['Impacto bps/100 base'] for r in plot_rows],
                    name='Impacto firmado, bps/100 unidades base'))
        fig.update_layout(height=280,title='Eficiencia de la agresión al instante del impulso',
                     xaxis_title='Ventanas retrospectivas',yaxis_title='Impacto bps / 100 unidades base',
                     margin=dict(l=10,r=10,t=45,b=20))
        st.plotly_chart(fig,use_container_width=True,key='mfv3_micro_impact_chart')
    feature=chosen['windows'][f'{win_choice}s']
    st.json(feature)
    st.caption('Una barra de impacto negativo indica que el precio se desplazó '
       'en sentido contrario a la presión elegida. No demuestra absorción pasiva. '
       '“Rechazo” requiere extremo observado antes del último segundo: '
       'nunca supone el orden de ticks dentro de un mismo bucket.')

    st.markdown('**Footprint RAW — solo al solicitarlo**')
    st.caption('Volumen BUY/SELL ejecutado por precio exacto en la ventana elegida. '
       'Se lee únicamente desde el SQLite CERRADO y con digest lógico correcto. '
       'No es profundidad de libro, ni se reconstruye desde 1s OHLC.')
    footprint_key=(session['tape'],eid,win_choice)
    if st.button('Leer Footprint real del tape verificado',key='mfv3_micro_footprint_btn'):
        try:
            with st.spinner('Comprobando digest RAW y agrupando AggTrades por precio...'):
                foot=read_footprint_from_closed_raw(session,chosen,window_seconds=win_choice)
            st.session_state['mfv3_micro_footprint']=(footprint_key,foot)
        except Exception as exc:
            st.error(f'Footprint no disponible: {exc}')
    fp=st.session_state.get('mfv3_micro_footprint')
    if fp and fp[0]==footprint_key:
        foot=fp[1]
        st.caption(f"{foot['trade_rows']:,} AggTrades · {foot['levels_total']} precios · "
                   f"mostrando {foot['levels_displayed']} por mayor volumen")
        table=pd.DataFrame(foot['levels'])
        if not table.empty:
            # Display the actual executed BUY/SELL quantity at the same levels.
            plot=go.Figure()
            plot.add_trace(go.Bar(y=table['price'],x=table['buy_qty'].astype(float),
                            orientation='h',name='BUY ejecutado',marker_color='#16a34a'))
            plot.add_trace(go.Bar(y=table['price'],x=-table['sell_qty'].astype(float),
                            orientation='h',name='SELL ejecutado',marker_color='#dc2626'))
            plot.update_layout(title='Footprint por precio exacto · ejecutado (no libro)',
                    barmode='relative',height=min(1000,300+10*len(table)),
                    xaxis_title='Volumen base (BUY + / SELL −)',yaxis_title='Precio',
                    margin=dict(l=10,r=10,t=55,b=20))
            st.plotly_chart(plot,use_container_width=True,key='mfv3_micro_footprint_chart')
        st.dataframe(table,use_container_width=True,hide_index=True)
        st.download_button('Descargar niveles Footprint (CSV)',table.to_csv(index=False),
            file_name='microstructure_raw_footprint.csv',mime='text/csv',
            key='mfv3_micro_footprint_csv')
    if st.button('Guardar corrida inmutable de features (verificar RAW + Replay)',
                 key='mfv3_micro_save'):
        try:
            with st.spinner('Revalidando RAW lógico, Replay SHA256 y cálculo determinista...'):
                target=save_feature_run(root,session,aggression,cat)
            st.success(f'Corrida persistida en: {target}')
        except Exception as exc:
            st.error(f'No se guardó la corrida: {exc}')
