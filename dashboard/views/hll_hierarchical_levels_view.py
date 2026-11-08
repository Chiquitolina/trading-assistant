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



# V1.1 VISUAL ONLY. Never alters a frozen level, tier, causal event, or ID.
# Spatial grouping is an *inspection aid* and not a new HLL detection.
ZONE_COLORS={'HIGH':'#ed9873', 'LOW':'#59bcbb', 'MIXED':'#bd94db'}


def group_price_zones(scores:pd.DataFrame, proximity_pct:float=.35) -> list[dict]:
    """Group centers near a fixed score-ranked anchor, never transitively.

    Important: both HLL and its event identities remain unchanged. No new
    level identity or reclassification is implied. Each group's leader is the
    first (highest-score) level, not a synthetic historical formation.
    """
    if not (0.05 <= proximity_pct <= 2.0):
        raise ValueError('Proximidad visual fuera de rango')
    if scores.empty:
        return []
    required={'level_id','center','tier','importance_score','origin','zone_low','zone_high',
              'formation_closed_ts','last_observed_closed_ts','state_at_cutoff'}
    if not required.issubset(scores.columns):
        raise ValueError('Faltan datos de línea HLL para formar grupos visuales')
    src=scores.copy()
    src['center']=pd.to_numeric(src.center,errors='raise')
    if not np.isfinite(src.center.to_numpy(dtype=float)).all() or (src.center<=0).any():
        raise ValueError('Precio de línea HLL inválido')
    if src.level_id.astype(str).duplicated().any():
        raise ValueError('IDs de líneas HLL duplicados')
    src=src.sort_values(['importance_score','formation_closed_ts'],ascending=[False,True],
                        kind='stable')
    groups=[]
    for r in src.itertuples(index=False):
        center=float(r.center)
        matches=[(abs(center-z['anchor'])/z['anchor']*100,i)
                 for i,z in enumerate(groups)
                 if abs(center-z['anchor'])/z['anchor']*100<=proximity_pct]
        if matches:
            _,idx=min(matches)
            groups[idx]['members'].append(str(r.level_id))
            groups[idx]['rows'].append(r)
        else:
            groups.append({'id':str(r.level_id),'anchor':center,
                           'leader':r,'members':[str(r.level_id)],'rows':[r]})
    for z in groups:
        origins={str(r.origin) for r in z['rows']}
        z['kind']=next(iter(origins)) if len(origins)==1 else 'MIXED'
        z['count']=len(z['rows'])
        z['min_center']=min(float(r.center) for r in z['rows'])
        z['max_center']=max(float(r.center) for r in z['rows'])
        z['tier']=str(z['leader'].tier)
        z['score']=int(z['leader'].importance_score)
        z['label']=(f"{z['tier']} {z['score']} · {z['anchor']:,.2f} · "
                    f"{z['count']} línea{'s' if z['count']!=1 else ''} · {z['kind']}")
    return groups


def limit_price_zones(zones:list[dict], n:int=7, min_spacing_pct:float=.0) -> list[dict]:
    """Keep ranked areas distinct even when detector produced many near bands."""
    if n<1:raise ValueError('Límite de zonas inválido')
    kept=[]
    for z in zones:
        if all(abs(z['anchor']-a['anchor'])/a['anchor']*100>=min_spacing_pct for a in kept):
            kept.append(z)
        if len(kept)>=n:break
    return kept


def events_for_line(events:pd.DataFrame, level_id:str, first:int, cutoff:int) -> pd.DataFrame:
    """Closed-bar markers of exactly one original HLL line; no future events."""
    if events.empty:return events.copy()
    if not {'level_id','observed_closed_ts','event_type','close'}.issubset(events):
        raise ValueError('CSV de eventos HLL incompleto')
    ts=pd.to_numeric(events.observed_closed_ts,errors='raise')
    if (ts.isna()).any():raise ValueError('Timestamp HLL inválido')
    mask=(events.level_id.astype(str).eq(str(level_id)) &
          ts.ge(first) & ts.le(cutoff) &
          events.event_type.isin(('TEST_FROM_ABOVE','TEST_FROM_BELOW',
                                  'DEFENSE_CONFIRMED','LOSS_CONFIRMED')))
    return events.loc[mask].sort_values('observed_closed_ts').copy()


def make_zone_chart(bars:pd.DataFrame,zones:list[dict],cutoff:int,days:int,
                    timeframe:str,*,selected_zone_id:str|None=None,
                    selected_line_id:str|None=None,events:pd.DataFrame|None=None):
    """Clean price-map: one line and one narrow band per visible zone.

    For a selected zone, markers refer only to the selected original level;
    the visual group never fabricates aggregate contacts or a new HLL event.
    """
    plot_bars,rendered_tf=chart_candles(bars,cutoff,days,timeframe)
    first=int(plot_bars.timestamp.iloc[0])
    fig=go.Figure()
    x=pd.to_datetime(plot_bars.timestamp,unit='ms',utc=True)
    fig.add_trace(go.Candlestick(x=x,open=plot_bars.open,high=plot_bars.high,
             low=plot_bars.low,close=plot_bars.close,name='BTC · '+rendered_tf,
             increasing_line_color='#21ac92',decreasing_line_color='#d56d78',
             increasing_fillcolor='#21ac92',decreasing_fillcolor='#d56d78'))
    shown=[z for z in zones if selected_zone_id is None or z['id']==selected_zone_id]
    for z in shown:
        r=z['leader']
        # The leader is an actual causal level. Do not extend its chart segment
        # earlier than its confirmation or beyond its observed end.
        start=max(first,int(r.formation_closed_ts))
        end=min(cutoff,int(r.last_observed_closed_ts))
        if start>=end:continue
        color=ZONE_COLORS.get(z['kind'],ZONE_COLORS['MIXED'])
        focused=selected_zone_id==z['id']
        x0=pd.Timestamp(start,unit='ms',tz='UTC')
        x1=pd.Timestamp(end,unit='ms',tz='UTC')
        fig.add_shape(type='rect',xref='x',yref='y',x0=x0,x1=x1,
              y0=float(r.zone_low),y1=float(r.zone_high),
              line=dict(width=0),fillcolor=color,opacity=.17 if focused else .105)
        description=(f"Zona visual {z['kind']} · {z['tier']}/{z['score']}"
              f"<br>{z['count']} horizontales originales próximas"
              f"<br>Centros: {z['min_center']:,.2f}–{z['max_center']:,.2f}"
              f"<br>Línea principal: {str(r.level_id)[:20]}"
              f"<br>Formación principal: {pd.Timestamp(r.formation_closed_ts,unit='ms',tz='UTC')}"
              f"<br>Score experimental al corte: {z['score']}/100")
        fig.add_trace(go.Scatter(x=[x0,x1],y=[float(r.center)]*2,mode='lines',
             line=dict(color=color,width=3.2 if focused else 2.1),
             name=f"{z['tier']} {z['score']} · {z['anchor']:,.0f} · ×{z['count']}",
             text=[description]*2,hovertemplate='%{text}<extra></extra>',
             showlegend=True))
        if focused:
            fig.add_annotation(x=x1,y=float(r.center),
                 text=f"{z['tier']} · {z['score']}/100 · {z['count']} líneas",
                 showarrow=False,xanchor='right',yshift=13,
                 font=dict(color=color,size=12),bgcolor='rgba(15,19,26,.85)')
    if selected_line_id and events is not None:
        marks=events_for_line(events,selected_line_id,first,cutoff)
        for typ,label,marker,color in (
            ('TEST_FROM_ABOVE','Contacto desde arriba','circle','#e2b15b'),
            ('TEST_FROM_BELOW','Contacto desde abajo','circle-open','#e2b15b'),
            ('DEFENSE_CONFIRMED','Defensa confirmada','triangle-up','#26b898'),
            ('LOSS_CONFIRMED','Pérdida confirmada','x','#f07c82')):
            sub=marks.loc[marks.event_type.eq(typ)]
            if sub.empty:continue
            fig.add_trace(go.Scatter(
                x=pd.to_datetime(sub.observed_closed_ts,unit='ms',utc=True),
                y=pd.to_numeric(sub.close,errors='raise'),mode='markers',
                marker=dict(symbol=marker,size=10,color=color,line=dict(width=1,color='#161b23')),
                name=f'{label} ({len(sub)})',
                text=sub.event_type.astype(str),
                hovertemplate='%{text}<br>%{x}<br>Precio de confirmación: %{y}<extra></extra>'))
    fig.update_layout(height=635,dragmode='pan',hovermode='closest',
         xaxis_rangeslider_visible=False,  # Do not duplicate dozens of lines underneath.
         xaxis_title='UTC · velas cerradas',yaxis_title='Precio',
         legend=dict(orientation='h',y=1.04,x=0,font=dict(size=10)),
         margin=dict(l=5,r=10,t=72,b=15),
         uirevision=f'hll-visual-v11-{selected_zone_id}-{selected_line_id}')
    return fig,rendered_tf


def render_hll_hierarchical_levels(root:Path):
    import streamlit as st
    root=Path(root)
    st.markdown('#### Jerarquía de líneas horizontales · HLL V1.1 (vista clara)')
    st.caption('Motor HLL Lifecycle V2.1 sin cambios. Las agrupaciones de precios son SOLO visuales: '
               'se preservan los level_id, puntuaciones, bandas y eventos históricos originales.')
    base=root/RUNS
    runs=sorted((p for p in base.iterdir() if p.is_dir() and (p/'manifest.json').is_file()),
                reverse=True) if base.is_dir() else []
    if not runs:
        st.info('Todavía no hay corridas jerárquicas congeladas.')
        st.code('python3 hll_hierarchical_levels_v1.py --plan --symbol BTCUSDT')
        return
    folder=st.selectbox('Corrida HLL congelada',runs,format_func=lambda x:x.name,
                        key='hllhier_run')
    try:
        @st.cache_data(show_spinner=False,max_entries=3)
        def cached(path,signature):
            return read_frozen(Path(path))
        files=('manifest.json','formations.csv','events.csv','ends.csv','candles_30m.csv','snapshot_asof.csv')
        signature=tuple((n,(folder/n).stat().st_mtime_ns,(folder/n).stat().st_size) for n in files)
        manifest,forms,events,ends,bars=cached(str(folder),signature)
    except (OSError,ValueError,KeyError,TypeError) as exc:
        st.error(f'Fuente HLL congelada no verificable: {exc}')
        return
    a,b,c=st.columns([2,1,1])
    with a:
        step=st.slider('Corte histórico · última vela cerrada 30m',
                       min_value=259,max_value=len(bars)-1,value=len(bars)-1,
                       key='hllhier_asof_idx')
        cutoff=int(bars.timestamp.iloc[step])+TF
        st.caption(f'Corte causal: {pd.Timestamp(cutoff,unit="ms",tz="UTC"):%Y-%m-%d %H:%M} UTC')
    with b:
        available=[d for d in (3,7,14,21,30,60) if d*48<=step+1]
        if not available:available=[3]
        default=21 if 21 in available else available[-1]
        days=st.selectbox('Días visibles',available,index=available.index(default),key='hllhier_days')
    with c:
        timeframe=st.selectbox('Velas visuales',['30m','1h','4h'],key='hllhier_tf')
    st.caption(f'Fuente congelada: {manifest["source_first_utc"]} → '
               f'{manifest["source_last_closed_utc"]} · '
               f'{manifest["source_provenance"]} · {manifest["symbol"]}.')
    try:
        scores=score_asof(forms,events,ends,bars,cutoff)
    except (ValueError,KeyError,TypeError) as exc:
        st.error(f'Corte causal inválido: {exc}');return
    k1,k2,k3=st.columns(3)
    k1.metric('Líneas HLL originales',len(scores))
    k2.metric('Líneas activas',int(scores.state_at_cutoff.eq('ACTIVE').sum()) if not scores.empty else 0)
    k3.metric('Líneas A/B',int(scores.tier.isin(['A','B']).sum()) if not scores.empty else 0)
    view=st.radio('Cómo ver los niveles',
        ['Mapa claro · zonas principales','Auditoría · líneas individuales'],
        horizontal=True,index=0,key='hllhier_visual_mode')
    f1,f2,f3=st.columns([1.3,1,1])
    with f1:
        tier=st.selectbox('Importancia mínima',['A','AB','ABC','ALL'],index=0,
                          format_func=lambda x:{'A':'Solo A','AB':'A + B',
                             'ABC':'A + B + C','ALL':'Todos A/B/C/D'}[x],key='hllhier_tiers')
    with f2:
        include_expired=st.checkbox('Incluir niveles vencidos',value=True,key='hllhier_expired')
    with f3:
        nmax=st.slider('Máximo de zonas / líneas',3,20,7,1,key='hllhier_max_draw_v11')
    wanted={'A':{'A'},'AB':{'A','B'},'ABC':{'A','B','C'},'ALL':{'A','B','C','D'}}[tier]
    filtered=scores.loc[scores.tier.isin(wanted)].copy()
    if not include_expired:
        filtered=filtered.loc[filtered.state_at_cutoff.eq('ACTIVE')]
    if filtered.empty:
        st.info('No hay niveles de importancia seleccionada en este corte.')
    elif view.startswith('Mapa claro'):
        c0,c1=st.columns([1,2])
        with c0:
            proximity=st.select_slider('Agrupar centros cercanos (%)',
                 options=[.10,.15,.20,.25,.30,.35,.40,.50,.60,.75,.90],value=.35,
                 key='hllhier_proximity')
        with c1:
            st.caption('Esta distancia agrupa visualmente líneas próximas por precio. '
                       'No fusiona niveles, no altera A/B/C/D y no supone confluencia estadística.')
        zones=group_price_zones(filtered,proximity)
        visible=limit_price_zones(zones,nmax)
        st.caption(f'{len(filtered)} líneas filtradas → {len(zones)} zonas de precio '
                   f'→ {len(visible)} zonas visibles. '
                   'Color: naranja HIGH (origen máximo), turquesa LOW (origen mínimo), '
                   'violeta MIXED. No indica automáticamente soporte/resistencia actual.')
        picks=['(Todas las zonas principales)']+[z['id'] for z in visible]
        labels={z['id']:z['label'] for z in visible}
        pick=st.selectbox('Inspeccionar zona',picks,
            format_func=lambda x:labels.get(x,x),key='hllhier_zone_pick')
        chosen=None if pick==picks[0] else next(z for z in visible if z['id']==pick)
        selected_line=None
        if chosen is not None:
            # Restrict this selector to genuine original levels from the chosen price group.
            members=filtered.loc[filtered.level_id.astype(str).isin(chosen['members'])].copy()
            members=members.sort_values('importance_score',ascending=False)
            options=members.level_id.astype(str).tolist()
            selected_line=st.selectbox('Línea HLL original (para mostrar sus marcas)',options,
                format_func=lambda x:(lambda r:
                  f'{r.tier} {r.importance_score} · {r.origin} · {r.center:,.2f} · {x[:12]}')(
                  members.loc[members.level_id.astype(str).eq(x)].iloc[0]),
                key='hllhier_line_pick_v11')
            st.caption('Los marcadores pertenecen SOLO a la línea individual elegida, '
                       'no son contactos artificiales de la zona agrupada.')
        try:
            fig,actual=make_zone_chart(bars,visible,cutoff,days,timeframe,
                selected_zone_id=chosen['id'] if chosen else None,
                selected_line_id=selected_line,events=events)
            st.plotly_chart(fig,use_container_width=True,key='hllhier_chart_v11')
            if actual!=timeframe:
                st.info(f'Resolución agregada visualmente a {actual}, preservando OHLC original.')
        except (ValueError,KeyError,TypeError) as exc:
            st.error(f'No se pudo dibujar el mapa de zonas: {exc}')
        table=pd.DataFrame([dict(grade=z['tier'],score=z['score'],
                precio_central=z['anchor'],tipo_origen=z['kind'],
                lineas_en_zona=z['count'],
                primer_centro=z['min_center'],ultimo_centro=z['max_center'],
                linea_lider=z['id']) for z in zones])
        st.markdown('**Mapa de zonas al corte**')
        st.dataframe(table,hide_index=True,use_container_width=True)
        if chosen is not None:
            st.markdown('**Horizontales originales dentro de la zona seleccionada**')
            st.dataframe(members[['tier','importance_score','origin','center',
                'state_at_cutoff','tests_pre','defenses_pre','losses_pre',
                'reconfirmations_pre','age_days','level_id']],
                hide_index=True,use_container_width=True)
    else:
        st.caption('Modo de auditoría: cada trazo corresponde exactamente a un level_id '
                   'original. Puede verse saturado; es intencional.')
        suppress_overlap=st.checkbox('Ocultar bandas físicamente superpuestas',
                                     value=True,key='hllhier_collapse')
        individual=visible_levels(filtered,'ALL',True,suppress_overlap,nmax)
        options=['(Todas las visibles)']+individual.level_id.astype(str).tolist()
        pick=st.selectbox('Línea individual para inspeccionar',options,key='hllhier_selected')
        shown=individual if pick==options[0] else individual.loc[individual.level_id.eq(pick)]
        try:
            fig,actual=make_chart(bars,shown,cutoff,days,timeframe,
                          selected_id=None if pick==options[0] else pick)
            # Important change also for the old auditor: no small duplicate chart below.
            fig.update_layout(xaxis_rangeslider_visible=False,height=635)
            st.plotly_chart(fig,use_container_width=True,key='hllhier_audit_chart_v11')
        except (ValueError,KeyError) as exc:
            st.error(f'No se pudo dibujar el gráfico individual: {exc}')
        st.dataframe(shown[['tier','importance_score','origin','center',
           'state_at_cutoff','tests_pre','defenses_pre','losses_pre',
           'reconfirmations_pre','age_days','level_id']],
           hide_index=True,use_container_width=True)
    st.download_button('Exportar todos los niveles al corte (CSV)',
          scores.to_csv(index=False).encode('utf-8'),file_name='hll_hierarchy_asof.csv',
          mime='text/csv',key='hllhier_export')
    st.caption('A≥75, B≥55, C≥35, D<35: puntuaciones exploratorias, no calibradas. '
               'La agrupación es exclusivamente visual. La clase asignada al CORTE no '
               'debe proyectarse retrospectivamente a la fecha de formación. '
               'No se usan TP/SL, retornos ni resultados futuros.')
