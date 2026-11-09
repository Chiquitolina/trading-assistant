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
from hll_structural_zones_v2 import RUNS as STRUCTURAL_RUNS, load_v2, snapshot_zones, zone_score_history
from hll_structural_zone_ranking_v21 import rank_zone_evidence, select_historical_map
from hll_structural_zone_recency_v221 import annotate_recency, select_recent_overlay
from hll_structural_behavior_inspector_v22 import (available_runs as behavior_runs,
    verified_load as load_behavior, episode_window, episode_marks)
from hll_btc_visual_reader_v1 import RELATIVE_RUN as BTC_1M_RUN, load_verified as load_btc_1m, plot_window as btc_plot_window
from hll_chart_vertical_v12 import price_axis_window, zone_in_price_window
from hll_historical_projection_v1 import projection_segments
from hll_reaction_overlay_v1 import (RELATIVE_RUN as REACTION_OVERLAY_RUN,
    KINDS as REACTION_KINDS, load_frozen as load_reaction_overlay,
    select_events as select_reaction_overlay, add_reaction_traces)
from hll_reaction_confluence_inspector_v1 import (
    load_candidates as load_confluence_candidates, event_context as confluence_context,
    focus_interval as confluence_interval, interval_candles as confluence_candles)

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


def chart_candles(bars:pd.DataFrame,cutoff:int,days:int,timeframe:str,max_bars:int=3200):
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
               *,selected_id:str|None=None,show_labels=True,
               project_expired:bool=False):
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
        _,projected=projection_segments(birth_ms=int(lv.formation_closed_ts),
            end_ms=int(lv.last_observed_closed_ts),first_ms=first,
            cutoff_ms=cutoff,enabled=project_expired)
        if start>=end and projected is None:continue
        color=COLORS.get(lv.tier,'#8995a7')
        focused=selected_id is not None and lv.level_id==selected_id
        width=3.3 if focused else {'A':2.5,'B':1.8,'C':1.2,'D':.9}[lv.tier]
        x0=pd.Timestamp(start,unit='ms',tz='UTC')
        x1=pd.Timestamp(end,unit='ms',tz='UTC')
        if start<end:
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
        if project_expired:
            _,extension=projection_segments(birth_ms=int(lv.formation_closed_ts),
                end_ms=int(lv.last_observed_closed_ts),first_ms=first,
                cutoff_ms=cutoff,enabled=True)
            if extension is not None:
                px0,px1=(pd.Timestamp(t,unit='ms',tz='UTC') for t in extension)
                fig.add_trace(go.Scatter(x=[px0,px1],y=[lv.center]*2,mode='lines',
                    line=dict(color=color,width=1.8,dash='dot'),opacity=.52,
                    name='Proyección histórica · '+str(lv.level_id)[:12],
                    text=['PROYECCIÓN VISUAL después del fin Lifecycle. No activa ni defensas.']*2,
                    hovertemplate='%{text}<br>%{x}<br>Centro %{y}<extra></extra>',
                    showlegend=focused))
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
# Recency is a separate visual dimension, NOT a change in importance or direction.
RECENCY_COLORS={'INTERACCION_3D':'#b4eb67',
                'INTERACCION_RECIENTE':'#60adff',
                'ZONA_NUEVA_SIN_INTERACCION_RECIENTE':'#e7b0ff'}


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
                    timeframe:str,*,visual_bars:pd.DataFrame|None=None,visual_manifest:dict|None=None,selected_zone_id:str|None=None,
                    selected_line_id:str|None=None,events:pd.DataFrame|None=None,
                    behavior_transitions:pd.DataFrame|None=None,
                    focus_window:tuple[int,int]|None=None,
                    reaction_events:pd.DataFrame|None=None,
                    reaction_selected:str|None=None,
                    vertical_mode:str="all",inspection_candles:pd.DataFrame|None=None,
                    inspection_candidates:pd.DataFrame|None=None,
                    inspection_focus:tuple[int,int]|None=None,
                    project_expired:bool=False):
    """Clean price-map: one line and one narrow band per visible zone.

    For a selected zone, markers refer only to the selected original level;
    the visual group never fabricates aggregate contacts or a new HLL event.
    """
    small=timeframe in ('1m','3m','5m','15m')
    if inspection_candles is not None:
        plot_bars=inspection_candles
        rendered_tf=timeframe
    elif small:
        if visual_bars is None or visual_manifest is None:
            raise ValueError('No se cargó el histórico BTC 1m verificado')
        # A selected behavior episode can still use the regular visible day window;
        # zoom only changes x-axis, never authorizes a candle after the HLL cut.
        plot_bars=btc_plot_window(visual_bars,visual_manifest,cutoff,days,timeframe)
        rendered_tf=timeframe
    elif focus_window is None:
        plot_bars,rendered_tf=chart_candles(bars,cutoff,days,timeframe)
    else:
        focus_start,focus_end=focus_window
        zoom_days=max(1,int(np.ceil((focus_end-focus_start)/86_400_000))+1)
        plot_bars,rendered_tf=chart_candles(bars,focus_end,zoom_days,timeframe)
    if vertical_mode not in ('price','all'):
        raise ValueError('Modo de escala vertical inválido')
    price_window=price_axis_window(plot_bars) if vertical_mode=='price' else None
    first=int(plot_bars.timestamp.iloc[0])
    fig=go.Figure()
    x=pd.to_datetime(plot_bars.timestamp,unit='ms',utc=True)
    fig.add_trace(go.Candlestick(x=x,open=plot_bars.open,high=plot_bars.high,
             low=plot_bars.low,close=plot_bars.close,name='BTC · '+rendered_tf,
             increasing_line_color='#21ac92',decreasing_line_color='#d56d78',
             increasing_fillcolor='#21ac92',decreasing_fillcolor='#d56d78'))
    shown=[z for z in zones if selected_zone_id is None or z['id']==selected_zone_id]
    for z in shown:
        if price_window is not None and not zone_in_price_window(z,price_window):
            continue
        r=z['leader']
        # The leader is an actual causal level. Do not extend its chart segment
        # earlier than its confirmation or beyond its observed end.
        start=max(first,int(z.get('created_at',r.formation_closed_ts)))
        end=min(cutoff,int(z.get('end_at',r.last_observed_closed_ts)))
        _,projected=projection_segments(birth_ms=int(z.get('created_at',r.formation_closed_ts)),
            end_ms=int(z.get('end_at',r.last_observed_closed_ts)),first_ms=first,
            cutoff_ms=cutoff,enabled=project_expired and inspection_candles is None)
        if start>=end and projected is None:continue
        fresh=(z.get('recent_overlay_enabled',False) and
               z.get('recent_status','SOLO_HISTORICA') in RECENCY_COLORS)
        color=(RECENCY_COLORS[z['recent_status']] if fresh else
               ZONE_COLORS.get(z['kind'],ZONE_COLORS['MIXED']))
        focused=selected_zone_id==z['id']
        x0=pd.Timestamp(start,unit='ms',tz='UTC')
        x1=pd.Timestamp(end,unit='ms',tz='UTC')
        if start<end:
            fig.add_shape(type='rect',xref='x',yref='y',x0=x0,x1=x1,
                  y0=float(z.get('low',r.zone_low)),y1=float(z.get('high',r.zone_high)),
                  line=dict(width=0),fillcolor=color,opacity=.17 if focused else .105)
        importance_label=str(z.get('score_label',f"{z['score']}/100"))
        zone_label='Subzona causal' if z.get('structural') else 'Zona visual'
        description=(f"{zone_label} {z['kind']} · {z['tier']}"
              f"<br>{z['count']} horizontales originales"
              f"<br>Centros: {z['min_center']:,.2f}–{z['max_center']:,.2f}"
              f"<br>Línea representativa: {str(r.level_id)[:20]}"
              f"<br>Inicio de trazo: {pd.Timestamp(int(z.get('created_at',r.formation_closed_ts)),unit='ms',tz='UTC')}"
              f"<br>Importancia al corte: {importance_label}"
              f"<br>Actividad: {z.get('recent_status','sin resaltado')}"
              f"<br>Vigencia TTL: {z.get('lifecycle','sin dato')}"
              f"<br>No indica soporte, flip ni compra/venta")
        if start<end:
            fig.add_trace(go.Scatter(x=[x0,x1],y=[float(r.center)]*2,mode='lines',
                 line=dict(color=color,width=3.2 if focused else 2.6 if fresh else 2.1),
             name=(('⏱ ' if fresh else '')+f"{z['tier']} · {importance_label} · "
                   f"{z['anchor']:,.0f} · ×{z['count']}"),
             text=[description]*2,hovertemplate='%{text}<extra></extra>',
             showlegend=True))
        if projected is not None:
            xp0,xp1=(pd.Timestamp(t,unit='ms',tz='UTC') for t in projected)
            fig.add_trace(go.Scatter(x=[xp0,xp1],y=[float(r.center)]*2,mode='lines',
                line=dict(color=color,width=2.1 if focused else 1.5,dash='dot'),
                opacity=.6,showlegend=focused,
                name='Proyección histórica · '+str(z['id'])[:12],
                text=[description+'<br>PROYECCIÓN VISUAL post-vencimiento; NO ACTIVE_TTL, '
                      'no confirma defensas ni REACTION PRE.']*2,
                hovertemplate='%{text}<extra></extra>'))
        if fresh:
            marker_ts=z.get('last_interaction_ts')
            status=z.get('recent_status')
            if marker_ts is None and status=='ZONA_NUEVA_SIN_INTERACCION_RECIENTE':
                marker_ts=z['created_at']
            if marker_ts is not None and first<=int(marker_ts)<=cutoff and int(marker_ts)>=start:
                fig.add_trace(go.Scatter(
                    x=[pd.Timestamp(int(marker_ts),unit='ms',tz='UTC')],
                    y=[float(r.center)],mode='markers',showlegend=False,
                    marker=dict(symbol='diamond',size=10,color=color,
                                line=dict(color='#172027',width=1)),
                    hovertemplate=(f"Actividad confirmada: {status}"
                                   "<br>%{x}<br>Marcador sobre centro de zona, no precio exacto del evento"
                                   "<extra></extra>")))
        if focused:
            fig.add_annotation(x=x1,y=float(r.center),
                 text=f"{z['tier']} · {importance_label} · {z['count']} líneas",
                 showarrow=False,xanchor='right',yshift=13,
                 font=dict(color=color,size=12),bgcolor='rgba(15,19,26,.85)')
    if inspection_candidates is not None:
        # Draw only the frozen PRE HLL candidates of this event. Never use
        # today’s ranking or retrospectively formed subzone membership.
        for r in inspection_candidates.itertuples(index=False):
            start=max(first,int(r.formation_closed_ts))
            end=int(inspection_focus[1]) if inspection_focus else cutoff
            if end<=start:continue
            low,high=float(r.zone_low),float(r.zone_high)
            if price_window is not None and (high<price_window[0] or low>price_window[1]):
                continue
            x0=pd.Timestamp(start,unit='ms',tz='UTC')
            x1=pd.Timestamp(end,unit='ms',tz='UTC')
            color='#f3ca70' if float(r.gap_atr)<=.5 else '#8d9aab'
            fig.add_shape(type='rect',xref='x',yref='y',x0=x0,x1=x1,
                y0=low,y1=high,fillcolor=color,opacity=.14,
                line=dict(width=0))
            text=(f"HLL PRE · {r.level_id}<br>Distancia: {float(r.gap_atr):.3f} ATR"
                  f"<br>Defensas: {int(r.prior_defenses)} · Pérdidas: {int(r.prior_losses)}"
                  f"<br>RETURN_INSIDE: {int(r.prior_returns)} (no confirma recuperación)"
                  "<br>Valores congelados al First Touch, no al corte del gráfico")
            fig.add_trace(go.Scatter(x=[x0,x1],y=[float(r.center)]*2,
                mode='lines',line=dict(color=color,width=2.4 if float(r.gap_atr)<=.5 else 1.2,
                    dash='solid' if float(r.gap_atr)<=.5 else 'dot'),
                name=f'HLL PRE · {str(r.level_id)[:9]} · {float(r.gap_atr):.2f} ATR',
                text=[text]*2,hovertemplate='%{text}<extra></extra>'))
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
    if behavior_transitions is not None and not behavior_transitions.empty:
        # These markers belong to the selected frozen episode; location uses the
        # contemporaneous OHLC CLOSE, not a fabricated event fill or trade price.
        colors={'TEST':'#e2b15b','RESOLUTION':'#8cb9fa'}
        symbols={'TEST':'circle','RESOLUTION':'diamond'}
        close_ts=(bars.timestamp.astype('int64')+TF).to_numpy()
        close_val=bars.close.to_numpy(dtype=float)
        for role,part in behavior_transitions.groupby('transition_role'):
            px=[];ts=[];desc=[]
            for r in part.itertuples(index=False):
                stamp=int(r.observed_closed_ts)
                j=int(np.searchsorted(close_ts,stamp,'left'))
                if j>=len(close_ts) or close_ts[j]!=stamp:continue
                ts.append(pd.Timestamp(stamp,unit='ms',tz='UTC'))
                px.append(float(close_val[j]))
                desc.append(f'{r.event_type} · línea {str(r.level_id)[:12]} · test {str(r.test_id)[:12]}')
            if ts:
                fig.add_trace(go.Scatter(x=ts,y=px,mode='markers',
                    name=f'V2.2-A {role} · {len(ts)}',text=desc,
                    marker=dict(symbol=symbols.get(role,'square'),size=11,
                                color=colors.get(role,'#b99df3'),line=dict(width=1,color='#121820')),
                    hovertemplate='%{text}<br>Cierre observado %{x}<br>Close 30m: %{y}<extra></extra>'))
    if reaction_events is not None and not reaction_events.empty:
        add_reaction_traces(fig,reaction_events,selected=reaction_selected)
    if inspection_focus is not None:
        fig.update_xaxes(range=[pd.Timestamp(inspection_focus[0],unit="ms",tz="UTC"),
                                pd.Timestamp(inspection_focus[1],unit="ms",tz="UTC")])
    elif focus_window is not None:
        fig.update_xaxes(range=[pd.Timestamp(focus_window[0],unit='ms',tz='UTC'),
                                pd.Timestamp(focus_window[1],unit='ms',tz='UTC')])
    fig.update_layout(height=(820 if small else 690),dragmode='pan',hovermode='closest',
         xaxis_rangeslider_visible=False,  # Do not duplicate dozens of lines underneath.
         xaxis_title='UTC · velas cerradas',yaxis_title='Precio',
         legend=dict(orientation='h',y=1.04,x=0,font=dict(size=10)),
         margin=dict(l=5,r=10,t=72,b=15),
         uirevision=f'hll-visual-v13-{cutoff}-{days}-{timeframe}-{vertical_mode}-{selected_zone_id}-{selected_line_id}-{inspection_focus}')
    if price_window is not None:
        fig.update_yaxes(range=list(price_window),autorange=False, fixedrange=False)
    return fig,rendered_tf


def render_hll_hierarchical_levels(root:Path):
    import streamlit as st
    root=Path(root)
    st.markdown('#### Jerarquía de líneas horizontales · HLL V2 (zonas causales)')
    st.caption('HLL Lifecycle V2.1 sin cambios. Mapa de macrozonas visuales V1.1 + subzonas causales V2. '
               'Los niveles originales, sus IDs y eventos no se modifican. No es señal de trading.')
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
        available=[d for d in (1,2,3,5,7,14,21,30,45,60) if d*48<=step+1]
        if not available:available=[1]
        default=21 if 21 in available else available[-1]
        days=st.selectbox('Días visibles',available,index=available.index(default),key='hllhier_days')
    with c:
        timeframe=st.selectbox('Velas visuales',['1m','3m','5m','15m','30m','1h','4h'],index=4,key='hllhier_tf')
    st.caption(f'Fuente congelada: {manifest["source_first_utc"]} → '
               f'{manifest["source_last_closed_utc"]} · '
               f'{manifest["source_provenance"]} · {manifest["symbol"]}.')
    try:
        scores=score_asof(forms,events,ends,bars,cutoff)
    except (ValueError,KeyError,TypeError) as exc:
        st.error(f'Corte causal inválido: {exc}');return
    k1,k2,k3=st.columns(3)
    k1.metric('Líneas HLL originales',len(scores))
    k2.metric('Sin expiración TTL',int(scores.state_at_cutoff.eq('ACTIVE').sum()) if not scores.empty else 0)
    k3.metric('Líneas A/B · escala V1',int(scores.tier.isin(['A','B']).sum()) if not scores.empty else 0)
    view=st.radio('Cómo ver los niveles',
        ['Mapa claro · zonas principales','Subzonas estructurales V2 · causales','Auditoría · líneas individuales'],
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
    project_expired=(include_expired and st.checkbox(
        'Prolongar horizontales vencidas · referencia visual discontinua',
        value=True,key='hllhier_projection_v1'))
    if project_expired:
        st.caption('El trazo continuo termina en Lifecycle. Su prolongación punteada '
                   'es solo una guía histórica hasta el corte: no implica ACTIVE, '
                   'defensa, recuperación ni señal PRE.')
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
                selected_line_id=selected_line,events=events,
                project_expired=project_expired)
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
    elif view.startswith('Subzonas estructurales'):
        v2root=root/STRUCTURAL_RUNS
        possible=sorted((p for p in v2root.iterdir() if p.is_dir() and (p/'manifest.json').is_file()),
                        reverse=True) if v2root.is_dir() else []
        matches=[]
        for candidate in possible:
            try:
                cm,cl=load_v2(candidate,folder/'manifest.json')
                matches.append((candidate,cm,cl))
            except (OSError,KeyError,ValueError,json.JSONDecodeError):
                continue
        if not matches:
            st.warning('No hay un escaneo Structural Zones V2 compatible con esta corrida HLL V1. '
                       'Generá V2 offline y refrescá esta pestaña. La corrida original sigue intacta.')
            st.code('python3 hll_structural_zones_v2.py --plan '
                    f'--v1-run-dir {folder.relative_to(root)}')
            return
        run_names=[p.name for p,_,_ in matches]
        v2tag=st.selectbox('Corrida congelada de subzonas estructurales',run_names,
                           key='hllhier_v2_run')
        v2folder,v2manifest,links=matches[run_names.index(v2tag)]
        try:
            structural,births=snapshot_zones(forms,events,ends,bars,links,cutoff)
        except (OSError,ValueError,KeyError) as exc:
            st.error('No se pudo calcular subzonas al corte: '+str(exc));return
        if structural.empty:
            st.info('No hay horizontales confirmadas antes de este corte.')
            return
        scale=st.radio('Criterio de importancia de las subzonas',
            ['V2.1 · evidencia propia y ranking relativo',
             'V2 original · máxima puntuación de integrante'],
            index=0, horizontal=True, key='hllhier_v21_rank_mode')
        updated=scale.startswith('V2.1')
        if updated:
            try:
                structural=rank_zone_evidence(structural,events,cutoff,
                                               cutoff-days*86_400_000)
            except (ValueError,KeyError,TypeError) as exc:
                st.error('Error de ranking experimental V2.1: '+str(exc))
                return
            st.caption('La escala V2.1 usa episodios deduplicados de la SUBZONA, '
                       'con rendimientos decrecientes. A/B/C/D es una posición RELATIVA '
                       'entre subzonas existentes al corte, no un score validado ni una señal. '
                       'El estado TTL, las defensas/pérdidas y la posición respecto al precio '
                       'se presentan por separado. El mapa largo es una revisión retrospectiva '
                       'con lo conocido al corte, NO un ranking conocido desde septiembre.')
            grade_col='relative_tier'
            rank_mode=st.radio('Distribución en el gráfico',
                ['Equilibrar períodos históricos y franjas de precio',
                 'Ranking puro · sin separación visual'],
                horizontal=True,index=0,key='hllhier_v21_distribution')
            spacing=st.slider('Separación visual mínima entre centros (%)',
                    0.25,2.0,0.70,0.05,key='hllhier_v21_spacing') if rank_mode.startswith('Equilibrar') else 0.0
        else:
            grade_col='tier'
            st.caption('Modo de comparación con el scoring LEGADO V2. Hereda '
                       'la máxima puntuación de un integrante; no es importancia propia.')
        # Independent current/local overlay. Historical importance is NEVER
        # changed by these controls, and B/C/D recent zones can appear alongside A.
        all_ranked=structural.copy()
        # Store the causal display end once for ALL zones, including B/C/D
        # candidates selectable through the local overlay.
        byid_known={str(r.level_id):r for r in scores.itertuples(index=False)}
        all_ranked['last_zone_observed_closed_ts']=all_ranked.level_ids.map(
            lambda member_json: max(int(byid_known[k].last_observed_closed_ts)
                for k in json.loads(member_json)))
        show_recency=False
        recency_ranked=None
        if updated:
            st.markdown('**Actividad temporal / niveles locales**')
            show_recency=st.checkbox('Resaltar subzonas recientes, incluso B/C/D',
                                    value=True,key='hllhier_v221_recency_on')
            if show_recency:
                rc1,rc2,rc3=st.columns(3)
                with rc1:
                    recency_days=st.selectbox('Actividad en últimos (días)',
                             [3,7,14],index=1,key='hllhier_v221_recency_days')
                with rc2:
                    n_recent=st.slider('Máximo de subzonas locales adicionales',
                             1,8,4,key='hllhier_v221_recent_count')
                with rc3:
                    distance_limit=st.select_slider('Distancia máxima al precio (%)',
                             options=[2.,3.,5.,8.,12.],value=8.,
                             key='hllhier_v221_recent_distance')
                try:
                    recency_ranked=annotate_recency(all_ranked,events,cutoff,
                                                    recency_days)
                except (KeyError,TypeError,ValueError) as exc:
                    st.error('Actualidad causal no verificable: '+str(exc));return
                st.caption('Lima: interacción confirmada ≤3 días; azul: interacción en '
                           'la ventana elegida; lila: subzona recién formada sin '
                           'interacción posterior. Solo se destacan datos conocidos '
                           'al CORTE. Reciente no significa soporte vigente, flip ni señal.')
        structural=structural.loc[structural[grade_col].isin(wanted)].copy()
        if not include_expired:
            structural=structural.loc[structural.lifecycle.eq('ACTIVE_TTL')]
        if structural.empty:
            st.info('No hay subzonas en este filtro. Probá A+B o Todos.')
            return
        if updated:
            structural=structural.sort_values(['evidence_index','zone_id'],
                              ascending=[False,True],kind='stable')
            # An old zone can be important historically but have no segment in
            # the CURRENT visible window. Keep it inspectable, not map-ranked.
            byid_at_cut={str(r.level_id):r for r in scores.itertuples(index=False)}
            def zone_last_known_end(members_json):
                return max(int(byid_at_cut[k].last_observed_closed_ts)
                           for k in json.loads(members_json))
            structural['last_zone_observed_closed_ts']=structural.level_ids.map(zone_last_known_end)
            eligible=structural.loc[(structural.created_at.astype('int64')<=cutoff) &
                ((structural.last_zone_observed_closed_ts.astype('int64')>
                  cutoff-days*86_400_000) | project_expired)]
            visible=(select_historical_map(eligible,nmax,spacing)
                     if spacing else eligible.head(nmax))
            local_count=0
            if show_recency and recency_ranked is not None:
                recent_meta=recency_ranked[['zone_id','recent_status',
                    'last_interaction_ts','days_since_last_interaction']]
                visible=visible.merge(recent_meta,on='zone_id',how='left',
                                      validate='one_to_one')
                # Remaining line slots are available to recent B/C/D zones,
                # without deleting or upgrading the historical ranked A zones.
                free_slots=max(0,min(n_recent,nmax-len(visible)))
                latest_price=float(bars.loc[bars.timestamp+TF<=cutoff,'close'].iloc[-1])
                extra=select_recent_overlay(recency_ranked,latest_price,
                    excluded_ids=set(visible.zone_id.astype(str)),
                    reserved_centers=visible.center.astype(float).tolist(),
                    limit=free_slots,spacing_pct=max(.35,spacing),
                    max_distance_pct=distance_limit,
                    include_partial_expired=include_expired)
                local_count=len(extra)
                if not extra.empty:
                    visible=pd.concat([visible,extra],ignore_index=True)
                st.caption(f'Mapa combinado: {len(visible)-local_count} zonas '
                           f'históricas + {local_count} locales recientes; '
                           f'{len(visible)} trazos en total. Las locales no se '
                           'reclasifican como A y pueden ser B/C/D.')
        else:
            structural=structural.sort_values(['importance_score','members'],
                              ascending=[False,False],kind='stable')
            visible=structural.head(nmax)
        st.caption(f'Subzonas con el grado elegido: {len(structural)}; '
                   f'con múltiples líneas: {int((structural.members>1).sum())}; '
                   f'dibujadas en el mapa: {len(visible)}. '
                   'El selector permite inspeccionar también las no dibujadas. '
                   'Una sola línea puede formar una subzona válida.')
        key_all='(Todas las subzonas del mapa)'
        # Always list ALL eligible zones, not only the first plotted ones.
        inspection=(recency_ranked if updated and show_recency and
                    recency_ranked is not None else structural)
        ids=[key_all]+inspection.zone_id.astype(str).tolist()
        by_code=inspection.set_index('zone_id',drop=False)
        def format_pick(code):
            if code==key_all:return code
            r=by_code.loc[code]
            if updated:
                label=(f'{r.relative_tier} · E{r.evidence_index:.1f} '
                       f'· P{r.relative_percentile:.0f}')
            else:
                label=f'{r.tier}{r.importance_score} (V2 original)'
            kind='subzona' if int(r.members)>1 else 'línea'
            return f'{label} · {r.center:,.0f} · {r.members} {kind} · {code}'
        pick=st.selectbox('Inspeccionar subzona estructural',ids,
                          format_func=format_pick,key='hllhier_v2_zone_pick')
        focus=None if pick==key_all else pick
        selected_behavior=None
        selected_behavior_marks=None
        focus_window=None
        if st.checkbox('Inspector de episodios V2.2-A · solo lectura',
                       value=False,key='hllhier_behavior_inspector_enabled'):
            candidates=behavior_runs(root,folder,v2folder,cutoff)
            if not candidates:
                st.info('No existe una auditoría V2.2-A compatible con este corte y estas fuentes. '
                        'Seleccioná el corte original de la auditoría o generá otra corrida offline.')
            else:
                behavior_folder=st.selectbox('Auditoría V2.2-A congelada',candidates,
                   format_func=lambda p:p.name,key='hllhier_behavior_run')
                try:
                    @st.cache_data(show_spinner=False,max_entries=3)
                    def cached_behavior(directory,v1path,v2path,cut,signature):
                        return load_behavior(Path(directory),Path(v1path),Path(v2path),cut)
                    filenames=('manifest.json','episodes.csv','episode_transitions.csv','behavior_summary.csv')
                    sig=tuple((n,(behavior_folder/n).stat().st_size,
                              (behavior_folder/n).stat().st_mtime_ns) for n in filenames)
                    behavior_manifest,behavior_ep,behavior_tr=cached_behavior(
                        str(behavior_folder),str(folder),str(v2folder),int(cutoff),sig)
                    valid_zones=set(inspection.zone_id.astype(str))
                    subset=behavior_ep.loc[behavior_ep.zone_id.astype(str).isin(valid_zones)].copy()
                    if focus is not None:
                        subset=subset.loc[subset.zone_id.astype(str).eq(focus)]
                    state_options=['TODOS']+sorted(subset.state.dropna().astype(str).unique().tolist())
                    state=st.selectbox('Resolución del episodio',state_options,
                                       key='hllhier_behavior_state')
                    if state!='TODOS':subset=subset.loc[subset.state.eq(state)]
                    subset=subset.sort_values(['first_test_closed_ts','episode_id'],ascending=[False,True])
                    if subset.empty:
                        st.info('No hay episodios para esta subzona y resolución.')
                    else:
                        codes=subset.episode_id.astype(str).tolist()
                        by_episode=subset.set_index('episode_id',drop=False)
                        def fmt_episode(code):
                            rr=by_episode.loc[code]
                            when=pd.Timestamp(int(rr.first_test_closed_ts),unit='ms',tz='UTC')
                            return (f'{when:%d/%m %H:%M} UTC · {rr.state} · {rr.side} '
                                    f'· {str(rr.zone_id)[:13]} · {str(code)[:15]}')
                        ep_id=st.selectbox('Seleccionar episodio físico',codes,
                                           format_func=fmt_episode,key='hllhier_behavior_episode')
                        selected_behavior=by_episode.loc[ep_id]
                        focus=str(selected_behavior.zone_id)
                        p1,p2=st.columns(2)
                        with p1: pre_h=st.selectbox('Horas anteriores',[3,6,12,24,48],index=2,
                                                    key='hllhier_behavior_pre_h')
                        with p2: post_h=st.selectbox('Horas posteriores',[3,6,12,24,48],index=2,
                                                     key='hllhier_behavior_post_h')
                        focus_window=episode_window(selected_behavior,cutoff,pre_h,post_h)
                        selected_behavior_marks=episode_marks(behavior_tr,ep_id,cutoff)
                        st.caption(f'Evento {ep_id} · {len(selected_behavior_marks)} transiciones originales · '
                                   f'{int(selected_behavior.member_test_count)} tests de líneas · '
                                   f'{int(selected_behavior.test_count_unique)} instantes de test. '
                                   'Se enfoca el gráfico en el episodio, sin alterar la subzona ni sus datos.')
                        st.dataframe(selected_behavior_marks[['observed_closed_ts','event_type',
                           'transition_role','level_id','test_id']].assign(
                            utc=lambda d:pd.to_datetime(d.observed_closed_ts,unit='ms',utc=True))[
                            ['utc','event_type','transition_role','level_id','test_id']],
                            hide_index=True,use_container_width=True)
                        st.caption('V2.2-A: resoluciones del Lifecycle original; no demuestra '
                                   'rechazo físico, aceptación, flip ni PnL. '
                                   'La identidad de subzona pertenece al corte auditado.')
                except (OSError,ValueError,KeyError,TypeError,IndexError,json.JSONDecodeError) as exc:
                    st.error(f'No se pudo abrir la auditoría V2.2-A: {exc}')
        drawn=visible if focus is None else inspection.loc[inspection.zone_id.eq(focus)]
        if updated and focus is not None and int(drawn.iloc[0].created_at) < cutoff-days*86_400_000:
            last_ts=int(drawn.iloc[0].last_zone_observed_closed_ts)
            if last_ts<=cutoff-days*86_400_000:
                st.info('Esta subzona es histórica y su ciclo terminó fuera de la '
                        'ventana visible. Ampliá los días o retrocedé el corte.')
        original_byid={str(r.level_id):r for r in scores.itertuples(index=False)}
        zones=[]
        for r in drawn.itertuples(index=False):
            leader=original_byid.get(r.leader_level_id)
            if leader is None:continue
            levels=json.loads(r.level_ids)
            centers=[float(original_byid[k].center) for k in levels]
            end_at=max(int(original_byid[k].last_observed_closed_ts) for k in levels)
            importance=(f'E{float(r.evidence_index):.1f} · '
                        f'P{float(r.relative_percentile):.0f}') if updated else f'{r.importance_score}/100 (V2)'
            recent_status=getattr(r,'recent_status','SOLO_HISTORICA')
            last_contact=getattr(r,'last_interaction_ts',None)
            if pd.isna(last_contact):last_contact=None
            zones.append(dict(id=r.zone_id,leader=leader,anchor=float(r.center),
                score=int(r.importance_score),
                tier=str(r.relative_tier if updated else r.tier),
                score_label=importance,structural=True,
                kind=str(r.origin_kind),count=int(r.members),members=levels,
                rows=[],label=str(r.zone_id),end_at=end_at,
                min_center=min(centers),max_center=max(centers),
                low=float(r.low),high=float(r.high),created_at=int(r.created_at),
                recent_overlay_enabled=show_recency,
                recent_status=recent_status,
                last_interaction_ts=last_contact,lifecycle=str(r.lifecycle)))
        individual=None
        constituent=[]
        if focus is not None:
            row=inspection.loc[inspection.zone_id.eq(focus)].iloc[0]
            constituent=json.loads(row.level_ids)
            st.caption(f'Subzona creada: {pd.Timestamp(row.created_at,unit="ms",tz="UTC")} · '
                       f'Historial desde su nacimiento: {row.tests_since_zone_birth} tests '
                       f'independientes, {row.defenses_since_birth} defensas, '
                       f'{row.losses_since_birth} pérdidas (deduplicación conservadora). '
                       f'Ubicación precio: {row.price_relation} · Última respuesta: '
                       f'{row.last_response}. No se infiere flip automáticamente.')
            individual=st.selectbox('Línea original para mostrar sus marcas',constituent,
                     format_func=lambda k: f'{original_byid[k].origin} · {original_byid[k].center:,.2f} · {k}',
                     key='hllhier_v2_line_pick')
        # Overlay de eventos First Touch congelados: activación bajo demanda.
        # La geometría/ranking HLL conserva su propia identidad y fecha al corte.
        reaction_marks=None
        reaction_selected=None
        confluence_inspection=None
        confluence_candidates=None
        confluence_window=None
        confluence_reveal=False
        if manifest.get('symbol')=='BTCUSDT' and st.checkbox(
                'Overlay Reaction Lab · First Touch BTC (solo lectura)',
                value=False,key='hllhier_reaction_overlay_enabled'):
            reaction_path=root/REACTION_OVERLAY_RUN
            try:
                @st.cache_data(show_spinner=False,max_entries=4)
                def cached_reaction_overlay(path,signature):
                    return load_reaction_overlay(Path(path),symbol='BTCUSDT')
                sig=tuple((f,(reaction_path/f).stat().st_size,
                           (reaction_path/f).stat().st_mtime_ns)
                          for f in ('audit.json','event_hll_labels.csv'))
                reaction_data,reaction_audit=cached_reaction_overlay(str(reaction_path),sig)
                st.caption('Fuente: piloto congelado Reaction × HLL de cinco símbolos. '
                           'En BTC contiene 181 First Touch; no son todas las REACTION históricas. '
                           'Marcadores en el precio del SWING y instante known_ts (no precio de fill).')
                chosen_kinds=st.multiselect('Estados de First Touch',list(REACTION_KINDS),
                    default=list(REACTION_KINDS),key='hllhier_reaction_types')
                chosen_sides=st.multiselect('Dirección', ['LONG','SHORT'],
                    default=['LONG','SHORT'],key='hllhier_reaction_sides')
                if focus_window is not None:
                    range_first=int(focus_window[0])
                else:
                    range_first=int(cutoff)-int(days)*86_400_000
                available,_=select_reaction_overlay(reaction_data,
                    cutoff=int(cutoff),first=range_first,
                    kinds=tuple(chosen_kinds),sides=tuple(chosen_sides),limit=100000)
                inspect_on=st.checkbox('Inspector individual de confluencias · PRE',
                        value=False,key='hllhier_confluence_inspect_on')
                if inspect_on:
                    # Historical events from full verified pilot, not just the
                    # last visible day. Never offer an event after HLL cutoff.
                    available,_=select_reaction_overlay(reaction_data,
                        cutoff=int(cutoff),first=int(reaction_data.known_ts.min()),
                        kinds=tuple(chosen_kinds),sides=tuple(chosen_sides),limit=100000)
                if not available.empty:
                    keys=['(Todos los eventos visibles)']+available.touch_key.astype(str).tolist()
                    event_by_key=available.set_index('touch_key')
                    def show_reaction(key):
                        if key==keys[0]:return key
                        item=event_by_key.loc[key]
                        return (f"{pd.Timestamp(int(item.known_ts),unit='ms',tz='UTC'):%d/%m %H:%M} "
                            f"UTC · {item.status} · {item.side} · {float(item.swing_price):,.2f} · "
                            f"{key[:30]}")
                    pick_reaction=st.selectbox('Inspeccionar First Touch de BTC',keys,
                         format_func=show_reaction,key='hllhier_reaction_selected')
                    reaction_selected=None if pick_reaction==keys[0] else pick_reaction
                    if reaction_selected:
                        record=event_by_key.loc[reaction_selected]
                        st.caption(f"touch_key: {reaction_selected} · HLL cerca 0.5 ATR: "
                            f"{int(record.hll_near_05atr_count)} · "
                            f"{record.hll_persistent_group} · cobertura {record.hll_coverage}. "
                            'Solo contexto causal PRE; no se muestran resultados futuros.')
                        if inspect_on:
                            try:
                                @st.cache_data(show_spinner=False,max_entries=3)
                                def cached_hll_candidates(path,signature):
                                    return load_confluence_candidates(Path(path),symbol='BTCUSDT')
                                cf=(reaction_path/'hll_candidates_near_1atr.csv')
                                sig_cf=(cf.stat().st_size,cf.stat().st_mtime_ns)
                                all_candidates=cached_hll_candidates(str(reaction_path),sig_cf)
                                all_pre,near_pre=confluence_context(record,all_candidates)
                                st.markdown('**Contexto estructural PRE congelado**')
                                x1,x2,x3=st.columns(3)
                                x1.metric('HLL a ≤0,5 ATR',len(near_pre))
                                x2.metric('Con defensas previas',int((near_pre.prior_defenses>0).sum()))
                                x3.metric('HLL a ≤1 ATR',len(all_pre))
                                if not all_pre.empty:
                                    st.dataframe(all_pre[['level_id','origin','gap_atr','prior_tests',
                                         'prior_defenses','prior_losses','prior_returns','zone_low',
                                         'zone_high']].rename(columns={'gap_atr':'distancia_ATR',
                                         'prior_returns':'RETURN_INSIDE_PRE'}),
                                         use_container_width=True,hide_index=True)
                                st.caption('RETURN_INSIDE es retorno registrado, NO recuperación '
                                           'sostenida. Estos niveles son del JOIN causal del evento; '
                                           'no se usa el ranking posterior de las subzonas.')
                                cpre,cpost=st.columns(2)
                                with cpre:
                                    hpre=st.selectbox('Horas PRE', [2,4,6,12,24],index=2,
                                                     key='hllhier_confluence_pre_h')
                                with cpost:
                                    hpost=st.selectbox('Horas POST', [2,4,6,12,24],index=2,
                                                     key='hllhier_confluence_post_h')
                                confluence_reveal=st.checkbox('Revelar velas POST (solo visual)',
                                    value=False,key='hllhier_confluence_show_post')
                                confluence_inspection=record
                                confluence_candidates=all_pre
                                confluence_hours=(hpre,hpost)
                                st.info('Modo inspección: el mapa HLL del corte se sustituye '
                                        'por las bandas PRE congeladas del First Touch. '
                                        'Sin simulación de entrada ni PnL.')
                            except (OSError,KeyError,ValueError,TypeError) as exc:
                                st.warning(f'Inspector PRE no disponible: {exc}')
                                confluence_inspection=None
                else:
                    st.info('No hay eventos del piloto para estos filtros y fechas. '
                            'Esto no significa que no existan otras REACTION históricas.')
                reaction_marks,total=select_reaction_overlay(reaction_data,
                    cutoff=int(cutoff),first=range_first,
                    kinds=tuple(chosen_kinds),sides=tuple(chosen_sides),
                    limit=240,chosen=reaction_selected)
                if total>len(reaction_marks):
                    st.caption(f'{total} eventos cumplen filtros; se muestran '
                               f'{len(reaction_marks)} marcadores distribuidos para preservar rendimiento.')
                else:
                    st.caption(f'{total} eventos First Touch visibles en esta ventana.')
            except (OSError,ValueError,KeyError,TypeError,json.JSONDecodeError) as exc:
                st.warning(f'Overlay no disponible; datos congelados sin verificar: {exc}')
        visual_bars=None
        visual_manifest=None
        if timeframe in ('1m','3m','5m','15m'):
            btc_dir=root/BTC_1M_RUN
            try:
                @st.cache_data(show_spinner=False,max_entries=2)
                def cached_btc_visual(path,signature):
                    return load_btc_1m(Path(path))
                signature=tuple((n,(btc_dir/n).stat().st_size,
                                  (btc_dir/n).stat().st_mtime_ns)
                                 for n in ('manifest.json','BTCUSDT_1m.csv'))
                visual_bars,visual_manifest=cached_btc_visual(str(btc_dir),signature)
                st.caption('Velas pequeñas: histórico BTC 1m congelado y verificado; '
                           'zonas HLL y eventos no se recalculan.')
            except (OSError,ValueError,KeyError,TypeError,json.JSONDecodeError) as exc:
                st.warning(f'Histórico visual BTC 1m no disponible: {exc}')
        inspection_candles=None
        if confluence_inspection is not None:
            try:
                instant=int(confluence_inspection.known_ts)
                if timeframe in ('1m','3m','5m','15m'):
                    if visual_bars is None or visual_manifest is None:
                        raise ValueError('Faltan velas BTC 1m verificadas')
                    source=visual_bars
                    src_minute=1
                    available_start=int(visual_manifest['start_ms'])
                    available_end=int(visual_manifest['end_exclusive_ms'])
                else:
                    source=bars
                    src_minute=30
                    available_start=int(bars.timestamp.iloc[0])
                    available_end=int(bars.timestamp.iloc[-1])+TF
                confluence_window=confluence_interval(instant,
                    pre_hours=confluence_hours[0],post_hours=confluence_hours[1],
                    reveal_post=confluence_reveal,cutoff_ms=int(cutoff),
                    available_start=available_start,available_end=available_end)
                inspection_candles=confluence_candles(source,first=confluence_window[0],
                    end=confluence_window[1],timeframe=timeframe,source_minute=src_minute)
                # Do not display other marker events or any HLL subzone ranked
                # at a later historical cutoff during PRE inspection.
                reaction_marks=reaction_data.loc[
                    reaction_data.touch_key.eq(str(confluence_inspection.touch_key))].copy()
            except (OSError,ValueError,KeyError,TypeError) as exc:
                st.warning(f'No se puede enfocar esta REACTION en {timeframe}: {exc}')
                confluence_inspection=None
                confluence_candidates=None
                confluence_window=None
                inspection_candles=None
        vertical_choice=st.radio('Escala vertical del gráfico',
            ['Automática · zoom en temporalidades chicas',
             'Precio visible · zoom limpio','Todas las zonas · contexto completo'],
            index=0,horizontal=True,key='hllhier_v12_vertical_mode')
        vertical_mode=('price' if vertical_choice.startswith('Precio') or
            (vertical_choice.startswith('Automática') and timeframe in
             ('1m','3m','5m','15m')) else 'all')
        if vertical_mode=='price':
            st.caption('Escala ajustada a las velas visibles: las zonas lejanas '
                'se ocultan solo del dibujo, no de los selectores ni de los datos HLL. '
                'Podés cambiar a contexto completo en cualquier momento.')
        try:
            fig,actual=make_zone_chart(bars,[] if inspection_candles is not None else zones,cutoff,days,timeframe,
                     inspection_candles=inspection_candles,
                     inspection_candidates=confluence_candidates if inspection_candles is not None else None,
                     inspection_focus=confluence_window if inspection_candles is not None else None,
                     visual_bars=visual_bars,visual_manifest=visual_manifest,
                     selected_zone_id=None if inspection_candles is not None else focus,
                     selected_line_id=None if inspection_candles is not None else individual,
                     events=events,
                     behavior_transitions=None if inspection_candles is not None else selected_behavior_marks,
                     focus_window=None if inspection_candles is not None else focus_window,
                     reaction_events=reaction_marks,reaction_selected=reaction_selected,
                     vertical_mode=vertical_mode,
                     project_expired=project_expired and inspection_candles is None)
            st.plotly_chart(fig,use_container_width=True,key='hllhier_structural_v2_chart')
            if actual!=timeframe:
                st.info(f'Visualización ajustada a {actual} con OHLC agregado real.')
        except (ValueError,KeyError,TypeError) as exc:
            st.error(f'Error en gráfico V2: {exc}')
        st.caption('Cada subzona empieza en su fecha de unión, nunca antes. '
                   'La puntuación/jerarquía se conoce al CORTE del gráfico; '
                   'no debe proyectarse retrospectivamente al nacimiento. '
                   'E=índice de evidencia propia sin techo; P=percentil relativo a las '
                   'subzonas del corte. Con el modo original se muestra V2 heredado.')
        if updated and show_recency and recency_ranked is not None:
            with st.expander('Actividad reciente de TODAS las subzonas (incluye B/C/D)'):
                recent_table=recency_ranked.loc[recency_ranked.has_recent_activity].copy()
                cols=['zone_id','center','relative_tier','lifecycle',
                      'recent_status','interactions_in_window',
                      'days_since_last_interaction','created_at','members']
                st.dataframe(recent_table[cols],hide_index=True,use_container_width=True)
        display_cols=['zone_id','center','low','high','members','tier',
              'importance_score','created_at','tests_since_zone_birth',
              'defenses_since_birth','losses_since_birth','lifecycle',
              'price_relation','last_response','level_ids']
        if updated:
            with st.expander('Tabla completa de importancia V2.1 y auditoría por zona'):
                st.dataframe(structural[['zone_id','center','members','relative_tier',
                    'evidence_index','relative_percentile','primary_phase',
                    'tests_in_window','tests_since_zone_birth',
                    'defenses_since_birth','losses_since_birth',
                    'lifecycle','price_relation','v2_tier','v2_score']],
                    hide_index=True,use_container_width=True)
        else:
            st.dataframe(structural[display_cols],hide_index=True,use_container_width=True)
        if focus is not None:
            st.markdown('**Líneas integrantes y puntuaciones actuales**')
            st.dataframe(scores.loc[scores.level_id.astype(str).isin(constituent),
                ['level_id','origin','center','tier','importance_score',
                 'tests_pre','defenses_pre','losses_pre','formation_closed_ts']],
                 hide_index=True,use_container_width=True)
            own=[b for b in births if b['zone_id']==focus]
            if own:
                st.caption('Linaje de esta agrupación: '+own[-1]['parent_zone_ids'])
            with st.expander('Historial del puntaje LEGADO V1 de sus integrantes · corte diario causal'):
                show_history=st.checkbox('Calcular historial LEGADO del mejor integrante (no es el índice V2.1)',
                        value=False,key='hllhier_v2_score_history')
                if show_history:
                    @st.cache_data(show_spinner=False,max_entries=30)
                    def cached_zone_history(v1_tag,v2_tag,zone_key,members_tuple,born,end):
                        return zone_score_history(forms,events,ends,bars,members_tuple,born,end)
                    changes=cached_zone_history(folder.name,v2tag,row.zone_id,
                            tuple(constituent),int(row.created_at),int(cutoff))
                    fig_score=go.Figure(go.Scatter(
                        x=pd.to_datetime(changes.cutoff_ms,unit='ms',utc=True),
                        y=changes.score,mode='lines+markers',
                        customdata=changes[['tier','leader']].to_numpy(),
                        hovertemplate='%{x}<br>Score %{y}/100<br>Tier %{customdata[0]}'
                                      '<br>Líder %{customdata[1]}<extra></extra>'))
                    fig_score.update_layout(height=260,margin=dict(l=6,r=6,t=10,b=16),
                                            yaxis=dict(range=[0,100],title='Importancia V1 / 100'))
                    st.plotly_chart(fig_score,use_container_width=True,
                                     key='hllhier_v2_score_plot')
                    st.caption('Score del mejor miembro conocido en cada fecha; no es '
                               'un score calibrado de la zona ni una serie de resultados futuros.')
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
                          selected_id=None if pick==options[0] else pick,
                          project_expired=project_expired)
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
    st.caption('V1/V2 originales: A≥75, B≥55, C≥35; V2.1: percentiles relativos '
               'de evidencia estructural causal (exploratorios, no calibrados). '
               'La macroagrupación V1.1 es visual, V2 exige evidencia causal. La clase asignada al CORTE no '
               'debe proyectarse retrospectivamente a la fecha de formación. '
               'No se usan TP/SL, retornos ni resultados futuros.')
