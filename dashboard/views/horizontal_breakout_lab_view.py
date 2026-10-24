"""Purely read-only UI for independently-generated Horizontal Breakout Lab reports."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from horizontal_breakout_lab import Archive, REL, TP_PCTS, HORIZONS, TF, M, utc
from horizontal_breakout_inspector_v11 import (ceiling_origin, display_state,
    trigger_inspection, confirmation_post)
from dashboard.views.horizontal_breakout_lifecycle_v12_view import render_v12


def _read(path):
    try:
        return pd.read_csv(path)
    except (FileNotFoundError, pd.errors.EmptyDataError):
        return pd.DataFrame()


def _as_timestamp(frame, field):
    if field not in frame: return pd.Series(dtype='datetime64[ns, UTC]')
    return pd.to_datetime(frame[field],unit='ms',utc=True,errors='coerce')


@st.cache_data(ttl=300,show_spinner=False)
def _load_views(folder):
    folder=Path(folder)
    return _read(folder/'setups.csv'),_read(folder/'breakouts.csv'),_read(folder/'outcomes.csv')


@st.cache_data(ttl=300,show_spinner=False)
def _chart_source(root,candles_dir,symbol):
    with Archive(Path(root),Path(candles_dir) if candles_dir else None) as source:
        return source.load(symbol)[:2]


def _histogram_outcomes(result, factor, col):
    if factor not in result or col not in result: return None
    f=result[[factor,col]].copy()
    f[factor]=pd.to_numeric(f[factor],errors='coerce')
    f=f.dropna()
    f=f[f[col].isin(['TP_FIRST','SL_FIRST','NO_HIT'])]
    if f.empty: return None
    if f[factor].nunique()<4:return None
    try: f['grupo']=pd.qcut(f[factor],q=4,duplicates='drop')
    except ValueError: return None
    agg=(f.groupby('grupo',observed=True)
         .agg(n=(col,'size'),tp=(col,lambda x:(x=='TP_FIRST').sum()),
              sl=(col,lambda x:(x=='SL_FIRST').sum()),
              no_hit=(col,lambda x:(x=='NO_HIT').sum())).reset_index())
    agg['tp_pct']=100*agg.tp/agg.n
    agg['grupo']=agg['grupo'].astype(str)
    return agg


def _utc_x(timestamp_ms):
    return pd.Timestamp(int(timestamp_ms), unit='ms', tz='UTC')


def _chart(pre, minute, row, view_mode, pre_bars, trigger, margin_pct):
    """Strict time cutoffs: PRE <= freeze; TRIGGER <= event; POST may extend."""
    freeze = int(row.formation_closed_ts)
    event = int(row.breakout_ts) if pd.notna(row.get('breakout_ts')) else None
    origin = ceiling_origin(row, pre)
    left = freeze - pre_bars * TF
    if view_mode == 'PRE' or event is None:
        latest = freeze
    elif view_mode == 'TRIGGER':
        latest = event
    else:
        latest = event + 48 * 60 * M
    # A 30m candle is only drawable when it was CLOSED by the chosen cutoff.
    c = pre[(pre.timestamp >= left) & (pre.timestamp + TF <= latest)].copy()
    if c.empty:
        st.warning('No hay velas 30m cerradas dentro de la ventana de este evento.')
        return
    x = pd.to_datetime(c.timestamp, unit='ms', utc=True)
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=.035,
                        row_heights=[.78,.22])
    fig.add_trace(go.Candlestick(x=x, open=c.open, high=c.high, low=c.low,
                   close=c.close, name='30m OHLC', increasing_line_color='#13a68b',
                   decreasing_line_color='#d55a5a', increasing_fillcolor='#13a68b',
                   decreasing_fillcolor='#d55a5a'), row=1,col=1)
    fig.add_trace(go.Bar(x=x, y=c.volume, name='Volumen 30m',
                  marker_color='#6b849d', opacity=.7), row=2,col=1)
    level = float(row.level)
    zlo, zhi = float(row.zone_low), float(row.zone_high)
    # During the forming window, the line is shown as a RECONSTRUCTION.
    # It did not exist as a frozen observable level until formation_closed_ts.
    window_start = max(left, origin['start_ts'])
    right = latest if view_mode != 'POST' else min(latest, int(c.timestamp.max())+TF)
    if right > window_start:
        fig.add_shape(type='rect',x0=_utc_x(window_start),x1=_utc_x(right),
                      y0=zlo,y1=zhi,xref='x',yref='y',
                      fillcolor='rgba(220,165,76,.11)',line_width=0,row=1,col=1)
    if freeze > window_start:
        fig.add_trace(go.Scatter(x=[_utc_x(window_start),_utc_x(freeze)],
                      y=[level,level],mode='lines',name='Origen reconstruido (no conocido aún)',
                      line=dict(color='#9b793c',width=1.5,dash='dot')),
                      row=1,col=1)
    if right > freeze:
        fig.add_trace(go.Scatter(x=[_utc_x(freeze),_utc_x(right)],
                      y=[level,level],mode='lines',name='Techo congelado observable',
                      line=dict(color='#dca54c',width=2,dash='dash'),
                      hovertemplate='Nivel congelado: %{y:.8f}<extra></extra>'),row=1,col=1)
    start = origin['start_ts']
    if start >= left and start <= latest:
        fig.add_vline(x=_utc_x(start),line_dash='dot',line_color='#a16c3b',row=1,col=1)
    if freeze >= left and freeze <= latest:
        fig.add_vline(x=_utc_x(freeze),line_dash='dot',line_color='#7c8ca9',row=1,col=1)
    touch_times = {x['open_ts']:x['number'] for x in origin['touches']}
    hits = c[c.timestamp.isin(touch_times)]
    if not hits.empty:
        fig.add_trace(go.Scatter(x=pd.to_datetime(hits.timestamp,unit='ms',utc=True),
                      y=hits.high, mode='markers+text',
                      text=['#'+str(touch_times[int(t)]) for t in hits.timestamp],
                      textposition='top center',textfont=dict(color='#e9b75c',size=10),
                      marker=dict(color='#dca54c',size=10,symbol='circle-open',line=dict(width=2)),
                      name='Visitas PRE'), row=1,col=1)
    if event is not None and view_mode != 'PRE':
        # For 1m trigger, the 30m candle containing the trigger is NOT displayed
        # until it has actually closed; use the independent 1m detail instead.
        fig.add_vline(x=_utc_x(event),line_color='#13a68b',line_dash='dot',
                      line_width=2,row=1,col=1)
        threshold = level*(1+margin_pct/100)
        fig.add_trace(go.Scatter(x=[_utc_x(max(left,freeze)),_utc_x(event)],
                      y=[threshold,threshold],mode='lines',name='Umbral primer cierre',
                      line=dict(color='#60b5e5',dash='dot',width=1.2)),row=1,col=1)
    fig.update_layout(height=560,showlegend=True,margin=dict(l=12,r=12,t=22,b=10),
                      xaxis_rangeslider_visible=False)
    st.plotly_chart(fig,use_container_width=True)
    visible_pre = c[c.timestamp < freeze]
    if len(visible_pre) < pre_bars:
        st.caption(f'Contexto PRE disponible: {len(visible_pre)} de {pre_bars} velas solicitadas; '
                   'la falta de velas antiguas no se rellena artificialmente.')
    if event is not None and view_mode != 'PRE' and not minute.empty:
        # Display only candles with a CLOSED minute at the selected cutoff.
        detail_start = max(freeze-90*M,event-90*M)
        detail_end = event if view_mode=='TRIGGER' else event+360*M
        sub = minute[(minute.timestamp>=detail_start) & (minute.timestamp+M<=detail_end)]
        if not sub.empty:
            st.caption('Detalle 1m: línea verde = cierre que activó el cruce. '
                       'La entrada simulada solo puede ocurrir desde la siguiente apertura disponible.')
            p = go.Figure(go.Candlestick(x=pd.to_datetime(sub.timestamp,unit='ms',utc=True),
                         open=sub.open,high=sub.high,low=sub.low,close=sub.close,
                         increasing_line_color='#13a68b',decreasing_line_color='#d55a5a'))
            p.add_hline(y=level,line_dash='dash',line_color='#dca54c')
            p.add_hline(y=level*(1+margin_pct/100),line_dash='dot',line_color='#60b5e5')
            p.add_vline(x=_utc_x(event),line_dash='dot',line_color='#18a07a')
            p.update_layout(height=340,xaxis_rangeslider_visible=False,
                            margin=dict(l=12,r=12,t=12,b=12))
            st.plotly_chart(p,use_container_width=True)



def render_horizontal_breakout_lab(root):
    root=Path(root)
    st.markdown('## 🧱 Horizontal Breakout Lab')
    st.caption('Investigación independiente de REACTIONs. Detecta bases horizontales, toques y ruptura del techo con OHLCV cerrado; no envía órdenes.')
    reports=root/REL/'runs'
    runs=sorted([p for p in reports.iterdir() if (p/'run_config.json').exists()],
                key=lambda p:p.stat().st_mtime,reverse=True) if reports.is_dir() else []
    if not runs:
        st.warning('Todavía no hay una corrida histórica. Ejecutar primero `python3 horizontal_breakout_lab.py --plan` y luego `--run`.')
        st.code('python3 horizontal_breakout_lab.py --plan --max-symbols 5\npython3 -u horizontal_breakout_lab.py --run --max-symbols 5\npython3 horizontal_breakout_lab.py --audit --max-symbols 5')
        return
    selected=st.selectbox('Corrida histórica',runs,format_func=lambda p:p.name,key='hb_run')
    import json
    cfg=json.loads((selected/'run_config.json').read_text(encoding='utf-8'))
    audit_path=selected/'audit.json'
    audit=json.loads(audit_path.read_text(encoding='utf-8')) if audit_path.is_file() else {}
    setups,breakouts,outcomes=_load_views(str(selected))
    if not audit.get('success'):
        st.error('La auditoría de esta corrida no está aprobada; revisar archivos y símbolos faltantes antes de interpretar estadísticas.')
    c=st.columns(5)
    for box,(label,value) in zip(c,[('Símbolos procesados',audit.get('completed_symbols','—')),
            ('Bases congeladas',len(setups)),('Rupturas',len(breakouts)),
            ('Con path 1m completo 48h',audit.get('outcome_counts',{}).get('path_states',{}).get('COMPLETE',0)),
            ('Sin path 1m completo',len(outcomes)-audit.get('outcome_counts',{}).get('path_states',{}).get('COMPLETE',0))]):
        box.metric(label,value)
    st.caption(f"Fuente: {cfg.get('source_type','?')} · Confirmación: {cfg.get('settings',{}).get('trigger','?')} · Ventanas: {cfg.get('settings',{}).get('windows','?')}")
    st.info('Sin datos 1m suficientes = CENSORED/NO_FILL, nunca SL. No inferir rendimiento de mercado sin cobertura completa, costos, control temporal y validación fuera de muestra.')
    tabs=st.tabs(['🧱 Todas las bases','🚀 Rupturas y resultados','📊 Factores','🔎 Inspector 30m / 1m','📋 Cobertura y metodología','🧭 V1.2 · Ciclo y fuerza'])
    with tabs[0]:
        if setups.empty:
            st.info('No se formaron bases verificables con esta cobertura o configuración.')
        else:
            states=sorted(setups.end_state.dropna().unique())
            wanted=st.multiselect('Estados de las bases',states,default=states,
                                  format_func=display_state,key='hb_states')
            filtered=setups[setups.end_state.isin(wanted)].copy()
            filtered['estado_visible']=filtered.end_state.map(display_state)
            fields=[x for x in ['symbol','formation_time_utc','window','touch_count','level','base_height_pct',
                                'pressure_higher_lows_pct','volume_pre_recent_ratio','estado_visible','source_30m'] if x in filtered]
            st.dataframe(filtered[fields],use_container_width=True,hide_index=True,height=350)
            st.download_button('⬇️ Todas las bases (CSV)',setups.to_csv(index=False).encode(),
                               file_name='horizontal_breakout_setups.csv',mime='text/csv',key='hb_download_setups')
            st.caption('ACTIVE_AT_END = candidato aún sin desenlace en el archivo. EXPIRED_NO_BREAK = no rompió dentro del TTL observado.')
    with tabs[1]:
        if breakouts.empty:
            st.warning('No hay rupturas detectadas en los datos cubiertos. No implica que el mercado no haya tenido rupturas.')
        else:
            joined=breakouts.merge(outcomes,on=['setup_id','symbol','breakout_ts'],how='left',validate='one_to_one')
            tp=st.select_slider('Objetivo (%)',options=list(TP_PCTS),value=2.0,key='hb_tp')
            horizon=st.selectbox('Horizonte (min)',HORIZONS,index=1,key='hb_horizon')
            outcome_col=f'tp{int(tp)}_h{horizon}'
            states=joined[outcome_col].value_counts(dropna=False)
            st.dataframe(states.rename('eventos').reset_index().rename(columns={'index':'estado',outcome_col:'estado'}),hide_index=True)
            cols=[x for x in ['symbol','formation_time_utc','level','touch_count','window',
                                'pressure_higher_lows_pct','volume_pre_recent_ratio','breakout_volume_ratio',
                                'breakout_strength_pct','entry_price','path_state',outcome_col] if x in joined]
            net_col=f'net_tp{int(tp)}_h{horizon}_pct'
            if net_col in joined:
                eligible=pd.to_numeric(joined[net_col],errors='coerce').dropna()
                st.caption(f'Resultados con retorno hipotético resoluble: {len(eligible)} / {len(joined)}')
                if len(eligible):
                    st.metric('Retorno neto medio por evento evaluable (sin simular slots)',f'{eligible.mean():.2f}%')
            st.dataframe(joined[cols],use_container_width=True,hide_index=True,height=350)
            st.download_button('⬇️ Rupturas con outcomes (CSV)',joined.to_csv(index=False).encode(),
                               file_name='horizontal_breakout_outcomes.csv',mime='text/csv',key='hb_download_breaks')
            st.caption('TP_FIRST significa que el TP tocó antes que el SL de 1,5%; no equivale a P&L. BOTH_SAME_1M es ambiguo y se informa por separado.')
    with tabs[2]:
        if breakouts.empty or outcomes.empty:
            st.info('No hay outcomes suficientes para analizar los factores.')
        else:
            joined=breakouts.merge(outcomes,on=['setup_id','symbol','breakout_ts'],how='inner',validate='one_to_one')
            tp=st.selectbox('TP para investigación (%)',TP_PCTS,index=1,key='hb_factor_tp')
            horizon=st.selectbox('Horizonte para investigación (min)',HORIZONS,index=1,key='hb_factor_h')
            result_col=f'tp{int(tp)}_h{horizon}'
            st.caption('Comparaciones exploratorias, sin ajuste estadístico por dependencia entre símbolos/días. Grupos con menos de 30 casos son solo descriptivos.')
            for factor,label in [('touch_count','Cantidad de contactos'),('window','Duración de base'),
                                 ('top_flatness_atr','Horizontalidad de máximos (ATR)'),
                                 ('pressure_higher_lows_pct','Presión de mínimos ascendentes (%)'),
                                 ('volume_pre_recent_ratio','Volumen reciente / base'),
                                 ('breakout_volume_ratio','Expansión de volumen en ruptura'),
                                 ('previous_trend_pct','Tendencia anterior (%)')]:
                if factor not in joined: continue
                table=_histogram_outcomes(joined,factor,result_col)
                with st.expander(label,expanded=False):
                    if table is None: st.write('Sin suficientes valores distintos y casos resueltos para comparar.')
                    else:
                        st.dataframe(table,hide_index=True,use_container_width=True)
                        if (table.n<30).any():st.warning('Al menos un cuartil tiene menos de 30 eventos.')
                        st.bar_chart(table.set_index('grupo')['tp_pct'])
    with tabs[3]:
        if setups.empty:
            st.info('No hay eventos para inspeccionar.')
        else:
            st.caption('PRE = ciego, hasta la formación. CRUCE = incluye únicamente la vela que '
                       'disparó la señal. POST = habilita el desenlace y la confirmación posterior.')
            view_choice=st.radio('Mostrar en gráfico',
                ['Hasta el cruce','Solo PRE (ciego)','POST (desenlace)'],horizontal=True,
                key='hb_v11_mode')
            mode={'Hasta el cruce':'TRIGGER','Solo PRE (ciego)':'PRE',
                  'POST (desenlace)':'POST'}[view_choice]
            pre_bars=st.select_slider('Velas 30m anteriores visibles',
                       options=[60,90,120,180,240],value=120,key='hb_v11_pre_bars')
            table=setups.sort_values('formation_closed_ts',ascending=False)
            symbols=sorted(table.symbol.dropna().unique())
            sym=st.selectbox('Símbolo',symbols,key='hb_symbol')
            sel=table[table.symbol==sym].head(250)
            keys=list(sel.setup_id)
            chosen=st.selectbox('Estructura',keys,format_func=lambda k:(
                f"{sel[sel.setup_id==k].iloc[0].formation_time_utc} · "
                f"{display_state(sel[sel.setup_id==k].iloc[0].end_state) if mode != 'PRE' else 'FORMED_PRE'} · "
                f"{int(sel[sel.setup_id==k].iloc[0].touch_count)} contactos"),key='hb_event')
            row=sel[sel.setup_id==chosen].iloc[0]
            event=breakouts[breakouts.setup_id==chosen] if not breakouts.empty else pd.DataFrame()
            trigger=cfg.get('settings',{}).get('trigger','30m')
            margin=float(cfg.get('settings',{}).get('margin_pct',.1))
            basic={'Formación PRE (UTC)':row.formation_time_utc,
                   'Nivel congelado':float(row.level), 'Zona inferior':float(row.zone_low),
                   'Zona superior':float(row.zone_high), 'Visitas PRE':int(row.touch_count),
                   'ATR PRE':float(row.atr_pre), 'Estado observable':(
                        'FORMED_PRE' if mode=='PRE' else display_state(row.end_state))}
            st.json(basic)
            try:
                with st.spinner('Leyendo gráfico desde archivo histórico (solo lectura)…'):
                    thirty,minute=_chart_source(str(root),cfg.get('candles_dir'),sym)
                info=ceiling_origin(row,thirty)
                with st.expander('Origen de la línea amarilla y detalle de toques',expanded=True):
                    st.write(f"**Ventana de origen:** {info['start_utc']} → "
                             f"{info['freeze_utc']} ({info['window']} velas 30m).")
                    st.write('El techo congelado es la mediana de los máximos del cluster '
                             'dentro de la tolerancia ATR, **no** la media de dos círculos. '
                             'Los números representan visitas independientes seleccionadas '
                             'por el detector.')
                    st.write(f"Tolerancia de zona: ± {float(row.tolerance_price):.8f} "
                             f"({float(row.atr_pre):.8f} ATR PRE).")
                    if not info['complete_window']:
                        st.warning('La ventana completa de formación NO está disponible en '
                                   'la fuente de gráfico actual. Revisar corrida/cobertura.')
                    st.dataframe(pd.DataFrame(info['touches']).rename(columns={
                        'number':'Visita','utc':'Apertura UTC','high':'Máximo de esa vela'}).drop(
                        columns=['open_ts'],errors='ignore'),hide_index=True,use_container_width=True)
                if mode!='PRE' and not event.empty:
                    trigger_info=trigger_inspection(thirty,minute,row,trigger=trigger,margin_pct=margin)
                    if trigger_info['status']=='CLOSE_ABOVE_LEVEL':
                        st.info(f"**CLOSE_ABOVE_LEVEL:** primer cierre {trigger} en "
                                f"{utc(int(row.breakout_ts))}. "
                                f"Cierre={trigger_info['trigger_close']:.8f}, "
                                f"umbral={trigger_info['threshold']:.8f} "
                                f"(techo +{margin:.2f}%). Esto todavía NO significa ruptura sostenida.")
                    else:
                        st.warning(f"No se pudo verificar el cruce en la fuente OHLCV "
                                   f"que usa el gráfico: {trigger_info['status']}. "
                                   'No modificar el evento histórico sin auditar la fuente.')
                if mode=='POST' and not event.empty:
                    diagnostic=confirmation_post(thirty,row)
                    if diagnostic['status']=='BREAKOUT_CONFIRMED_POST':
                        st.success('**BREAKOUT_CONFIRMED (POST):** dos cierres consecutivos '
                                   'de velas 30m completamente cerradas por encima del techo '
                                   'de la zona congelada. '
                                   f"Confirmado en {utc(diagnostic['confirmed_at_ts'])}.")
                    elif diagnostic['status']=='NOT_CONFIRMED_IN_4_BARS':
                        st.warning('**Sin confirmación sostenida:** no hubo dos cierres '
                                   'consecutivos arriba de la zona dentro de las siguientes '
                                   'cuatro velas 30m. No equivale por sí solo a SL.')
                    else:
                        st.warning('**Confirmación indeterminada:** faltan velas POST '
                                   'continuas para aplicar la regla.')
                    r=outcomes[outcomes.setup_id==chosen] if not outcomes.empty else pd.DataFrame()
                    if not r.empty:st.dataframe(r,hide_index=True,use_container_width=True)
                _chart(thirty,minute,row,mode,pre_bars,trigger,margin)
            except Exception as exc:
                st.error(f'No se pudo mostrar el inspector de {sym}: {exc}')
    with tabs[4]:
        st.json({k:v for k,v in audit.items() if k not in ('run_manifest',)})
        st.markdown('**Reglas congeladas:** cada estructura utiliza velas 30m cerradas, con al menos dos visitas independientes a un techo local. Los precios de nivel y tolerancias quedan inmóviles después de la formación. El trigger se confirma con cierre 30m o 1m, y la entrada simulada es la apertura de la siguiente vela 1m.')
        st.markdown('**V1.1 visual:** en corridas existentes BREAKOUT se presenta como CLOSE_ABOVE_LEVEL. BREAKOUT_CONFIRMED es solo un diagnóstico POST (dos cierres 30m consecutivos por encima de la zona dentro de cuatro velas posteriores); no altera setups/outcomes ni opera en LIVE.')
        st.markdown('**Limitaciones:** la detección 30m puede perder rupturas intrabar, 1m histórico puede estar incompleto y las fuentes agregadas de 15m se declaran PROXY. No se interpretan paths censurados como SL. No se usan REACTIONs en la detección.')
        st.code('python3 horizontal_breakout_lab.py --plan --max-symbols 5\npython3 -u horizontal_breakout_lab.py --run --max-symbols 5\npython3 horizontal_breakout_lab.py --audit --max-symbols 5')

    with tabs[5]:
        render_v12(root)
