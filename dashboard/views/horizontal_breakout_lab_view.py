"""Purely read-only UI for independently-generated Horizontal Breakout Lab reports."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from horizontal_breakout_lab import Archive, REL, TP_PCTS, HORIZONS, TF, M, utc


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


def _chart(pre,minute,row,show_future):
    freeze=int(row.formation_closed_ts)
    break_ts=int(row.breakout_ts) if pd.notna(row.get('breakout_ts')) else None
    earliest=freeze-80*TF
    latest=freeze+(48*60*M if show_future else 0)
    c=pre[(pre.timestamp>=earliest)&(pre.timestamp<latest)].copy()
    if c.empty:
        st.warning('No hay velas 30m dentro de la ventana de este evento.')
        return
    x=pd.to_datetime(c.timestamp,unit='ms',utc=True)
    fig=make_subplots(rows=2,cols=1,shared_xaxes=True,vertical_spacing=.035,row_heights=[.78,.22])
    fig.add_trace(go.Candlestick(x=x,open=c.open,high=c.high,low=c.low,close=c.close,
                                 name='30m OHLC',increasing_line_color='#13a68b',
                                 decreasing_line_color='#d55a5a',increasing_fillcolor='#13a68b',
                                 decreasing_fillcolor='#d55a5a'),row=1,col=1)
    fig.add_trace(go.Bar(x=x,y=c.volume,name='Volumen 30m',marker_color='#6b849d',opacity=.7),row=2,col=1)
    level=float(row.level)
    zlo,zhi=float(row.zone_low),float(row.zone_high)
    fig.add_hrect(y0=zlo,y1=zhi,fillcolor='#dca54c',opacity=.12,line_width=0,row=1,col=1)
    fig.add_hline(y=level,line_color='#dca54c',line_dash='dash',annotation_text='Techo PRE',row=1,col=1)
    fig.add_vline(x=pd.Timestamp(freeze,unit='ms',tz='UTC'),line_dash='dot',line_color='#7c8ca9',row=1,col=1)
    try:
        times=__import__('json').loads(row.touch_open_timestamps)
        touch=c[c.timestamp.isin(times)]
        if len(touch):
            fig.add_trace(go.Scatter(x=pd.to_datetime(touch.timestamp,unit='ms',utc=True),
                y=touch.high,mode='markers',marker=dict(color='#dca54c',size=10,symbol='circle-open',line=dict(width=2)),
                name='Toques PRE'),row=1,col=1)
    except (ValueError,TypeError):pass
    if break_ts is not None and show_future:
        fig.add_vline(x=pd.Timestamp(break_ts,unit='ms',tz='UTC'),line_color='#18a07a',line_width=2,row=1,col=1)
    fig.update_layout(height=530,showlegend=True,margin=dict(l=12,r=12,t=18,b=10),
                      xaxis_rangeslider_visible=False)
    st.plotly_chart(fig,use_container_width=True)
    if break_ts is not None and show_future and not minute.empty:
        sub=minute[(minute.timestamp>=break_ts-30*M)&(minute.timestamp<break_ts+360*M)]
        if not sub.empty:
            st.caption('Detalle 1m: el triángulo/entrada solo es operable a partir de la apertura posterior a la confirmación.')
            p=go.Figure(go.Candlestick(x=pd.to_datetime(sub.timestamp,unit='ms',utc=True),
                         open=sub.open,high=sub.high,low=sub.low,close=sub.close,
                         increasing_line_color='#13a68b',decreasing_line_color='#d55a5a'))
            p.add_hline(y=level,line_dash='dash',line_color='#dca54c')
            p.add_vline(x=pd.Timestamp(break_ts,unit='ms',tz='UTC'),line_dash='dot',line_color='#18a07a')
            p.update_layout(height=335,xaxis_rangeslider_visible=False,margin=dict(l=12,r=12,t=12,b=12))
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
    tabs=st.tabs(['🧱 Todas las bases', '🚀 Rupturas y resultados', '📊 Factores', '🔎 Inspector 30m / 1m', '📋 Cobertura y metodología', '🧬 Estructuras V2.6', '🎯 Flip LONG V2.7', '🧱 Estructura V2.8'])
    with tabs[5]:
        from dashboard.views.hll_v26_structure_dashboard_view import render_hll_v26_structure_dashboard
        render_hll_v26_structure_dashboard(root)
    with tabs[6]:
        from dashboard.views.hll_v27_flip_dashboard_view import render_hll_v27_flip_dashboard
        render_hll_v27_flip_dashboard(root)
    with tabs[7]:
        from dashboard.views.hll_v28_structure_dashboard_view import render_hll_v28_structure_dashboard
        render_hll_v28_structure_dashboard(root)
    with tabs[0]:
        if setups.empty:
            st.info('No se formaron bases verificables con esta cobertura o configuración.')
        else:
            states=sorted(setups.end_state.dropna().unique())
            wanted=st.multiselect('Estados de las bases',states,default=states,key='hb_states')
            filtered=setups[setups.end_state.isin(wanted)]
            fields=[x for x in ['symbol','formation_time_utc','window','touch_count','level','base_height_pct',
                                'pressure_higher_lows_pct','volume_pre_recent_ratio','end_state','source_30m'] if x in filtered]
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
            # Show both failed and successful setups, without outcome-based preselection.
            table=setups.sort_values('formation_closed_ts',ascending=False)
            symbols=sorted(table.symbol.dropna().unique())
            sym=st.selectbox('Símbolo',symbols,key='hb_symbol')
            sel=table[table.symbol==sym].head(250)
            keys=list(sel.setup_id)
            chosen=st.selectbox('Estructura',keys,format_func=lambda k:(
                f"{sel[sel.setup_id==k].iloc[0].formation_time_utc} · {sel[sel.setup_id==k].iloc[0].end_state} · "
                f"{int(sel[sel.setup_id==k].iloc[0].touch_count)} contactos"),key='hb_event')
            row=sel[sel.setup_id==chosen].iloc[0]
            show_future=st.checkbox('Mostrar desenlace posterior (POST)',value=False,key='hb_post')
            st.write({'Formación PRE (UTC)':row.formation_time_utc,'Nivel congelado':float(row.level),
                      'Toques':int(row.touch_count),'ATR PRE':float(row.atr_pre),'Estado':row.end_state})
            if show_future:
                r=outcomes[outcomes.setup_id==chosen] if not outcomes.empty else pd.DataFrame()
                if not r.empty:st.dataframe(r,hide_index=True,use_container_width=True)
            try:
                with st.spinner('Leyendo gráfico desde archivo histórico (solo lectura)…'):
                    thirty,minute=_chart_source(str(root),cfg.get('candles_dir'),sym)
                _chart(thirty,minute,row,show_future)
            except Exception as exc:
                st.error(f'No se pudo cargar OHLCV de {sym}: {exc}')
    with tabs[4]:
        st.json({k:v for k,v in audit.items() if k not in ('run_manifest',)})
        st.markdown('**Reglas congeladas:** cada estructura utiliza velas 30m cerradas, con al menos dos visitas independientes a un techo local. Los precios de nivel y tolerancias quedan inmóviles después de la formación. El trigger se confirma con cierre 30m o 1m, y la entrada simulada es la apertura de la siguiente vela 1m.')
        st.markdown('**Limitaciones:** la detección 30m puede perder rupturas intrabar, 1m histórico puede estar incompleto y las fuentes agregadas de 15m se declaran PROXY. No se interpretan paths censurados como SL. No se usan REACTIONs en la detección.')
        st.code('python3 horizontal_breakout_lab.py --plan --max-symbols 5\npython3 -u horizontal_breakout_lab.py --run --max-symbols 5\npython3 horizontal_breakout_lab.py --audit --max-symbols 5')
