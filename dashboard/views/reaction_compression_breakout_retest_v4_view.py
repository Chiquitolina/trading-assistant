"""V4 Compression breakout/retest research: read-only visualization."""
from __future__ import annotations
import json
from pathlib import Path


def _load(project):
    import pandas as pd
    from reaction_compression_breakout_retest_v4 import OUTPUT,_hash,v3
    p=Path(project)/v3.ROOT_REL/OUTPUT
    meta=json.loads((p/'summary.json').read_text(encoding='utf-8'))
    if _hash(p/'structures_v4.csv')!=meta['structures_sha256'] or _hash(p/'event_best_v4.csv')!=meta['best_sha256']:
        raise ValueError('SHA256 V4 incorrecto. Reconstruir y auditar antes de visualizar.')
    structures=pd.read_csv(p/'structures_v4.csv',low_memory=False)
    events=pd.read_csv(p/'event_best_v4.csv',low_memory=False)
    if len(structures)!=meta['rows'] or len(events)!=meta['events'] or events.event_key.duplicated().any():
        raise ValueError('V4: recuentos o identidades duplicadas')
    return structures,events,meta


def _summary(x,group,min_n=20):
    import pandas as pd
    rows=[]
    for name,g in x.groupby(group,dropna=False):
        wins=int((g.outcome=='TP_FIRST').sum());loses=int((g.outcome=='SL_FIRST').sum())
        nohit=int((g.outcome=='NO_HIT').sum());unknown=int((~g.outcome.isin(['TP_FIRST','SL_FIRST','NO_HIT'])).sum())
        n=wins+loses
        # Wilson approximate 95% interval to discourage interpreting 3/4 as strong.
        if n:
            p=wins/n;z=1.96;den=1+z*z/n
            midpoint=(p+z*z/(2*n))/den
            half=(z*((p*(1-p)/n+z*z/(4*n*n))**0.5))/den
            lower=100*max(0,midpoint-half);upper=100*min(1,midpoint+half)
        else:lower=upper=float('nan')
        rows.append({group:str(name),'REACTIONs':len(g),'TP_FIRST':wins,'SL_FIRST':loses,
                     'NO_HIT':nohit,'No resueltas/otras':unknown,
                     'TP entre resueltos (%)':round(100*wins/n,2) if n>=min_n else None,
                     'Wilson 95% inferior':round(lower,2) if n>=min_n else None,
                     'Wilson 95% superior':round(upper,2) if n>=min_n else None,
                     'días':g.date_utc.nunique(),'símbolos':g.symbol.nunique()})
    return pd.DataFrame(rows).sort_values('REACTIONs',ascending=False) if rows else pd.DataFrame()


def render_v4(source_events,project_root):
    import pandas as pd
    import streamlit as st
    from reaction_compression_breakout_retest_v4 import OUTPUT,v3
    from dashboard.views.reaction_compression_lineage_view import chart_event
    project=Path(project_root)
    st.markdown('### 🧱 Compression Breakout Retest V4')
    st.caption('Scanner original de compresiones aplicado a cierres históricos. Caja y selección congeladas ANTES del retest; ' 
               'etiquetas de toque y hold disponibles solo DESPUÉS de cerrar su vela 1m.')
    folder=project/v3.ROOT_REL/OUTPUT
    if not (folder/'summary.json').exists():
        st.info('Falta ejecutar `reaction_compression_breakout_retest_v4.py --plan`, luego `--run` y `--audit`.')
        return
    @st.cache_data(show_spinner=False,max_entries=3)
    def cached(path,mtime):return _load(Path(path))
    try:structures,all_events,meta=cached(str(project),(folder/'summary.json').stat().st_mtime_ns)
    except Exception as exc:st.error(f'V4: {exc}');return
    selected_keys=set(source_events.event_key.astype(str))
    events=all_events[all_events.event_key.astype(str).isin(selected_keys)].copy()
    if events.empty:
        st.info('La selección actual no contiene REACTIONs del piloto V4; cambiá los filtros superiores.')
        return
    kpis=st.columns(4)
    kpis[0].metric('Eventos del piloto',f"{meta['events']:,}")
    kpis[1].metric('En filtros actuales',f'{len(events):,}')
    kpis[2].metric('Ventanas evaluadas',f"{meta['rows']:,}")
    kpis[3].metric('Con todas las búsquedas completas',f"{int(all_events.coverage_all_5.eq(1).sum()):,}")
    st.warning('Resultados HISTÓRICOS EXPLORATORIOS: no son operaciones reales. Los porcentajes son de OBSERVED_OPEN ' 
               'TP1/SL0,5 en 180m; confirmar con entrada recalculada si exigimos cerrar la vela del retest.')
    st.caption('El primer retest solo es VERIFICADO cuando todas las velas 1m anteriores están disponibles; UNKNOWN_GAP ' 
               'no se incluye dentro de primeros retests. La V3 tenía un error de indexado de mechas, corregido en V4.')
    if meta['cohort']=='live-stratified':
        st.caption('Muestra estratificada por día/dirección; no representativa por frecuencia de señales. No extrapolar prevalencias sin ponderación.')
    if not all_events.coverage_all_5.eq(1).all():
        st.info('Hay ventanas históricas incompletas: una no detección no equivale a ausencia de compresión.')
    t1,t2,t3=st.tabs(['Resultados globales','Estructuras y calidad','Inspector + curvas'])
    with t1:
        st.markdown('#### Clasificación de retest sobre una estructura elegida causalmente')
        st.dataframe(_summary(events,'selection_state_known'),use_container_width=True,hide_index=True)
        st.markdown('#### Antigüedad de la primera ruptura confirmada')
        st.dataframe(_summary(events,'selection_age_bucket'),use_container_width=True,hide_index=True)
        st.markdown('#### Condición previa al retest (la vela del retest NO se usa)')
        st.dataframe(_summary(events,'selection_state_pre'),use_container_width=True,hide_index=True)
        st.markdown('#### Primer retest verificable versus repetido')
        st.dataframe(_summary(events,'selection_first_retest_status'),use_container_width=True,hide_index=True)
        st.markdown('#### Cruce de antigüedad × resultado conocido')
        counts=events.pivot_table(index='selection_age_bucket',columns='selection_state_known',values='event_key',aggfunc='count',fill_value=0)
        st.dataframe(counts,use_container_width=True)
        st.markdown('#### Robustez temporal y LONG/SHORT')
        data=events.copy();dates=sorted(data.date_utc.dropna().unique())
        if len(dates)>=4:
            split=max(1,int(len(dates)*.6));train=set(dates[:split])
            data['particion']=data.date_utc.map(lambda d:'Días anteriores' if d in train else 'Días posteriores')
            st.dataframe(_summary(data,'particion',min_n=20),use_container_width=True,hide_index=True)
        st.dataframe(_summary(data,'side',min_n=20),use_container_width=True,hide_index=True)
        st.caption('Una partición posterior examinada reiteradamente NO es validación prospectiva independiente. ' 
                   'No seleccionar umbrales mirando sus resultados y luego llamarlos out-of-sample.')
    with t2:
        tf=st.selectbox('Timeframe',['15m','30m'],key='v4_tf')
        n=st.selectbox('Velas de compresión',list(v3.WINDOWS[tf]),key='v4_window',
                       format_func=lambda x:f'{x} velas + referencia separada {2*x}')
        tab=structures[structures.timeframe.eq(tf)&structures.window.eq(n)&structures.event_key.isin(events.event_key)].copy()
        st.dataframe(_summary(tab.merge(events[['event_key','outcome']],on='event_key',validate='one_to_one'),
                              'state_known'),hide_index=True,use_container_width=True)
        st.markdown('#### Caja, breakout, distancia a la pared y calidad')
        columns=['event_key','symbol','side','coverage','history_source','state_pre','age_bucket','state_known',
            'first_retest_status','box_high','box_low','wall','pre_wall_distance_pct','pre_wall_distance_atr',
            'known_wick_distance_pct','known_close_side_pct','age_break_min','breakout_strength_pct',
            'breakout_body_share','breakout_quote_ratio20','readiness_score','snapshots_eligible','snapshots_qualified']
        st.dataframe(tab[columns].sort_values('age_break_min'),hide_index=True,use_container_width=True,height=420)
        st.download_button('Descargar estructuras V4 (filtros actuales)',tab.to_csv(index=False).encode(),
           file_name='compression_breakout_retest_v4.csv',mime='text/csv',key='v4_export')
        st.caption('30m MIXED/PROXY: la proporción nativa frente a 15m reconstruida debe auditarse. ' 
                   'El score del scanner NO expresa probabilidad de acierto.')
    with t3:
        available=structures[structures.event_key.isin(events.event_key)&structures.breakout_ts.notna()].copy()
        if available.empty:
            st.info('No hay cajas rotas detectadas entre los eventos filtrados.')
            return
        available=available.sort_values(['retest_ts','age_break_min'],ascending=[False,True])
        labels={f'{r.event_key}|{r.timeframe}|{r.window}':f'{r.symbol} {r.side} · {r.date_utc} · '
            f'{r.timeframe}/{r.window} · {r.state_known}' for r in available.itertuples(index=False)}
        chosen=st.selectbox('Evento + estructura',list(labels),format_func=labels.get,key='v4_inspect')
        key,tf,window=chosen.rsplit('|',2)
        item=available[(available.event_key==key)&(available.timeframe==tf)&(available.window==int(window))].iloc[0]
        fig=chart_event(project,item)
        if fig:st.plotly_chart(fig,use_container_width=True)
        st.dataframe(pd.DataFrame([item.to_dict()]),hide_index=True,use_container_width=True)
        st.caption('La caja y el breakout están anclados en velas de TF cerradas; el retest mostrado se evalúa ' 
                   'solo después del cierre de su vela 1m. La trayectoria POST se puede inspeccionar en la pestaña ' 
                   '«Inspector por REACTION» del Retest Quality Lab.')
