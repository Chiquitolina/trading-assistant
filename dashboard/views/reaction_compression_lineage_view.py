"""Compression -> breakout -> retest atlas. Research-only Streamlit view."""
from __future__ import annotations
from pathlib import Path
import json
from contextlib import closing
from collections import Counter

from reaction_compression_lineage import ROOT_REL, OUT_NAME, hash_file, ro, _merge_bar_data, derive_30m, SCALE_GROUPS, SCALE_LABELS, M

STATE_NAME={
 'BROKEN_RETEST_KNOWN':'Ruptura + retest del límite (vela cerrada)',
 'BROKEN_NEAR_WALL_PRE':'Ruptura + precio próximo al límite antes del retest',
 'BROKEN_NO_RETEST':'Ruptura sin retest del límite',
 'INSIDE_BOX':'REACTION dentro de caja sin ruptura',
 'OPPOSITE_FIRST':'Primera ruptura contraria a la dirección',
 'NO_BREAK_BEFORE_RETEST':'Caja sin ruptura previa',
 'NO_QUALIFIED_BOX':'Ninguna caja calificada en rejilla evaluada',
 'INSUFFICIENT_HISTORY':'Histórico insuficiente',
 'GAP_AFTER_BOX':'Faltan velas entre caja y retest'}


def joined_rows(project,source_events):
    import pandas as pd
    folder=project/ROOT_REL/OUT_NAME
    cp=folder/'lineage_structures.csv';sp=folder/'summary.json'
    if not cp.exists() or not sp.exists():return None,None
    summary=json.loads(sp.read_text(encoding='utf-8'))
    if hash_file(cp)!=summary['sha256']:raise RuntimeError('El CSV de estructuras no coincide con el checksum auditado.')
    if source_events['event_key'].duplicated().any():raise RuntimeError('Duplicados en la selección de resultados.')
    structures=pd.read_csv(cp,low_memory=False)
    if len(structures)!=summary['rows']:raise RuntimeError('Número de estructuras distinto del resumen')
    return structures,summary


def summarize(frame,min_resolved=20):
    """Keep missing coverage, NO_HIT, ambiguity explicit; never hide denominator."""
    import pandas as pd
    if frame.duplicated('event_key').any():raise ValueError('Duplicate events bias statistics')
    result=[]
    for state,group in frame.groupby('lineage_state',dropna=False):
        n=len(group);count=Counter(group['outcome'].fillna('SIN_PATH'))
        tp=count.get('TP_FIRST',0);sl=count.get('SL_FIRST',0)
        result.append({'estructura':STATE_NAME.get(state,state),'codigo':state,
         'REACTIONs':n,'TP_FIRST':tp,'SL_FIRST':sl,
         'NO_HIT':count.get('NO_HIT',0),'AMBIGUOUS':count.get('AMBIGUOUS',0),
         'SIN_PATH':count.get('SIN_PATH',0),'NOT_ENTERED':count.get('NOT_ENTERED',0),
         'TP_resueltas_%':round(100*tp/(tp+sl),2) if tp+sl>=min_resolved else None,
         'fechas':group['date_utc'].nunique(),'simbolos':group['symbol'].nunique()})
    return pd.DataFrame(result).sort_values('REACTIONs',ascending=False)


def chart_event(project,row):
    import pandas as pd
    import plotly.graph_objects as go
    from reaction_compression_lineage import TF
    tf=row['timeframe'];r=int(row['retest_ts']);s=row['symbol']
    if pd.isna(row['box_start_ts']):return None
    start=int(row['box_start_ts'])-TF[tf]*5
    with closing(ro(project/ROOT_REL/'reaction_full_context_v1.sqlite3')) as con:
        ctx15,_=_merge_bar_data(con,None,s,'15m')
        ctx30,_=_merge_bar_data(con,None,s,'30m')
    if tf=='15m':candles=ctx15
    else:
        derived=derive_30m(ctx15)
        candles=pd.concat([ctx30,derived[~derived.timestamp.isin(ctx30.timestamp)]],ignore_index=True)
    # The optional cache contributes native prehistory if available.
    extra=project/ROOT_REL/'reaction_compression_history_v1.sqlite3'
    if extra.exists():
        with closing(ro(project/ROOT_REL/'reaction_full_context_v1.sqlite3')) as ctx, closing(ro(extra)) as hist:
            native,_=_merge_bar_data(ctx,hist,s,tf)
            extended15,_=_merge_bar_data(ctx,hist,s,'15m')
        if tf=='15m':candles=native
        else:
            derived=derive_30m(extended15)
            candles=pd.concat([native,derived[~derived.timestamp.isin(native.timestamp)]],ignore_index=True)
    # Do not display a higher-timeframe candle that was still open at REACTION.
    candles=candles[(candles.timestamp>=start)&(candles.timestamp+TF[tf]<=r)].sort_values('timestamp')
    if candles.empty:return None
    x=pd.to_datetime(candles.timestamp,unit='ms',utc=True)
    fig=go.Figure(go.Candlestick(x=x,open=candles.open,high=candles.high,
        low=candles.low,close=candles.close,name=tf))
    begin=pd.to_datetime(int(row['box_start_ts']),unit='ms',utc=True)
    frozen=pd.to_datetime(int(row['box_last_close_ts']),unit='ms',utc=True)
    fig.add_hrect(y0=float(row['box_low']),y1=float(row['box_high']),
        fillcolor='rgba(39,156,147,.06)',line_width=0)
    fig.add_shape(type='rect',xref='x',yref='y',x0=begin,x1=frozen,
       y0=float(row['box_low']),y1=float(row['box_high']),
       line=dict(color='#16a085',dash='dot',width=1.6),fillcolor='rgba(22,160,133,.07)')
    wall=float(row['wall']);fig.add_hline(y=wall,line_dash='dash',line_color='#d7a453',
                                  annotation_text='Pared congelada')
    if pd.notna(row['breakout_ts']):
        fig.add_vline(x=pd.to_datetime(int(row['breakout_ts']),unit='ms',utc=True),
             line_color='#159b84',line_dash='dot')
    # Marker is the close of the exact 1m REACTION candle: not a guessed 15m/30m price.
    with closing(ro(project/ROOT_REL/'reaction_full_context_v1.sqlite3')) as ctx:
        exact=ctx.execute(
            "SELECT open,high,low,close FROM candles WHERE symbol=? AND timeframe='1m' AND open_ts=?",
            (s,r)).fetchone()
    point=pd.to_datetime(r+M,unit='ms',utc=True)
    if exact is not None:
        direction=str(row['side'])
        fig.add_trace(go.Scatter(x=[point],y=[float(exact[3])],mode='markers+text',
            marker=dict(symbol='triangle-up' if direction=='LONG' else 'triangle-down',
                        color='#16a085' if direction=='LONG' else '#e87985',size=15,
                        line=dict(width=1.5,color='#ffffff')),
            text=[f'REACTION {direction}'],textposition='top center',
            textfont=dict(size=11,color='#f2d486'),name='REACTION 1m cerrada',
            hovertemplate=(f'REACTION {direction}<br>Vela 1m cerrada: %{{x}}<br>'
                           f'O: {float(exact[0]):.8g} · H: {float(exact[1]):.8g}<br>'
                           f'L: {float(exact[2]):.8g} · C: %{{y:.8g}}<extra></extra>')))
    fig.add_vline(x=point,line_color='#d66d74',line_dash='dash')
    fig.update_xaxes(range=[x.iloc[0],pd.to_datetime(r+2*TF[tf],unit='ms',utc=True)])
    fig.update_layout(height=490,xaxis_rangeslider_visible=False,

       title=f'{s} {row["side"]} · {tf} · caja congelada, ruptura y retest',
       margin=dict(t=45,r=10,b=10,l=10))
    return fig


def render_lineage_atlas(frame,project_root):
    import pandas as pd
    import streamlit as st
    from dashboard.services.reaction_reverse_engineering import one_per_symbol_wave
    project=Path(project_root)
    st.markdown('### 🗜️ Compression Lineage — cajas, rupturas y retests')
    st.caption('Escalas CORTA 15m (20–30), INTERMEDIA 15m (40–80), AMPLIA 30m (40–80); base SEPARADA 2×. Clasificación independiente de Structural Level Atlas. El nivel y la reacción se muestran sin datos posteriores en los gráficos.')
    store=project/ROOT_REL/OUT_NAME/'lineage_structures.csv'
    if not store.is_file():
        st.info('Primero construir el replay histórico offline en el VPS: `python3 reaction_compression_lineage.py --plan` y `--run --max-events 100`. Para el universo completo: `--run` sin límite.')
        st.caption('96 velas 15m equivalen a 24 horas, insuficientes para compresiones 30m de 40–80 velas con referencia 2×. Las faltantes aparecen como INSUFFICIENT_HISTORY.')
        return
    @st.cache_data(show_spinner=False,max_entries=2)
    def cached(root,modified):return joined_rows(Path(root),frame)
    try:struct,meta=cached(str(project),store.stat().st_mtime_ns)
    except Exception as exc:st.error(str(exc));return
    first=st.columns(4)
    first[0].metric('REACTIONs evaluadas',f"{meta['events']:,}")
    first[1].metric('Del universo congelado',f"{meta['total_frozen_events']:,}")
    first[2].metric('Estructuras evaluadas',f"{len(struct):,}")
    first[3].metric('Universo completo','Sí' if meta['complete_universe'] else 'Piloto')
    if not meta['complete_universe']:st.warning('PILOTO PARCIAL: no extrapoles los porcentajes al universo completo.')
    controls=st.columns([1.3,1,1])
    scale=controls[0].selectbox('Escala estructural',list(SCALE_GROUPS),
         format_func=lambda v:SCALE_LABELS[v],key='rql_lineage_scale')
    tf,expected_sizes=SCALE_GROUPS[scale]
    opts=[n for n in expected_sizes if n in meta['windows'].get(tf,[])]
    if not opts:
        st.warning('El replay guardado es anterior a esta escala. Reejecutá --run y --audit para incluirla.');return
    n=controls[1].selectbox('Duración compresión',opts,key='rql_lineage_n',
                         format_func=lambda v:f'{v} velas ({v*TF[tf]/(60*M):g}h), base {2*v}')
    group=controls[2].selectbox('Cobertura',['Todas','Solo búsqueda completa','Completas + estructuras detectadas'],key='rql_lineage_cov')
    dat=struct[struct.timeframe.eq(tf)&struct.window.eq(n)].merge(
       frame[['event_key','outcome','entry_price','entry_ts','entry_delay_min',
            'path_quality']],on='event_key',how='left',validate='one_to_one')
    # All chosen filters (LIVE, day etc) already apply to frame. For unbiased group comparisons
    # use the full result-filter-independent source from the parent.
    if group=='Solo búsqueda completa':dat=dat.loc[dat.coverage.eq('COMPLETE_SEARCH')].copy()
    if group=='Completas + estructuras detectadas':
        dat=dat.loc[dat.coverage.eq('COMPLETE_SEARCH') | dat.snapshots_qualified.gt(0)].copy()
    if dat.empty:st.info('No hay eventos evaluables en esta selección');return
    counts=dat.coverage.value_counts()
    st.caption('Cobertura: '+', '.join(f'{k}: {v}' for k,v in counts.items()))
    if tf=='30m':st.warning('Si `history_source` contiene PROXY, las velas 30m se reconstruyeron de 15m; no son 30m nativas verificadas. Considerá ampliar el histórico independiente.')
    valid=dat.loc[dat.coverage.eq('COMPLETE_SEARCH') | dat.snapshots_qualified.gt(0)].copy()
    if valid.empty:st.warning('Sin búsqueda completa ni estructuras positivas. No se puede concluir que no hubo compresión.');return
    # No qualified candidate with incomplete coverage is censored (not a clean negative).
    valid=valid[~((valid.coverage!='COMPLETE_SEARCH')&valid.lineage_state.eq('NO_QUALIFIED_BOX'))]
    summary=summarize(valid)
    st.markdown('#### Resultados por tipo estructural')
    st.dataframe(summary,hide_index=True,use_container_width=True)
    st.caption('TP primero % = TP / (TP+SL), sin costos y solo entre resueltos. NO_HIT y eventos sin trayectoria permanecen explícitos. La prueba de retest de pared utiliza la vela 1m YA CERRADA (KNOWN), no el instante anterior.')
    st.markdown('#### Repetibilidad cronológica')
    dates=sorted(valid.date_utc.astype(str).unique())
    if len(dates)>=4:
        cut=max(1,min(len(dates)-1,round(len(dates)*.60)))
        parts=[]
        for name,ds in [('Días iniciales',dates[:cut]),('Días posteriores',dates[cut:])]:
            x=summarize(valid[valid.date_utc.isin(ds)],min_resolved=15)
            if len(x):x.insert(0,'periodo',name);parts.append(x)
        if parts:st.dataframe(pd.concat(parts,ignore_index=True)[['periodo','estructura','REACTIONs','TP_FIRST','SL_FIRST','TP_resueltas_%','fechas']],hide_index=True,use_container_width=True)
    st.markdown('#### Coincidencias entre timeframes — COMPLEMENTARIAS, nunca requisito')
    confl_path=project/ROOT_REL/OUT_NAME/'lineage_confluence.csv'
    if confl_path.is_file() and hash_file(confl_path)==meta.get('confluence_sha256'):
        confluence=pd.read_csv(confl_path,low_memory=False)
        joined=confluence.merge(frame[['event_key','outcome','date_utc']],on='event_key',how='inner',
             validate='one_to_one',suffixes=('','_replay'))
        before=len(joined)
        if len(joined):
            # One row per event, not one row per timeframe.
            totals=joined.groupby('confluence',dropna=False).agg(
                opportunities=('event_key','size'),
                TP_FIRST=('outcome',lambda x:int(x.eq('TP_FIRST').sum())),
                SL_FIRST=('outcome',lambda x:int(x.eq('SL_FIRST').sum())),
                NO_HIT=('outcome',lambda x:int(x.eq('NO_HIT').sum())),
                symbols=('symbol','nunique')).reset_index()
            totals['TP_resueltas_%']=(100*totals.TP_FIRST/(totals.TP_FIRST+totals.SL_FIRST)).round(2)
            totals.loc[(totals.TP_FIRST+totals.SL_FIRST)<20,'TP_resueltas_%']=float('nan')
            st.dataframe(totals,hide_index=True,use_container_width=True)
            st.caption(f'{before} REACTIONs distintas. `NO_MATCH_INCOMPLETE_COVERAGE` nunca se interpreta como ausencia de compresión.')
    else:st.warning('No se encuentra o no valida la tabla de confluencia. Reejecutar `--run` y `--audit`.')
    st.markdown('#### Inspector: caja histórica → ruptura → REACTION')
    picks=valid[valid.snapshots_qualified.gt(0)].sort_values('retest_ts',ascending=False).head(1000)
    if picks.empty:st.info('Aún no se identificaron cajas calificadas');return
    choices={r.event_key:f'{r.symbol} · {r.side} · {r.date_utc} · {r.lineage_state}' for r in picks.itertuples()}
    chosen=st.selectbox('REACTION para inspeccionar',list(choices),format_func=choices.get,key='rql_lineage_pick')
    selected=picks[picks.event_key.eq(chosen)].iloc[0]
    fig=chart_event(project,selected)
    if fig:st.plotly_chart(fig,use_container_width=True)
    st.caption('Triángulo = precio de cierre de la vela 1m de REACTION (conocido al cerrar); línea roja = su cierre. Las velas HTF dibujadas están completas antes de la señal.')
    st.dataframe(pd.DataFrame([selected.to_dict()]),hide_index=True,use_container_width=True)
    st.caption('Una caja NO se define mirando su ruptura posterior. La detección ocurre antes; la ruptura se comprueba después y siempre antes del retest. Investigación retrospectiva, no confirmación de edge.')
    st.download_button('Descargar cruces estructurales CSV',
       valid.to_csv(index=False).encode('utf-8'),file_name='reaction_compression_lineage_selection.csv',
       mime='text/csv',key='rql_lineage_download')
