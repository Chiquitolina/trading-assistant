"""V2.8.2: one stable PRE cohort, POST outcomes drawn on the *same* episode.

Standalone block imported by the existing V2.8 Streamlit view. No trading code.
"""
from __future__ import annotations
import json
from pathlib import Path
import pandas as pd

TF=1_800_000
RUNS=Path('reports/horizontal_level_structure_quality_v282/runs')
CANDLES=Path('reports/horizontal_level_lifecycle_v21/candles_aug20_oct25_core15_btc')
PATTERNS={
    'ASCENDING_CLEAN':'A · Mínimos ascendentes limpios',
    'ASCENDING_CHOPPY':'B · Mínimos ascendentes con cruces desordenados',
    'HORIZONTAL_BASE':'C · Base horizontal bajo resistencia',
    'MULTI_CROSS_RANGE':'D · Múltiples cruces / rango errático',
    'IMPULSIVE_APPROACH':'E · Aproximación impulsiva al techo',
    'DESCENDING_LOW_SEQUENCE':'F · Mínimos descendentes',
    'OTHER':'G · Mixta / otra estructura',
    'UNKNOWN':'Sin contexto suficiente',
}
STATES={
    'FLIP_CONFIRMADO':'Flip confirmado',
    'SIN_FLIP_EN_VENTANA':'Sin flip dentro del seguimiento V2.6',
    'INDETERMINADO':'No resuelto / censurado',
}


def load_cohort(folder):
    file=folder/'pre_cohort.csv'
    df=pd.read_csv(file,low_memory=False)
    required={'attempt_id','symbol','post_outcome','breakout_closed_ts',
              'pre_closed_ts','flip_confirmed_closed_ts','retest_closed_ts',
              'failed_closed_ts','zone_low','zone_high','v26_status'}
    for w in (24,48,96):
        required.update({f'pre_pattern_{w}',f'pre{w}_pivot_lows_json',f'flip_pattern_{w}'})
    if required-set(df):raise ValueError('V2.8.2: columnas ausentes: '+', '.join(sorted(required-set(df))))
    if df.attempt_id.isna().any() or df.attempt_id.duplicated().any():
        raise ValueError('V2.8.2: attempt_id duplicado o vacío')
    if df.post_outcome.isna().any() or not set(df.post_outcome).issubset(STATES):
        raise ValueError('V2.8.2: estado POST inválido')
    if (df.loc[df.post_outcome.eq('FLIP_CONFIRMADO'),'flip_confirmed_closed_ts'].isna().any()
            or df.loc[~df.post_outcome.eq('FLIP_CONFIRMADO'),'flip_confirmed_closed_ts'].notna().any()):
        raise ValueError('V2.8.2: fecha de confirmación inconsistente')
    return df


def filtered(df, window, pattern='TODAS', symbol='Todos', state='TODOS'):
    field=f'pre_pattern_{int(window)}'
    choices=df.copy()
    if pattern!='TODAS':choices=choices[choices[field].eq(pattern)]
    if symbol!='Todos':choices=choices[choices.symbol.eq(symbol)]
    if state!='TODOS':choices=choices[choices.post_outcome.eq(state)]
    return choices


def _clock(value):
    if value is None or pd.isna(value):return None
    n=int(value)
    return n if n>0 else None


def _date(ms):
    return pd.Timestamp(int(ms),unit='ms',tz='UTC')


def make_chart(row,bars,window,show_post=False,show_flip_pivots=False):
    import plotly.graph_objects as go
    pre=int(row.pre_closed_ts)
    breakout=int(row.breakout_closed_ts)
    flip=_clock(row.flip_confirmed_closed_ts)
    retest=_clock(row.retest_closed_ts)
    failed=_clock(row.failed_closed_ts)
    end=pre if not show_post else max([breakout]+[v for v in [flip,retest,failed] if v is not None])
    # After the final classified event show a few more CLOSED 30m bars.
    end+=0 if not show_post else 12*TF
    start=pre-(int(window)+8)*TF
    visible=bars.loc[(bars.timestamp>=start)&(bars.timestamp+TF<=end)].copy()
    if visible.empty:raise ValueError('No hay velas 30m para PRE/POST')
    if not bars.timestamp.eq(pre-TF).any():
        raise ValueError('Falta vela al corte PRE')
    if show_post and flip is not None and not bars.timestamp.eq(flip-TF).any():
        raise ValueError('Falta vela del cierre que confirmó el flip')
    x=pd.to_datetime(visible.timestamp,unit='ms',utc=True)
    fig=go.Figure()
    fig.add_trace(go.Candlestick(x=x,open=visible.open,high=visible.high,
                                 low=visible.low,close=visible.close,name='Velas 30m',
                                 increasing_line_color='#17a883',decreasing_line_color='#d75f69',
                                 increasing_fillcolor='#17a883',decreasing_fillcolor='#d75f69'))
    lo=float(row.zone_low);hi=float(row.zone_high)
    fig.add_hrect(y0=lo,y1=hi,fillcolor='#d5a352',opacity=.17,line_width=0)
    fig.add_hline(y=lo,line_color='#bc9553',line_dash='dot')
    fig.add_hline(y=hi,line_color='#bc9553',line_dash='dash')
    # Pivots are always *original PRE pivots*, even after showing future candles.
    pivot_field=f'pre{window}_pivot_lows_json'
    raw=row.get(pivot_field,'[]')
    pivots=json.loads(raw) if isinstance(raw,str) and raw else []
    if any(int(p['known_closed_ts'])>pre for p in pivots):
        raise ValueError('Lookahead: pivote PRE confirmado después del corte')
    if pivots:
        fig.add_trace(go.Scatter(x=[_date(p['open_ts']) for p in pivots],
            y=[float(p['low']) for p in pivots],mode='markers+lines',
            line=dict(color='#f9bc55',width=1.5,dash='dot'),
            marker=dict(size=9,color='#f9bc55',symbol='triangle-up'),
            name='Mínimos confirmados en PRE',
            text=[f"Confirmado: {_date(p['known_closed_ts'])}" for p in pivots],
            hovertemplate='%{text}<br>Precio: %{y}<extra></extra>'))
    # PRE cutoff is always visible without revealing future outcome.
    fig.add_vline(x=_date(pre),line_color='#9aa6b7',line_dash='dash',line_width=1.4)
    if show_post:
        fig.add_vrect(x0=_date(breakout-TF),x1=_date(end),
                      fillcolor='#2b9c90',opacity=.04,line_width=0)
        fig.add_vline(x=_date(breakout),line_color='#4297ef',line_dash='dot',line_width=1.6)
        if retest is not None:
            fig.add_vline(x=_date(retest),line_color='#f0bd5e',line_dash='dot',line_width=1.5)
        if flip is not None:
            close=bars.loc[bars.timestamp.eq(flip-TF),'close'].iloc[0]
            fig.add_trace(go.Scatter(x=[_date(flip-TF)],y=[float(close)],mode='markers',
                marker=dict(color='#10c48c',size=13,symbol='diamond',line=dict(width=1,color='#ffffff')),
                name='FLIP confirmado al cierre',
                text=[str(_date(flip))],hovertemplate='Flip conocido al cierre: %{text}<extra></extra>'))
            fig.add_vline(x=_date(flip),line_color='#10c48c',line_width=2)
        if failed is not None:
            # A flip can confirm AND be lost later: draw both, not just the first.
            fig.add_vline(x=_date(failed),line_color='#e46069',line_width=2)
        if show_flip_pivots and flip is not None:
            field=f'flip{window}_pivot_lows_json'
            raw_flip=row.get(field,'[]'); fp=json.loads(raw_flip) if isinstance(raw_flip,str) else []
            fp=[p for p in fp if int(p['known_closed_ts'])<=flip]
            if fp:
                fig.add_trace(go.Scatter(x=[_date(p['open_ts']) for p in fp],
                  y=[float(p['low']) for p in fp],mode='markers',
                  marker=dict(color='#57a4f5',size=8,symbol='circle-open'),
                  name='Pivotes conocidos al FLIP (solo POST)'))
    fig.update_layout(height=540,xaxis_rangeslider_visible=False,
        margin=dict(l=12,r=10,t=22,b=15),xaxis_title='UTC',yaxis_title='Precio',showlegend=True)
    return fig


def render_hll_v282_pre_followup(root,blind=True):
    """Return True if V282 panel was rendered, False if its run isn't installed."""
    import streamlit as st
    root=Path(root)
    parent=root/RUNS
    runs=sorted([p for p in parent.iterdir() if (p/'pre_cohort.csv').is_file() and
                 (p/'manifest.json').is_file()],key=lambda p:p.stat().st_mtime,reverse=True) if parent.is_dir() else []
    if not runs:return False
    st.markdown('#### Inspector de candidatos PRE → desenlace del mismo intento · V2.8.2')
    folder=st.selectbox('Corrida de cohortes PRE',runs,format_func=lambda p:p.name,key='hll282_run')
    try:df=load_cohort(folder)
    except (OSError,ValueError,KeyError) as ex:
        st.error(f'V2.8.2 incompleto: {ex}')
        return True
    a,b,c=st.columns([1,2,1.5])
    with a:window=st.selectbox('Ventana 30m',[24,48,96],index=1,key='hll282_window')
    with b:
        pattern=st.selectbox('Tipo de estructura original PRE',['TODAS']+list(PATTERNS),
            format_func=lambda x: 'Todas las estructuras PRE' if x=='TODAS' else PATTERNS[x],key='hll282_pattern')
    with c:
        symbol=st.selectbox('Símbolo',['Todos']+sorted(df.symbol.unique().tolist()),key='hll282_symbol')
    if blind:
        st.caption('Auditoría ciega activa: los desenlaces están ocultos. Desactivala arriba para ver cuándo confirmó cada candidato.')
        state='TODOS'
    else:
        state=st.selectbox('Desenlace posterior',['TODOS']+list(STATES),
            format_func=lambda x:'Todos los desenlaces' if x=='TODOS' else STATES[x],key='hll282_state')
    sel=filtered(df,window,pattern,symbol,state)
    st.caption(f'{len(sel)} intentos originales V2.6 en esta selección; PRE se mantiene fijo. Cambiar el desenlace no reclasifica los intentos.')
    if not blind:
        counts=sel.post_outcome.value_counts()
        slots=st.columns(4)
        for ix,(label,n) in enumerate([('Detectados PRE',len(sel)),
             ('Flip confirmado',int(counts.get('FLIP_CONFIRMADO',0))),
             ('Sin flip en ventana',int(counts.get('SIN_FLIP_EN_VENTANA',0))),
             ('No resuelto',int(counts.get('INDETERMINADO',0)))]):
            slots[ix].metric(label,n)
        st.caption('"Sin flip" significa solo dentro del seguimiento definido en V2.6; no implica que nunca haya confirmado. Censurados y no resueltos se mantienen separados.')
    if sel.empty:
        st.info('No hay casos bajo esos filtros.')
        return True
    sel=sel.sort_values('breakout_closed_ts',ascending=False).copy()
    sel['label']=(sel.symbol+' · '+pd.to_datetime(sel.breakout_closed_ts,unit='ms',utc=True).dt.strftime('%Y-%m-%d %H:%M UTC')+' · '+sel.attempt_id.astype(str).str[:10])
    ids=sel.attempt_id.tolist()
    idx=st.selectbox('Episodio PRE (ID original, no cambia por su resultado)',range(len(sel)),
        format_func=lambda i:sel.label.iloc[i],key='hll282_episode')
    row=sel.iloc[idx]
    show_post=st.checkbox('Mostrar en el mismo gráfico breakout, retest y desenlace POST',
        value=not blind,disabled=blind,key='hll282_show_post')
    show_flip_pivots=st.checkbox('Agregar pivotes conocidos recién al confirmar el flip',
        value=False,disabled=not show_post,key='hll282_flip_pivots')
    p=root/CANDLES/f'{row.symbol}_30m.csv'
    if not p.is_file():
        st.warning(f'Faltan velas originales: {p}')
        return True
    try:
        bars=pd.read_csv(p,usecols=['timestamp','open','high','low','close']).sort_values('timestamp').reset_index(drop=True)
        fig=make_chart(row,bars,window,show_post,show_flip_pivots)
        st.plotly_chart(fig,use_container_width=True,key='hll282_chart')
    except (OSError,ValueError,KeyError,TypeError) as ex:
        st.error(f'No se pudo mostrar el gráfico PRE → POST: {ex}')
        return True
    st.info(f'Estructura original PRE: **{PATTERNS.get(row[f"pre_pattern_{window}"])}**. '
            'Permanece en este grupo incluso cuando cambia su forma después.')
    if show_post:
        st.markdown(f'**Desenlace V2.6:** {STATES[row.post_outcome]} · `{row.v26_status}`')
        if row.post_outcome=='FLIP_CONFIRMADO':
            st.success(f"Confirmación conocida: **{_date(row.flip_confirmed_closed_ts).strftime('%Y-%m-%d %H:%M UTC')}** "
                       f"· Geometría al FLIP: {PATTERNS.get(row[f'flip_pattern_{window}'],'Sin contexto')}.")
            if _clock(row.failed_closed_ts) is not None:
                st.warning('Atención: el nivel se perdió posteriormente; esto no elimina la confirmación que ya ocurrió.')
        elif row.post_outcome=='INDETERMINADO':
            st.warning('Seguimiento censurado/no resuelto. No debe computarse como fracaso.')
        elif row.post_outcome=='SIN_FLIP_EN_VENTANA':
            st.caption('No se confirmó un flip bajo las reglas y horizonte de V2.6.')
    st.caption('Las marcas verdes/rojas son desenlaces posteriores; NO se conocen al corte PRE y no deben usarse para construir las etiquetas originales.')
    return True
