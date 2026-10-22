"""Independent compression and horizontal-level exploration, never gated on confluence.

READ ONLY. Each algorithm is executed upstream on frozen pre-retest candles.
Outcome labels are joined only for the displays, never for choosing a box/level.
"""
from __future__ import annotations
from pathlib import Path


def join_independent(outcomes, compression, levels):
    """One row per event; insufficient coverage is UNKNOWN rather than negative."""
    import pandas as pd
    for name, data in [('outcomes', outcomes), ('compression',compression), ('levels',levels)]:
        if data.event_key.duplicated().any():
            raise ValueError(f'{name}: duplicate event IDs')
    a=outcomes[['event_key','symbol','side','date_utc','outcome','retest_ts']].copy()
    c=compression[['event_key','selection_state_pre','selection_state_known','selection_timeframe',
                   'selection_window','has_qualified_any','coverage_all_5']].copy()
    l=levels[['event_key','selection_state_pre','selection_state_known','selection_timeframe',
              'selection_pre_near_level','has_level','coverage_all']].copy()
    c=c.rename(columns={k:'compression_'+k for k in c.columns if k!='event_key'})
    l=l.rename(columns={k:'level_'+k for k in l.columns if k!='event_key'})
    joined=a.merge(c,on='event_key',how='inner',validate='one_to_one')
    joined=joined.merge(l,on='event_key',how='inner',validate='one_to_one')
    def compression_category(row):
        if row['compression_coverage_all_5']!=1:return 'COBERTURA_INSUFICIENTE'
        if pd.notna(row['compression_selection_timeframe']):return 'RUPTURA_DE_COMPRESION'
        if row['compression_has_qualified_any']==1:return 'COMPRESION_SIN_RUPTURA_DIRECCIONAL'
        return 'SIN_COMPRESION_CALIFICADA'
    def level_category(row):
        if row['level_coverage_all']!=1:return 'COBERTURA_INSUFICIENTE'
        if row['level_has_level']!=1:return 'SIN_NIVEL_CONFIRMADO'
        st=str(row['level_selection_state_pre'])
        if st=='ACCEPTED_DEFENDED':return 'NIVEL_ACEPTADO_DEFENDIDO'
        if st=='RECLAIM_ACCEPTED':return 'NIVEL_RECUPERADO_ACEPTADO'
        if st=='LOST_LEVEL_PRE':return 'NIVEL_PERDIDO'
        return 'OTRO_NIVEL_CONFIRMADO'
    joined['categoria_compresion']=joined.apply(compression_category,axis=1)
    joined['categoria_nivel']=joined.apply(level_category,axis=1)
    joined['compresion_relevante']=joined.categoria_compresion.eq('RUPTURA_DE_COMPRESION')
    joined['nivel_favorable']=joined.categoria_nivel.isin(
        ['NIVEL_ACEPTADO_DEFENDIDO','NIVEL_RECUPERADO_ACEPTADO'])
    def class_pair(row):
        if ('COBERTURA_INSUFICIENTE' in (row['categoria_compresion'],row['categoria_nivel'])):
            return 'COBERTURA_PARCIAL'
        if row['compresion_relevante'] and row['nivel_favorable']:return 'AMBAS'
        if row['compresion_relevante']:return 'SOLO_RUPTURA_COMPRESION'
        if row['nivel_favorable']:return 'SOLO_NIVEL_ACEPTADO'
        return 'NINGUNA_DE_LAS_DOS'
    joined['relacion_independiente']=joined.apply(class_pair,axis=1)
    return joined


def stats_by_group(frame,field,min_resolved=20):
    import pandas as pd
    out=[]
    for kind,g in frame.groupby(field,dropna=False):
        tp=int(g.outcome.eq('TP_FIRST').sum());sl=int(g.outcome.eq('SL_FIRST').sum())
        n=tp+sl
        out.append({'Grupo':str(kind),'REACTIONs':len(g),'TP_FIRST':tp,'SL_FIRST':sl,
            'NO_HIT':int(g.outcome.eq('NO_HIT').sum()),
            'AMBIGUOUS':int(g.outcome.eq('AMBIGUOUS').sum()),
            'Sin resultado':int(g.outcome.isna().sum()+g.outcome.isin(['NOT_ENTERED']).sum()),
            'TP entre resueltas (%)':round(100*tp/n,2) if n>=min_resolved else None,
            'N resueltas':n,'Días':g.date_utc.nunique(),'Símbolos':g.symbol.nunique()})
    return pd.DataFrame(out).sort_values('REACTIONs',ascending=False) if out else pd.DataFrame()


def render_independent_structures(source_events,project_root):
    import pandas as pd
    import streamlit as st
    from dashboard.views.reaction_compression_breakout_retest_v4_view import _load as load_v4
    from dashboard.views.reaction_structural_level_atlas_view import load as load_level,chart_level
    from dashboard.views.reaction_compression_lineage_view import chart_event
    project=Path(project_root)
    st.markdown('### 🧭 Compresiones y niveles: investigaciones independientes')
    st.caption('No exigimos confluencia: evaluamos CADA hipótesis por separado y recién después sus coincidencias. '
               'La clasificación procede de velas ANTERIORES al retest; TP/SL solo se agrega para comparar resultados.')
    try:
        comp,comp_events,cm=load_v4(project)
        levels,level_events,lm=load_level(project)
    except (FileNotFoundError,OSError,KeyError,ValueError,RuntimeError) as ex:
        st.info('Necesitamos los dos análisis construidos y auditados: V4 --run/--audit y Structural Level Atlas --run/--audit.')
        st.caption(str(ex));return
    if cm.get('frozen_cohort_sha256')!=lm.get('frozen_cohort_sha256'):
        st.error('Cohortes congeladas diferentes. No se permite cruzar sus etiquetas.');return
    joined=join_independent(source_events,comp_events,level_events)
    if joined.empty:
        st.info('Estos filtros no tienen eventos que estén simultáneamente en ambos experimentos. '
                'Se necesita ejecutar ambos sobre la MISMA muestra (cohort y max-events).');return
    possible=set(comp_events.event_key)&set(level_events.event_key)
    st.caption(f'{len(joined)} REACTIONs presentes en ambos análisis, frente a {len(possible)} en ambas cohortes completas. '
               f'V4: {cm["events"]} eventos, Atlas: {lm["events"]}.')
    if cm['events']!=lm['events'] or set(comp_events.event_key)!=set(level_events.event_key):
        st.warning('Las cohortes analizadas no coinciden completamente: los grupos de este panel solo representan su INTERSECCIÓN.')
    min_n=st.slider('Mínimo casos resueltos para tasa descriptiva',5,100,20,5,key='ind_min_n')
    st.markdown('#### Compresión por sí misma — no depende del Atlas')
    st.dataframe(stats_by_group(joined,'categoria_compresion',min_n),use_container_width=True,hide_index=True)
    st.markdown('#### Nivel horizontal por sí mismo — no depende de compresión')
    st.dataframe(stats_by_group(joined,'categoria_nivel',min_n),use_container_width=True,hide_index=True)
    st.markdown('#### Comparación adicional: solo una estructura, ambas o ninguna')
    st.dataframe(stats_by_group(joined,'relacion_independiente',min_n),use_container_width=True,hide_index=True)
    st.caption('No se seleccionan mejores umbrales a partir de TP/SL. Un nivel histórico presente en todos los eventos '
               'no demuestra ventaja predictiva; se requieren controles sin REACTION y fechas nuevas.')
    st.markdown('#### Inspector comparativo — el MISMO evento en ambos detectores')
    pick=joined.sort_values('retest_ts',ascending=False)
    labels={r.event_key:f'{r.symbol} {r.side} · {r.date_utc} · {r.relacion_independiente}'
            for r in pick.itertuples(index=False)}
    selected=st.selectbox('REACTION',list(labels),format_func=labels.get,key='ind_pick')
    item=pick[pick.event_key.eq(selected)].iloc[0]
    left,right=st.columns(2)
    with left:
        st.markdown('**Compression Breakout Retest V4**')
        opts=comp[(comp.event_key==selected)&comp.breakout_ts.notna()].copy()
        if opts.empty:
            st.info('No se detectó ruptura direccional de una caja calificada con cobertura suficiente.')
        else:
            # Prefer the event-selected box if one was assigned, no outcome-based choice.
            if pd.notna(item['compression_selection_timeframe']):
                chosen=opts[(opts.timeframe==item['compression_selection_timeframe'])&
                            (opts.window==item['compression_selection_window'])]
                if not chosen.empty:opts=chosen
            fig=chart_event(project,opts.iloc[0])
            if fig is not None:st.plotly_chart(fig,use_container_width=True,key='ind_comp_chart')
            st.caption(f"Estado previo: {item['compression_selection_state_pre']}")
    with right:
        st.markdown('**Structural Level Atlas**')
        level_tf=item['level_selection_timeframe']
        opts=levels[(levels.event_key==selected)&(levels.timeframe==level_tf)].copy()
        if opts.empty or pd.isna(opts.iloc[0]['level_price']):
            st.info('No existe un nivel horizontal confirmado para este evento y timeframe.')
        else:
            fig=chart_level(project,item,opts.iloc[0])
            if fig is not None:st.plotly_chart(fig,use_container_width=True,key='ind_level_chart')
            st.caption(f"Estado previo: {item['level_selection_state_pre']}")
    st.dataframe(pd.DataFrame([item.to_dict()]),hide_index=True,use_container_width=True)
    st.download_button('Exportar comparación independiente',joined.to_csv(index=False).encode('utf-8'),
                       file_name='compression_vs_level_independiente.csv',mime='text/csv',key='ind_export')
