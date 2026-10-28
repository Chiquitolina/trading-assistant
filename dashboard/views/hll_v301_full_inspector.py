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



def visual_entries_for_episode(row, bars:pd.DataFrame, events:list, minute_dir:Path):
    """Same V2.9.3 markers used by the original inspector, NEVER from CROSS_UP.

    A 30m confirmed continuation/reclaim is verified against its next 30m OHLC.
    The flip requires an observed next 1m OPEN. No backfilled/fake entry.
    """
    from dashboard.views.hll_v293_entry_markers import get_entries
    anchors=get_entries(row,bars,None,events,minute_dir=minute_dir,
                        include_breakout_reference=True)
    active=[e for e in anchors if e['key']!='BREAKOUT_30M']
    keys=[e['key'] for e in active]
    if len(keys)!=len(set(keys)):
        raise ValueError('Entrada repetida por ruta en un intento')
    return active, [e for e in anchors if e['key']=='BREAKOUT_30M']


def evaluate_local_25(path:Path, reference:dict, horizon_min:int,
                      fee_pct:float=.1,slippage_pct:float=0.0)->pd.DataFrame:
    """Read true 1m for one verified route, with strict OPEN/clock validation."""
    from dashboard.views.hll_v294_entry_matrix import read_minute_window
    from dashboard.views.hll_v295_net_matrix import first_touch_net
    if horizon_min not in (120,720,2880):raise ValueError('Horizonte inválido')
    entry_ts=int(reference['entry_ts']);entry_price=float(reference['price'])
    bars,state=read_minute_window(Path(path),entry_ts,horizon_min)
    if not bars.empty:
        observed=float(bars.open.iloc[0])
        if abs(observed-entry_price)>max(1e-10,entry_price*1e-7):
            raise ValueError('El OPEN 1m no coincide con la entrada verificada')
    out=first_touch_net(bars,entry_price,state,entry_ts,fee_pct,slippage_pct)
    out['path_state']=state
    return out


def render_matrix_25(st,detail:pd.DataFrame,subtitle:str):
    """Both first-touch (TP/SL) and net grids, always preserving censored rows."""
    from dashboard.views.hll_v294_entry_matrix import matrix_table
    if detail is None or detail.empty:
        st.info('No hay evaluación individual para este episodio.');return
    required={'tp_pct','sl_pct','result','net_pct'}
    if missing:=required-set(detail):
        st.error(f'Matriz sin campos: {sorted(missing)}');return
    if detail.duplicated(['sl_pct','tp_pct']).any() or len(detail)!=25:
        st.error('La matriz no contiene exactamente 25 escenarios únicos');return
    st.markdown(f'**{subtitle}**')
    st.caption('Primera barrera tocada en 1m; TP/SL en la misma vela = ambiguo. '
               'TIMEOUT al cierre del horizonte completo; censuras sin PnL imputado.')
    st.markdown('**Matriz de desenlaces — TP / SL / TIMEOUT / CENSURA**')
    st.dataframe(matrix_table(detail),use_container_width=True)
    st.markdown('**Matriz de retornos netos estimados (%)**')
    g=detail.pivot(index='sl_pct',columns='tp_pct',values='net_pct').reindex(index=range(1,6),columns=range(1,6))
    g.index=[f'SL −{i}%' for i in range(1,6)]
    g.columns=[f'TP +{i}%' for i in range(1,6)]
    st.dataframe(g,use_container_width=True)
    cols=['tp_pct','sl_pct','result','path_state','net_pct','first_hit_min','first_hit_minute_utc']
    st.dataframe(detail[[c for c in cols if c in detail]],hide_index=True,use_container_width=True)
    st.download_button('Exportar las 25 combinaciones de este episodio',detail.to_csv(index=False).encode('utf-8'),
        file_name='hll_290_episodio_matriz_tp_sl.csv',mime='text/csv',key='hll301_individual_export')


def audit_entry_evidence(pre:pd.DataFrame, v29:pd.DataFrame, candles_dir:Path,
                         minute_dir:Path|None=None, source_sha:str|None=None)->pd.DataFrame:
    """Read-only entry inventory, one row per PRE attempt.

    V29 entries require actual 30m next OPEN + matching signal candle.
    FLIP_1M entries require a full, provenance-matched V301 1m archive;
    a confirmed flip without that archive is PENDING, never NO_SIGNAL.
    Note: not a PnL/continuity estimate for the trade's future path.
    """
    import numpy as np
    from dashboard.views.hll_v301_full290 import minute_status
    cols=['attempt_id','symbol','post_outcome','flip_confirmed_closed_ts']
    if missing:=set(cols)-set(pre):raise ValueError('PRE sin columnas '+str(missing))
    if pre.attempt_id.isna().any() or pre.attempt_id.duplicated().any():
        raise ValueError('PRE con attempt_id duplicado')
    items=pre[cols].copy()
    items['attempt_id']=items.attempt_id.astype(str)
    items['flip_signal']=(items.post_outcome.eq('FLIP_CONFIRMADO') &
        pd.to_numeric(items.flip_confirmed_closed_ts,errors='coerce').notna())
    items['flip_open']=False
    items['continuation_signal']=False
    items['continuation_open']=False
    items['reclaim_signal']=False
    items['reclaim_open']=False
    items['bad_30m_open']=False

    if not v29.empty:
        need={'attempt_id','branch','status','confirmed_ts','signal_open_ts','signal_close','next_open_ts','next_open'}
        if missing:=need-set(v29):raise ValueError('V29 sin columnas '+str(missing))
        if v29.duplicated(['attempt_id','branch']).any():raise ValueError('V29 con rutas duplicadas')
        events=v29.loc[v29.status.eq('CONFIRMED') & v29.branch.isin(
             ['BREAKOUT_CONTINUATION','RECLAIM_CONTINUATION'])].copy()
        if not set(events.attempt_id.astype(str)).issubset(set(items.attempt_id)):
            raise ValueError('Evento V29 fuera del universo PRE')
        events=events.merge(items[['attempt_id','symbol']],on='attempt_id',how='left',validate='many_to_one')
        events['confirmed_clock']=pd.to_numeric(events.confirmed_ts,errors='coerce')
        events['signal_clock']=pd.to_numeric(events.signal_open_ts,errors='coerce')
        events['next_clock']=pd.to_numeric(events.next_open_ts,errors='coerce')
        events['saved_next_open']=pd.to_numeric(events.next_open,errors='coerce')
        events['saved_signal_close']=pd.to_numeric(events.signal_close,errors='coerce')
        events['observed_open']=np.nan
        events['observed_signal_close']=np.nan
        for symbol, idx in events.groupby('symbol',sort=False).groups.items():
            f=Path(candles_dir)/f'{symbol}_30m.csv'
            if not f.is_file():continue
            b=pd.read_csv(f,usecols=['timestamp','open','close'],float_precision='round_trip')
            b['timestamp']=pd.to_numeric(b.timestamp,errors='raise').astype('int64')
            if b.timestamp.duplicated().any():raise ValueError(f'Duplicación de velas 30m: {symbol}')
            op=b.set_index('timestamp').open
            cl=b.set_index('timestamp').close
            events.loc[idx,'observed_open']=events.loc[idx,'confirmed_clock'].map(op)
            events.loc[idx,'observed_signal_close']=events.loc[idx,'signal_clock'].map(cl)
        valid=(events.confirmed_clock.notna() & events.signal_clock.notna() &
               events.observed_open.gt(0) & events.observed_signal_close.gt(0) &
               events.confirmed_clock.eq(events.signal_clock+1800000) &
               (events.next_clock.isna()|events.next_clock.eq(events.confirmed_clock)) &
               (events.saved_next_open.isna() |
                  (events.saved_next_open.sub(events.observed_open).abs() <=
                   np.maximum(1e-10,events.observed_open.abs()*1e-7))) &
               events.saved_signal_close.gt(0) &
               (events.saved_signal_close.sub(events.observed_signal_close).abs() <=
                np.maximum(1e-10,events.observed_signal_close.abs()*1e-7)))
        events['verified']=valid.fillna(False)
        for branch,prefix in [('BREAKOUT_CONTINUATION','continuation'),('RECLAIM_CONTINUATION','reclaim')]:
            q=events.loc[events.branch.eq(branch),['attempt_id','verified']]
            items[f'{prefix}_signal']=items.attempt_id.isin(q.attempt_id)
            items[f'{prefix}_open']=items.attempt_id.isin(q.loc[q.verified,'attempt_id'])
        bad=set(events.loc[~events.verified,'attempt_id'])
        items['bad_30m_open']=items.attempt_id.isin(bad)

    # Only genuine V301 files, not the sparse V300 ASCENDING_CLEAN cohort;
    # otherwise the counts would mix two different universes.
    if minute_dir is not None and source_sha:
        flip_needed=items.loc[items.flip_signal & ~(
            items.continuation_open|items.reclaim_open)]
        if not flip_needed.empty:
            for sym,g in flip_needed.groupby('symbol',sort=False):
                f=Path(minute_dir)/f'{sym}_1m.csv'
                if not f.is_file():continue
                # Source hash, full length and byte hash must match the current 290 run.
                if minute_status(sym,{'pre_sha256':source_sha})!='FULL':continue
                # Read each symbol once, not once per flip; no fabricated candle.
                observed=pd.read_csv(f,usecols=['timestamp','open'],float_precision='round_trip')
                observed['timestamp']=pd.to_numeric(observed.timestamp,errors='raise').astype('int64')
                if observed.timestamp.duplicated().any() or not observed.timestamp.is_monotonic_increasing:
                    raise ValueError(f'Histórico 1m duplicado/desordenado: {sym}')
                prices=observed.set_index('timestamp').open
                stamps=pd.to_numeric(g.flip_confirmed_closed_ts,errors='raise').astype('int64')+60000
                opens=stamps.map(prices)
                valid=opens.notna() & np.isfinite(opens) & opens.gt(0)
                items.loc[g.index,'flip_open']=valid.to_numpy()

    has_open=(items.flip_open | items.continuation_open | items.reclaim_open)
    has_signal=(items.flip_signal | items.continuation_signal | items.reclaim_signal)
    pending_flip=items.flip_signal & ~items.flip_open
    items['entry_status']='SIN_SENAL_CONFIRMADA'
    items.loc[has_signal,'entry_status']='SENAL_SIN_OPEN_VERIFICADO'
    items.loc[pending_flip & ~has_open,'entry_status']='FLIP_PENDIENTE_1M'
    items.loc[has_open,'entry_status']='CON_ENTRADA_VERIFICADA'
    items['entry_reason']='Ninguna ruta confirmó según las reglas congeladas'
    items.loc[has_signal,'entry_reason']='Hay señal confirmada; revisar disponibilidad/compatibilidad del OPEN'
    items.loc[pending_flip & ~has_open,'entry_reason']='Flip confirmado; falta verificar OPEN 1m en la corrida integral'
    items.loc[items.bad_30m_open & ~has_open,'entry_reason']='V29 confirmó, pero el OPEN o cierre 30m no coincide con OHLC'
    items.loc[has_open,'entry_reason']='Al menos una ruta con OPEN histórico verificado'
    return items.drop(columns=['symbol','post_outcome','flip_confirmed_closed_ts'])


def _visible_summary_rows(summary:pd.DataFrame,window:int,pat:str,mechanism:str,
                          ready_mechanisms:bool)->pd.DataFrame:
    """Only filter summary by dimensions explicitly present in the frozen grouping."""
    s=summary[summary.pre_window.eq(window)]
    if pat!='TODAS':s=s[s.pre_pattern.eq(pat)]
    if ready_mechanisms and mechanism!='TODOS':
        if window!=48:raise ValueError('Resumen agregado solo tiene mecanismo PRE 48')
        s=s[s.mechanism_pre_48.eq(mechanism)]
    return s

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
    # Single frozen-cohort inventory. A 30m confirmation is not automatically
    # a verified entry, and unavailable 1m is not a detector failure.
    @st.cache_data(show_spinner='Auditando los OPEN disponibles del universo 290…',max_entries=2)
    def cached_entry_evidence(pre_file,ev_file,candles,minute_files,sha,
                              pre_mtime,ev_mtime,minute_mtime):
        q=pd.read_csv(pre_file,usecols=['attempt_id','symbol','post_outcome','flip_confirmed_closed_ts'],low_memory=False)
        v=pd.read_csv(ev_file,low_memory=False) if ev_file else pd.DataFrame()
        return audit_entry_evidence(q,v,Path(candles),Path(minute_files),sha)
    ev_path=(root/V29/'continuation_events.csv') if ready['V29'] else None
    minutes_path=root/OUT/'minutes'
    src_path=folder/'pre_cohort.csv'
    evidence=cached_entry_evidence(str(src_path),str(ev_path) if ev_path else None,
        str(root/CANDLES),str(minutes_path),digest(src_path),src_path.stat().st_mtime_ns,
        ev_path.stat().st_mtime_ns if ev_path else -1,
        minutes_path.stat().st_mtime_ns if minutes_path.is_dir() else -1)
    if len(evidence)!=len(pre) or evidence.attempt_id.duplicated().any():
        st.error('El inventario de entradas no tiene cardinalidad PRE correcta');return True
    pre=pre.merge(evidence,on='attempt_id',how='left',validate='one_to_one')
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
    if not blind:
        count_base=sel.copy()
        has_entry=count_base.entry_status.eq('CON_ENTRADA_VERIFICADA')
        with_entry=int(has_entry.sum())
        without_entry=len(count_base)-with_entry
        a,b,c,d=st.columns(4)
        a.metric('Con entrada verificada',with_entry)
        b.metric('Sin entrada verificada',without_entry)
        c.metric('Sin señal confirmada',int(count_base.entry_status.eq('SIN_SENAL_CONFIRMADA').sum()))
        d.metric('Flip pendiente 1m',int(count_base.entry_status.eq('FLIP_PENDIENTE_1M').sum()))
        st.caption('Sin entrada verificada incluye tanto episodios sin señal como señales cuyo OPEN todavía '
                   'no se pudo comprobar. La falta de 1m NO demuestra que el detector haya fallado. '
                   'Cada intento se cuenta una sola vez aunque tenga varias rutas.')
        filt=st.selectbox('Auditar episodios por entrada',
            ['TODOS','CON_ENTRADA_VERIFICADA','SIN_ENTRADA_VERIFICADA',
             'SIN_SENAL_CONFIRMADA','FLIP_PENDIENTE_1M','SENAL_SIN_OPEN_VERIFICADO'],
            format_func=lambda x:{'TODOS':'Todos los intentos',
             'CON_ENTRADA_VERIFICADA':'Solo con entrada verificada',
             'SIN_ENTRADA_VERIFICADA':'Solo sin entrada verificada (incluye pendientes)',
             'SIN_SENAL_CONFIRMADA':'Sin ninguna ruta confirmada',
             'FLIP_PENDIENTE_1M':'Flip confirmado con OPEN 1m pendiente',
             'SENAL_SIN_OPEN_VERIFICADO':'Otras señales confirmadas sin OPEN verificable'}.get(x,x),
            key='hll301_entry_audit_filter')
        if filt=='SIN_ENTRADA_VERIFICADA':sel=sel[~has_entry]
        elif filt!='TODOS':sel=sel[sel.entry_status.eq(filt)]
        with st.expander('Auditoría de entradas · motivos y CSV',expanded=False):
            reasons=count_base.groupby(['entry_status','entry_reason'],dropna=False).agg(
                intentos=('attempt_id','size'),simbolos=('symbol','nunique')).reset_index()
            st.dataframe(reasons,hide_index=True,use_container_width=True)
            export_cols=['attempt_id','symbol','breakout_closed_ts',field,'v26_status',
                'post_outcome','continuation_status','reclaim_status','entry_status','entry_reason',
                'flip_open','continuation_open','reclaim_open']
            st.download_button('Exportar inventario de entradas y ausencias',
                count_base[[c for c in export_cols if c in count_base]].to_csv(index=False).encode('utf-8'),
                file_name='hll_290_auditoria_entradas_unificada.csv',mime='text/csv',
                key='hll301_entry_audit_export')
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
            grouped=sel.copy()
            grouped['_flip_yes']=grouped.post_outcome.eq('FLIP_CONFIRMADO').astype(int)
            grouped['_cont_yes']=(grouped.continuation_status.eq('CONFIRMED').astype(int)
                                  if ready['V29'] else 0)
            grouped['_reclaim_yes']=(grouped.reclaim_status.eq('CONFIRMED').astype(int)
                                     if ready['V29'] else 0)
            grp=grouped.groupby(keys,dropna=False).agg(intentos=('attempt_id','size'),
                simbolos=('symbol','nunique'),flips=('_flip_yes','sum'),
                continuaciones=('_cont_yes','sum'),reclaims=('_reclaim_yes','sum'))
            st.dataframe(grp.reset_index().sort_values('intentos',ascending=False).head(200),
                         use_container_width=True,hide_index=True)
            st.download_button('Exportar comparativa estructural filtrada',
                grp.reset_index().to_csv(index=False).encode('utf-8'),
                file_name='hll_290_estructuras_mecanismos_rutas.csv',mime='text/csv',key='hll301_structure_export')
    if sel.empty:st.info('No hay eventos bajo estos filtros.');return True
    sel=sel.sort_values('breakout_closed_ts',ascending=False)
    chosen=sel.head(1200).copy()
    if len(sel)>1200:st.caption('Para evitar un selector gigante se muestran los 1.200 episodios más recientes; filtrá por símbolo para buscar otros.')
    chosen['pick']=(chosen.symbol+' | '+pd.to_datetime(chosen.breakout_closed_ts,unit='ms',utc=True).dt.strftime('%Y-%m-%d %H:%M')+' | '+chosen.attempt_id.astype(str).str[:10])
    episode_labels=dict(zip(chosen.attempt_id,chosen['pick']))
    eid=st.selectbox('Episodio para inspección',chosen.attempt_id.tolist(),format_func=lambda x:episode_labels.get(x,x),key='hll301_episode')
    r=chosen.loc[chosen.attempt_id.eq(eid)].iloc[0]
    st.markdown(f"**{r.symbol} · {PATTERNS.get(r[field],str(r[field]))}** · `{r.v26_status}`")
    if not blind:
        st.markdown('**Diagnóstico de entrada de este intento**')
        st.write({'Estado':r.entry_status,'Motivo':r.entry_reason,
                 'Flip':r.post_outcome,
                 'Continuación V2.9':r.get('continuation_status','NO_EVALUADA'),
                 'Reclaim V2.9':r.get('reclaim_status','NO_EVALUADA')})
        st.caption('El detector solo puede utilizar información disponible al confirmar cada ruta. '
                   'Una subida posterior en el gráfico no confirma retrospectivamente una entrada.')
    post=st.checkbox('Dibujar desenlaces y rutas POST',value=not blind,disabled=blind,key='hll301_show_post')
    post_n=st.selectbox('Velas 30m POST',[24,48,96,160,240],index=3,key='hll301_post_n')
    show_flip_pivots=st.checkbox('Mostrar pivotes conocidos al confirmar el flip',value=False,
         disabled=blind or not post,key='hll301_flip_pivots')
    path=root/CANDLES/f'{r.symbol}_30m.csv'
    if not path.is_file():st.warning(f'No hay archivo de velas {path}');return True
    bars=pd.read_csv(path,usecols=['timestamp','open','high','low','close'],float_precision='round_trip')
    bars=bars.sort_values('timestamp').reset_index(drop=True)
    if post:
        actual=int((bars.timestamp.ge(int(r.breakout_closed_ts)) &
                    bars.timestamp.lt(int(r.breakout_closed_ts)+int(post_n)*1800000)).sum())
        st.caption(f'Cobertura del gráfico: {actual}/{post_n} velas POST de 30m disponibles. '
                   'El PnL utiliza recorridos independientes de 1m, no el número visible de velas 30m.')
        if actual<post_n:
            st.warning('El gráfico POST termina antes de la ventana visual solicitada. '
                       'Eso no equivale a una trayectoria de 1m completa o incompleta.')
    events=ev.loc[ev.attempt_id.eq(str(eid))].to_dict('records') if ready['V29'] and post else []
    # Full 15-symbol inspector's original entry/guide controls are also available
    # for sep290. All three confirmed routes use the same underlying helpers.
    st.markdown('#### Entradas observadas y guías TP/SL en el gráfico')
    display_entries=st.checkbox('Marcar OPEN de flip / continuación / reclaim',value=True,
         disabled=blind or not post,key='hll301_show_entries')
    display_breakout=st.checkbox('Mostrar también OPEN del CROSS_UP (solo referencia, no entrada)',
         value=False,disabled=blind or not post or not display_entries,key='hll301_breakout_reference')
    display_guides=st.checkbox('Líneas TP +1% a +5% / SL −1% a −5%',value=True,
         disabled=blind or not post,key='hll301_tp_sl_guides')
    anchors=[];breakout_refs=[];selected_reference=None;ref_key=None
    if post and not blind:
        try:
            anchors,breakout_refs=visual_entries_for_episode(r,bars,events,root/OUT/'minutes')
        except (OSError,ValueError,KeyError,TypeError) as ex:
            st.error(f'Entrada confirmada sin OPEN compatible: {ex}. No se dibujan TP/SL no verificados.')
        if anchors:
            st.caption(f'{len(anchors)} ruta(s) con OPEN observado en este intento; la ruptura original no cuenta como entrada.')
            if display_guides:
                options={x['key']:x for x in anchors}
                preference=['FLIP_1M','V29_CONTINUATION_30M','V29_RECLAIM_30M']
                default=next((x for x in preference if x in options),next(iter(options)))
                ref_key=st.selectbox('Entrada de referencia para las líneas TP/SL',list(options),
                    index=list(options).index(default),
                    format_func=lambda x:options[x]['label'],key='hll301_guide_reference')
                selected_reference=options[ref_key]
                max_pct=st.select_slider('Mostrar líneas hasta ±',options=[1,2,3,4,5],value=5,key='hll301_guide_pct')
                ca,cb=st.columns(2)
                with ca:show_tp=st.checkbox('Líneas TP +%',value=True,key='hll301_guide_tp')
                with cb:show_sl=st.checkbox('Líneas SL −%',value=True,key='hll301_guide_sl')
        else:
            st.info('Este episodio aún no tiene OPEN de entrada confirmado y verificable. '
                    'No se dibujan TP/SL; CROSS_UP por sí solo no es una operación.')
    try:
        fig=make_chart(r,bars,window,post,show_flip_pivots,events,post_n)
        if selected_reference is not None and (show_tp or show_sl):
            from dashboard.views.hll_v292_percent_guides import overlay_visual_levels
            fig=overlay_visual_levels(fig,selected_reference,max_pct=max_pct,
                                      include_tp=show_tp,include_sl=show_sl)
        if display_entries and post and not blind:
            from dashboard.views.hll_v293_entry_markers import overlay_entry_markers
            fig=overlay_entry_markers(fig,anchors+(breakout_refs if display_breakout else []),
                                      selected_reference=ref_key)
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
    if anchors:
        st.dataframe(pd.DataFrame([{'Ruta':x['short'],'Entrada UTC':_date(x['entry_ts']).strftime('%Y-%m-%d %H:%M'),
              'OPEN observado':x['price'],'Nivel base de guías':x['key']==ref_key} for x in anchors]),
              hide_index=True,use_container_width=True)
    if post and events:
        with st.expander('Detalles V2.9 · continuación y reclaim (sin filtrar por resultado)',expanded=False):
            st.dataframe(pd.DataFrame(events)[['branch','status','confirmed_ts','next_open','displacement_atr']],
                         hide_index=True,use_container_width=True)
    if not blind:
        st.markdown('#### Matriz TP/SL 1%–5% · primer toque real en 1m')
        scope='FIRST_PER_LEVEL' if unit=='FIRST_PER_LEVEL' else 'ALL'
        ent,scens=_episode_entries(str(r.symbol),str(eid),scope)
        available={x['key']:x for x in anchors}
        if ent is not None and not ent.empty:
            st.caption('Evaluación histórica de la corrida integral V3.0.1: solo entradas verificadas.')
            view_cols=[c for c in ['route','entry_utc','entry_price','entry_id'] if c in ent]
            st.dataframe(ent[view_cols],hide_index=True,use_container_width=True)
        if available:
            route_sel=st.selectbox('Ruta de entrada para la matriz',list(available),
                       format_func=lambda k:available[k]['description'],key='hll301_matrix_route')
            horizon=st.selectbox('Horizonte después de la entrada',[120,720,2880],
                       format_func=lambda h:f'{h//60} horas',index=1,key='hll301_horizon')
            st.markdown('**Costes estimados para evaluación individual**')
            ca,cb=st.columns(2)
            with ca:fee_pct=st.number_input('Comisión total (%)',min_value=0.,max_value=5.,value=.1,step=.05,key='hll301_fee')
            with cb:slippage_pct=st.number_input('Deslizamiento total (%)',min_value=0.,max_value=5.,value=0.,step=.05,key='hll301_slip')
            chosen=available[route_sel]
            # Prefer previously computed scenarios only when they match frozen
            # costs. For different costs, recalculate from verified 1m OHLC.
            already=(scens.loc[scens.horizon_min.eq(horizon)&scens.route.eq(route_sel)].copy()
                     if scens is not None and not scens.empty else pd.DataFrame())
            if not already.empty and fee_pct==.1 and slippage_pct==0.:
                render_matrix_25(st,already,f'{chosen["description"]} · {horizon//60} horas')
            else:
                minute_path=root/OUT/'minutes'/f'{r.symbol}_1m.csv'
                if not minute_path.is_file():
                    st.info('Matriz individual pendiente: faltan las velas completas de 1m de este símbolo. '
                            'La estructura y las entradas de 30m ya se pueden inspeccionar.')
                elif st.button('Calcular las 25 combinaciones para esta entrada',key='hll301_eval_individual'):
                    try:
                        from dashboard.views.hll_v301_full290 import minute_status,digest,PRE
                        source={'pre_sha256':digest(root/PRE)}
                        if minute_status(str(r.symbol),source)!='FULL':
                            raise ValueError('El histórico 1m no tiene manifiesto completo y compatible')
                        result=evaluate_local_25(minute_path,chosen,horizon,float(fee_pct),float(slippage_pct))
                        render_matrix_25(st,result,f'{chosen["description"]} · {horizon//60} horas')
                    except (ValueError,OSError,KeyError) as ex:
                        st.error(f'No se calculó la matriz: {ex}')
        else:
            st.info('Matriz individual pendiente: ninguna ruta tiene OPEN verificable en este episodio.')
        st.markdown('#### Matriz estadística agregada · todas las estructuras y rutas')
        matrix=root/OUT/f'entries_{scope}'/'matrix_by_route_pattern_month.csv'
        if not matrix.is_file():
            st.info('La matriz agregada todavía no fue evaluada para los 290. '
                    'Primero deben completarse los históricos 1m, V2.9.2, las entradas y su finalización. '
                    'No se utilizan resultados del core15 para simular los 290.')
        else:
            with st.expander('Comparación agregada: Flip / Continuación / Reclaim · TP/SL 1–5%',expanded=True):
                sm=read_cached_csv(matrix,matrix.stat().st_mtime_ns)
                if ready['V292'] and mech_name!='TODOS' and window!=48:
                    st.warning('La agregación por mecanismo está congelada a PRE48. '
                               'Elegí ventana 48 para filtrar por mecanismo.')
                sm=_visible_summary_rows(sm,window,pat,mech_name if window==48 else 'TODOS',ready['V292'])
                c1,c2,c3=st.columns(3)
                with c1:
                    group=st.selectbox('Grupo',['AMBOS','ORIGINAL_15','ADDITIONAL_275'],key='hll301_cmp_group')
                with c2:
                    month=st.selectbox('Mes',['TODOS']+sorted(sm.entry_month.dropna().astype(str).unique()),key='hll301_cmp_month')
                with c3:
                    hh=st.selectbox('Horizonte',[120,720,2880],index=1,
                        format_func=lambda h:f'{h//60} horas',key='hll301_cmp_h')
                if group!='AMBOS':sm=sm[sm.universe_group.eq(group)]
                if month!='TODOS':sm=sm[sm.entry_month.astype(str).eq(month)]
                sm=sm[sm.horizon_min.eq(hh)]
                st.caption('Matriz preagregada por clasificación PRE, mecanismo 48, ruta, mes y grupo. '
                    'Los filtros de símbolo individual, desenlace POST y etiqueta de continuidad NO aplican '
                    'a estos totales; no se muestra como si siguiera esa selección. '
                    'Coste congelado: comisión 0,10%, deslizamiento 0%. No es un portfolio.')
                if sm.empty:
                    st.info('Sin resultados bajo esos criterios.')
                else:
                    for route_name,part in sm.groupby('route'):
                        st.markdown(f'**{route_name}**')
                        # Aggregate month and PRE-mechanism cells with proper known denominators.
                        work=part.assign(weighted_net=part.mean_net_known_pct.mul(part.known))
                        agg=work.groupby(['sl_pct','tp_pct'],as_index=False).agg(
                            entries=('entries','sum'),known=('known','sum'),
                            tp_first=('tp_first','sum'),sl_first=('sl_first','sum'),
                            timeout=('timeout','sum'),ambiguous=('ambiguous','sum'),
                            censored=('censored','sum'),weighted_net=('weighted_net','sum'))
                        agg['net_mean_pct']=agg.weighted_net.div(agg.known.replace(0,float('nan')))
                        agg=agg.drop(columns=['weighted_net'])
                        g=agg.pivot(index='sl_pct',columns='tp_pct',values='net_mean_pct').reindex(index=range(1,6),columns=range(1,6))
                        g.index=[f'SL −{i}%' for i in range(1,6)]
                        g.columns=[f'TP +{i}%' for i in range(1,6)]
                        st.dataframe(g,use_container_width=True)
                        with st.expander(f'Detalle: cobertura y desenlaces de {route_name}',expanded=False):
                            st.dataframe(agg,hide_index=True,use_container_width=True)
                    st.download_button('Exportar matriz agregada filtrada',sm.to_csv(index=False).encode('utf-8'),
                       file_name=f'hll_290_aggregate_{scope}.csv',mime='text/csv',key='hll301_csv')
                    st.warning('Exploratorio: no optimizar TP/SL retrospectivamente ni tratar niveles correlacionados '
                               'como operaciones independientes.')
    return True
