"""Streamlit read-only V1.2 level episodes and candle-strength review."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from horizontal_breakout_lab import Archive, TF
from horizontal_breakout_lifecycle_v12 import OUTPUT


def _load(path):
    try: return pd.read_csv(path)
    except (FileNotFoundError,pd.errors.EmptyDataError):return pd.DataFrame()


def _dt(ms):
    return pd.Timestamp(int(ms),unit='ms',tz='UTC')


def _show_candles(level, attempts, tests, bars, mode, pre_bars, focus_ts=None):
    freeze=int(level.formation_closed_ts)
    relevant=attempts.sort_values('signal_closed_ts')
    event_ts=int(focus_ts) if focus_ts is not None else (int(relevant.signal_closed_ts.iloc[-1]) if not relevant.empty else freeze)
    # PRE is a strict cutoff: never display a 30m candle that was not yet closed.
    cutoff=freeze if mode=='PRE' else event_ts if mode=='SIGNAL' else event_ts+48*60*60*1000
    start=freeze-pre_bars*TF
    c=bars[(bars.timestamp>=start)&(bars.timestamp+TF<=cutoff)].copy()
    if c.empty:
        st.info('No hay velas nativas 30m cerradas para el gráfico.')
        return
    xx=pd.to_datetime(c.timestamp,unit='ms',utc=True)
    fig=make_subplots(rows=2,cols=1,shared_xaxes=True,vertical_spacing=.035,row_heights=[.78,.22])
    fig.add_trace(go.Candlestick(x=xx,open=c.open,high=c.high,low=c.low,close=c.close,
        increasing_line_color='#13a68b',decreasing_line_color='#d55a5a',
        increasing_fillcolor='#13a68b',decreasing_fillcolor='#d55a5a',name='30m OHLC'),row=1,col=1)
    fig.add_trace(go.Bar(x=xx,y=c.volume,marker_color='#76879b',name='Volumen 30m'),row=2,col=1)
    low,high=float(level.zone_low),float(level.zone_high)
    fig.add_hrect(y0=low,y1=high,fillcolor='rgba(214,157,62,.13)',line_width=0,row=1,col=1)
    fig.add_hline(y=float(level.level),line_dash='dash',line_color='#e5af48',row=1,col=1)
    if start<freeze<=cutoff:
        fig.add_vline(x=_dt(freeze),line_dash='dot',line_color='#7c8ca9',row=1,col=1)
    # BEFORE freeze, yellow line is retrospective reconstruction, not a live signal.
    try: visits=json.loads(level.touch_open_timestamps)
    except (TypeError,ValueError):visits=[]
    hits=c[(c.timestamp.isin(visits))&(c.timestamp<freeze)]
    if not hits.empty:
        fig.add_trace(go.Scatter(x=pd.to_datetime(hits.timestamp,unit='ms',utc=True),y=hits.high,
                        mode='markers',marker=dict(color='#e5af48',size=10,symbol='circle-open',line=dict(width=2)),
                        name='Toques fundadores PRE'),row=1,col=1)
    if mode!='PRE' and not tests.empty:
        observed=tests[tests.test_closed_ts<=cutoff]
        if not observed.empty:
            fig.add_trace(go.Scatter(x=pd.to_datetime(observed.test_closed_ts,unit='ms',utc=True),
                             y=observed.test_high,mode='markers',name='LEVEL_TEST posteriores',
                             marker=dict(color='#c88f42',size=8,symbol='square-open')),row=1,col=1)
    if mode!='PRE' and not relevant.empty:
        visible=relevant[relevant.signal_closed_ts<=cutoff]
        strong=visible[visible.strength_class=='STRONG_BREAKOUT']
        weak=visible[visible.strength_class!='STRONG_BREAKOUT']
        for f,name,color in [(weak,'Intentos / cruces','#66a7cf'),(strong,'Fuerza al cierre','#19ce9e')]:
            if not f.empty:
                fig.add_trace(go.Scatter(x=pd.to_datetime(f.signal_closed_ts,unit='ms',utc=True),
                                 y=f.signal_close,mode='markers',name=name,
                                 marker=dict(color=color,size=12,symbol='diamond')),
                                 row=1,col=1)
    fig.update_layout(height=590,xaxis_rangeslider_visible=False,margin=dict(t=16,b=10,l=4,r=4))
    st.plotly_chart(fig,use_container_width=True)
    st.caption('Línea amarilla = nivel congelado. Marcadores celestes = cruces débiles; verdes = fuerza al cierre. '
               'El intervalo antes de la marca vertical de formación es reconstrucción histórica, NO un nivel disponible antes de ese instante.')


def render_v12(root):
    root=Path(root)
    folder=root/OUTPUT
    runs=sorted((x for x in folder.iterdir() if (x/'run_config.json').is_file()),
                key=lambda p:p.stat().st_mtime,reverse=True) if folder.is_dir() else []
    st.markdown('### 🧭 V1.2 · Ciclo del nivel y fuerza de ruptura')
    st.caption('El mismo level_id agrupa intentos sucesivos. STRONG usa solo datos disponibles al cierre 30m. PRE/SIGNAL/POST limitan **el gráfico**; las tablas muestran resultados históricos completos y no son una vista ciega.')
    if not runs:
        st.info('Primero ejecutá `python3 horizontal_breakout_lifecycle_v12.py --plan`, luego `--run` y `--audit`. El panel V1 no se modifica.')
        return
    run=st.selectbox('Corrida V1.2',runs,format_func=lambda x:x.name,key='hbl12_run')
    config=json.loads((run/'run_config.json').read_text(encoding='utf-8'))
    audit=json.loads((run/'audit.json').read_text(encoding='utf-8')) if (run/'audit.json').exists() else {}
    levels,attempts,outcomes,tests=(_load(run/f'{f}.csv') for f in ('levels','attempts','outcomes','tests'))
    if not audit.get('success'):
        st.error('Auditoría incompleta o no ejecutada; no interpretar tasas.')
    c=st.columns(4)
    for b,label,value in zip(c,['Niveles independientes','Intentos','STRONG al cierre','Paths 48h completos'],
                             [len(levels),len(attempts),audit.get('strong',0),audit.get('path_counts',{}).get('COMPLETE',0)]):
        b.metric(label,value)
    if attempts.empty:
        st.warning('No hubo intentos observables en la corrida seleccionada.')
        if not levels.empty:st.dataframe(levels,hide_index=True,use_container_width=True)
        return
    joined=attempts.merge(outcomes,on=['attempt_id','level_id','symbol','signal_closed_ts'],how='left',validate='one_to_one')
    st.warning('Una señal fuerte no equivale a trade rentable. Muchos eventos de una misma zona/símbolo están correlacionados. Comparar solo outcomes observados; CENSORED no es SL.')
    st.dataframe(joined.groupby('strength_class',dropna=False).agg(
        intentos=('attempt_id','size'),niveles=('level_id','nunique'),
        con_fill=('entry_price',lambda x:x.notna().sum()),
        cobertura_48h=('path_state',lambda x:(x=='COMPLETE').sum()),
        tp2_360=('tp2_h360',lambda x:(x=='TP_FIRST').sum()),
        sl2_360=('tp2_h360',lambda x:(x=='SL_FIRST').sum()),
        censurados_360=('tp2_h360',lambda x:(x=='CENSORED').sum()),
    ).reset_index(),hide_index=True,use_container_width=True)
    st.download_button('⬇️ Intentos y resultados V1.2 (CSV)',joined.to_csv(index=False).encode(),
                      file_name='horizontal_breakout_lifecycle_v12.csv',mime='text/csv',key='hbl12_export')
    st.divider()
    symbols=sorted(attempts.symbol.unique())
    sym=st.selectbox('Símbolo · V1.2',symbols,key='hbl12_symbol')
    selection=levels[levels.symbol==sym].sort_values('formation_closed_ts',ascending=False)
    ids=list(selection.level_id)
    level_id=st.selectbox('Nivel horizontal congelado',ids,format_func=lambda k:(
        f"{_dt(selection[selection.level_id==k].iloc[0].formation_closed_ts)} · "
        f"{selection[selection.level_id==k].iloc[0].attempt_count} intentos · "
        f"{selection[selection.level_id==k].iloc[0].end_state}"),key='hbl12_level')
    level=selection[selection.level_id==level_id].iloc[0]
    records=attempts[attempts.level_id==level_id].sort_values('signal_closed_ts')
    st.write(f"**Techo congelado:** {float(level.level):.8f} · zona {float(level.zone_low):.8f}–{float(level.zone_high):.8f} · "
             f"{int(level.touch_count)} toques PRE · {int(level.window)} velas de formación")
    level_tests=tests[tests.level_id==level_id] if not tests.empty else pd.DataFrame()
    st.caption(f"Visitas posteriores al nivel: {len(level_tests)} (LEVEL_TEST), aparte de los toques fundadores PRE.")
    st.dataframe(records[[c for c in ('signal_utc','attempt_number','trigger_kind','strength_class',
        'volume_ratio20','body_atr','close_position','new_high_lookback',
        'zone_cleared','strength_checks_passed','post_label') if c in records]],
        hide_index=True,use_container_width=True)
    focus_options=[int(x) for x in records.signal_closed_ts]
    focus=st.selectbox('Intento a inspeccionar (cierre UTC)', focus_options, index=len(focus_options)-1,
        format_func=lambda k: (f'{_dt(k)} · ' + records[records.signal_closed_ts==k].iloc[0].strength_class),
        key='hbl12_attempt')
    mode=st.radio('Límite visual de información',('PRE','SIGNAL','POST'),index=1,horizontal=True,key='hbl12_mode')
    pre_bars=st.select_slider('Contexto PRE · velas de 30m',options=[60,90,120,180,240],value=120,key='hbl12_pre')
    try:
        with Archive(root,Path(config['candles_dir']) if config.get('candles_dir') else None) as a:
            candles,_,_,_=a.load(sym)
        # Historical outcomes don't change when switching graph display modes.
        _show_candles(level,records,level_tests,candles,mode,pre_bars,focus)
    except Exception as exc:
        st.error(f'No se pudo leer OHLCV para el inspector: {exc}')
    with st.expander('Metodología y cobertura V1.2'):
        st.json(audit)
        st.caption('STRONG = cierre > techo de zona, volumen relativo 20x ≥2, cuerpo alcista ≥0.6 ATR de formación, '
                   'cierre en cuarto superior y high > máximo de 12 velas previas; todos conocidos al cierre de 30m. '
                   'Si falta histórico para alguno, UNKNOWN_STRENGTH. '
                   'POST_HOLD = dos cierres futuros sobre la zona; no se incorpora a la señal. '
                   'Solo entradas teóricas en apertura del siguiente 1m; sin cartera, funding ni slippage. '
                   'El motor de REACTIONs y LIVE siguen intactos.')
