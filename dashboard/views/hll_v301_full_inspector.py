"""Explore the complete 290-symbol frozen HLL cohort without mixing core15 artifacts.

Read only Streamlit panel. V291/V29/V292 and 1m results are optional and linked
only after matching original PRE source hashes. No fictitious entries or fills.
"""
from __future__ import annotations
import json
from pathlib import Path
import pandas as pd
from dashboard.views.hll_v282_pre_followup_view import make_chart, PATTERNS, STATES
from dashboard.views.hll_v301_full290 import PRE,CANDLES,V291,V29,MECH,OUT,digest


def verify_artifacts(pre_path:Path):
    result={}
    source=digest(pre_path)
    paths={'V291':(V291/'manifest.json','original_sha256'),
           'V29':(V29/'manifest.json','source_sha256'),
           'V292':(MECH/'manifest.json','source_prehash')}
    for label,(manifest,field) in paths.items():
        if not manifest.is_file():result[label]=False;continue
        actual=json.loads(manifest.read_text(encoding='utf-8')).get(field)
        if actual!=source:raise ValueError(f'{label} pertenece a otro PRE; no mezclar archivos')
        result[label]=True
    return result


def add_statuses(pre, v29):
    """One-to-one 30m route join, never update PRE morphology from POST."""
    if pre.attempt_id.duplicated().any() or v29.duplicated(['attempt_id','branch']).any():
        raise ValueError('IDs duplicados en PRE/V29')
    known=set(pre.attempt_id)
    if not set(v29.attempt_id).issubset(known):raise ValueError('Señales V29 externas a cohorte PRE')
    out=pre.copy()
    for branch,col in [('BREAKOUT_CONTINUATION','continuation_status'),
                       ('RECLAIM_CONTINUATION','reclaim_status')]:
        r=v29.loc[v29.branch.eq(branch),['attempt_id','status']].rename(columns={'status':col})
        out=out.merge(r,on='attempt_id',how='left',validate='one_to_one')
    return out


def _selected_batch(scope,symbol):
    base=OUT/f'entries_batches_{scope}'
    if not base.is_dir():return None
    for p in sorted(base.iterdir()):
        if not p.is_dir() or not (p/'metadata.json').is_file():continue
        m=json.loads((p/'metadata.json').read_text())
        if symbol in m.get('symbols',[]):return p
    return None


def _episode_entries(symbol,attempt,scope):
    path=_selected_batch(scope,symbol)
    if path is None or not (path/'entry_events.csv').is_file():return None,None
    try:
        ent=pd.read_csv(path/'entry_events.csv',low_memory=False)
        ent=ent.loc[ent.attempt_id.eq(str(attempt))]
        if ent.empty:return ent,pd.DataFrame()
        frames=[]
        for chunk in pd.read_csv(path/'entry_scenarios_25.csv',chunksize=25000,low_memory=False):
            z=chunk.loc[chunk.attempt_id.eq(str(attempt))]
            if not z.empty:frames.append(z)
        return ent,(pd.concat(frames,ignore_index=True) if frames else pd.DataFrame())
    except (OSError,ValueError,pd.errors.EmptyDataError):return None,None


def render_full290(root:Path,folder:Path,pre:pd.DataFrame,blind:bool=False):
    import streamlit as st
    from dashboard.views.hll_v282_pre_followup_view import _date

    @st.cache_data(show_spinner=False,max_entries=6)
    def read_cached_csv(path,mtime_ns,columns=None):
        return pd.read_csv(path,usecols=list(columns) if columns else None,low_memory=False)
    from dashboard.views.hll_v292_inspector_overlay import overlay_pre_contacts,LABELS,overlay_physical_clusters
    root=Path(root)
    st.markdown('#### HLL 290 · Inspector integral de estructuras y rutas')
    st.caption('Universo V2.8.2 congelado: 55.717 intentos · 290 símbolos. '
       'Datos PRE sin modificar; POST, continuación y reclaim se agregan por attempt_id. '
       'Las tasas operativas son descriptivas, no una cartera ejecutada.')
    try:
        ready=verify_artifacts(folder/'pre_cohort.csv')
    except (ValueError,OSError) as e:
        st.error(str(e));return True
    if ready['V291']:
        ap=root/V291/'integrity_audit.csv'
        a=read_cached_csv(ap,ap.stat().st_mtime_ns)
        if len(a)!=len(pre) or set(a.attempt_id)!=set(pre.attempt_id):
            st.error('V2.9.1: los IDs no coinciden con V2.8.2');return True
        pre=pre.merge(a,on='attempt_id',how='left',validate='one_to_one')
    if ready['V29']:
        ep=root/V29/'continuation_events.csv'
        ev=read_cached_csv(ep,ep.stat().st_mtime_ns)
        pre=add_statuses(pre,ev)
    else:ev=pd.DataFrame();st.info('V2.9 continuación/reclaim todavía no ejecutada. El inspector PRE funciona igualmente.')
    if ready['V292']:
        mech_path=root/MECH/'mechanism_attempts.csv'
        mech=read_cached_csv(mech_path,mech_path.stat().st_mtime_ns,tuple(['attempt_id']+[f'mechanism_{w}' for w in (24,48,96)]))
        if len(mech)!=len(pre) or set(mech.attempt_id)!=set(pre.attempt_id):
            st.error('V2.9.2 tiene un universo de IDs incompatible');return True
        fields=['attempt_id']+[f'mechanism_{w}' for w in (24,48,96)]
        pre=pre.merge(mech[fields],on='attempt_id',how='left',validate='one_to_one')
    else:st.info('V2.9.2 de los 290 pendiente: categorías V2.8.2, velas y rutas siguen disponibles.')
    selection,other=st.columns([1,1])
    with selection:
        unit=st.radio('Unidad',['TODOS','FIRST_PER_LEVEL'],horizontal=True,key='hll301_unit')
    with other:
        window=st.selectbox('Ventana PRE 30m',[24,48,96],index=1,key='hll301_window')
    use_revised=ready['V291'] and st.checkbox('Aplicar auditoría de directriz V2.9.1',value=False,key='hll301_v291')
    field=f'pre_pattern_v291_{window}' if use_revised else f'pre_pattern_{window}'
    sel=pre
    if unit=='FIRST_PER_LEVEL':
        sel=(sel.sort_values(['breakout_closed_ts','attempt_id'],kind='stable')
              .drop_duplicates(['symbol','level_id'],keep='first'))
    a,b,c=st.columns([2,1,1])
    with a:
        pat=st.selectbox('Estructura PRE',['TODAS']+sorted(sel[field].dropna().unique().tolist()),
          format_func=lambda x:'Todas las estructuras' if x=='TODAS' else PATTERNS.get(x,x),key='hll301_pattern')
    with b:symbol=st.selectbox('Símbolo',['Todos']+sorted(sel.symbol.unique().tolist()),key='hll301_symbol')
    with c:
        mech_name=st.selectbox('Mecanismo PRE', ['TODOS']+sorted(sel[f'mechanism_{window}'].dropna().unique().tolist()),
          format_func=lambda x:LABELS.get(x,x),key='hll301_mech') if ready['V292'] else 'TODOS'
    if pat!='TODAS':sel=sel[sel[field].eq(pat)]
    if symbol!='Todos':sel=sel[sel.symbol.eq(symbol)]
    if ready['V292'] and mech_name!='TODOS':sel=sel[sel[f'mechanism_{window}'].eq(mech_name)]
    if not blind:
        a,b=st.columns([1,1])
        with a:outcome=st.selectbox('Desenlace de flip',['TODOS']+list(STATES),key='hll301_post')
        with b:
            routes=['TODAS','FLIP','CONT_CONFIRMADA','RECLAIM_CONFIRMADO'] if ready['V29'] else ['TODAS','FLIP']
            route=st.selectbox('Ruta confirmada',routes,key='hll301_route')
        if outcome!='TODOS':sel=sel[sel.post_outcome.eq(outcome)]
        if route=='FLIP':sel=sel[sel.post_outcome.eq('FLIP_CONFIRMADO')]
        elif route=='CONT_CONFIRMADA':sel=sel[sel.continuation_status.eq('CONFIRMED')]
        elif route=='RECLAIM_CONFIRMADO':sel=sel[sel.reclaim_status.eq('CONFIRMED')]
    st.caption(f'Intentos en la selección: {len(sel):,} · símbolos: {sel.symbol.nunique()}'.replace(',','.'))
    if not blind:
        cells=st.columns(4)
        values=[('Intentos',len(sel)),('Flip',int(sel.post_outcome.eq('FLIP_CONFIRMADO').sum())),
                ('Continuación',int(sel.continuation_status.eq('CONFIRMED').sum()) if ready['V29'] else 0),
                ('Reclaim',int(sel.reclaim_status.eq('CONFIRMED').sum()) if ready['V29'] else 0)]
        for c,(name,n) in zip(cells,values):c.metric(name,n)
        with st.expander('Comparar estructuras, mecanismos y rutas',expanded=True):
            keys=[field]
            if ready['V292']:keys.append(f'mechanism_{window}')
            grp=sel.groupby(keys,dropna=False).agg(intentos=('attempt_id','size'),simbolos=('symbol','nunique'),
                flips=('post_outcome',lambda x:int(x.eq('FLIP_CONFIRMADO').sum())))
            st.dataframe(grp.reset_index().sort_values('intentos',ascending=False).head(200),use_container_width=True,hide_index=True)
            if ready['V29']:
                st.caption('Las continuaciones/reclaims se muestran en los contadores superiores según la selección.')
    if sel.empty:st.info('No hay eventos bajo estos filtros.');return True
    sel=sel.sort_values('breakout_closed_ts',ascending=False)
    chosen=sel.head(1200).copy()
    if len(sel)>1200:st.caption('Para evitar un selector gigante se muestran los 1.200 episodios más recientes; filtrá por símbolo para buscar otros.')
    chosen['pick']=(chosen.symbol+' | '+pd.to_datetime(chosen.breakout_closed_ts,unit='ms',utc=True).dt.strftime('%Y-%m-%d %H:%M')+' | '+chosen.attempt_id.astype(str).str[:10])
    episode_labels=dict(zip(chosen.attempt_id,chosen['pick']))
    eid=st.selectbox('Episodio para inspección',chosen.attempt_id.tolist(),format_func=lambda x:episode_labels.get(x,x),key='hll301_episode')
    r=chosen.loc[chosen.attempt_id.eq(eid)].iloc[0]
    st.markdown(f"**{r.symbol} · {PATTERNS.get(r[field],str(r[field]))}** · `{r.v26_status}`")
    post=st.checkbox('Dibujar desenlaces y rutas POST',value=not blind,disabled=blind,key='hll301_show_post')
    post_n=st.selectbox('Velas 30m POST',[24,48,96,160,240],index=3,key='hll301_post_n')
    path=root/CANDLES/f'{r.symbol}_30m.csv'
    if not path.is_file():st.warning(f'No hay archivo de velas {path}');return True
    bars=pd.read_csv(path,usecols=['timestamp','open','high','low','close'],float_precision='round_trip')
    events=ev.loc[ev.attempt_id.eq(str(eid))].to_dict('records') if ready['V29'] and post else []
    try:
        fig=make_chart(r,bars,window,post,False,events,post_n)
        if ready['V292']:
            touch_path=root/MECH/'pre_contacts.csv'
            contacts=read_cached_csv(touch_path,touch_path.stat().st_mtime_ns)
            selected=contacts[contacts.attempt_id.eq(str(eid)) & contacts.window.eq(window)]
            fig=overlay_pre_contacts(fig,selected,r.pre_closed_ts)
            if st.checkbox('Mostrar solapes físicos PRE',value=False,key='hll301_physical'):
                fig=overlay_physical_clusters(fig,r,bars,window)
        st.plotly_chart(fig,use_container_width=True,key='hll301_chart')
    except (ValueError,KeyError,TypeError,OSError) as e:
        st.error(f'No se pudo graficar esta estructura: {e}');return True
    if post and events:
        st.dataframe(pd.DataFrame(events)[['branch','status','confirmed_ts','next_open','displacement_atr']],hide_index=True,use_container_width=True)
    if not blind:
        results=OUT/'entries_FIRST_PER_LEVEL'/'manifest.json'
        st.markdown('#### Entradas verificadas y resultados TP/SL')
        scope='FIRST_PER_LEVEL' if unit=='FIRST_PER_LEVEL' else 'ALL'
        ent,scens=_episode_entries(str(r.symbol),str(eid),scope)
        if ent is None:
            st.caption('La evaluación de 1m para esta unidad todavía está pendiente. No se inventan entradas ni TP/SL.')
        elif ent.empty:st.caption('Este episodio no generó entradas verificadas en las tres rutas.')
        else:
            st.dataframe(ent[['route','entry_utc','entry_price','entry_id']],hide_index=True,use_container_width=True)
            if scens is not None and not scens.empty:
                horizon=st.selectbox('Horizonte de la matriz',[120,720,2880],index=1,key='hll301_horizon')
                route=st.selectbox('Ruta de la matriz',sorted(scens.route.unique()),key='hll301_matrix_route')
                part=scens.loc[scens.horizon_min.eq(horizon)&scens.route.eq(route)]
                if not part.empty:
                    g=part.pivot(index='sl_pct',columns='tp_pct',values='net_pct')
                    g.index=[f'SL −{int(i)}%' for i in g.index]
                    g.columns=[f'TP +{int(i)}%' for i in g.columns]
                    st.dataframe(g,use_container_width=True)
                    st.dataframe(part[['tp_pct','sl_pct','result','path_state','net_pct','minutes_to_exit']],
                        hide_index=True,use_container_width=True)
        matrix=OUT/f'entries_{scope}'/'matrix_by_route_pattern_month.csv'
        if matrix.is_file():
            with st.expander('Matriz agregada · todas las estructuras, flip, continuación y reclaim',expanded=False):
                sm=pd.read_csv(matrix)
                sm=sm[sm.pre_window.eq(window)]
                if pat!='TODAS':sm=sm[sm.pre_pattern.eq(pat)]
                if ready['V292'] and mech_name!='TODOS':sm=sm[sm.mechanism_pre_48.eq(mech_name)]
                col1,col2,col3=st.columns(3)
                with col1:comparison=st.selectbox('Grupo de símbolos',['AMBOS','ORIGINAL_15','ADDITIONAL_275'],key='hll301_cmp_group')
                with col2:month=st.selectbox('Mes',['TODOS']+sorted(sm.entry_month.dropna().astype(str).unique()),key='hll301_cmp_month')
                with col3:hh=st.selectbox('Horizonte',[120,720,2880],index=1,key='hll301_cmp_h')
                if comparison!='AMBOS':sm=sm[sm.universe_group.eq(comparison)]
                if month!='TODOS':sm=sm[sm.entry_month.astype(str).eq(month)]
                sm=sm[sm.horizon_min.eq(hh)]
                st.caption('Resultados netos teóricos: TP/SL ±1–5%, desagregados por ruta, mes, grupo y mecanismo PRE de 48 velas. '
                           'La matriz no suma operaciones independientes de una misma ruptura.')
                if sm.empty:st.info('Sin resultados en la selección.')
                else:
                    st.dataframe(sm.sort_values(['route','tp_pct','sl_pct']).head(3000),hide_index=True,use_container_width=True)
                    st.download_button('Exportar matriz filtrada',sm.to_csv(index=False),
                           file_name=f'hll_290_matrix_{scope}.csv',mime='text/csv',key='hll301_csv')
    return True
