"""Read-only interactive V1.3 episode view inside the existing BTC 60D lab.

No replay, detector, network, trading or dataset writes. PRE is filtered at
the payload level: candles, labels, links, comparison rows and downloads share
the same closed-data boundary. All Streamlit imports are render-time only.
"""
from __future__ import annotations

import hashlib
import json
import time
import textwrap
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

M = 60_000
DAY = 1440*M
BASE = Path('reports/reaction_lab/btc_multiswing_60d_v1')
RUN = BASE/'btc_sep09_nov08_structural_behavior_fixed_vs_asof_v13'
P12 = BASE/'btc_sep09_nov08_structural_behavior_15m_1h_v12'
P121 = BASE/'btc_sep09_nov08_structural_behavior_pre_safety_v121'
FREEZE = BASE/'btc_sep09_nov08_60d_v1'
CATALOG = BASE/'btc_sep09_nov08_swing_catalog_6tf_v1'
VERSION = 'btc-multiswing-structural-behavior-fixed-vs-causal-stage-v1.3'
PREFIX = 'ms60_v13_'
POST_KEY = PREFIX+'reveal_post'
PRESETS = ('LOW_23_OCT', 'HIGH_22_OCT', 'CATALOG')
BOOKMARKS = {
    'LOW_23_OCT': ('msv2_69f4a7f7cf54ab7a4214', 'msv2_69f4a7f7cf54ab7a4214|EP|1792722660000',
                   'msv2_69f4a7f7cf54ab7a4214|V29|1792775940000'),
    'HIGH_22_OCT': ('msv2_f2cf7789ed5ecf4f4f81', 'msv2_f2cf7789ed5ecf4f4f81|EP|1792653540000',
                    'msv2_f2cf7789ed5ecf4f4f81|V19|1792663380000'),
}
INDEX_FILES = ((RUN,'episode_sequences_v13.csv'), (RUN,'asof_transition_observations_v13.csv'),
               (RUN,'h1_m15_links_asof_v13.csv'), (P12,'visit_pre_v12.csv'),
               (P121,'geometric_stage_history_v121.csv'))
STATUS_LABELS = {
    'SAME_LABEL_AND_LINEAGE': 'Misma etiqueta y ruptura asociada',
    'SAME_LABEL_DIFFERENT_LINEAGE': 'Misma etiqueta, otra ruptura asociada',
    'CHANGED_LABEL': 'Etiqueta diferente', 'REMOVED_UNDER_ASOF': 'No se emite en ese cierre',
    'INDETERMINATE': 'Datos insuficientes o ambiguos',
}


def sha256(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1<<20),b''):h.update(block)
    return h.hexdigest()


def utc(timestamp):
    return pd.to_datetime(int(timestamp),unit='ms',utc=True).strftime('%Y-%m-%d %H:%M UTC')


def iso_values(values):
    return pd.to_datetime(np.asarray(values,dtype='int64'),unit='ms',utc=True).strftime('%Y-%m-%dT%H:%M:%SZ').tolist()


def signature(root, paths):
    return tuple((str(p), (root/p).stat().st_size, (root/p).stat().st_mtime_ns) for p in paths)


def manifest(folder, version=None):
    m=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    if not m.get('immutable_run') or (version and m.get('version')!=version):
        raise ValueError('Manifiesto de estudio incorrecto: '+str(folder.name))
    return m


def verified_csv(folder, name, m):
    expected=m.get('files_sha256',{}).get(name)
    if not expected or sha256(folder/name)!=expected:
        raise ValueError('Archivo incompleto o modificado: '+name)
    return pd.read_csv(folder/name,low_memory=False)


@lru_cache(maxsize=2)
def _load_index(root_string, token):
    root=Path(root_string)
    run=manifest(root/RUN,VERSION)
    p12=manifest(root/P12)
    p121=manifest(root/P121)
    for label,path in [('v12',P12),('v121',P121)]:
        if run.get('parent_manifest_sha256',{}).get(label)!=sha256(root/path/'manifest.json'):
            raise ValueError('El run V1.3 no corresponde a los padres disponibles: '+label)
    frames=[verified_csv(root/path,name,run if path==RUN else p12 if path==P12 else p121)
            for path,name in INDEX_FILES]
    episodes,events,links,visits,stages=frames
    if (episodes.episode_id.duplicated().any() or events.asof_transition_id.duplicated().any() or
            visits.event_id.duplicated().any() or not set(events.episode_id)<=set(episodes.episode_id)):
        raise ValueError('Índice de episodios o transiciones inconsistente')
    return {'root':root,'manifest':run,'episodes':episodes,'events':events,
            'links':links,'visits':visits,'stages':stages}


def load_index(project):
    root=Path(project).resolve()
    paths=[RUN/'manifest.json',P12/'manifest.json',P121/'manifest.json']+[p/n for p,n in INDEX_FILES]
    return _load_index(str(root),signature(root,paths))


@lru_cache(maxsize=2)
def _load_prices(root_string, token):
    from btc_multiswing_60d_explorer_v1 import load_snapshot, aggregate_closed
    full,_,source=load_snapshot(Path(root_string))
    start,end=int(full.timestamp.iloc[0]),int(source['end'])
    bars={}
    for tf,name in [(15,'15m'),(60,'1h'),(240,'4h')]:
        b=aggregate_closed(full,name,start,end).copy()
        b['closed_ts']=b.timestamp+tf*M
        bars[tf]=b
    return bars,source


def load_prices(project, study):
    root=Path(project).resolve()
    paths=[FREEZE/'manifest.json',FREEZE/'BTCUSDT_1m.csv',FREEZE/'BTCUSDT_1m_warmup.csv',
           CATALOG/'manifest.json',CATALOG/'confirmed_swings.csv']
    bars,source=_load_prices(str(root),signature(root,paths))
    for key in ('start','end','source_sha256','catalog_sha256'):
        if source[key]!=study['manifest']['source_provenance'].get(key):
            raise ValueError('La fuente congelada no corresponde al run V1.3: '+key)
    return bars,source


@lru_cache(maxsize=2)
def _load_comparison(root_string, token):
    root=Path(root_string)
    return verified_csv(root/RUN,'observation_comparison_v13.csv',manifest(root/RUN,VERSION))


def load_comparison(project):
    root=Path(project).resolve()
    return _load_comparison(str(root),signature(root,[RUN/'manifest.json',RUN/'observation_comparison_v13.csv']))


def clear_caches():
    _load_index.cache_clear();_load_prices.cache_clear();_load_comparison.cache_clear()


def stage_at(stages, region, cutoff):
    rows=stages.loc[stages.region_id.eq(region)&stages.known_from.le(int(cutoff))]
    if rows.empty:return None
    return rows.sort_values(['known_from','independent_extrema'],kind='mergesort').iloc[-1]


def price_position(price, stage):
    if stage is None:return 'UNKNOWN'
    return 'BELOW' if price<float(stage.band_low) else 'ABOVE' if price>float(stage.band_high) else 'INSIDE'


def visible_end(cutoff, source_end, reveal, post_minutes):
    if not reveal:return int(cutoff)
    if int(post_minutes)<=0:raise ValueError('Horizonte POST inválido')
    return min(int(source_end),int(cutoff)+int(post_minutes)*M)


def slice_bars(bars, start, end):
    # Filter by CLOSE, never by OPEN: no partially open 1h/4h in PRE.
    return bars.loc[bars.closed_ts.gt(int(start))&bars.closed_ts.le(int(end))].copy()


def visible_events(events, region, start, end, tf=None, episode=None, details=False):
    mask=events.region_id.eq(region)&events.confirmation_ts.gt(int(start))&events.confirmation_ts.le(int(end))
    if tf is not None:
        name={15:'M15_CLOSED',60:'H1_CLOSED'}.get(tf)
        if name is None:return events.iloc[0:0].copy()
        mask &= events.source_tf.eq(name)
    if episode is not None:mask &= events.episode_id.eq(episode)
    result=events.loc[mask].copy()
    if not details:result=result.loc[~result.event_type.str.startswith(('CLOSE_','DEPART_'))]
    return (result.sort_values(['confirmation_ts','asof_transition_id'],kind='mergesort')
            .drop_duplicates('asof_physical_transition_id').copy())


def visible_links(links, episode, end):
    h=links.h1_confirmation_ts.le(int(end))
    m=links.m15_confirmation_ts.isna()|links.m15_confirmation_ts.le(int(end))
    return links.loc[links.episode_id.eq(episode)&h&m].copy()


def visible_comparison(frame, episode, end):
    return frame.loc[frame.episode_id.eq(episode)&frame.confirmation_ts.le(int(end))].copy()


def blind_navigation(state, nav):
    """Must run BEFORE instantiating the POST checkbox on this rerun."""
    token=tuple(nav)
    changed=state.get(PREFIX+'navigation')!=token
    if changed:state[POST_KEY]=False
    state[PREFIX+'navigation']=token
    return changed


def cutoff_for(mode, episode, visit=None, transition=None, transition_cut='BEFORE_CANDLE'):
    if mode=='EPISODE':return int(episode['pre_cutoff_ts'])
    if mode=='VISIT':return int(visit['pre_cutoff_ts'])
    if mode=='TRANSITION':
        return int(transition['bar_open_ts'] if transition_cut=='BEFORE_CANDLE' else transition['confirmation_ts'])
    raise ValueError('Modo de corte inválido')


def event_label(kind, direction=''):
    side={'ABOVE':'arriba','BELOW':'debajo','INSIDE':'dentro'}.get(str(direction),'')
    if kind.startswith('BREAK_'):return 'Cruce '+side+' · 2 cierres'
    if kind.startswith('EXIT_'):return 'Salida '+side+' · 2 cierres'
    if kind=='DEFEND_ORIGIN_2C':return 'Defensa desde '+side
    if kind.startswith('FAILED_BREAK_'):return 'Ruptura fallida · retorno al origen'
    if kind.startswith('FAILED_EXIT_'):return 'Salida fallida · retorno dentro'
    if kind=='RETEST_TOUCH':return 'Retest · contacto de rango'
    if kind.startswith('RETEST_HOLD_'):return 'Cierres sostienen '+side
    if kind.startswith('REENTER_ORIGIN_'):return 'Primer regreso al lado de origen'
    if kind.startswith('REENTER_BAND_'):return 'Primer reingreso a la banda'
    if kind=='ACCEPT_INSIDE_3C':return 'Aceptación dentro · 3 cierres'
    if kind.startswith('CLOSE_'):return 'Cambio de cierre: '+side
    if kind.startswith('DEPART_'):return 'Alejamiento '+side+' · 2 cierres'
    return kind


def band_points(stages, region, start, end):
    known=stages.loc[stages.region_id.eq(region)&stages.known_from.le(int(end))]
    known=known.sort_values(['known_from','independent_extrema'],kind='mergesort').drop_duplicates('known_from',keep='last')
    old=known.loc[known.known_from.le(start)].tail(1)
    chosen=pd.concat([old,known.loc[known.known_from.gt(start)]])
    if chosen.empty:return [],[],[]
    t=[max(int(start),int(x)) for x in chosen.known_from]
    lo,hi=chosen.band_low.astype(float).tolist(),chosen.band_high.astype(float).tolist()
    if t[-1]<end:t.append(int(end));lo.append(lo[-1]);hi.append(hi[-1])
    return iso_values(t),lo,hi


def snapshot_table(bars, stages, region, cutoff):
    s=stage_at(stages,region,cutoff);rows=[]
    for tf,label in [(15,'15m'),(60,'1h'),(240,'4h')]:
        q=bars[tf].loc[bars[tf].closed_ts.le(int(cutoff))].tail(1)
        rows.append({'Temporalidad':label,'Último cierre UTC':utc(q.closed_ts.iloc[0]) if len(q) else 'Sin cierre',
                     'Precio':float(q.close.iloc[0]) if len(q) else None,
                     'Posición con la etapa conocida al corte':price_position(float(q.close.iloc[0]),s) if len(q) else 'UNKNOWN'})
    return pd.DataFrame(rows)


def build_figure(bars, stages, events, *, region, episode, cutoff, end, tf_minutes,
                 lookback_minutes, origin_side, compare_fixed=False, details=False,
                 selected_transition_id=None):
    start=int(cutoff)-int(lookback_minutes)*M
    candles=slice_bars(bars,start,end)
    visible=visible_events(events,region,start,end,tf_minutes,details=details)
    color='#48c7aa' if origin_side=='LOW' else '#e3af59'
    fill='rgba(72,199,170,.17)' if origin_side=='LOW' else 'rgba(227,175,89,.17)'
    bx,lo,hi=band_points(stages,region,start,end)
    data=[]
    if bx:
        data.extend([{'type':'scatter','x':bx,'y':lo,'mode':'lines','line':{'color':color,'width':1,'shape':'hv'},
                      'name':'Etapa confirmada as-of','legendgroup':'band','showlegend':False,'hoverinfo':'skip'},
                     {'type':'scatter','x':bx,'y':hi,'mode':'lines','line':{'color':color,'width':1,'shape':'hv'},
                      'fill':'tonexty','fillcolor':fill,'name':'Etapa confirmada as-of','legendgroup':'band','hoverinfo':'skip'}])
    if compare_fixed:
        t=max(start,int(episode['pre_cutoff_ts']))
        if t<=end:
            for key in ('stage_low','stage_high'):
                data.append({'type':'scatter','x':iso_values([t,end]),'y':[float(episode[key])]*2,
                             'mode':'lines','line':{'color':'#b8c3d8','width':1,'dash':'dot'},
                             'name':'Referencia fija del episodio','legendgroup':'fixed','showlegend':key=='stage_low','hoverinfo':'skip'})
    data.append({'type':'candlestick','x':iso_values(candles.closed_ts.to_numpy()),
                 'open':candles.open.astype(float).tolist(),'high':candles.high.astype(float).tolist(),
                 'low':candles.low.astype(float).tolist(),'close':candles.close.astype(float).tolist(),
                 'increasing':{'line':{'color':'#45bc9b'},'fillcolor':'#45bc9b'},
                 'decreasing':{'line':{'color':'#ee7477'},'fillcolor':'#ee7477'},
                 'name':{15:'15m',60:'1h',240:'4h'}[tf_minutes],'showlegend':False})
    if len(visible):
        text=[]
        for r in visible.itertuples(index=False):
            direction='' if pd.isna(r.direction) else str(r.direction)
            text.append(event_label(r.event_type,direction)+'<br>'+utc(r.confirmation_ts)+
                        '<br>Etapa disponible: '+utc(r.stage_known_from)+
                        ('<br>Orden intraminuto sin resolver' if r.evidence_status=='OHLC_INTRABAR_SEQUENCE_UNKNOWN' else ''))
        data.append({'type':'scatter','x':iso_values(visible.confirmation_ts.to_numpy()),
                     'y':visible.confirmation_close.astype(float).tolist(),'mode':'markers','name':'Eventos confirmados',
                     'marker':{'size':8,'color':'#a7d7ff','line':{'color':'#0c1320','width':1}},
                     'text':text,'hovertemplate':'%{text}<br>Precio: %{y:.2f}<extra></extra>'})
    annotations=[]
    # Filter the chosen episode BEFORE physical deduplication. The same event
    # can belong to several episodes; a representative from another episode
    # must not make the selected transition disappear from navigation.
    selected_events=visible_events(events,region,start,end,tf_minutes,episode=str(episode['episode_id']),details=True)
    focus=selected_events
    focus=focus.loc[focus.event_type.str.startswith(('BREAK_','EXIT_','FAILED_','DEFEND_','RETEST_HOLD_'))|focus.event_type.eq('RETEST_TOUCH')]
    focus=focus.sort_values('confirmation_ts').drop_duplicates('event_type',keep='last').tail(5)
    if selected_transition_id and selected_transition_id in set(selected_events.asof_transition_id):
        selected=selected_events.loc[selected_events.asof_transition_id.eq(selected_transition_id)]
        focus=pd.concat([focus,selected]).drop_duplicates('asof_transition_id')
        data.append({'type':'scatter','x':iso_values(selected.confirmation_ts.to_numpy()),
                     'y':selected.confirmation_close.astype(float).tolist(),'mode':'markers',
                     'marker':{'size':15,'symbol':'circle-open','color':'#ffd166','line':{'width':2}},
                     'name':'Transición elegida','hovertemplate':'Transición elegida<br>%{x}<br>%{y:.2f}<extra></extra>'})
    for i,r in enumerate(focus.itertuples(index=False)):
        fraction=(int(r.confirmation_ts)-start)/max(1,end-start)
        right=fraction>.5
        label='<br>'.join(textwrap.wrap(event_label(r.event_type,'' if pd.isna(r.direction) else str(r.direction)),28))
        annotations.append({'x':iso_values([r.confirmation_ts])[0],'y':float(r.confirmation_close),
            'text':label,'xanchor':'right' if right else 'left',
            'showarrow':True,'arrowhead':0,'ax':-28 if right else 28,'ay':-42 if i%2==0 else 44,
            'bgcolor':'rgba(12,19,32,.92)','bordercolor':'#36536f','font':{'size':10,'color':'#e8edf6'},
            'arrowcolor':'#99bbda'})
    cut=iso_values([cutoff])[0]
    shapes=[{'type':'line','xref':'x','yref':'paper','x0':cut,'x1':cut,'y0':0,'y1':1,
             'line':{'color':'#f6cb64','width':1.4,'dash':'dash'}}]
    if end>cutoff:
        shapes.insert(0,{'type':'rect','xref':'x','yref':'paper','x0':cut,'x1':iso_values([end])[0],
                         'y0':0,'y1':1,'line':{'width':0},'fillcolor':'rgba(125,153,205,.09)','layer':'below'})
    annotations.append({'x':cut,'y':1,'yref':'paper','text':'Corte PRE','showarrow':False,
                        'xanchor':'right','yanchor':'top','font':{'size':10,'color':'#f6cb64'}})
    label={15:'15m · interacción local',60:'1h · estructura e historia',240:'4h · contexto cerrado'}[tf_minutes]
    layout={'title':{'text':label,'x':.01},'height':430,'paper_bgcolor':'#0c1320','plot_bgcolor':'#121d2e',
            'font':{'color':'#e6edf7','size':11},'margin':{'l':60,'r':25,'t':55,'b':45},
            'xaxis':{'type':'date','range':iso_values([start,end]),'rangeslider':{'visible':False},
                     'gridcolor':'#27354a','title':{'text':'UTC · hora de cierre'}},
            'yaxis':{'title':{'text':'USD'},'gridcolor':'#27354a','fixedrange':False},
            'hovermode':'closest','legend':{'orientation':'h','y':1.04,'x':.15},
            'shapes':shapes,'annotations':annotations,
            'uirevision':f'{region}|{episode["episode_id"]}|{cutoff}|{end}|{tf_minutes}|{lookback_minutes}'}
    return {'data':data,'layout':layout}


def timeline_table(frame, cutoff):
    rows=[]
    for r in frame.itertuples(index=False):
        direction='' if pd.isna(r.direction) else str(r.direction)
        rows.append({'Hora UTC':utc(r.confirmation_ts),'TF':'15m' if r.source_tf=='M15_CLOSED' else '1h',
                     'Evento':event_label(r.event_type,direction),'Lado':direction or '—',
                     'Tramo':'PRE' if int(r.confirmation_ts)<=cutoff else 'POST',
                     'Precio de confirmación':float(r.confirmation_close),
                     'Límite inferior':float(r.band_low_asof),'Límite superior':float(r.band_high_asof)})
    return pd.DataFrame(rows)


def _safe_select(st,label,options,key,default=None,format_func=None):
    options=list(options)
    if not options:return None
    state=st.session_state
    if state.get(key) not in options:state[key]=default if default in options else options[0]
    kwargs={'key':key}
    if format_func is not None:kwargs['format_func']=format_func
    return st.selectbox(label,options,**kwargs)


def width_options(st):
    # Keep the VPS's older Streamlit API supported while avoiding deprecation
    # warnings on newer releases. Dataframe width used to accept integers only.
    try:
        version=tuple(int(x) for x in st.__version__.split('.')[:2])
    except (AttributeError,ValueError):version=(0,0)
    return {'width':'stretch'} if version>=(1,51) else {'use_container_width':True}


def render_multiswing_behavior_v13(project, st=None):
    if st is None:
        import streamlit as st
    started=time.perf_counter()
    root=Path(project).resolve()
    st.markdown('#### Episodios estructurales · V1.3')
    try:
        study=load_index(root)
    except FileNotFoundError:
        st.info('El run V1.3 todavía no está disponible. Esperá MS_BEHAVIOR_V13_FINISHED y actualizá la lectura.')
        if st.button('Actualizar lectura',key=PREFIX+'refresh_missing'):
            clear_caches();st.rerun()
        return {'status':'RUN_NOT_AVAILABLE'}
    except (ValueError,KeyError,OSError) as exc:
        st.error('No se pudo verificar el estudio V1.3: '+str(exc))
        return {'status':'INVALID_STUDY'}
    state=st.session_state
    width=width_options(st)
    presets={'LOW_23_OCT':'LOW · 23 octubre · visita 29',
             'HIGH_22_OCT':'HIGH · 22 octubre · visita 19', 'CATALOG':'Explorar todos los episodios BTC'}
    preset=_safe_select(st,'Punto de partida',PRESETS,PREFIX+'preset','LOW_23_OCT',presets.get)
    changed_preset=state.get(PREFIX+'last_preset')!=preset
    if changed_preset:
        state[POST_KEY]=False
        if preset in BOOKMARKS:
            r,e,v=BOOKMARKS[preset]
            state[PREFIX+'region']=r;state[PREFIX+'episode']=e;state[PREFIX+'visit']=v
            state[PREFIX+'anchor']='VISIT'
        state[PREFIX+'last_preset']=preset
    eps=study['episodes']
    if preset in BOOKMARKS:eps=eps.loc[eps.region_id.eq(BOOKMARKS[preset][0])]
    else:
        side=_safe_select(st,'Origen de la banda',('BOTH','LOW','HIGH'),PREFIX+'side','BOTH',
                          lambda x:{'BOTH':'Ambos','LOW':'LOW','HIGH':'HIGH'}[x])
        if side!='BOTH':eps=eps.loc[eps.band_origin_side.eq(side)]
    if eps.empty:
        st.info('No hay episodios bajo este filtro.');return {'status':'NO_EPISODES'}
    regions=sorted(eps.region_id.unique())
    sides=eps.drop_duplicates('region_id').set_index('region_id').band_origin_side.to_dict()
    band_col,episode_col=st.columns(2)
    with band_col:
        region=_safe_select(st,'Banda',regions,PREFIX+'region',format_func=lambda r:sides[r]+' · '+r)
    eps=eps.loc[eps.region_id.eq(region)].sort_values('pre_cutoff_ts')
    epmap=eps.set_index('episode_id').to_dict('index')
    with episode_col:
        eid=_safe_select(st,'Episodio',eps.episode_id,PREFIX+'episode',format_func=lambda x:'Desde '+utc(epmap[x]['pre_cutoff_ts']))
    ep={**epmap[eid],'episode_id':eid}
    nav=_safe_select(st,'Situar el corte PRE',('VISIT','EPISODE','TRANSITION'),PREFIX+'anchor','VISIT',
        lambda x:{'VISIT':'Antes del minuto de una visita','EPISODE':'Inicio del episodio',
                  'TRANSITION':'Vela de una transición'}[x])
    visit=None;transition=None;tid=None;vid=None;transition_cut='BEFORE_CANDLE'
    if nav=='VISIT':
        visits=study['visits'].loc[study['visits'].episode_id.eq(eid)].sort_values('pre_cutoff_ts')
        vmap=visits.set_index('event_id').to_dict('index')
        vid=_safe_select(st,'Visita',visits.event_id,PREFIX+'visit',format_func=lambda x:
                         f"Visita {int(vmap[x]['visit_number'])} · {utc(vmap[x]['pre_cutoff_ts'])} · desde {vmap[x]['approach_position']}")
        if vid is None:st.info('Este episodio no tiene visitas disponibles.');return {'status':'NO_VISITS'}
        visit=vmap[vid]
    elif nav=='TRANSITION':
        candidates=study['events'].loc[study['events'].episode_id.eq(eid)].sort_values(['confirmation_ts','source_tf','asof_transition_id'])
        emap=candidates.set_index('asof_transition_id').to_dict('index')
        numbers={x:i+1 for i,x in enumerate(candidates.asof_transition_id)}
        tid=_safe_select(st,'Vela de estudio',candidates.asof_transition_id,PREFIX+'transition',
            format_func=lambda x:f"#{numbers[x]} · {'15m' if emap[x]['source_tf']=='M15_CLOSED' else '1h'} · abre {utc(emap[x]['bar_open_ts'])}")
        if tid is None:st.info('Este episodio no tiene transiciones disponibles.');return {'status':'NO_TRANSITIONS'}
        transition=emap[tid]
        transition_cut=_safe_select(st,'Instante de la vela',('BEFORE_CANDLE','AT_CONFIRMATION'),PREFIX+'transition_cut','BEFORE_CANDLE',
            lambda x:{'BEFORE_CANDLE':'PRE antes de abrir la vela','AT_CONFIRMATION':'Al cierre confirmado'}[x])
        st.caption('La etiqueta de la transición aparece solo cuando su cierre queda incluido en el corte o se revela POST.')
    cutoff=cutoff_for(nav,ep,visit,transition,transition_cut)
    try:bars,source=load_prices(root,study)
    except (ValueError,FileNotFoundError,KeyError,OSError) as exc:
        st.error('No se pudo abrir la fuente congelada: '+str(exc));return {'status':'INVALID_PRICES'}
    c1,c2,c3=st.columns(3)
    with c1:local_hours=_safe_select(st,'Historia 15m · horas',(12,24,48,72),PREFIX+'local_hours',24)
    with c2:h1_days=_safe_select(st,'Historia 1h · días',(1,3,7,14,30),PREFIX+'h1_days',7)
    with c3:h4_days=_safe_select(st,'Contexto 4h · días',(1,3,7,14,30,60),PREFIX+'h4_days',14)
    blind_navigation(state,(preset,region,eid,nav,vid or '',tid or '',transition_cut,cutoff,local_hours,h1_days,h4_days))
    post_col,horizon_col,compare_col=st.columns(3)
    with post_col:reveal=st.checkbox('Revelar POST',key=POST_KEY,value=False)
    with horizon_col:post_minutes=_safe_select(st,'POST · minutos',(15,30,60,180,360,720,1440),PREFIX+'post_minutes',720)
    end=visible_end(cutoff,source['end'],reveal,post_minutes)
    with compare_col:compare=st.checkbox('Comparar con la referencia fija',key=PREFIX+'compare',value=False)
    with st.expander('Opciones de la cronología'):
        details=st.checkbox('Incluir cambios de cierre y alejamientos',key=PREFIX+'details',value=False)
        history=st.checkbox('Incluir eventos previos de la región en la cronología',key=PREFIX+'history',value=True)
    st.caption('Corte común: '+utc(cutoff)+' · '+('POST revelado hasta '+utc(end) if reveal else 'POST oculto')+
               ' · Las tres vistas utilizan únicamente velas cerradas dentro de su tramo.')
    st.dataframe(snapshot_table(bars,study['stages'],region,cutoff),**width)
    if stage_at(study['stages'],region,cutoff) is None:
        st.info('Todavía no había una etapa geométrica confirmada en este corte; se conservan los precios cerrados sin atribuirles una banda futura.')
    if reveal and end<cutoff+post_minutes*M:
        st.caption('El POST está recortado por el final de la fuente congelada.')
    for tf,minutes in [(15,local_hours*60),(60,h1_days*1440),(240,h4_days*1440)]:
        figure=build_figure(bars[tf],study['stages'],study['events'],region=region,episode=ep,
            cutoff=cutoff,end=end,tf_minutes=tf,lookback_minutes=minutes,origin_side=ep['band_origin_side'],
            compare_fixed=compare,details=details,selected_transition_id=tid)
        st.plotly_chart(figure,**width,key=PREFIX+'chart_'+str(tf),
                        config={'displaylogo':False,'scrollZoom':True})
    st.markdown('##### Cronología conocida en el tramo visible')
    first=cutoff-max(local_hours*60,h1_days*1440)*M
    timeline=visible_events(study['events'],region,first,end,episode=None if history else eid,details=details)
    shown=timeline_table(timeline,cutoff)
    if shown.empty:st.info('No hay eventos confirmados en el tramo visible.')
    else:
        st.dataframe(shown,**width)
        st.download_button('Descargar cronología visible',shown.to_csv(index=False).encode('utf-8-sig'),
            file_name='multiswing_v13_cronologia_visible.csv',mime='text/csv',key=PREFIX+'download_timeline')
    with st.expander('Relación entre eventos 15m y 1h del episodio'):
        links=visible_links(study['links'],eid,end)
        links=links.loc[links.link_status.isin(['LAGGED_CONFIRMATION','SAME_CLOSE'])]
        display=[]
        for r in links.itertuples(index=False):
            display.append({'Evento':event_label(r.event_type,'' if pd.isna(r.direction) else str(r.direction)),
                'Cierre 15m UTC':utc(r.m15_confirmation_ts),'Cierre 1h UTC':utc(r.h1_confirmation_ts),
                'Retraso · minutos':int(r.lag_minutes),
                'Relación':'Mismo cierre' if r.link_status=='SAME_CLOSE' else 'Confirmación posterior',
                'Cambian los límites entre confirmaciones':bool(r.geometry_changed_between_confirmations)})
        if display:st.dataframe(pd.DataFrame(display),**width)
        else:st.caption('No hay un vínculo confirmado visible para este episodio.')
    if compare:
        try:
            audit=visible_comparison(load_comparison(root),eid,end)
            st.markdown('##### Referencia fija frente a geometría causal')
            display=pd.DataFrame({'Cierre UTC':audit.confirmation_ts.map(utc),'TF':audit.source_tf,
                'Etiqueta con referencia fija':audit.parent_event_type,'Etiqueta causal':audit.asof_event_type.fillna('—'),
                'Comparación':audit.comparison_status.map(STATUS_LABELS)})
            st.dataframe(display,**width)
        except (ValueError,FileNotFoundError,KeyError,OSError) as exc:
            st.warning('Comparación no disponible: '+str(exc))
    st.caption('La navegación recorre episodios históricos. 4h aporta contexto cerrado; los eventos de esta versión se calculan en 15m y 1h.')
    st.caption(f'Vista V1.3: {time.perf_counter()-started:.2f} s · datos calculados offline; sin reconstrucción de episodios al navegar.')
    return {'status':'OK','cutoff':cutoff,'visible_end':end,'post_revealed':bool(reveal),
            'episode_id':eid,'region_id':region,'visible_timeline_rows':len(shown)}
