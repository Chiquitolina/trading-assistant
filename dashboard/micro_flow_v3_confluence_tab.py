"""Optional research-only Streamlit panel for Replay VE x AggTrades confluence."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from research.micro_flow_v3.volume_aggression_confluence import (
    COHORTS, observed_volume_candles, discover_volume_exhaustion,
    compare_confluence, save_confluence_run,
)


def _time(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%d/%m %H:%M:%S UTC')


def _frame(study, reveal):
    table=[]
    for n, case in enumerate(study['cases']):
        row={'#':n+1, 'Instante disponible UTC':_time(case['signal']['available_ms']),
             'Presión (dirección VE si coincide)':case['pressure_side'],
             'Hipótesis (NO entrada)':case['hypothesis_direction'],
             'Segmento':case['segment_id']}
        if reveal:
            row.update({'Cohorte retrospectiva':case['cohort'],
                        'VE ID':case['ve_event_id'],'AE ID':case['ae_event_id'],
                        'Estado referencia':case['state'],
                        'Referencia (NO fill)':(case['entry_reference'] or {}).get('entry_reference_price')})
            for horizon in study['parameters']['horizons_seconds']:
                outcome=case['outcomes'].get(f'{horizon}s',{})
                row.update({f'Estado {horizon}s':outcome.get('state'),
                     f'Bruto {horizon}s %':outcome.get('return_directional_pct'),
                     f'Neto estimado {horizon}s %':outcome.get('net_after_illustrative_cost_pct'),
                     f'MFE {horizon}s %':outcome.get('mfe_directional_pct'),
                     f'MAE {horizon}s %':outcome.get('mae_directional_pct')})
        table.append(row)
    return pd.DataFrame(table)


def _chart(session, ve, aggression, case, *, tf, before, after, reveal):
    cutoff=case['signal']['available_ms']
    start=cutoff-before*60_000
    end=cutoff+after*60_000 if reveal else cutoff
    segment=case['segment_id']
    candles=[c for c in ve['candles'] if c['segment_id']==segment
       and start<=c['timestamp']<=end and c['available_ms']<=end]
    buckets=[r for r in session['buckets'] if r['segment_id']==segment
       and start<=r['second_start_ms']<=end and r['available_wall_ns']<=end*1_000_000]
    if not buckets:
        return None
    fig=make_subplots(rows=4,cols=1,shared_xaxes=True,vertical_spacing=.028,
                      row_heights=[.48,.18,.18,.16],
                      subplot_titles=(f"ETHUSDT · velas observadas {tf}m + precio Replay 1s",
                                      'Volumen por vela ETH · BUY / SELL',
                                      'Delta agresivo ETH · 1s',
                                      'CVD observado ETH'))
    if candles:
        x=pd.to_datetime([c['timestamp'] for c in candles],unit='ms',utc=True)
        fig.add_trace(go.Candlestick(x=x,
             open=[c['open'] for c in candles],high=[c['high'] for c in candles],
             low=[c['low'] for c in candles],close=[c['close'] for c in candles],
             name=f'Candles observadas {tf}m',
             increasing_line_color='#16a34a',decreasing_line_color='#dc2626'),row=1,col=1)
        fig.add_trace(go.Bar(x=x,y=[c['buy_qty'] for c in candles],name='Buy ETH por vela',
                             marker_color='#16a34a'),row=2,col=1)
        fig.add_trace(go.Bar(x=x,y=[c['sell_qty'] for c in candles],name='Sell ETH por vela',
                             marker_color='#dc2626'),row=2,col=1)
    x=pd.to_datetime([r['second_start_ms'] for r in buckets],unit='ms',utc=True)
    fig.add_trace(go.Scatter(x=x,y=[float(r['close']) for r in buckets],
             name='Precio Replay 1s',line=dict(color='#38bdf8',width=1.0)),row=1,col=1)
    fig.add_trace(go.Bar(x=x,y=[float(r['delta_qty']) for r in buckets],
              name='Delta 1s',marker_color=['#16a34a' if float(r['delta_qty'])>=0
                  else '#dc2626' for r in buckets]),row=3,col=1)
    fig.add_trace(go.Scatter(x=x,y=[float(r['cvd_qty']) for r in buckets],
              name='CVD',line=dict(color='#f472b6',width=1.2)),row=4,col=1)
    marks=[]
    for e in ve['events']:
        if e['segment_id']==segment and start<=e['available_ms']<=end:
            marks.append((e['available_ms'],e['candle_close'],'VE '+e['pressure_side'],'#eab308','diamond'))
    for e in aggression['events']:
        ts=e['impulse']['available_ms']
        if e['segment_id']==segment and start<=ts<=end:
            marks.append((ts,e['impulse']['observed_close'],
                          'AE '+e['pressure_side'],'#a78bfa','x'))
    for ts,price,label,color,symbol in marks:
        fig.add_trace(go.Scatter(x=[pd.to_datetime(ts,unit='ms',utc=True)],y=[price],
              mode='markers',marker=dict(color=color,symbol=symbol,size=11),
              name=label,legendgroup=label,showlegend=True),row=1,col=1)
    when=pd.to_datetime(case['signal']['available_ms'],unit='ms',utc=True)
    fig.add_vline(x=when,line_dash='dot',line_color='#22d3ee',row=1,col=1)
    fig.add_annotation(x=when,y=1.01,yref='paper',text='Ambos conocidos / señal',
                       showarrow=False,font=dict(size=10,color='#22d3ee'))
    fig.update_layout(height=970,hovermode='x unified',barmode='stack',
                      margin=dict(l=10,r=10,t=70,b=20),
                      legend=dict(orientation='h',y=-.09))
    fig.update_xaxes(rangeslider_visible=False)
    return fig


def render_confluence_study(root, session, aggression, *, default_reveal=False):
    st.divider()
    st.subheader('🔬 Confluence Study · Volume Exhaustion + AggTrades')
    st.caption('Velas/volumen derivados del mismo Replay 1s. VE es un detector RSI + '
        'volumen NUEVO y exploratorio: no está verificada la paridad con el antiguo '
        'VolumeExhaustionCollector. AE conserva las reglas congeladas de Fase 2.')
    with st.form('mfv3_confluence_form'):
        a,b,c,d=st.columns(4)
        with a:
            tf=st.selectbox('Velas y VE',options=[1,5],format_func=lambda x:f'{x}m',key='mfv3_cf_tf')
        with b:
            volume_ratio=st.number_input('Volumen de vela >= × media 30 previas',
                        min_value=1.0,max_value=20.0,value=2.0,step=.25)
        with c:
            n_spikes=st.selectbox('Picos requeridos en últimas 4 velas',[1,2,3,4],index=0)
        with d:
            match_distance=st.selectbox('Coincidencia +/- segundos del cierre',[0,30,60,120],index=2)
        a,b,c=st.columns(3)
        with a:
            rsi_low=st.number_input('RSI sobreventa <=',min_value=5.,max_value=49.,value=35.,step=5.)
        with b:
            rsi_high=st.number_input('RSI sobrecompra >=',min_value=51.,max_value=95.,value=65.,step=5.)
        with c:
            min_cover=st.slider('Cobertura mínima observada por vela',.1,1.,.5,.05)
        submitted=st.form_submit_button('Calcular confluencia en Replay completo',type='primary')
    settings=(session['replay_path'],tf,float(volume_ratio),n_spikes,match_distance,
              float(rsi_low),float(rsi_high),float(min_cover),aggression['source_buckets_sha256'])
    if submitted:
        try:
            with st.spinner('Derivando velas + RSI causal + matching y outcomes...'):
                candles=observed_volume_candles(session['buckets'],tf)
                ve=discover_volume_exhaustion(candles,timeframe_minutes=tf,
                    high_volume_ratio=volume_ratio,
                    oversold_rsi=rsi_low,overbought_rsi=rsi_high,
                    count_high_volume_in_last4=n_spikes,
                    min_coverage_fraction=min_cover)
                comparison=compare_confluence(session['buckets'],ve,aggression,
                                              max_distance_seconds=match_distance)
            st.session_state['mfv3_confluence_study']=(settings,ve,comparison)
        except Exception as exc:
            st.error(f'Confluence Study no pudo ejecutarse: {exc}')
            return
    saved=st.session_state.get('mfv3_confluence_study')
    if saved is None:
        st.info('Ejecutá Calcular confluencia. Ningún RAW/Replay se modifica.');return
    if saved[0]!=settings:
        st.warning('Cambió la configuración o la sesión: recalculá la confluencia.');return
    _,ve,study=saved
    metrics=st.columns(4)
    for col,title,value in zip(metrics,
        ('Velas con cobertura','Volume Exhaustion','Aggression impulses','VE + AE emparejados'),
        (ve['summary']['eligible_candles'],study['summary']['ve_events'],
         study['summary']['ae_events'],'—')):
        col.metric(title,value)
    st.caption('El matching es 1:1 por proximidad observada a la ventana de vela. La '
      'coincidencia alrededor del CIERRE de la vela puede conocerse DESPUÉS del primer impulso; no es un disparador '
      'retroactivo. Los cohortes SOLO se asignan retrospectivamente.')
    reveal=st.checkbox('Revelar cohortes y resultados futuros',value=default_reveal,
                       key='mfv3_cf_reveal')
    if reveal:
        st.metric('Coincidencias VE + AE',study['summary']['matched_events'])
    records=study['cases']
    frame=_frame(study,reveal)
    st.dataframe(frame,use_container_width=True,hide_index=True)
    if reveal:
        st.download_button('Exportar confluencia completa (CSV)',frame.to_csv(index=False),
               file_name='micro_flow_v3_volume_aggression_confluence.csv',mime='text/csv',
               key='mfv3_cf_csv')
        st.markdown('**Resultados por cohorte, lado y horizonte**')
        comp=pd.DataFrame(study['comparisons'])
        st.dataframe(comp,use_container_width=True,hide_index=True)
        st.download_button('Exportar comparación (CSV)',comp.to_csv(index=False),
              file_name='micro_flow_v3_confluence_comparison.csv',mime='text/csv',
              key='mfv3_cf_comparison_csv')
    else:
        st.info('Los cohortes descriptivos y outcomes permanecen ocultos hasta revelarlos.')
    if records:
        idx=st.selectbox('Inspeccionar coincidencia o señal individual',range(len(records)),
             format_func=lambda i:f"{i+1} · {_time(records[i]['signal']['available_ms'])} · "
                                   f"{records[i]['pressure_side']}",key='mfv3_cf_case')
        case=records[idx]
        a,b=st.columns(2)
        with a:
            before=st.slider('Minutos anteriores',5,60,10,5,key='mfv3_cf_before')
        with b:
            after=st.slider('Minutos posteriores',5,30,15,5,key='mfv3_cf_after')
        chart=_chart(session,ve,aggression,case,tf=tf,before=before,after=after,reveal=reveal)
        if chart is not None:
            st.plotly_chart(chart,use_container_width=True,key='mfv3_cf_plot')
        if reveal:
            st.json(case)
    st.warning('Exploratorio: VE_ONLY/AE_ONLY dependen retrospectivamente de la ausencia '
        'de match; las cohortes pueden solaparse temporalmente. 12 bps son un costo '
        'ILUSTRATIVO. Entradas = referencias OPEN 1s conocidas tras publicación, NO fills. '
        'Los horizontes sin cobertura se censuran.')
    if st.button('Guardar corrida de confluencia con verificación RAW',key='mfv3_cf_save'):
        try:
            with st.spinner('Verificando SOURCE RAW y Replay antes de persistir...'):
                path=save_confluence_run(root,session,ve,aggression,study,verify_raw=True)
            st.success(f'Investigación guardada con manifiesto y hashes: {path}')
        except Exception as exc:
            st.error(f'No se guardó: {exc}')
