"""Inspector HLL: exploración descriptiva PRE del universo CROSS_UP 290.

No toca señales, vistas existentes, POST de referencia, órdenes ni LIVE.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

from hll_structural_discovery_v1 import CANDLES, OUTPUT_PARENT, TF, bar_window, sha

REQUIRED=('attempt_id','symbol','level_id','pre_closed_ts','zone_low','zone_high',
          'atr_pre','status','family_0p5','family_1p0','family_1p5')
FAMILY_LABELS={
 'ASCENSO_ESCALONADO':'Ascenso escalonado',
 'RECONSTRUCCION_ENTRE_CONTACTOS':'Reconstrucción entre contactos',
 'OSCILACION_EN_RESISTENCIA':'Oscilación en resistencia',
 'REBOTE_TRAS_CORRECCION':'Rebote tras corrección',
 'DETERIORO_DESDE_MINIMO':'Deterioro desde mínimo',
 'CORRECCION_DOMINANTE':'Corrección dominante',
 'SECUENCIA_MIXTA_INDETERMINADA':'Mixta / indeterminada',
 'INDETERMINADA_SIN_MINIMO':'Sin mínimo confirmado',
}


def load_discovery(directory: Path):
    manifest_path=directory/'manifest.json'
    csv_path=directory/'attempts_pre.csv'
    if not manifest_path.is_file() or not csv_path.is_file():
        return None
    manifest=json.loads(manifest_path.read_text(encoding='utf8'))
    if manifest.get('version')!='HLL_STRUCTURAL_DISCOVERY_V1' or manifest.get('reads_post') is not False:
        raise ValueError('Manifiesto de Discovery incompatible/no causal')
    # Lightweight integrity: expensive SHA check done once on actual render via cache below.
    if sha(csv_path)!=manifest.get('output_sha256',{}).get('attempts_pre.csv'):
        raise ValueError('Hash de CSV Discovery no coincide con manifiesto')
    df=pd.read_csv(csv_path,low_memory=False)
    if set(REQUIRED)-set(df):raise ValueError('Faltan columnas de Discovery')
    if len(df)!=int(manifest['attempts']) or df.attempt_id.isna().any() or df.attempt_id.duplicated().any():
        raise ValueError('Cardinalidad/IDs de Discovery inválidos')
    if df.symbol.nunique()!=int(manifest['symbols']):raise ValueError('Símbolos no coinciden con manifiesto')
    if 'post_outcome' in df or 'v26_status' in df:
        raise ValueError('El archivo de Discovery expone columnas POST')
    return df,manifest


def drawable_bars(candles,pre,pre_count,post_count):
    """Trailing PRE must be contiguous; any unavailable tail is explicit."""
    import hll_structural_approach_ohlc_pre as ohlc
    if pre_count not in (48,96,144,192,288,384) or post_count not in (0,12,24,48,72,96):
        raise ValueError('Ventana visual no permitida')
    before=[]
    for ts in range(pre-TF,pre-pre_count*TF-1,-TF):
        if ts not in candles:break
        before.append(candles[ts])
    before.reverse()
    if not before or before[-1]['close_ts']!=pre:
        raise ValueError('No hay ninguna vela cerrada justo antes de PRE')
    future=[]
    for ts in range(pre,pre+post_count*TF,TF):
        if ts not in candles:break
        future.append(candles[ts])
    return before,future


def chart_for_attempt(r,candles,scale,pre_bars,post_bars,show_pivots):
    import plotly.graph_objects as go
    import hll_phase_reconstruction_pre as phase
    pre=int(r['pre_closed_ts'])
    before,future=drawable_bars(candles,pre,pre_bars,post_bars)
    display=before+future
    def ts(b):return pd.Timestamp(b['open_ts'],unit='ms',tz='UTC')
    fig=go.Figure(go.Candlestick(x=[ts(b) for b in display],
             open=[b['open'] for b in display],high=[b['high'] for b in display],
             low=[b['low'] for b in display],close=[b['close'] for b in display],
             name='30m',increasing_line_color='#169977',decreasing_line_color='#d45b67'))
    lo=float(r['zone_low']);hi=float(r['zone_high'])
    fig.add_hrect(y0=lo,y1=hi,opacity=.16,fillcolor='#d5a24d',line_width=0)
    for price in (lo,hi):fig.add_hline(y=price,line_dash='dash',line_color='#bd9248')
    fig.add_vline(x=pd.Timestamp(pre,unit='ms',tz='UTC'),line_dash='dash',line_color='#975ad0')
    if len(before)>96:
        fig.add_vline(x=pd.Timestamp(pre-96*TF,unit='ms',tz='UTC'),line_dash='dot',line_color='#8b99a7')
    if show_pivots and r['status']=='COMPLETE_PRE':
        core=bar_window(candles,pre,96)
        if core is None:raise ValueError('Faltan las 96 velas del overlay PRE')
        m={k:(int(r[k]) if k=='pre_closed_ts' else float(r[k]) if k in ('zone_low','zone_high','atr_pre') else str(r[k]))
           for k in ('attempt_id','symbol','level_id','pre_closed_ts','zone_low','zone_high','atr_pre')}
        _,pivots,legs,_=phase.describe_attempt(core,m,scale)
        for kind,col in [('LOW','#19aa80'),('HIGH','#d7a045')]:
            ps=[p for p in pivots if p['pivot_type']==kind and int(p['confirmed_known_closed_ts'])<=pre]
            if ps:
                fig.add_trace(go.Scatter(x=[pd.Timestamp(int(p['pivot_open_ts']),unit='ms',tz='UTC') for p in ps],
                         y=[float(p['pivot_price_close']) for p in ps],mode='markers',
                         marker=dict(size=8,color=col,symbol='triangle-up' if kind=='LOW' else 'triangle-down'),
                         name=f'Pivote {kind} confirmado PRE',
                         text=[f"Conocido: {pd.Timestamp(int(p['confirmed_known_closed_ts']),unit='ms',tz='UTC')}" for p in ps],
                         hovertemplate='%{text}<br>Precio: %{y}<extra></extra>'))
        for leg in legs:
            confirmed=leg['status']=='CONFIRMED_PRE'
            fig.add_trace(go.Scatter(x=[pd.Timestamp(int(leg['start_pivot_open_ts']),unit='ms',tz='UTC'),
                pd.Timestamp(int(leg['end_extreme_open_ts']),unit='ms',tz='UTC')],
                y=[float(leg['start_pivot_price_close']),float(leg['end_extreme_price_close'])],
                mode='lines',line=dict(color='#28b084' if leg['leg_direction']=='UP' else '#e6a753',
                                       dash='solid' if confirmed else 'dot',width=1.7),
                name='Fase '+leg['leg_direction']+' '+('confirmada' if confirmed else 'provisional'),showlegend=False))
    fig.update_layout(height=580,xaxis_rangeslider_visible=False,
            margin=dict(l=8,r=8,t=18,b=20),xaxis_title='UTC',yaxis_title='Precio')
    return fig,len(before),len(future)


def render_hll_structural_discovery(root):
    import streamlit as st
    import hll_structural_approach_ohlc_pre as ohlc
    root=Path(root)
    folder=root/OUTPUT_PARENT/'full290_v1'
    pilot_folders=sorted((root/OUTPUT_PARENT).glob('pilot_first_*_v1')) if (root/OUTPUT_PARENT).is_dir() else []
    folders=([folder] if (folder/'manifest.json').is_file() else []) + [p for p in pilot_folders if (p/'manifest.json').is_file()]
    st.markdown('#### 🔎 Discovery estructural PRE · 290 símbolos')
    st.caption('Universo condicionado a CROSS_UP; clasificaciones morfológicas, NO setups confirmados ni predicción de ruptura.')
    if not folders:
        st.info('Sin corrida Discovery. Ejecutá primero `python3 hll_structural_discovery_v1.py --plan` y un piloto `--run --max-symbols 5`.')
        return
    selected=st.selectbox('Corrida Discovery',folders,format_func=lambda p:p.name,key='hll_discovery_run')
    @st.cache_data(show_spinner=False)
    def cached_read(path,mtime):
        return load_discovery(Path(path))
    try:
        payload=cached_read(str(selected),(selected/'attempts_pre.csv').stat().st_mtime_ns)
        if payload is None:raise ValueError('Archivos de corrida incompletos')
        data,manifest=payload
    except (ValueError,OSError,KeyError) as e:
        st.error(f'No se pudo cargar Discovery: {e}');return
    c1,c2,c3=st.columns(3)
    c1.metric('Intentos totales',len(data))
    c2.metric('Con PRE 96 completo',int(data.status.eq('COMPLETE_PRE').sum()))
    c3.metric('Sin PRE 96 completo',int(data.status.ne('COMPLETE_PRE').sum()))
    scale=st.selectbox('Sensibilidad ATR',[0.5,1.0,1.5],index=1,key='hll_discovery_atr')
    field={0.5:'family_0p5',1.0:'family_1p0',1.5:'family_1p5'}[scale]
    names=sorted(data[field].dropna().astype(str).unique().tolist())
    c4,c5,c6=st.columns(3)
    with c4: fam=st.selectbox('Familia',['TODAS']+names,format_func=lambda x:FAMILY_LABELS.get(x,x),key='hll_discovery_family')
    with c5: sym=st.selectbox('Símbolo',['TODOS']+sorted(data.symbol.unique()),key='hll_discovery_sym')
    with c6: agree=st.selectbox('Acuerdo ATR',['TODOS','IGUAL_3_ATR','DISCREPA_ATR','SIN_DATOS'],key='hll_discovery_agree')
    filtered=data
    if fam!='TODAS':filtered=filtered[filtered[field].eq(fam)]
    if sym!='TODOS':filtered=filtered[filtered.symbol.eq(sym)]
    if agree!='TODOS':filtered=filtered[filtered.scale_agreement.eq(agree)]
    st.caption(f'{len(filtered)} intentos con estos filtros. Ninguno se selecciona por POST ni PnL.')
    if filtered.empty:return
    filtered=filtered.sort_values(['symbol','pre_closed_ts','attempt_id'])
    pages=max(1,math.ceil(len(filtered)/80))
    page=st.number_input('Página de intentos',min_value=1,max_value=pages,value=1,step=1,key='hll_discovery_page')
    shown=filtered.iloc[(page-1)*80:page*80]
    display_cols=['symbol','attempt_id','pre_closed_ts',field,'scale_agreement','original_pre_pattern_48',
                  'intercontact_48','latest_recovery_retention_1p0','preserved_fraction_last3_1p0','status']
    st.dataframe(shown[[x for x in display_cols if x in shown]],hide_index=True,use_container_width=True)
    options=shown.attempt_id.tolist()
    attempt=st.selectbox('Inspeccionar intento de esta página',options,
             format_func=lambda aid:f'{shown.loc[shown.attempt_id.eq(aid),"symbol"].iloc[0]} · {aid}',
             key='hll_discovery_attempt')
    row=shown.loc[shown.attempt_id.eq(attempt)].iloc[0]
    if row['status']!='COMPLETE_PRE':
        st.warning(f'PRE no evaluable: {row["reason"]}. No se fabricó clasificación.');return
    pre_bars=st.selectbox('Velas PRE para visualizar',[48,96,144,192,288,384],index=4,key='hll_discovery_pre_count')
    show_post=st.checkbox('Revelar POST (solo visual, no afecta familias)',value=False,key='hll_discovery_post')
    post_bars=st.selectbox('Velas POST',[12,24,48,72,96],index=4,key='hll_discovery_post_count',disabled=not show_post) if show_post else 0
    pivots=st.checkbox('Mostrar pivotes y fases PRE',value=True,key='hll_discovery_pivots')
    candle=root/CANDLES/f'{row.symbol}_30m.csv'
    @st.cache_data(show_spinner=False)
    def cached_candles(path,mtime):
        return ohlc.load_candles(Path(path),timestamp_kind='open')[0]
    if not candle.is_file():st.warning(f'Falta histórico para {row.symbol}');return
    try:
        candles=cached_candles(str(candle),candle.stat().st_mtime_ns)
        fig,actual_pre,actual_post=chart_for_attempt(row,candles,scale,pre_bars,post_bars,pivots)
        st.plotly_chart(fig,use_container_width=True,key='hll_discovery_chart')
        st.caption(f'PRE real mostrado: {actual_pre}/{pre_bars} velas · POST real mostrado: {actual_post}/{post_bars if show_post else 0}. '
                   'Fases calculadas exclusivamente con 96 velas PRE.')
    except (ValueError,TypeError,KeyError) as e:
        st.error(f'No se pudo dibujar el episodio: {e}');return
    st.markdown(f'**Familia {scale:g} ATR:** {FAMILY_LABELS.get(str(row[field]),str(row[field]))}')
    suffix={0.5:'0p5',1.0:'1p0',1.5:'1p5'}[scale]
    for name,col in [('A favor',f'evidence_{suffix}'),('En contra',f'contradictions_{suffix}'),('Limitaciones',f'limitations_{suffix}')]:
        value=row.get(col)
        st.caption(f'{name}: {value if pd.notna(value) and str(value).strip() else "Sin observaciones específicas"}')
    if scale!=1.0:st.caption('La tabla principal muestra métricas de continuidad a 1 ATR; los argumentos de familia sí corresponden a la escala elegida.')
    st.caption('Una familia positiva no implica calidad, ruptura futura, entrada real ni rentabilidad.')
