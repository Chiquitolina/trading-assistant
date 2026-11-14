"""Read-only execution inspector; UI 0.1.1 adds a POST-blind PRE review batch."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import pandas as pd

import btc_multiswing_execution_replay_v01 as replay
from dashboard.views import reaction_multiswing_behavior_v13_view as structural

PREFIX='ms60_execution_v01_'
POST_KEY=structural.POST_KEY
DEFENSE_CASE='msv2_328a9f856221c0e15000|V13|1789391160000'
FILES=('execution_events_v01.csv','trigger_candidates_v01.csv','entry_price_references_v01.csv',
       'trigger_path_outcomes_v01.csv','visit_health_v01.csv')
LABELS={'CONTACT':'Contacto 1m','DEFENSE_CONFIRMED':'Defensa / rechazo 5m',
        'BREAK_UP_CONFIRMED':'Ruptura arriba · 2 cierres 5m','BREAK_DOWN_CONFIRMED':'Ruptura debajo · 2 cierres 5m',
        'EXIT_CONFIRMED':'Salida desde origen interior · 5m','RETEST_CONTACT':'Retest observado 1m',
        'RETEST_HOLD_CONFIRMED':'Retest sostenido · 2 velas 5m nuevas','BREAK_FAILED':'Ruptura fallida',
        'DEFENSE_FAILED':'Defensa fallida','EXIT_REVERSED':'Salida revertida','EXIT_FAILED_RETURN_INSIDE':'Salida fallida · regreso dentro',
        'REENTER_BAND':'Reingreso dentro','ACCEPT_INSIDE':'Aceptación dentro · 3 cierres 5m',
        'ACCEPT_OUTSIDE':'Permanencia exterior · sin retest','GEOMETRY_REVALIDATE':'Geometría revisada · cadena reiniciada'}
FAMILIES={'DEFENSE':'Defensa / rechazo','BREAK_DIRECT':'Ruptura directa','BREAK_RETEST':'Ruptura + retest sostenido'}
REVIEW_SIZE=12
REVIEW_COLUMNS=('event_id','region_id','pre_cutoff_ts','flip_direction','reaction_pre',
                'band_age_hours','prior_h1_defenses','max_separation_pct','pre_position')


def reference_visit_ids():
    return {replay.SELECTED_CASE,DEFENSE_CASE}|{value[2] for value in structural.BOOKMARKS.values()}


def build_review_batch(eligible, *, excluded_regions=(), size=REVIEW_SIZE):
    """Sample the frozen PRE catalogue by date rank, never by later behavior.

    Keep the first eligible visit per band, exclude entire reviewed bands,
    and spread the sample over the available date range. This is a
    retrospective review sample, not an online eligibility rule.
    """
    if not isinstance(size,int) or isinstance(size,bool) or size<=0:
        raise ValueError('El tamaño del lote debe ser un entero positivo')
    required={'event_id','region_id','pre_cutoff_ts'}
    if not required.issubset(eligible.columns):
        raise ValueError('Faltan identificadores PRE para construir el lote')
    frame=eligible.loc[:,[c for c in REVIEW_COLUMNS if c in eligible.columns]].copy()
    frame=frame.loc[~frame.region_id.isin(set(excluded_regions))]
    frame=frame.sort_values(['pre_cutoff_ts','event_id']).drop_duplicates('region_id',keep='first')
    count=min(size,len(frame))
    positions=[0] if count==1 else [i*(len(frame)-1)//(count-1) for i in range(count)]
    result=frame.iloc[positions].reset_index(drop=True)
    result.insert(0,'review_order',range(1,len(result)+1))
    return result


def review_sheet(batch,visits):
    """Only whitelisted PRE metadata and empty, manually completed fields."""
    meta=visits[['event_id','band_origin_side','approach_position']]
    frame=batch.merge(meta,on='event_id',how='left',validate='one_to_one')
    sides={'ABOVE':'Arriba','BELOW':'Abajo','INSIDE':'Dentro','UNKNOWN':'Desconocido'}
    sheet=pd.DataFrame({'Caso':frame.review_order,'Llegada UTC':frame.pre_cutoff_ts.map(replay.utc),
        'Origen de banda':frame.band_origin_side,'Llegada desde PRE':frame.approach_position.map(sides),
        'Banda ID':frame.region_id,'Visita ID':frame.event_id,
        'Cambio de función PRE':frame.flip_direction.map(structural.FLIP_LABELS),
        'Antigüedad PRE horas':frame.band_age_hours.round(1),
        'Defensas previas 1h':frame.prior_h1_defenses,
        'Alejamiento previo %':frame.max_separation_pct.round(3)})
    for name in ('Lectura PRE (manual)','Familia confirmada (manual)',
                 'Hipótesis LONG-SHORT (manual)','Invalidación (manual)','Observación POST (manual)'):
        sheet[name]=''
    return sheet


@lru_cache(maxsize=2)
def _load(root_string,token):
    root=Path(root_string);folder=root/replay.DEFAULT_OUTPUT
    manifest=replay.verified_output(folder)
    for name,expected in manifest['input_sha256'].items():
        if replay.digest(root/name)!=expected:raise ValueError('La fuente del run cambió: '+name)
    data=replay.load_inputs(root)
    frames={name:pd.read_csv(folder/name,low_memory=False) for name in FILES}
    frames['events']=frames[FILES[0]];frames['triggers']=frames[FILES[1]]
    frames['references']=frames[FILES[2]];frames['outcomes']=frames[FILES[3]];frames['health']=frames[FILES[4]]
    return dict(**frames,manifest=manifest,study=data)


def load_execution(project):
    root=Path(project).resolve()
    paths=[replay.DEFAULT_OUTPUT/'manifest.json']+[replay.DEFAULT_OUTPUT/name for name in FILES]
    paths += list(replay.input_paths(root))
    return _load(str(root),structural.signature(root,[Path(p) for p in paths]))


def visible_payload(data,visit_id,end):
    """Filter BEFORE merging references or rendering any table/download."""
    events=data['events'].loc[data['events'].visit_id.eq(visit_id)&data['events'].available_ts.le(int(end))].copy()
    triggers=data['triggers'].loc[data['triggers'].visit_id.eq(visit_id)&data['triggers'].available_ts.le(int(end))].copy()
    refs=data['references'].loc[data['references'].visit_id.eq(visit_id)&data['references'].reference_open_ts.notna()
                                &data['references'].reference_open_ts.le(int(end))].copy()
    outcomes=data['outcomes'].loc[data['outcomes'].visit_id.eq(visit_id)&data['outcomes'].evaluation_available_ts.notna()
                                &data['outcomes'].evaluation_available_ts.le(int(end))].copy()
    outcomes=outcomes.loc[outcomes.event_id.isin(triggers.event_id)]
    return events,triggers,refs,outcomes


def blind_navigation(state,token):
    token=tuple(token)
    changed=state.get(PREFIX+'navigation')!=token
    if changed:state[POST_KEY]=False
    state[PREFIX+'navigation']=token
    return changed


def execution_figure(bars,stages,events,*,region,cutoff,end,start,tf,origin_side):
    b=bars.loc[bars.closed_ts.gt(int(start))&bars.closed_ts.le(int(end))]
    selected=events.loc[events.available_ts.gt(int(start))&events.available_ts.le(int(end))]
    x,low,high=structural.band_points(stages,region,int(start),int(end))
    color='#48c7aa' if origin_side=='LOW' else '#e3af59'
    data=[]
    if x:
        data.extend([dict(type='scatter',x=x,y=low,mode='lines',name='Geometría as-of',legendgroup='band',showlegend=False,
                          line=dict(color=color,width=1,shape='hv'),hoverinfo='skip'),
                     dict(type='scatter',x=x,y=high,mode='lines',name='Geometría as-of',legendgroup='band',
                          line=dict(color=color,width=1,shape='hv'),fill='tonexty',fillcolor='rgba(120,164,190,.12)',hoverinfo='skip')])
    data.append(dict(type='candlestick',x=structural.iso_values(b.closed_ts.to_numpy()),open=b.open.tolist(),high=b.high.tolist(),
        low=b.low.tolist(),close=b.close.tolist(),name=f'{tf}m',showlegend=False,
        increasing=dict(line=dict(color='#45bc9b'),fillcolor='#45bc9b'),decreasing=dict(line=dict(color='#ee7477'),fillcolor='#ee7477')))
    styles={'CONTACT':('circle','#b8c9d8'),'DEFENSE_CONFIRMED':('diamond','#64d5ad'),
            'BREAK_UP_CONFIRMED':('triangle-up','#ffd166'),'BREAK_DOWN_CONFIRMED':('triangle-down','#ffd166'),
            'RETEST_CONTACT':('circle-open','#efb66b'),'RETEST_HOLD_CONFIRMED':('square','#79dcae'),
            'BREAK_FAILED':('x','#f17e86'),'DEFENSE_FAILED':('x','#f17e86'),
            'GEOMETRY_REVALIDATE':('diamond-open','#a1a9bc')}
    for kind,g in selected.groupby('event_type',sort=False):
        symbol,tint=styles.get(kind,('circle','#a1bfd9'))
        text=[LABELS.get(kind,kind)+'<br>Disponible: '+replay.utc(r.available_ts)+
              '<br>'+str(r.direction)+'<br>Límites: '+f'{r.band_low:.2f}–{r.band_high:.2f}' for r in g.itertuples(index=False)]
        data.append(dict(type='scatter',x=structural.iso_values(g.available_ts.to_numpy()),y=g.confirmation_close.tolist(),
            mode='markers',name=LABELS.get(kind,kind),marker=dict(size=9,symbol=symbol,color=tint),text=text,
            hovertemplate='%{text}<br>Precio: %{y:.2f}<extra></extra>'))
    cut=structural.iso_values([cutoff])[0]
    shapes=[dict(type='line',xref='x',yref='paper',x0=cut,x1=cut,y0=0,y1=1,line=dict(color='#ffd166',dash='dash',width=1.2))]
    if end>cutoff:shapes.insert(0,dict(type='rect',xref='x',yref='paper',x0=cut,x1=structural.iso_values([end])[0],
        y0=0,y1=1,line=dict(width=0),fillcolor='rgba(125,153,205,.09)',layer='below'))
    return dict(data=data,layout=dict(title=dict(text=f'{tf}m · '+('contactos y secuencia' if tf==1 else 'gatillos con cierres nuevos'),x=.01),
        height=450,paper_bgcolor='#0c1320',plot_bgcolor='#121d2e',font=dict(color='#e6edf7',size=11),
        margin=dict(l=60,r=20,t=60,b=60),xaxis=dict(type='date',range=structural.iso_values([start,end]),rangeslider=dict(visible=False),
            title=dict(text='UTC · hora de cierre'),gridcolor='#27354a'),yaxis=dict(title=dict(text='USD'),gridcolor='#27354a'),
        shapes=shapes,legend=dict(orientation='h',y=1.08,x=.1),
        annotations=[dict(x=cut,y=1,yref='paper',text='Corte PRE',showarrow=False,xanchor='right',font=dict(color='#ffd166',size=10))],
        uirevision=f'exec|{region}|{cutoff}|{end}|{tf}|{start}'))


def render_execution_v01(project,st=None):
    if st is None:import streamlit as st
    root=Path(project).resolve();width=structural.width_options(st);state=st.session_state
    st.markdown('#### Ejecución experimental · 1m / 5m · V0.1')
    try:data=load_execution(root)
    except FileNotFoundError:
        state[POST_KEY]=False
        st.info('El replay V0.1 todavía no está disponible. Ejecutá el plan y el run offline y actualizá la lectura.')
        st.code('python3 btc_multiswing_execution_replay_v01.py --plan\npython3 btc_multiswing_execution_replay_v01.py --run')
        if st.button('Actualizar replay',key=PREFIX+'refresh'):_load.cache_clear();st.rerun()
        return {'status':'RUN_NOT_AVAILABLE'}
    except (ValueError,KeyError,OSError) as exc:
        state[POST_KEY]=False;st.error('No se pudo verificar el replay: '+str(exc));return {'status':'INVALID_RUN'}
    study=data['study'];all_visits=study['visits'];visits=all_visits.copy();batch=None
    reference_ids=reference_visit_ids()
    references=all_visits.loc[all_visits.event_id.isin(reference_ids)]
    scope=structural._safe_select(st,'Casos de ejecución',('REFERENCES','REVIEW_PRE','FLIPS_PRE','ALL'),PREFIX+'scope','REFERENCES',
        lambda x:{'REFERENCES':f'Ejemplos de auditoría · {len(references)} seleccionados',
                  'REVIEW_PRE':'Lote PRE · 12 casos adicionales',
                  'FLIPS_PRE':'Flips con historia · filtro PRE inicial',
                  'ALL':f'Todas las llegadas BTC · {len(all_visits)} visitas'}[x])
    if scope=='REFERENCES':
        visits=references.copy()
        st.info('Estos ejemplos fueron seleccionados para auditar la lógica; no son todos los casos encontrados. '
                'Para ampliar, elegí el lote PRE o los flips con historia.')
    elif scope in ('REVIEW_PRE','FLIPS_PRE'):
        eligible=structural.filter_flip_catalog(structural.load_flip_catalog(root))
        if scope=='REVIEW_PRE':
            batch=build_review_batch(eligible,excluded_regions=references.region_id)
            visits=visits.loc[visits.event_id.isin(batch.event_id)].copy()
            st.caption('Una visita por banda, excluyendo las cinco bandas ya revisadas. Reparto por fecha '
                       'dentro del catálogo PRE congelado; el resultado posterior no interviene en la selección.')
            sheet=review_sheet(batch,all_visits)
            with st.expander('Lote PRE · lista y ficha de revisión'):
                st.dataframe(sheet[['Caso','Llegada UTC','Origen de banda','Llegada desde PRE',
                                    'Antigüedad PRE horas','Defensas previas 1h']],**width)
                st.download_button('Descargar ficha del lote PRE',sheet.to_csv(index=False).encode('utf-8-sig'),
                    file_name='LOTE_PRE_12_BTC_REVISION.csv',mime='text/csv',key=PREFIX+'download_review')
        else:visits=visits.loc[visits.event_id.isin(eligible.event_id)].copy()
    visits=visits.sort_values(['pre_cutoff_ts','event_id'])
    st.caption(f'Mostrando {len(visits)} visitas a {visits.region_id.nunique()} bandas. '
               f'Replay completo BTC: {len(all_visits)} visitas a {all_visits.region_id.nunique()} bandas.')
    st.caption('HIGH / LOW indica el origen de la banda. Las reglas operativas LONG / SHORT todavía no están cerradas.')
    visitmap=visits.set_index('event_id').to_dict('index')
    review_numbers={} if batch is None else batch.set_index('event_id').review_order.to_dict()
    vid=structural._safe_select(st,'Llegada para estudiar',visits.event_id,PREFIX+'visit',DEFENSE_CASE,
        lambda x:(f'Caso {review_numbers[x]}/{len(batch)} · ' if batch is not None else '')+
                  replay.utc(visitmap[x]['pre_cutoff_ts'])+' · Origen: '+visitmap[x]['band_origin_side']+
                  f" · visita {int(visitmap[x]['visit_number'])} · "+visitmap[x]['region_id'])
    if vid is None:state[POST_KEY]=False;st.info('No hay visitas bajo este filtro.');return {'status':'NO_VISITS'}
    visit={**visitmap[vid],'event_id':vid};region=visit['region_id'];arrival=int(visit['pre_cutoff_ts'])
    if state.get(PREFIX+'last_case')!=(scope,vid):
        state[POST_KEY]=False;state[PREFIX+'cut']='ARRIVAL';state[PREFIX+'last_case']=(scope,vid)
    nav=structural._safe_select(st,'Corte del replay',('ARRIVAL','BEFORE_TRIGGER','AT_TRIGGER'),PREFIX+'cut','ARRIVAL',
        lambda x:{'ARRIVAL':'Antes del minuto de llegada','BEFORE_TRIGGER':'Antes de la vela que confirma un gatillo',
                  'AT_TRIGGER':'Al confirmar un gatillo'}[x])
    trigger_id=''
    if nav=='ARRIVAL':cutoff=arrival
    else:
        candidates=data['triggers'].loc[data['triggers'].visit_id.eq(vid)].sort_values(['available_ts','event_id'])
        cmap=candidates.set_index('event_id').to_dict('index');numbers={x:i+1 for i,x in enumerate(candidates.event_id)}
        trigger_id=structural._safe_select(st,'Vela para inspección retrospectiva',candidates.event_id,PREFIX+'trigger',
            format_func=lambda x:f"#{numbers[x]} · 5m abre "+replay.utc(cmap[x]['bar_open_ts']))
        if trigger_id is None:
            state[POST_KEY]=False;st.info('El run no contiene gatillos para esta visita. Volvé al corte de llegada para inspeccionar su recorrido.')
            return {'status':'NO_TRIGGERS'}
        cutoff=int(cmap[trigger_id]['bar_open_ts' if nav=='BEFORE_TRIGGER' else 'available_ts'])
    a,b=st.columns(2)
    with a:history1=structural._safe_select(st,'Historia 1m · minutos',(60,180,360),PREFIX+'history1',180)
    with b:history5=structural._safe_select(st,'Historia 5m · horas',(6,12,24),PREFIX+'history5',24)
    blind_navigation(state,(scope,vid,nav,trigger_id,cutoff,history1,history5))
    a,b=st.columns(2)
    with a:reveal=st.checkbox('Revelar POST de ejecución',key=POST_KEY,value=False)
    with b:post=structural._safe_select(st,'POST de ejecución · minutos',(30,60,180,360,720),PREFIX+'post',180)
    end=structural.visible_end(cutoff,study['source']['end'],reveal,post)
    events,triggers,refs,outcomes=visible_payload(data,vid,end)
    st.caption('Llegada: '+replay.utc(arrival)+' · corte: '+replay.utc(cutoff)+' · '+('POST hasta '+replay.utc(end) if reveal else 'POST oculto')+
        f". El run busca gatillos durante {data['manifest']['config']['horizon_minutes']} minutos desde cada llegada.")
    st.caption('Dos cierres 5m nuevos; el retest exige separación y regreso. Su sostén exige dos velas completas posteriores. '
               'Se muestran gatillos experimentales y referencias de precio; salidas, tamaño y costes todavía no están configurados.')
    st.caption('El flip anterior queda como contexto. Una nueva prueba desde arriba o abajo puede activar defensa/rechazo; '
               'la familia ruptura + retest exige observar esa ruptura y su regreso dentro de la ventana del replay.')
    for col,(family,label) in zip(st.columns(3),FAMILIES.items()):
        q=triggers.loc[triggers.family.eq(family)]
        with col:
            st.markdown('**'+label+'**')
            st.caption('Sin gatillo confirmado en lo visible' if q.empty else
                       'Último: '+replay.utc(q.available_ts.max())+f' · {len(q)} registrado(s)')
    market=study['market']
    minute=market.frame.assign(closed_ts=market.closes)
    five=replay.closed_bars(market.frame,5,int(market.opens[0]),int(market.closes[-1]))
    for tf,frame,lookback in [(1,minute,history1),(5,five,history5*60)]:
        fig=execution_figure(frame,study['stages'],events,region=region,cutoff=cutoff,end=end,
                             start=cutoff-int(lookback)*replay.M,tf=tf,origin_side=visit['band_origin_side'])
        st.plotly_chart(fig,**width,key=PREFIX+'chart'+str(tf),config={'displaylogo':False,'scrollZoom':True})
    with st.expander('Contexto estructural · 15m / 1h / 4h'):
        bars,_=structural.load_prices(root,structural.load_index(root))
        ep=structural.load_index(root)['episodes'].set_index('episode_id').loc[visit['episode_id']].to_dict()
        ep['episode_id']=visit['episode_id']
        for tf,lookback in [(15,24*60),(60,7*1440),(240,14*1440)]:
            fig=structural.build_figure(bars[tf],study['stages'],study['legacy'],region=region,episode=ep,
                cutoff=cutoff,end=end,tf_minutes=tf,lookback_minutes=lookback,origin_side=visit['band_origin_side'])
            st.plotly_chart(fig,**width,key=PREFIX+'context'+str(tf),config={'displaylogo':False})
        st.caption('Estas marcas mantienen las definiciones estructurales V1.3. Los gatillos 5m se muestran aparte.')
    st.markdown('##### Secuencia conocida de esta visita')
    if events.empty:st.caption('Todavía no hay eventos posteriores a la llegada dentro del corte visible.')
    else:
        shown=pd.DataFrame({'Disponible UTC':events.available_ts.map(replay.utc),'Evento':events.event_type.map(LABELS),
                           'Lado':events.direction,'Cierre':events.confirmation_close,'Límite inferior':events.band_low,'Límite superior':events.band_high})
        st.dataframe(shown,**width)
        st.download_button('Descargar eventos visibles',events.to_csv(index=False).encode('utf-8-sig'),
            file_name='execution_v01_eventos_visibles.csv',mime='text/csv',key=PREFIX+'download_events')
    with st.expander('Disponibilidad del gatillo y referencia posterior de precio'):
        if triggers.empty:st.caption('No hay gatillos confirmados en el tramo visible.')
        else:
            joined=triggers.merge(refs[['event_id','reference_open_ts','reference_open_price']],on='event_id',how='left')
            shown=pd.DataFrame({'Familia':joined.family.map(FAMILIES),'Gatillo UTC':joined.available_ts.map(replay.utc),
                'Referencia UTC':joined.reference_open_ts.map(lambda x:replay.utc(x) if pd.notna(x) else 'Todavía no disponible'),
                'Precio de referencia':joined.reference_open_price})
            st.dataframe(shown,**width)
            st.caption('Apertura 1m estrictamente posterior al gatillo. Es una referencia OHLC, no un fill garantizado ni PnL.')
    with st.expander('Recorrido bruto posterior · solo horizontes ya observados'):
        if outcomes.empty:st.caption('No hay horizontes completos disponibles dentro del corte visible.')
        else:
            st.dataframe(outcomes[['family','horizon_minutes','mfe_pct','mae_pct','gross_markout_pct']].assign(family=lambda f:f.family.map(FAMILIES)).rename(columns={
                'family':'Familia','horizon_minutes':'Horizonte · min','mfe_pct':'Excursión favorable %',
                'mae_pct':'Excursión adversa %','gross_markout_pct':'Cambio direccional bruto %'}),**width)
            st.caption('Descriptores del recorrido desde la referencia: no incluyen salidas, costes ni resultado neto de una operación.')
    health=data['health'].loc[data['health'].visit_id.eq(vid)].iloc[0]
    if end>=int(health.observed_until_ts) and bool(health.right_censored):
        st.info('Ventana incompleta/censurada: '+str(health.terminal_reason))
    return dict(status='OK',visit_id=vid,scope=scope,case_count=len(visits),band_count=visits.region_id.nunique(),
                cutoff=cutoff,visible_end=end,post_revealed=bool(reveal),
                visible_events=len(events),visible_triggers=len(triggers),visible_references=len(refs),visible_outcomes=len(outcomes))
