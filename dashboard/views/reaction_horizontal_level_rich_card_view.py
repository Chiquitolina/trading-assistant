"""Replaces Horizontal Level Rich Card with minimalist Horizontal Level Research.

Single question: are REACTIONs near historically recognized/demonstrably defended
15m/30m levels different from others? No HL/LH, triangles or compression features.
Uses frozen Atlas V2 data and already selected First-Touch outcome, read-only.
"""
from __future__ import annotations

from pathlib import Path
import pandas as pd

from reaction_horizontal_level_research import (
    MODEL_VERSION, GROUPS, A, B, C, UNKNOWN, NEAR_ATR,
    build, important, outcome, group_summary, zone_rank, load_retest_candles,
)


def _pretty(x):
    try:
        return f'{float(x):.6g}' if pd.notna(x) else '—'
    except (ValueError,TypeError):
        return '—'


def render_horizontal_level_rich_card(source_events,project_root,*,tp_pct=1.0,
                                      sl_pct=0.5,outcome_horizon=180,
                                      entry_mode='OBSERVED_OPEN'):
    """Compatibility entrypoint: integration tab index remains untouched."""
    import streamlit as st
    import reaction_structural_level_atlas_v2 as atlas
    from dashboard.views.reaction_structural_level_atlas_v2_view import load, _read_structural_figure
    from dashboard.views.reaction_outcome_overlay import get_post_bars, overlay_future, outcome_explanation

    project=Path(project_root).resolve()
    folder=project/atlas.v1.lineage.ROOT_REL/atlas.OUTPUT
    st.markdown('### 🎯 Horizontal Level Research')
    st.caption('Una pregunta: ¿las REACTIONs que interactúan con niveles horizontales '
               'previamente reconocidos muestran mejores recorridos? '
               'Reutiliza Atlas V2 y First Touch; no usa HL/LH, presión ni compresiones.')
    if not (folder/'summary.json').is_file():
        st.warning('No existe la cohorte Atlas V2; ejecutá su auditoría antes de estudiar niveles.')
        return

    @st.cache_data(max_entries=3,show_spinner=False)
    def _read_audited(root,summary_mtime,events_mtime,levels_mtime,version):
        return load(Path(root))

    try:
        levels,events,summary=_read_audited(str(project),
             (folder/'summary.json').stat().st_mtime_ns,
             (folder/'event_map.csv').stat().st_mtime_ns,
             (folder/'candidate_levels.csv').stat().st_mtime_ns,MODEL_VERSION)
        needed=['event_key','outcome','entry_ts','entry_price','first_touch_minute']
        missing=[x for x in needed if x not in source_events.columns]
        if missing: raise ValueError('First Touch sin columnas: '+', '.join(missing))
    except Exception as exc:
        st.error(f'No se pudieron auditar las fuentes Atlas V2 / First Touch: {exc}')
        return

    controls=st.columns([1,1],gap='small')
    tf=controls[0].selectbox('Timeframe del nivel',('TODOS','15m','30m'),
        key='hlr_v1_tf')
    direction=controls[1].selectbox('Dirección',('TODAS','LONG','SHORT'),
        key='hlr_v1_side')
    try:
        # Validate exact 1m OHLC. Atlas V2 `known_touch` is one-sided and can
        # report false contact with levels many ATR away. Never reuse it.
        events_ohlc=load_retest_candles(project,events)
        categorized,qualifying=build(events_ohlc,levels,tf)
        chosen=source_events[needed].drop_duplicates('event_key').copy()
        chosen['outcome_norm']=chosen['outcome'].map(outcome)
        df=categorized.merge(chosen,on='event_key',how='inner',validate='one_to_one')
    except Exception as exc:
        st.error(f'No se pudo clasificar la cohorte causal: {exc}')
        return
    if direction!='TODAS':df=df[df.side.eq(direction)].copy()
    if df.empty:
        st.info('No hay REACTIONs del Atlas V2 en la cohorte y filtros actuales.');return

    valid=df.loc[df.level_group.ne(UNKNOWN)]
    k=st.columns(4)
    k[0].metric('REACTIONs',len(df))
    k[1].metric('A · Tocó',int(df.level_group.eq(A).sum()))
    k[2].metric('B · Cerca',int(df.level_group.eq(B).sum()))
    k[3].metric('C · Sin nivel cercano',int(df.level_group.eq(C).sum()))
    false_old=int(df.atlas_claimed_touch_not_verified.sum())
    if false_old:
        st.warning(f'**Auditoría Atlas V2:** {false_old} REACTIONs que el indicador anterior '
                   'consideraba contactos NO superan la prueba de intersección OHLC 1m. '
                   'Esta pestaña las reclasifica; no modifica los CSV históricos Atlas V2.')
    if df.level_group.eq(UNKNOWN).any():
        st.warning(f'{int(df.level_group.eq(UNKNOWN).sum())} eventos NO EVALUABLES por cobertura o '
                   'cierre de REACTION no verificable. No se incluyen en el grupo C.')

    st.caption('**Contacto REAL validado:** la vela 1m debe cruzar la zona: '
               '`high >= zone_low` Y `low <= zone_high`. Se ignora el `known_touch` '
               'unidireccional de Atlas V2. Las velas 1m ausentes son NO EVALUABLES.')
    st.caption(f'**Regla de importancia PRE congelada:** 2 pivotes 3×3 confirmados '
               f'+ al menos 1 episodio independiente de defensa previo, en 15m/30m. '
               f'**Cerca:** ≤{NEAR_ATR:g} ATR del último cierre HTF anterior. '
               'No se utiliza TP/SL para elegir niveles.')
    st.markdown('**Comparación principal · A / B / C**')
    overall=group_summary(df,min_resolved=30)
    order={GROUPS[A]:0,GROUPS[B]:1,GROUPS[C]:2,GROUPS[UNKNOWN]:3}
    if not overall.empty:
        overall=overall.sort_values('Grupo',key=lambda col:col.map(order).fillna(4))
        st.dataframe(overall,hide_index=True,use_container_width=True)
    st.caption(f'Resultados First Touch seleccionados arriba: {entry_mode}, '
               f'TP +{tp_pct:g}% / SL −{sl_pct:g}%, {outcome_horizon} minutos. '
               'Las tasas e IC Wilson solo se muestran con ≥30 resultados TP/SL; '
               'NO_HIT y SIN_RESULTADO no son pérdidas automáticas.')

    st.markdown('**Control temporal: cercanía conocida ANTES de la REACTION**')
    pre=df.copy()
    pre['pre_group']=pre.pre_bucket.map({'PRE_CERCA':'Nivel importante cerca (PRE)',
             'PRE_LEJOS_O_SIN_NIVEL':'Sin nivel importante cerca (PRE)',
             'NO_EVALUABLE':'Sin cobertura suficiente'})
    pre_summary=group_summary(pre,col='pre_group',min_resolved=30)
    if not pre_summary.empty:
        st.dataframe(pre_summary,hide_index=True,use_container_width=True)
    st.caption('La cercanía PRE sí era conocida antes de la REACTION. '
               'En cambio, A (tocó) y B (no tocó) usan la vela 1m de REACTION '
               'ya cerrada: sus tasas son descriptivas y NO prueban una entrada al open.')

    with st.expander('Auditoría simple · dirección, defensa previa y placebo',expanded=False):
        st.markdown('**Resultados separados por LONG / SHORT (no mezcla de direcciones)**')
        subtables=[]
        for side,part in df.groupby('side'):
            out=group_summary(part,min_resolved=30)
            if not out.empty:
                out.insert(0,'Dirección',side)
                subtables.append(out)
        if subtables:
            st.dataframe(pd.concat(subtables,ignore_index=True),hide_index=True,use_container_width=True)
        st.caption('Episodios independientes y dos pivotes anteriores determinan importancia. '
                   'No se suman velas consecutivas como defensas independientes.')
        diag=valid[valid.important_levels.gt(0)]
        if not diag.empty:
            n=len(diag)
            real=int(diag.pre_bucket.eq('PRE_CERCA').sum())
            fake=int(diag.placebo_near_pre.eq(True).sum())
            st.caption(f'**Placebo de proximidad (diagnóstico PRE, N={n}):** '
                       f'zonas reales cerca: {real}/{n}; zonas trasladadas ±3 ATR cerca: {fake}/{n}. '
                       'Este placebo NO simula contactos ni operaciones ficticias; '
                       'no sustituye eventos sin REACTION ni validación futura.')
        else:
            st.caption('Sin niveles importantes elegibles para la comprobación placebo.')
        st.caption('Muestra explorada retrospectivamente, no validación fuera de muestra. '
                   'Cualquier corte futuro debe evaluarse sobre fechas aún no inspeccionadas.')

    st.markdown('**Inspector · una REACTION y su desenlace (nivel solo si A/B)**')
    group_option=st.selectbox('Interacción con nivel importante',
        ('TODOS',A,B,C,UNKNOWN),
        format_func=lambda x:'Todos los grupos' if x=='TODOS' else GROUPS[x],
        key='hlr_v1_group')
    inspect=df if group_option=='TODOS' else df.loc[df.level_group.eq(group_option)]
    result=st.selectbox('Resultado (solo filtra el inspector)',
        ('TODOS','TP_FIRST','SL_FIRST','NO_HIT','AMBIGUOUS','SIN_RESULTADO'),
        key='hlr_v1_result')
    if result!='TODOS': inspect=inspect.loc[inspect.outcome_norm.eq(result)]
    if inspect.empty:
        st.info('No hay eventos para el filtro del inspector.');return
    recent=inspect.sort_values('retest_ts',ascending=False)
    labels={r.event_key:f'{r.symbol} {r.side} · {r.date_utc} · '
            f'{GROUPS[r.level_group]} · {r.outcome_norm}'
            for r in recent.itertuples(index=False)}
    key=st.selectbox('Abrir REACTION',list(labels),format_func=lambda k:labels[k],
                     key='hlr_v1_event')
    item=recent.loc[recent.event_key.eq(key)].iloc[0].copy()
    st.markdown(f'#### {item.symbol} {item.side} · {GROUPS[item.level_group]}')
    if pd.notna(item.relevant_zone_id):
        st.caption('Nivel PRE: '+str(item.relevant_timeframe)+' · zona '+
             _pretty(item.relevant_zone_low)+'–'+_pretty(item.relevant_zone_high)+
             ' · '+_pretty(item.relevant_gap_atr)+' ATR del cierre PRE · '+
             str(int(item.relevant_pivots))+' pivotes confirmados · '+
             str(int(item.relevant_episodes))+' episodios independientes de defensa antes de la señal.')

    else:
        if item.level_group==C:
            st.info('No hay nivel importante dentro de 0,50 ATR de la vela 1m de REACTION. '
                    'No dibujamos zonas lejanas para evitar distorsionar la escala.')
        else:
            st.info('No existe contacto verificable o falta cobertura 1m; no se inventa un nivel cercano.')
    if item.level_group in (A,B):
        st.caption('La etiqueta de contacto se conoce SOLO al cierre de la vela de REACTION. '
                   'Si se opera basándose en ella, la entrada debe re-simularse después.')

    # Plot ONLY one selected verified level. No HL/LH, no other decorative zones.
    candidate=qualifying.loc[qualifying.event_key.eq(key)]
    selected_zone=candidate.loc[candidate.zone_id.eq(item.relevant_zone_id)]
    if not selected_zone.empty:
        single=selected_zone.iloc[[0]].copy()
        item['primary_zone_id']=single.iloc[0].zone_id
        item['primary_timeframe']=single.iloc[0].timeframe
    else:
        single=candidate.iloc[:0].copy()
        item['primary_zone_id']=None
        item['primary_timeframe']='15m'
    l,r=st.columns([1,1])
    shown_tf=l.radio('Velas anteriores',['15m','30m'],
                  index=0 if str(item.primary_timeframe)=='15m' else 1,
                  horizontal=True,key='hlr_v1_tf_chart')
    post_m=r.select_slider('Velas POST 1m (solo gráfico)',options=[15,30,60,180,360],
                           value=180,key='hlr_v1_post')
    try:
        plot_zone=single.copy()
        if not plot_zone.empty and str(item.relevant_timeframe)!=shown_tf:
            # Visually project the SAME frozen level onto the alternate TF;
            # do not reassign its source/confirmation timeframe.
            plot_zone['timeframe']=shown_tf
            st.caption(f'Zona originada y confirmada en {item.relevant_timeframe}, '
                       f'proyectada sobre velas {shown_tf} (mismo precio).')
        fig=_read_structural_figure(project,item,plot_zone,display_tf=shown_tf)
        if fig is None:
            st.warning('No existe histórico PRE suficiente para dibujar este evento.');return
        try:
            from dashboard.views.reaction_horizontal_rich_chart import enhance_pre_candles
        except ImportError:
            st.caption('Helper HTF relleno no disponible; se muestran velas estándar.')
        else:
            fig,_=enhance_pre_candles(fig,shown_tf,show_higher_lows=False)
        bars,path_state=get_post_bars(project,item.event_key,item.retest_ts)
        if bars:
            fig=overlay_future(fig,bars,retest_ts=item.retest_ts,side=item.side,
               entry_ts=item.entry_ts,entry_price=item.entry_price,
               outcome=item.outcome_norm,first_touch_minute=item.first_touch_minute,
               tp_pct=tp_pct,sl_pct=sl_pct,outcome_horizon=outcome_horizon,
               display_minutes=post_m,pre_hours=12)
        st.plotly_chart(fig,use_container_width=True,key='hlr_v1_plot')
        st.caption(outcome_explanation(item.outcome_norm,item.first_touch_minute,
               tp_pct=tp_pct,sl_pct=sl_pct,outcome_horizon=outcome_horizon,
               display_minutes=post_m,path_state=path_state,entry_mode=entry_mode))
        st.caption('Gráfico: solo zonas A/B con contacto/proximidad 1m comprobada; '
                   'cierre de REACTION (KNOWN), trayectoria POST (solo evaluación). '
                   'No se dibujan niveles distantes como si fueran soporte/resistencia local.')
    except Exception as exc:
        st.error(f'Inspector: no se pudo reconstruir el gráfico: {exc}')

    with st.expander('Exportar la muestra clasificada (sin recalcular señales)',expanded=False):
        cols=['event_key','symbol','side','date_utc','level_group','pre_bucket',
              'important_levels','relevant_zone_id','relevant_timeframe','relevant_gap_atr',
              'relevant_episodes','relevant_pivots','known_touch_verified',
              'known_low','known_high','relevant_retest_gap_atr',
              'atlas_claimed_touch','atlas_claimed_touch_not_verified','outcome_norm']
        st.download_button('Descargar CSV de investigación',
              df[cols].to_csv(index=False).encode('utf-8'),
              file_name='horizontal_level_research_v1.csv',mime='text/csv',
              key='hlr_v1_export')
