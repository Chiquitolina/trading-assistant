"""Principal Level Reaction Filter for the frozen Structural Level Atlas V2.

Research-only UI. It never recomputes levels from outcomes and never writes
historical databases. Selection is based exclusively on V2 PRE fields;
First Touch outcomes are attached to the fixed selected events afterwards.
"""
from __future__ import annotations

from pathlib import Path
from math import sqrt

import pandas as pd

FAVOURABLE_FAMILIES = (
    'NEAR_ACCEPTED_DEFENDED', 'NEAR_RECLAIM_ACCEPTED',
)
# Prespecified exploratory bins. Do not optimize with the inspected TP/SL data.
DISTANCE_OPTIONS = (
    'TODAS', 'DENTRO_0_0.25_ATR', 'CERCA_0.25_0.50_ATR',
    'CERCA_HASTA_0.50_ATR', 'CONTEXTO_0.50_1.50_ATR',
    'DISTANTE_MAS_1.50_ATR', 'SIN_MEDICION',
)
SPACE_OPTIONS = (
    'TODOS', 'MENOS_0.50_PCT', 'ENTRE_0.50_1_PCT',
    'ENTRE_1_2_PCT', 'MAS_2_PCT', 'SIN_OBSTACULO_VERIFICABLE',
)
REQUIRED = (
    'event_key', 'symbol', 'side', 'date_utc', 'retest_ts', 'pre_family',
    'primary_distance_atr', 'primary_timeframe', 'primary_zone_id',
    'space_to_opposition_pct', 'all_covered', 'outcome',
)


def prepare_filter_frame(events: pd.DataFrame) -> pd.DataFrame:
    """Prepare explanatory PRE-only bins, preserving every source event.

    Never use outcome in level bands or decision rules. Missing measurements
    remain missing; no obstacle found != ample free room.
    """
    missing=[c for c in REQUIRED if c not in events.columns]
    if missing:
        raise ValueError('Atlas V2: faltan columnas del mapa / replay: '+', '.join(missing))
    if events.event_key.isna().any() or events.event_key.duplicated().any():
        raise ValueError('Atlas V2: event_key nulo o duplicado; se bloquea el cruce')
    result=events.copy()
    distance=pd.to_numeric(result['primary_distance_atr'], errors='coerce')
    space=pd.to_numeric(result['space_to_opposition_pct'], errors='coerce')
    result['distance_bin']='SIN_MEDICION'
    good=distance.notna() & distance.ge(0)
    result.loc[good & distance.le(.25), 'distance_bin']='DENTRO_0_0.25_ATR'
    result.loc[good & distance.gt(.25) & distance.le(.50),'distance_bin']='CERCA_0.25_0.50_ATR'
    result.loc[good & distance.gt(.50) & distance.le(1.50),'distance_bin']='CONTEXTO_0.50_1.50_ATR'
    result.loc[good & distance.gt(1.50),'distance_bin']='DISTANTE_MAS_1.50_ATR'
    result['space_bin']='SIN_OBSTACULO_VERIFICABLE'
    valid=space.notna() & space.ge(0)
    result.loc[valid & space.lt(.50),'space_bin']='MENOS_0.50_PCT'
    result.loc[valid & space.ge(.50) & space.lt(1),'space_bin']='ENTRE_0.50_1_PCT'
    result.loc[valid & space.ge(1) & space.lt(2),'space_bin']='ENTRE_1_2_PCT'
    result.loc[valid & space.ge(2),'space_bin']='MAS_2_PCT'
    result['accepted_close_pre'] = (
        result.pre_family.isin(FAVOURABLE_FAMILIES)
        & good & distance.le(.50)
        & result.all_covered.eq(1)
    )
    return result


def filter_reactions(prepared: pd.DataFrame, *, distance='CERCA_HASTA_0.50_ATR',
                     families=None, side='TODAS', timeframe='TODOS',
                     space='TODOS') -> pd.DataFrame:
    """Only PRE attributes decide membership; never use outcome here."""
    z=prepared.loc[prepared['all_covered'].eq(1)].copy()
    if distance not in DISTANCE_OPTIONS:raise ValueError('Distancia inválida')
    if space not in SPACE_OPTIONS:raise ValueError('Espacio inválido')
    if distance=='CERCA_HASTA_0.50_ATR':
        z=z[z.distance_bin.isin(DISTANCE_OPTIONS[1:3])]
    elif distance!='TODAS':z=z[z.distance_bin.eq(distance)]
    if families is not None:
        z=z[z.pre_family.isin(families)]
    if side!='TODAS':
        if side not in ('LONG','SHORT'):raise ValueError('Dirección inválida')
        z=z[z.side.eq(side)]
    if timeframe!='TODOS':
        if timeframe not in ('15m','30m'):raise ValueError('TF inválido')
        z=z[z.primary_timeframe.eq(timeframe)]
    if space!='TODOS':z=z[z.space_bin.eq(space)]
    return z


def wilson(tp:int, n:int) -> tuple[float|None,float|None]:
    if n==0:return None,None
    p=tp/n;z=1.959963984540054
    den=1+z*z/n
    center=(p+z*z/(2*n))/den
    delta=z*sqrt((p*(1-p)+z*z/(4*n))/n)/den
    return 100*max(0,center-delta),100*min(1,center+delta)


def _one_summary(name:str, z:pd.DataFrame, min_n:int) -> dict:
    statuses=z.outcome.fillna('SIN_PATH')
    tp=int(statuses.eq('TP_FIRST').sum())
    sl=int(statuses.eq('SL_FIRST').sum())
    no_hit=int(statuses.eq('NO_HIT').sum())
    ambiguous=int(statuses.eq('AMBIGUOUS').sum())
    other=len(z)-tp-sl-no_hit-ambiguous
    n=tp+sl
    low,high=wilson(tp,n)
    return {
        'Grupo':str(name),'REACTIONs':int(len(z)),'TP_FIRST':tp,'SL_FIRST':sl,
        'NO_HIT':no_hit,'AMBIGUOUS':ambiguous,'Otros/sin path':int(other),
        'N resueltos':n,'TP resueltos (%)':round(100*tp/n,1) if n else None,
        'Wilson 95% mín':round(low,1) if low is not None else None,
        'Wilson 95% máx':round(high,1) if high is not None else None,
        'Muestra suficiente':bool(n>=min_n),
        'Días':int(z.date_utc.nunique()),'Símbolos':int(z.symbol.nunique()),
    }


def outcome_groups(prepared:pd.DataFrame, by:list[str], min_n:int=20) -> pd.DataFrame:
    """Descriptive only, preserve no-hits/missing outside the resolved denominator."""
    if not by:raise ValueError('Agrupación vacía')
    if any(c not in prepared.columns for c in by):raise KeyError(by)
    rows=[]
    for key,g in prepared.groupby(by,dropna=False,sort=True,observed=True):
        if not isinstance(key,tuple):key=(key,)
        label=' | '.join(str(value) for value in key)
        rows.append(_one_summary(label,g,min_n))
    return pd.DataFrame(rows,columns=list(_one_summary('',prepared.iloc[:0],min_n)))


def compare_to_base(prepared:pd.DataFrame,selected:pd.DataFrame,min_n:int=20)->pd.DataFrame:
    """Never double-count selected events in the comparison denominator."""
    if set(selected.event_key)-set(prepared.event_key):
        raise ValueError('Selección contiene eventos ajenos a la cohorte')
    other=prepared.loc[~prepared.event_key.isin(set(selected.event_key))]
    return pd.DataFrame([
        _one_summary('SELECCIÓN',selected,min_n),
        _one_summary('RESTO DE COHORTE',other,min_n),
        _one_summary('COHORTE EN FILTROS SUPERIORES',prepared,min_n),
    ])


def render_principal_level_filter(events,levels,project_root,*,tp_pct=1.0,sl_pct=0.5,
                                  outcome_horizon=180,entry_mode='OBSERVED_OPEN'):
    """Render inside an existing Atlas V2 Streamlit tab, with unique widget keys."""
    import streamlit as st
    from dashboard.views.reaction_outcome_overlay import (
        get_post_bars,overlay_future,outcome_explanation,
    )
    # Lazy import avoids circular module initialization when rendering V2.
    from dashboard.views.reaction_structural_level_atlas_v2_view import _read_structural_figure

    st.markdown('#### 🎯 Principal Level Reaction Filter')
    st.caption('Buscamos REACTIONs realmente cercanas al nivel principal VERDE ya congelado. '
               'La distancia se mide con el cierre PRE anterior al retest, NO con su mecha ni con precios POST. '
               'Los umbrales ATR son exploratorios, no optimizados.')
    try:prepared=prepare_filter_frame(events)
    except (ValueError,KeyError) as exc:
        st.error(str(exc));return
    if prepared.empty:
        st.info('No hay eventos del Atlas V2 dentro de los filtros superiores.');return
    unique_families=sorted(prepared.pre_family.dropna().unique().tolist())
    x=st.columns([1.25,1.35,1.0,1.0],gap='small')
    distance=x[0].selectbox('Distancia PRE al nivel verde',DISTANCE_OPTIONS,index=3,key='prlf_distance',
        format_func=lambda v:{
          'TODAS':'Todas las distancias','DENTRO_0_0.25_ATR':'Muy cerca (0–0,25 ATR)',
          'CERCA_0.25_0.50_ATR':'Cerca (0,25–0,50 ATR)',
          'CERCA_HASTA_0.50_ATR':'Hasta 0,50 ATR',
          'CONTEXTO_0.50_1.50_ATR':'Contexto (0,50–1,50 ATR)',
          'DISTANTE_MAS_1.50_ATR':'Distante (>1,50 ATR)',
          'SIN_MEDICION':'Sin distancia verificable',
        }[v])
    family_selection=x[1].multiselect('Familia estructural PRE',unique_families,
        default=[a for a in FAVOURABLE_FAMILIES if a in unique_families],
        key='prlf_families',help='Vacío equivale a todas las familias; no se selecciona por TP/SL.')
    side=x[2].selectbox('Dirección',('TODAS','LONG','SHORT'),key='prlf_side')
    timeframe=x[3].selectbox('TF del nivel principal',('TODOS','15m','30m'),key='prlf_tf')
    space=st.selectbox('Espacio hasta zona contraria (exploratorio; NO obligatorio)',
        SPACE_OPTIONS,key='prlf_space',format_func=lambda v:{
            'TODOS':'Sin filtrar espacio','MENOS_0.50_PCT':'Menos de 0,50%',
            'ENTRE_0.50_1_PCT':'0,50–1%', 'ENTRE_1_2_PCT':'1–2%',
            'MAS_2_PCT':'2% o más','SIN_OBSTACULO_VERIFICABLE':'Obstáculo no identificado',
        }[v])
    min_n=st.slider('Mínimo de casos resueltos para interpretar una tasa',5,100,20,5,
                    key='prlf_min_n')
    selected=filter_reactions(prepared,distance=distance,
          families=family_selection or None,side=side,timeframe=timeframe,space=space)
    a,b,c,d=st.columns(4)
    a.metric('REACTIONs filtradas',len(selected))
    b.metric('TP primero',int(selected.outcome.eq('TP_FIRST').sum()))
    c.metric('SL primero',int(selected.outcome.eq('SL_FIRST').sum()))
    d.metric('N resueltos',int(selected.outcome.isin(['TP_FIRST','SL_FIRST']).sum()))
    st.caption('Todas las tasas usan TP_FIRST / (TP_FIRST + SL_FIRST). '
               'NO_HIT y sin path no se consideran ni victorias ni pérdidas. '
               f'Escenario heredado: {entry_mode}, TP +{tp_pct:g}% / SL −{sl_pct:g}%, '
               f'horizonte {outcome_horizon}m. Mismo episodio puede generar múltiples señales correlacionadas.')
    st.markdown('**Filtro elegido frente al resto, sobre la MISMA cohorte superior**')
    st.dataframe(compare_to_base(prepared,selected,min_n),hide_index=True,use_container_width=True)
    if selected.empty:
        st.warning('No hay REACTIONs que cumplan esta combinación; quitá algún filtro.');return
    st.markdown('**Cruces para evitar conclusiones por dirección o por un solo día**')
    group_by=st.selectbox('Agrupar resultados por',(
        'distance_bin','pre_family','side','space_bin','primary_timeframe','date_utc'
    ),format_func=lambda s:{
        'distance_bin':'Distancia al nivel','pre_family':'Familia PRE',
        'side':'Dirección','space_bin':'Espacio contrario','primary_timeframe':'15m / 30m',
        'date_utc':'Fecha UTC'}[s],key='prlf_group')
    st.dataframe(outcome_groups(prepared,[group_by],min_n),hide_index=True,use_container_width=True)

    st.markdown('**Matriz: familia × cercanía al nivel (cohorte superior, sin filtrar familias)**')
    mat=outcome_groups(prepared,['pre_family','distance_bin'],min_n)
    if not mat.empty:
        st.dataframe(mat,hide_index=True,use_container_width=True,height=270)
    st.caption('Wilson 95% y “Muestra suficiente” son descriptivos. '
               'Explorar combinaciones en estos mismos diez días NO es validación independiente. '
               'El espacio contrario del Atlas V2 es preliminar y se calcula con cierres del timeframe de origen; '
               'no representa por sí solo espacio ejecutable desde OBSERVED_OPEN.')

    cols=['symbol','side','date_utc','primary_timeframe','pre_family',
          'primary_distance_atr','primary_distance_pct','primary_level_price',
          'space_to_opposition_pct','primary_known_touch','primary_known_hold',
          'outcome','event_key']
    cols=[x for x in cols if x in selected.columns]
    st.markdown('**REACTIONs que cumplen los filtros**')
    st.dataframe(selected[cols].sort_values(['date_utc','symbol'],ascending=[False,True]),
                 use_container_width=True,hide_index=True,height=260)
    st.caption('`primary_known_touch` y `primary_known_hold` describen la vela de 1m de la REACTION YA CERRADA; '
               'no se usan para seleccionar eventos ni para anticipar una entrada anterior a esa vela.')
    st.download_button('Exportar estas REACTIONs',selected.to_csv(index=False).encode('utf-8'),
        file_name='principal_level_reactions_filtrado.csv',mime='text/csv',key='prlf_export')

    st.markdown('#### Inspector: nivel principal + REACTION + desenlace POST')
    labels={r.event_key:f'{r.symbol} {r.side} · {r.date_utc} · {r.pre_family} · {r.outcome}'
            for r in selected.sort_values('retest_ts',ascending=False).itertuples(index=False)}
    pick=st.selectbox('Evento cercano al nivel',options=list(labels),
        format_func=lambda k:labels.get(k,k),key='prlf_selected_event')
    item=selected[selected.event_key==pick].iloc[0]
    z=levels[levels.event_key.eq(pick)]
    ts=item.primary_timeframe
    if pd.isna(ts) or str(ts) not in ('15m','30m'):ts='15m'
    tf=st.radio('Velas del mapa',['15m','30m'],index=0 if ts=='15m' else 1,
                horizontal=True,key='prlf_chart_tf')
    draw_post=st.select_slider('Velas posteriores en el mismo gráfico (min)',
                options=[15,30,60,180,360],value=180,key='prlf_display_minutes')
    try:
        fig=_read_structural_figure(Path(project_root),item,z,display_tf=tf,max_zones=4)
        bars,path_state=get_post_bars(Path(project_root),item.event_key,item.retest_ts)
        if fig is None:
            st.info('No hay velas estructurales disponibles para este evento.');return
        if bars:
            fig=overlay_future(fig,bars,retest_ts=item.retest_ts,side=item.side,
                entry_ts=getattr(item,'entry_ts',None),entry_price=getattr(item,'entry_price',None),
                outcome=getattr(item,'outcome',None),first_touch_minute=getattr(item,'first_touch_minute',None),
                tp_pct=tp_pct,sl_pct=sl_pct,outcome_horizon=outcome_horizon,
                display_minutes=draw_post,pre_hours=24)
        st.plotly_chart(fig,use_container_width=True,key='prlf_fig')
        st.info(outcome_explanation(getattr(item,'outcome',None),
                getattr(item,'first_touch_minute',None),tp_pct=tp_pct,sl_pct=sl_pct,
                outcome_horizon=outcome_horizon,display_minutes=draw_post,
                path_state=path_state,entry_mode=entry_mode))
    except Exception as exc:
        st.error('Error inspeccionando estructura/post: '+str(exc))
    st.caption('La REACTION se marca con el cierre de su vela 1m, posterior al instante PRE. '
               'TP/SL pertenecen al replay seleccionado y no modifican ni el nivel ni su clasificación previa. '
               'El triángulo NO es un fill ejecutado.')
