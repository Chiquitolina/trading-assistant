"""Historical, read-only HLL hierarchy plot inside the existing Horizontal Level Lab.

No detector recomputation on Streamlit rerun: read an immutable frozen scan only.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from hll_hierarchical_levels_v1 import RUNS, TF, sha, score_asof

COLORS={'A':'#df7770','B':'#e2b15b','C':'#6a9dd6','D':'#8995a7'}


def verified_run(folder: Path):
    folder=Path(folder)
    manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('version')!='hll-hierarchical-levels-v1.0-exploratory':
        raise ValueError('Versión desconocida de la jerarquía HLL')
    for name,expected in manifest['files_sha256'].items():
        file=folder/name
        if not file.is_file() or sha(file)!=expected:
            raise ValueError(f'Archivo de jerarquía HLL ausente o alterado: {name}')
    return manifest


def read_frozen(folder:Path):
    manifest=verified_run(folder)
    f=pd.read_csv(folder/'formations.csv',low_memory=False)
    e=pd.read_csv(folder/'events.csv',low_memory=False)
    x=pd.read_csv(folder/'ends.csv',low_memory=False)
    b=pd.read_csv(folder/'candles_30m.csv',float_precision='round_trip')
    if len(b)!=manifest['bars_30m'] or len(f)!=manifest['levels_detected']:
        raise ValueError('Cardinalidad HLL incorrecta respecto a manifest')
    for field in ('timestamp','open','high','low','close'):
        if field not in b:raise ValueError('Faltan velas 30m congeladas')
    t=b.timestamp.to_numpy(dtype=np.int64)
    if (np.diff(t)!=TF).any() or int(t[-1])+TF!=manifest['cutoff_ms']:
        raise ValueError('Histórico congelado discontinuo o con final incorrecto')
    return manifest,f,e,x,b


def chart_candles(bars:pd.DataFrame,cutoff:int,days:int,timeframe:str,max_bars:int=1800):
    """Never invent wicks while limiting plot complexity. UTC aligned full OHLC groups."""
    if timeframe not in ('30m','1h','4h'):
        raise ValueError('Timeframe visual inválido')
    width={'30m':1,'1h':2,'4h':8}[timeframe]
    start=cutoff-int(days)*86_400_000
    avail=bars.loc[(bars.timestamp+TF<=cutoff)&(bars.timestamp+TF>start)].copy()
    if len(avail)<2:raise ValueError('Sin velas completas dentro de esta ventana')
    if timeframe=='30m' and len(avail)>max_bars:
        # Auto-resolution: choose a genuine OHLC aggregation, not point-skip.
        width=2 if len(avail)/2<=max_bars else 8
        timeframe='1h (auto)' if width==2 else '4h (auto)'
    if width>1:
        ts=avail.timestamp.astype('int64')
        keys=(ts//(width*TF))*(width*TF)
        v=avail.assign(bucket=keys)
        groups=v.groupby('bucket',sort=True)
        o=groups.agg(timestamp=('timestamp','min'),open=('open','first'),high=('high','max'),
                     low=('low','min'),close=('close','last'),n=('timestamp','size'))
        # Show only FULL and aligned aggregates; leave boundary parts unplotted.
        o=o.loc[(o.n==width)&(o.timestamp.astype('int64')==o.index.astype('int64'))]
        avail=o[['timestamp','open','high','low','close']].reset_index(drop=True)
    if avail.empty:raise ValueError('No hay grupos OHLC completos para este rango')
    if len(avail)>max_bars:raise ValueError('Demasiadas velas para el máximo visual')
    return avail,timeframe


def visible_levels(scores:pd.DataFrame,min_tier:str='AB',include_expired:bool=True,
                   suppress_overlap:bool=True,limit:int=25):
    if min_tier not in ('A','AB','ABC','ALL'):
        raise ValueError('Filtro de importancia inválido')
    wanted={'A':{'A'},'AB':{'A','B'},'ABC':{'A','B','C'},'ALL':{'A','B','C','D'}}[min_tier]
    chosen=scores.loc[scores.tier.isin(wanted)].copy()
    if not include_expired:
        chosen=chosen.loc[chosen.state_at_cutoff.eq('ACTIVE')]
    chosen=chosen.sort_values(['importance_score','distance_atr'],ascending=[False,True])
    if suppress_overlap:
        selected=[]
        for r in chosen.itertuples(index=False):
            overlap=False
            for old in selected:
                width=min(float(r.zone_high)-float(r.zone_low),
                          float(old.zone_high)-float(old.zone_low))
                inter=max(0,min(float(r.zone_high),float(old.zone_high))-
                            max(float(r.zone_low),float(old.zone_low)))
                if width>0 and inter/width>=.6:
                    overlap=True;break
            if not overlap:
                selected.append(r)
            if len(selected)>=limit:break
        return pd.DataFrame([v._asdict() for v in selected],columns=scores.columns)
    return chosen.head(limit).copy()


def make_chart(bars:pd.DataFrame,scores:pd.DataFrame,cutoff:int,days:int,timeframe:str,
               *,selected_id:str|None=None,show_labels=True):
    plot_bars,rendered_tf=chart_candles(bars,cutoff,days,timeframe)
    first=int(plot_bars.timestamp.iloc[0]);last=int(plot_bars.timestamp.iloc[-1])+TF
    fig=go.Figure()
    ts=pd.to_datetime(plot_bars.timestamp,unit='ms',utc=True)
    fig.add_trace(go.Candlestick(x=ts,open=plot_bars.open,high=plot_bars.high,
        low=plot_bars.low,close=plot_bars.close,name='Velas '+rendered_tf,
        increasing_line_color='#16a487',decreasing_line_color='#d36770',
        increasing_fillcolor='#16a487',decreasing_fillcolor='#d36770'))
    for lv in scores.itertuples(index=False):
        start=max(first,int(lv.formation_closed_ts))
        end=min(cutoff,int(lv.last_observed_closed_ts))
        if start>=end:continue
        color=COLORS.get(lv.tier,'#8995a7')
        focused=selected_id is not None and lv.level_id==selected_id
        width=3.3 if focused else {'A':2.5,'B':1.8,'C':1.2,'D':.9}[lv.tier]
        x0=pd.Timestamp(start,unit='ms',tz='UTC')
        x1=pd.Timestamp(end,unit='ms',tz='UTC')
        fig.add_shape(type='rect',xref='x',yref='y',x0=x0,x1=x1,
                      y0=float(lv.zone_low),y1=float(lv.zone_high),
                      line=dict(width=0),fillcolor=color,opacity=.09 if not focused else .19)
        fig.add_trace(go.Scatter(x=[x0,x1],y=[lv.center,lv.center],mode='lines',
            line=dict(color=color,width=width,dash='solid' if focused else 'dash'),
            name=f'{lv.tier} · {lv.origin} · {lv.importance_score}',
            text=[f'{lv.level_id}<br>Confirmada: {pd.Timestamp(lv.formation_closed_ts,unit="ms",tz="UTC")}'
                  f'<br>Tests {lv.tests_pre} · Defensas {lv.defenses_pre} · Pérdidas {lv.losses_pre}'
                  f'<br>Importancia al corte: {lv.importance_score}/100']*2,
            hovertemplate='%{text}<br>Precio %{y}<extra></extra>',showlegend=focused))
        if focused and show_labels:
            fig.add_annotation(x=x1,y=lv.center,text=f'{lv.tier} {lv.importance_score} · {lv.origin}',
                               showarrow=False,xanchor='right',yshift=10,font=dict(color=color,size=11))
    fig.update_layout(height=690,dragmode='pan',hovermode='x unified',
                      xaxis_rangeslider_visible=True,
                      xaxis_title='UTC · ventana histórica con velas cerradas',yaxis_title='Precio',
                      margin=dict(l=8,r=12,t=22,b=28),showlegend=selected_id is not None,
                      uirevision='hll-hierarchy-v1')
    return fig,rendered_tf


def render_hll_hierarchical_levels(root:Path):
    import streamlit as st
    root=Path(root)
    st.markdown('#### Jerarquía de líneas horizontales · HLL V1 (experimental)')
    st.caption('Detector HLL Lifecycle 2×2 original; importancia estructural calculada offline con '
               'contactos, defensas, reconfirmaciones, pérdidas y antigüedad. No es una señal de trading.')
    base=root/RUNS
    runs=sorted((p for p in base.iterdir() if p.is_dir() and (p/'manifest.json').is_file()),
                reverse=True) if base.is_dir() else []
    if not runs:
        st.info('Todavía no hay una corrida jerárquica congelada. Primero ejecutá el piloto BTC 30m del módulo hll_hierarchical_levels_v1.py.')
        st.code('python3 hll_hierarchical_levels_v1.py --plan --symbol BTCUSDT')
        return
    folder=st.selectbox('Corrida de líneas HLL congeladas',runs,format_func=lambda x:x.name,
                        key='hllhier_run')
    try:
        @st.cache_data(show_spinner=False,max_entries=3)
        def cached(path,mtime):
            return read_frozen(Path(path))
        files=('manifest.json','formations.csv','events.csv','ends.csv','candles_30m.csv','snapshot_asof.csv')
        signature=tuple((n,(folder/n).stat().st_mtime_ns,(folder/n).stat().st_size) for n in files)
        m,forms,events,ends,bars=cached(str(folder), signature)
    except (OSError,ValueError,KeyError,TypeError) as exc:
        st.error(f'Fuente HLL congelada no verificable: {exc}')
        return
    cutoff_max=int(bars.timestamp.iloc[-1])+TF
    # All controls use only the already-closed prefix ending at cutoff.
    a,b,c=st.columns([2,1,1])
    with a:
        step=st.slider('Corte histórico (última vela 30m cerrada)',
                       min_value=259,max_value=len(bars)-1,value=len(bars)-1,key='hllhier_asof_idx')
        cutoff=int(bars.timestamp.iloc[step])+TF
        st.caption(f'Corte causal: {pd.Timestamp(cutoff,unit="ms",tz="UTC"):%Y-%m-%d %H:%M} UTC')
    with b:
        available=[d for d in (3,7,14,21,30,60) if d*48<=step+1]
        if not available:available=[3]
        default=30 if 30 in available else available[-1]
        days=st.selectbox('Días visibles',available,index=available.index(default),key='hllhier_days')
    with c:
        timeframe=st.selectbox('Velas visuales',['30m','1h','4h'],key='hllhier_tf')
    st.caption(f'Fuente congelada: {m["source_first_utc"]} → {m["source_last_closed_utc"]} · '
               f'{m["source_provenance"]} · {m["symbol"]}. '
               'Si la ventana solicitada no está cubierta, no se inventan velas.')
    try:
        scores=score_asof(forms,events,ends,bars,cutoff)
    except (ValueError,KeyError,TypeError) as exc:
        st.error(f'Corte causal inválido: {exc}');return
    d1,d2,d3=st.columns([1.4,1,1])
    with d1:
        tier=st.selectbox('Importancia mínima',['A','AB','ABC','ALL'],index=1,
                          format_func=lambda x:{'A':'Solo A','AB':'A + B','ABC':'A + B + C','ALL':'Todos A/B/C/D'}[x],
                          key='hllhier_tiers')
    with d2:
        include_expired=st.checkbox('Niveles históricos vencidos',value=True,key='hllhier_expired')
    with d3:
        suppress_overlap=st.checkbox('Agrupar bandas superpuestas',value=True,key='hllhier_collapse')
    c1,c2,c3=st.columns(3)
    c1.metric('Líneas confirmadas al corte',len(scores))
    c2.metric('Líneas activas',int(scores.state_at_cutoff.eq('ACTIVE').sum()) if not scores.empty else 0)
    c3.metric('Categorías A/B',int(scores.tier.isin(['A','B']).sum()) if not scores.empty else 0)
    limit=st.slider('Máximo de líneas dibujadas',5,50,20,5,key='hllhier_max_draw')
    selected=visible_levels(scores,tier,include_expired,suppress_overlap,limit)
    options=['(Todas las visibles)']+selected.level_id.astype(str).tolist()
    pick=st.selectbox('Línea individual para inspeccionar',options,key='hllhier_selected',
        format_func=lambda x:x if x==options[0] else (lambda r:f'{r.tier} {r.importance_score} · {r.origin} · {r.center:.6g} · {x[:10]}')(
            selected.loc[selected.level_id.eq(x)].iloc[0]))
    if selected.empty:
        st.info('No hay líneas de las categorías seleccionadas al corte.')
    else:
        shown=selected if pick==options[0] else selected.loc[selected.level_id.eq(pick)]
        try:
            fig,actual=make_chart(bars,shown,cutoff,days,timeframe,
                                  selected_id=None if pick==options[0] else pick)
            st.plotly_chart(fig,use_container_width=True,key='hllhier_chart')
            if actual!=timeframe:
                st.info(f'El gráfico se agregó a {actual} para limitar las velas dibujadas; '
                        'las líneas HLL siguen siendo detectadas sobre 30m.')
        except (ValueError,KeyError) as exc:
            st.error(f'No se pudo dibujar el histórico: {exc}')
        st.dataframe(shown[['tier','importance_score','origin','center','state_at_cutoff',
           'tests_pre','defenses_pre','losses_pre','reconfirmations_pre','age_days','distance_atr','level_id']],
           hide_index=True,use_container_width=True)
    st.download_button('Exportar jerarquía al corte (CSV)',scores.to_csv(index=False).encode('utf-8'),
                       file_name='hll_hierarchy_asof.csv',mime='text/csv',key='hllhier_export')
    st.caption('A≥75, B≥55, C≥35, D<35: pesos preliminares definidos sin resultados de trading. '
               'Las categorías reflejan evidencia acumulada al CORTE seleccionado; '
               'no deben interpretarse como grado retroactivo desde la formación. '
               'Un nivel vencido deja de proyectarse tras su última fecha válida. '
               'Jerarquía estructural y vigencia son conceptos independientes.')
