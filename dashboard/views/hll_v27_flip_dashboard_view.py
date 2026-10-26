"""Read-only HLL V2.7 analysis panel for the existing Horizontal Breakout dashboard."""
from __future__ import annotations
from pathlib import Path
import pandas as pd

OUTDIR=Path('reports/horizontal_level_flip_research_v27/runs')
V26DIR=Path('reports/horizontal_level_structure_lab_v26/runs/core15_extended_v26')
CANDLES30=Path('reports/horizontal_level_lifecycle_v21/candles_aug20_oct25_core15_btc')


def _read_bundle(folder):
    needed=['entry_candidates.csv','paths.csv','trades.csv','summary.csv','coverage.csv','manifest.json']
    missing=[x for x in needed if not (folder/x).is_file()]
    if missing:return None,missing
    frames={x[:-4]:pd.read_csv(folder/x,low_memory=False) for x in needed if x.endswith('.csv')}
    import json
    frames['manifest']=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    e=frames['entry_candidates']
    if e.duplicated(['attempt_id','entry_type']).any():raise ValueError('Duplicated entry candidates')
    if frames['paths'].duplicated(['attempt_id','entry_type','horizon_min']).any():
        raise ValueError('Duplicated path records')
    return frames,[]


def _filter_trades(d,typ,tp,stop,h,symbol='Todos'):
    out=d[(d.entry_type==typ)&d.tp_pct.eq(tp)&d.stop_type.eq(stop)&d.horizon_min.eq(h)]
    if symbol!='Todos':out=out[out.symbol.eq(symbol)]
    return out


def _episode_fig(attempt,entries,bars,tp,stop_name,scope_minutes,post=True):
    import plotly.graph_objects as go
    from datetime import datetime,timezone
    clock=int(entries.known_closed_ts.min())
    i=bars.index[bars.timestamp.eq(int(attempt.known_closed_ts)-1800000)]
    if len(i)!=1:raise ValueError('Missing source 30m breakout candle')
    index=int(i[0]);left=max(0,index-48);right=min(len(bars),index+max(36,scope_minutes//30+8))
    view=bars.iloc[left:right]
    xs=pd.to_datetime(view.timestamp,unit='ms',utc=True)
    fig=go.Figure(go.Candlestick(x=xs,open=view.open,high=view.high,low=view.low,close=view.close,
        name='30m',increasing_line_color='#159b70',decreasing_line_color='#d65b68'))
    lo=float(attempt.zone_low);hi=float(attempt.zone_high)
    fig.add_hrect(y0=lo,y1=hi,fillcolor='#d6a251',opacity=.17,line_width=0)
    fig.add_hline(y=lo,line_color='#c4934a',line_dash='dash')
    fig.add_hline(y=hi,line_color='#c4934a',line_dash='dash')
    fig.add_vline(x=pd.Timestamp(int(attempt.known_closed_ts),unit='ms',tz='UTC'),line_dash='dot',line_color='#3470cb')
    type_colors={'FLIP_CONFIRMED':'#16a34a','FLIP_SUSTAINED_4B':'#2cc98d'}
    for r in entries.itertuples(index=False):
        col=type_colors.get(r.entry_type,'#17a673')
        time=pd.Timestamp(int(r.known_closed_ts),unit='ms',tz='UTC')
        fig.add_vline(x=time,line_dash='dot',line_color=col)
        if pd.notna(r.entry_price) and float(r.entry_price)>0:
            fig.add_trace(go.Scatter(x=[time],y=[float(r.entry_price)],mode='markers',
                marker=dict(size=11,symbol='diamond',color=col),name=r.entry_type))
        if r.entry_type=='FLIP_CONFIRMED':
            sl=r.zone_stop_price if stop_name=='ZONE_ATR_0P1' else (
                r.retest_stop_price if stop_name=='RETEST_ATR_0P1' else None)
            if stop_name.startswith('FIXED_') and pd.notna(r.entry_price):
                s=float(stop_name.split('_')[1].replace('P','.'))
                sl=float(r.entry_price)*(1-s/100)
            if pd.notna(sl) and sl is not None and float(sl)>0:
                fig.add_hline(y=float(sl),line_color='#e76464',line_dash='dot',
                    annotation_text='SL desde flip confirmado (referencia)',annotation_position='bottom right')
            if pd.notna(r.entry_price):
                fig.add_hline(y=float(r.entry_price)*(1+tp/100),line_color='#2ca86c',line_dash='dot',
                    annotation_text=f'TP {tp:g}% desde flip confirmado',annotation_position='top right')
    fig.update_layout(height=530,xaxis_rangeslider_visible=False,
        margin=dict(l=20,r=20,t=25,b=25),xaxis_title='UTC',yaxis_title='Precio',showlegend=True)
    return fig


def render_hll_v27_flip_dashboard(root):
    import streamlit as st
    root=Path(root)
    st.markdown('### 🎯 Flip LONG · entradas y riesgo V2.7')
    st.caption('Investigación retrospectiva independiente. Entradas solo después de FLIP_CONFIRMED / FLIP_SUSTAINED_4B; no opera ni recomienda órdenes.')
    main=root/OUTDIR
    runs=sorted([p for p in main.iterdir() if (p/'manifest.json').is_file()],
        key=lambda p:p.stat().st_mtime,reverse=True) if main.is_dir() else []
    if not runs:
        st.warning('Aún no hay evaluación V2.7. Primero ejecutá hll_v27_flip_research.py --plan y --run. No afecta V2.6.')
        return
    run=st.selectbox('Corrida V2.7',runs,format_func=lambda x:x.name,key='hll27_run')
    try:data,missing=_read_bundle(run)
    except (ValueError,OSError,KeyError) as ex:
        st.error(f'Los resultados V2.7 no son consistentes: {ex}');return
    if missing:st.error('Faltan resultados V2.7: '+', '.join(missing));return
    e=data['entry_candidates'];trade=data['trades'];paths=data['paths']
    a,b,c,d=st.columns(4)
    a.metric('Flips confirmados',e[e.entry_type.eq('FLIP_CONFIRMED')].attempt_id.nunique())
    b.metric('Sostenidos 4B',e[e.entry_type.eq('FLIP_SUSTAINED_4B')].attempt_id.nunique())
    c.metric('Rutas 1m completas',int(paths.path_state.eq('COMPLETE_1M').sum()))
    d.metric('Rutas censuradas',int(paths.path_state.ne('COMPLETE_1M').sum()))
    st.warning('No confundir el resultado POST del nivel con una entrada causal. Ambos relojes son posteriores a la confirmación. No hay slippage, funding ni simulación de cartera.')
    l,r1,r2=st.columns([1.2,1,1])
    with l: typ=st.selectbox('Momento de entrada',['FLIP_CONFIRMED','FLIP_SUSTAINED_4B'],key='hll27_clock')
    with r1: h=st.selectbox('Horizonte (min)',sorted(trade.horizon_min.dropna().unique().astype(int)),index=0,key='hll27_h')
    with r2: sym=st.selectbox('Símbolo',['Todos']+sorted(e.symbol.unique()),key='hll27_sym')
    x,y=st.columns(2)
    with x: tp=st.selectbox('Take profit (%)',sorted(trade.tp_pct.dropna().unique()),index=0,key='hll27_tp')
    with y: stop=st.selectbox('Stop',sorted(trade.stop_type.dropna().unique()),key='hll27_stop')
    t=_filter_trades(trade,typ,tp,stop,h,sym)
    counts=t.path_state.value_counts().to_dict()
    st.caption(f'{len(t)} episodios para la combinación · completos: {counts.get("COMPLETE_1M",0)} · sin cobertura: {len(t)-counts.get("COMPLETE_1M",0)}')
    good=t[t.path_state.eq('COMPLETE_1M') & t.outcome.isin(['TP_FIRST','SL_FIRST','TIME_EXIT','BOTH_SAME_1M_SL_ASSUMED'])]
    if good.empty:st.info('Sin rutas completas para esta combinación. No interpretar un censor como pérdida.')
    else:
        net=pd.to_numeric(good.return_after_fee_pct,errors='coerce')
        a,b,c,d=st.columns(4)
        a.metric('Media neta/evento',f'{net.mean():+.3f}%')
        b.metric('TP primero',f'{int(good.outcome.eq("TP_FIRST").sum())}/{len(good)}')
        c.metric('SL primero',f'{int(good.outcome.eq("SL_FIRST").sum())}/{len(good)}')
        d.metric('STOP medio',f'{pd.to_numeric(good.stop_risk_pct,errors="coerce").mean():.2f}%')
    with st.expander('📊 Comparador general de TP / SL',expanded=False):
        ss=data['summary']
        ss=ss[ss.entry_type.eq(typ)&ss.horizon_min.eq(h)]
        st.dataframe(ss.sort_values('mean_net_pct',ascending=False),hide_index=True,use_container_width=True,height=330)
        st.caption('Exploratorio: las combinaciones se prueban en los mismos episodios. No optimizar aquí una estrategia con la misma muestra.')
    with st.expander('📋 Cobertura minuto a minuto',expanded=False):
        st.dataframe(data['coverage'],hide_index=True,use_container_width=True)
    choices=e[e.entry_type.eq(typ)]
    if sym!='Todos':choices=choices[choices.symbol.eq(sym)]
    if choices.empty:st.info('Sin entradas para este filtro.');return
    choices=choices.sort_values('known_closed_ts',ascending=False).copy()
    choices['choice']=(choices.symbol+' · '+pd.to_datetime(choices.known_closed_ts,unit='ms',utc=True).dt.strftime('%Y-%m-%d %H:%M UTC')+' · '+choices.attempt_id.str[:10])
    chosen=st.selectbox('Inspector de episodio',choices.choice.tolist(),key='hll27_event')
    row=choices[choices.choice.eq(chosen)].iloc[0]
    peer=e[e.attempt_id.eq(row.attempt_id)].sort_values('known_closed_ts')
    a26file=root/V26DIR/'breakout_attempts.csv'
    if not a26file.is_file():st.info('Falta V2.6 para dibujar nivel y velas.');return
    a26=pd.read_csv(a26file,low_memory=False)
    attempt=a26[a26.attempt_id.eq(row.attempt_id)]
    candle=root/CANDLES30/f'{row.symbol}_30m.csv'
    if attempt.empty or not candle.is_file():st.warning('Faltan el intento o las velas 30m');return
    bars=pd.read_csv(candle)
    try:fig=_episode_fig(attempt.iloc[0],peer,bars,tp,stop,h)
    except (ValueError,KeyError) as ex:st.warning(str(ex));return
    st.plotly_chart(fig,use_container_width=True,key='hll27_episode_chart')
    st.caption('Las líneas de TP y SL corresponden a FLIP_CONFIRMED como referencia; la segunda entrada tiene precio y stop propios. Se muestran los dos relojes, sin adelantar confirmaciones.')
    comparable=t[t.attempt_id.eq(row.attempt_id)]
    other=_filter_trades(trade,'FLIP_SUSTAINED_4B' if typ=='FLIP_CONFIRMED' else 'FLIP_CONFIRMED',tp,stop,h,sym)
    pair=pd.concat([comparable,other[other.attempt_id.eq(row.attempt_id)]],ignore_index=True)
    fields=[i for i in ['entry_type','known_closed_ts','entry_price','horizon_min','tp_pct','stop_type','stop_price','stop_risk_pct','path_state','outcome','return_after_fee_pct','mfe_to_exit_pct','mae_to_exit_pct','bars_to_exit'] if i in pair]
    st.markdown('**Entrada A vs. B sobre el mismo intento (cuando ambas existieron)**')
    st.dataframe(pair[fields],hide_index=True,use_container_width=True)
    st.caption('Una misma ruptura puede producir dos señales y compartir trayectoria. No sumar estas operaciones como si fueran independientes.')
