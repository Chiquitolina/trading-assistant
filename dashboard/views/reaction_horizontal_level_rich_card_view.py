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
    ROLE_VERSION, ROLE_LABELS, ROLE_REBOUND, ROLE_BREAK, ROLE_IN_ZONE,
    ROLE_NO_NEAR, ROLE_UNKNOWN, build_pre_roles,
)
from reaction_horizontal_lifecycle_meaning import (
    meaning_label as meaning_of_level_pre, origin_consistency as origin_consistency_pre,
)
from reaction_horizontal_level_lifecycle import (
    LABELS as LIFECYCLE_LABELS, VERSION as LIFECYCLE_VERSION, OUTPUT as LIFECYCLE_OUTPUT,
    ACTIVE as LEVEL_ACTIVE, LOST as LEVEL_LOST, FAILED as LEVEL_FAILED,
    RECLAIMED as LEVEL_RECLAIMED, UNKNOWN as LEVEL_UNKNOWN,
    NO_NEAR as LEVEL_NO_NEAR, read_checked_cache,
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
        # Independent causal PRE-only role; no retest 1m OHLC enters this computation.
        role_data=build_pre_roles(events,levels,tf)
        categorized=categorized.merge(role_data,on='event_key',how='left',validate='one_to_one')
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

    # Experimental structural role versus the direction of the REACTION.
    # This entire block is PRE causal and is disjoint from KNOWN A/B/C.
    st.markdown('**Nuevo experimento · rol PRE del nivel vs dirección de la REACTION**')
    st.caption('Se elige exclusivamente el nivel importante MÁS CERCANO al último cierre '
               '15m/30m anterior, siempre dentro de 0,50 ATR de su propio timeframe. '
               'LONG sobre soporte y SHORT bajo resistencia son rebotes compatibles; '
               'LONG bajo resistencia y SHORT sobre soporte requieren ruptura. '
               'Dentro de la zona no se asigna dirección. **No se usa la vela de '
               'REACTION ni el TP/SL para elegir el nivel o su rol.**')
    role_tab=df.assign(role_pre_label=df.pre_role.map(ROLE_LABELS))
    role_summary=group_summary(role_tab,col='role_pre_label',min_resolved=30)
    role_order={ROLE_LABELS[k]:i for i,k in enumerate(
        (ROLE_REBOUND,ROLE_BREAK,ROLE_IN_ZONE,ROLE_NO_NEAR,ROLE_UNKNOWN))}
    if not role_summary.empty:
        role_summary=role_summary.sort_values('Grupo',
            key=lambda col:col.map(role_order).fillna(5))
        st.dataframe(role_summary,hide_index=True,use_container_width=True)
    st.caption('Comparación descriptiva del escenario First Touch seleccionado arriba. '
               'NO_HIT, ambigüedades y SIN_RESULTADO no son SL ni integran la tasa entre '
               'resueltas; se informan por separado. No son retornos netos ni resultados '
               'independientes entre eventos del mismo símbolo.')
    # Independent chronological PRE level lifecycle. An offline cache is required
    # so the Streamlit renderer NEVER downloads prices or mutates Atlas/VPS data.
    st.markdown('**Nuevo estudio PRE · ¿nivel vigente, perdido, retesteado o recuperado?**')
    st.caption('Solo velas 15m/30m cerradas ANTES de la REACTION; zona congelada y '
               'mismo nivel elegido por el rol PRE. Dos cierres HTF consecutivos '
               'del otro lado confirman un cambio. Un retest fallido requiere una '
               'vela HTF POST-ruptura que toque la banda y cierre rechazándola. '
               'No usa el TP/SL para clasificar.')
    lifecycle_ready=False
    lifecycle_file=project/atlas.v1.lineage.ROOT_REL/LIFECYCLE_OUTPUT/'event_lifecycle.csv'
    lifecycle_summary=project/atlas.v1.lineage.ROOT_REL/LIFECYCLE_OUTPUT/'summary.json'
    if not lifecycle_file.is_file() or not lifecycle_summary.is_file():
        st.info('Falta estudio PRE: ejecutar `python3 reaction_horizontal_level_lifecycle.py --plan`, '
                '`--run` y `--audit` en la raíz del proyecto. El resto de la pestaña sigue disponible.')
    else:
        @st.cache_data(max_entries=6,show_spinner=False)
        def _read_lifecycle_cache(root,tf_filter,cache_mtime,meta_mtime,atlas_mtime,atlas_levels_mtime,version):
            # Load always against actual Atlas PRE event/zone selection (never TP/SL).
            lv,ev,_=load(Path(root))
            roles=build_pre_roles(ev,lv,tf_filter)
            return read_checked_cache(root,ev,roles,tf_filter)
        try:
            lifecycle=_read_lifecycle_cache(str(project),tf,
                lifecycle_file.stat().st_mtime_ns,lifecycle_summary.stat().st_mtime_ns,
                (folder/'event_map.csv').stat().st_mtime_ns,
                (folder/'candidate_levels.csv').stat().st_mtime_ns,LIFECYCLE_VERSION)
            lifecycle=lifecycle.rename(columns={'reason':'lifecycle_reason'})
            df=df.merge(lifecycle.drop(columns=['tf_filter','symbol','side','date_utc','retest_ts',
                       'role_pre','role_distance_atr','zone_low','zone_high','timeframe'],errors='ignore'),
                        on='event_key',how='left',validate='one_to_one')
            lifecycle_ready=True
            # Historical 'RECUPERADO' means a return to the initial pair-of-closes
            # side, not necessarily recovery of a previously lost SUPPORT.
            # Keep raw classification for inspectors, but split the table by
            # confirmed *side* so unlike transitions are not pooled as one.
            df['lifecycle_detail_label'] = [
                meaning_of_level_pre(st,side)
                for st,side in zip(df.lifecycle_state,df.initial_side)
            ]
            status_df=df.assign(lifecycle_label=df.lifecycle_detail_label)
            status_summary=group_summary(status_df,col='lifecycle_label',min_resolved=30)
            status_order={meaning_of_level_pre(st,side):i for i,(st,side) in enumerate(
                [(s,d) for s in (LEVEL_ACTIVE,LEVEL_LOST,LEVEL_FAILED,LEVEL_RECLAIMED)
                 for d in ('ABOVE','BELOW')] +
                [(LEVEL_UNKNOWN,None),(LEVEL_NO_NEAR,None)])}
            if not status_summary.empty:
                status_summary=status_summary.sort_values('Grupo',
                    key=lambda col:col.map(status_order).fillna(9))
                st.dataframe(status_summary,hide_index=True,use_container_width=True)
            st.caption('**Aclaración:** «regresó al lado inicial» no equivale necesariamente a '
                       'recuperar soporte. El lado inicial son los primeros dos cierres HTF '
                       'del mismo lado después de la formación, no el tipo de pivote de origen. '
                       'Las recuperaciones ahora se separan por ARRIBA/ABAJO; '
                       'esta lectura tampoco consulta resultados futuros.')
            st.caption('La pertenencia a las clases se determina sin consultar outcomes. '
                       'Los porcentajes mostrados son descriptivos; no justifican modificar el Scanner LIVE.')
            with st.expander('Auditar ciclo PRE · estado por LONG/SHORT y timestamps',expanded=False):
                dis=[]
                for side,part in status_df.groupby('side'):
                    tab=group_summary(part,col='lifecycle_label',min_resolved=30)
                    if not tab.empty:
                        tab.insert(0,'Dirección',side)
                        dis.append(tab)
                if dis:st.dataframe(pd.concat(dis,ignore_index=True),hide_index=True,use_container_width=True)
                st.caption('Perdido: lado opuesto al lado inicialmente aceptado. '
                           'Recuperado: vuelve al lado inicial con dos cierres confirmados. '
                           'Indeterminado: histórico o cierres insuficientes; no equivale a nivel vigente.')
        except Exception as exc:
            st.warning('Estudio de ciclo PRE sin auditar o desactualizado: '+str(exc)+
                       '. Ejecutar `python3 reaction_horizontal_level_lifecycle.py --run` y `--audit`.')

    with st.expander('Desglose causal · LONG / SHORT y timeframe del nivel',expanded=False):
        by_side=[]
        for side,part in role_tab.groupby('side'):
            table=group_summary(part,col='role_pre_label',min_resolved=30)
            if not table.empty:
                table.insert(0,'Dirección',side)
                by_side.append(table)
        if by_side:
            st.dataframe(pd.concat(by_side,ignore_index=True),hide_index=True,
                         use_container_width=True)
        st.caption('Un mismo evento tiene un único nivel PRE elegido por proximidad '
                   'normalizada; una zona originada en HIGH puede convertirse en soporte '
                   'tras un flip, por lo que NO se interpreta HIGH como resistencia eterna.')
        tf_rows=[]
        for tf_value,part in role_tab.loc[role_tab.role_timeframe.notna()].groupby('role_timeframe'):
            table=group_summary(part,col='role_pre_label',min_resolved=30)
            if not table.empty:
                table.insert(0,'TF del nivel',tf_value)
                tf_rows.append(table)
        if tf_rows:
            st.dataframe(pd.concat(tf_rows,ignore_index=True),hide_index=True,
                         use_container_width=True)
        st.caption('Si la muestra por grupo no alcanza 30 TP/SL resueltos, no se '
                   'muestra porcentaje ni Wilson; inspeccionar conteos y ampliar '
                   'la cohorte sin cambiar criterios.')

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

    st.markdown('**Inspector · una REACTION, el nivel PRE y su desenlace**')
    group_option=st.selectbox('Interacción con nivel importante',
        ('TODOS',A,B,C,UNKNOWN),
        format_func=lambda x:'Todos los grupos' if x=='TODOS' else GROUPS[x],
        key='hlr_v1_group')
    inspect=df if group_option=='TODOS' else df.loc[df.level_group.eq(group_option)]
    role_option=st.selectbox('Rol del nivel ANTES de la señal (solo inspector)',
        ('TODOS',ROLE_REBOUND,ROLE_BREAK,ROLE_IN_ZONE,ROLE_NO_NEAR,ROLE_UNKNOWN),
        format_func=lambda x:'Todos los roles PRE' if x=='TODOS' else ROLE_LABELS[x],
        key='hlr_v1_role')
    if role_option!='TODOS': inspect=inspect.loc[inspect.pre_role.eq(role_option)]
    if lifecycle_ready:
        lifecycle_choice=st.selectbox('Historia del nivel PRE (solo inspector)',
             ('TODOS',LEVEL_ACTIVE,LEVEL_LOST,LEVEL_FAILED,LEVEL_RECLAIMED,LEVEL_UNKNOWN,LEVEL_NO_NEAR),
             format_func=lambda x:'Todos los estados' if x=='TODOS' else LIFECYCLE_LABELS[x],
             key='hlr_lifecycle_v1_inspect')
        if lifecycle_choice!='TODOS':
            inspect=inspect.loc[inspect.lifecycle_state.eq(lifecycle_choice)]
    result=st.selectbox('Resultado (solo filtra el inspector)',
        ('TODOS','TP_FIRST','SL_FIRST','NO_HIT','AMBIGUOUS','SIN_RESULTADO'),
        key='hlr_v1_result')
    if result!='TODOS': inspect=inspect.loc[inspect.outcome_norm.eq(result)]
    if inspect.empty:
        st.info('No hay eventos para el filtro del inspector.');return
    recent=inspect.sort_values('retest_ts',ascending=False)
    labels={r.event_key:f'{r.symbol} {r.side} · {r.date_utc} · '
            f'{GROUPS[r.level_group]} · {ROLE_LABELS.get(r.pre_role,r.pre_role)} · {r.outcome_norm}'
            for r in recent.itertuples(index=False)}
    key=st.selectbox('Abrir REACTION',list(labels),format_func=lambda k:labels[k],
                     key='hlr_v1_event')
    item=recent.loc[recent.event_key.eq(key)].iloc[0].copy()
    st.markdown(f'#### {item.symbol} {item.side} · {GROUPS[item.level_group]}')
    st.caption('**Rol PRE:** '+ROLE_LABELS.get(item.pre_role,str(item.pre_role))+
               '. Es posición del nivel respecto al último cierre HTF; '
               'no confirma defensa, rechazo ni ruptura posterior.')
    if pd.notna(item.role_zone_id):
        st.caption('**Nivel elegido para el experimento PRE:** '+str(item.role_timeframe)+
                   ' · zona '+_pretty(item.role_zone_low)+'–'+_pretty(item.role_zone_high)+
                   ' · cierre previo '+_pretty(item.role_pre_close)+
                   ' · distancia '+_pretty(item.role_distance_atr)+' ATR · '+
                   _pretty(item.role_defenses_pre)+' episodios de defensa previos · '+
                   'posición '+str(item.role_position)+
                   ' · origen '+str(item.role_origin)+'.')
    if lifecycle_ready:
        st.caption('**Historia PRE del nivel:** '+meaning_of_level_pre(item.lifecycle_state,item.initial_side)+
                   ' · lado inicialmente aceptado '+str(item.initial_side)+
                   ' · última pérdida '+str(item.last_break_close_ts)+
                   ' · último retest fallido '+str(item.last_failed_retest_close_ts)+
                   ' · última recuperación '+str(item.last_reclaim_close_ts)+
                   ' · evaluación '+str(item.lifecycle_reason)+'.')
    if pd.notna(item.relevant_zone_id):
        st.caption('Nivel del grupo A/B (evaluado con vela KNOWN): '+str(item.relevant_timeframe)+' · zona '+
             _pretty(item.relevant_zone_low)+'–'+_pretty(item.relevant_zone_high)+
             ' · '+_pretty(item.relevant_gap_atr)+' ATR del cierre PRE · '+
             str(int(item.relevant_pivots))+' pivotes confirmados · '+
             str(int(item.relevant_episodes))+' episodios independientes de defensa antes de la señal.')

    else:
        if item.level_group==C:
            st.info('Grupo C: no hay nivel importante dentro de 0,50 ATR del rango 1m KNOWN. '
                    'Puede existir un nivel cercano en el cierre HTF PRE anterior; '
                    'ambas mediciones corresponden a momentos diferentes.')
        else:
            st.info('No existe contacto verificable o falta cobertura 1m; no se inventa un nivel cercano.')
    if item.level_group in (A,B):
        st.caption('La etiqueta de contacto se conoce SOLO al cierre de la vela de REACTION. '
                   'Si se opera basándose en ella, la entrada debe re-simularse después.')

    # Plot one level. Prefer the PRE-chosen level used in the role comparison;
    # otherwise use the verified A/B level. NEVER choose using TP/SL.
    candidate=qualifying.loc[qualifying.event_key.eq(key)]
    plot_id=(item.role_zone_id if pd.notna(item.role_zone_id)
             else item.relevant_zone_id)
    plot_origin=(item.role_timeframe if pd.notna(item.role_zone_id)
                 else item.relevant_timeframe)
    selected_zone=candidate.loc[candidate.zone_id.eq(plot_id)]
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
        if not plot_zone.empty and str(plot_origin)!=shown_tf:
            # Visually project the SAME frozen level onto the alternate TF;
            # do not reassign its source/confirmation timeframe.
            plot_zone['timeframe']=shown_tf
            st.caption(f'Zona originada y confirmada en {plot_origin}, '
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
        st.caption('Gráfico: nivel PRE más cercano (si existe); de lo contrario, '
                   'zona A/B validada usando OHLC 1m. Un nivel puede ser cercano '
                   'PRE aunque no haya contacto KNOWN, o viceversa. '
                   'El cierre de REACTION y la trayectoria POST solo sirven '
                   'para evaluación, nunca para elegir nivel o rol PRE.')
    except Exception as exc:
        st.error(f'Inspector: no se pudo reconstruir el gráfico: {exc}')

    with st.expander('Exportar la muestra clasificada (sin recalcular señales)',expanded=False):
        cols=['event_key','symbol','side','date_utc','level_group','pre_bucket',
              'pre_role','role_zone_id','role_timeframe','role_position',
              'role_pre_close','role_zone_low','role_zone_high','role_distance_atr',
              'role_defenses_pre','role_pivots_pre','role_near_count',
              'important_levels','relevant_zone_id','relevant_timeframe','relevant_gap_atr',
              'relevant_episodes','relevant_pivots','known_touch_verified',
              'known_low','known_high','relevant_retest_gap_atr',
              'atlas_claimed_touch','atlas_claimed_touch_not_verified','outcome_norm']
        if lifecycle_ready:
            cols.extend(['lifecycle_state','initial_side','last_break_close_ts',
                         'last_failed_retest_close_ts','last_reclaim_close_ts',
                         'history_quality','lifecycle_reason'])
        st.download_button('Descargar CSV de investigación PRE + KNOWN',
              df[cols].to_csv(index=False).encode('utf-8'),
              file_name='horizontal_level_research_v1.csv',mime='text/csv',
              key='hlr_v1_export')
