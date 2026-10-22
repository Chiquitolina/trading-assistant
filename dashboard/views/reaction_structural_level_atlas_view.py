"""Read-only Structural Level Atlas panel; no live market API, bot or Redis."""
from __future__ import annotations
import json
from pathlib import Path
from contextlib import closing


def load(project):
    import pandas as pd
    import reaction_structural_level_atlas as atlas
    folder=Path(project)/atlas.lineage.ROOT_REL/atlas.OUTPUT
    m=json.loads((folder/'summary.json').read_text(encoding='utf-8'))
    if m['version']!=atlas.VERSION:raise RuntimeError('Versión del Atlas no coincide')
    if any(atlas.lineage.hash_file(folder/name)!=digest for name,digest in m['file_sha256'].items()):
        raise RuntimeError('SHA256 del Atlas inválido, ejecutar --audit')
    frame=pd.read_csv(folder/'levels_by_timeframe.csv',low_memory=False)
    events=pd.read_csv(folder/'event_best.csv',low_memory=False)
    if len(frame)!=2*len(events) or len(events)!=m['events'] or events.event_key.duplicated().any():
        raise RuntimeError('Atlas: cantidad o identidad inconsistentes')
    return frame,events,m


def wilson(wins,losses):
    n=wins+losses
    if not n:return float('nan'),float('nan')
    p=wins/n;z=1.96;den=1+z*z/n
    mid=(p+z*z/(2*n))/den
    half=z*(p*(1-p)/n+z*z/(4*n*n))**.5/den
    return max(0,100*(mid-half)),min(100,100*(mid+half))


def summarize(data,column,min_n=30):
    import pandas as pd
    result=[]
    for label,g in data.groupby(column,dropna=False):
        tp=int(g.outcome.eq('TP_FIRST').sum());sl=int(g.outcome.eq('SL_FIRST').sum())
        nohit=int(g.outcome.eq('NO_HIT').sum());ambig=int(g.outcome.eq('AMBIGUOUS').sum())
        valid=tp+sl
        lo,hi=wilson(tp,sl)
        result.append({'Categoría':str(label),'REACTIONs':len(g),'TP':tp,'SL':sl,
            'NO_HIT':nohit,'AMBIGUOUS':ambig,'Resueltas':valid,
            'TP-first %':round(100*tp/valid,2) if valid>=min_n else None,
            'Wilson 95% mín':round(lo,2) if valid>=min_n else None,
            'Wilson 95% máx':round(hi,2) if valid>=min_n else None,
            'Días':g.date_utc.nunique(),'Símbolos':g.symbol.nunique()})
    return pd.DataFrame(result).sort_values('REACTIONs',ascending=False) if result else pd.DataFrame()


def diagnostic_comparison(frame):
    """Within-day/side contrast; descriptive only, no selection or tuning."""
    import pandas as pd
    z=frame[frame.outcome.isin(['TP_FIRST','SL_FIRST'])].copy()
    groups=[]
    for (day,side),all_day in z.groupby(['date_utc','side']):
        base=all_day.outcome.eq('TP_FIRST').mean()
        g=all_day[all_day.selection_pre_near_level.eq(True)]
        for cat,gg in g.groupby('selection_state_pre'):
            if len(gg)<5:continue
            groups.append({'Fecha':day,'Dirección':side,'Categoría':cat,'N':len(gg),
               'TP%':round(100*gg.outcome.eq('TP_FIRST').mean(),1),
               'Base grupo%':round(100*base,1),
               'Diferencia pp':round(100*(gg.outcome.eq('TP_FIRST').mean()-base),1)})
    return pd.DataFrame(groups)


def chart_level(project,event_row,timeframe_row):
    import pandas as pd
    import plotly.graph_objects as go
    import reaction_structural_level_atlas as atlas
    import reaction_compression_breakout_retest_v4 as v4
    from contextlib import closing
    p=Path(project)
    archive=p/atlas.lineage.ROOT_REL/atlas.lineage.CTX_NAME
    history=p/atlas.lineage.ROOT_REL/atlas.lineage.HISTORY_NAME
    hist=atlas.lineage.ro(history) if history.is_file() else None
    try:
        with closing(atlas.lineage.ro(archive)) as ctx:
            df15,df30,src=v4._merge_tf(ctx,hist,str(event_row['symbol']))
    finally:
        if hist is not None:hist.close()
    df=(df15 if str(timeframe_row['timeframe'])=='15m' else df30).reset_index()
    tf=str(timeframe_row['timeframe']);step=atlas.STEP[tf]
    r=int(event_row['retest_ts'])
    df=df[(df.timestamp+step<=r)&(df.timestamp>=r-96*step)].copy()
    if df.empty:return None
    date=pd.to_datetime(df.timestamp,unit='ms',utc=True)
    fig=go.Figure(go.Candlestick(x=date,open=df.open,high=df.high,low=df.low,close=df.close,
                                  increasing_line_color='#18a58b',decreasing_line_color='#d87979',name=tf))
    if pd.notna(timeframe_row['level_price']):
        zl=float(timeframe_row['zone_low']);zh=float(timeframe_row['zone_high']);price=float(timeframe_row['level_price'])
        fig.add_hrect(y0=zl,y1=zh,fillcolor='#0d9488',opacity=.12,line_width=0)
        fig.add_hline(y=price,line_color='#0d9488',line_dash='dash',annotation_text='Zona congelada')
        form=timeframe_row.get('formation_closed_ts')
        if pd.notna(form):
            xform=pd.to_datetime(int(form),unit='ms',utc=True)
            fig.add_shape(type='line',x0=xform,x1=xform,y0=zl,y1=zh,
                          line=dict(color='#64748b',width=1,dash='dot'))
    # The marker uses the archived exact REACTION 1m candle (only KNOWN after close).
    with closing(atlas.lineage.ro(archive)) as ctx:
        reaction=ctx.execute(
            "SELECT open,high,low,close FROM candles WHERE symbol=? AND timeframe='1m' AND open_ts=?",
            (str(event_row['symbol']),r)).fetchone()
    stamp=pd.to_datetime(r+atlas.lineage.M,unit='ms',utc=True)
    if reaction is not None:
        direction=str(event_row['side'])
        fig.add_trace(go.Scatter(x=[stamp],y=[float(reaction[3])],mode='markers+text',
            marker=dict(symbol='triangle-up' if direction=='LONG' else 'triangle-down',
                        color='#16a085' if direction=='LONG' else '#e87985',size=15,
                        line=dict(color='#ffffff',width=1.5)),text=[f'REACTION {direction}'],
            textposition='top center',textfont=dict(color='#f2d486',size=11),
            hovertemplate=(f'REACTION {direction} · vela 1m cerrada<br>'
                           f'O: {float(reaction[0]):.8g} · H: {float(reaction[1]):.8g}<br>'
                           f'L: {float(reaction[2]):.8g} · C: %{{y:.8g}}<extra></extra>')))
    fig.add_vline(x=stamp,line_dash='dash',line_color='#d87979')
    fig.update_xaxes(range=[date.iloc[0],pd.to_datetime(r+2*step,unit='ms',utc=True)])
    fig.update_layout(title=f"{event_row['symbol']} · {tf} · nivel histórico y REACTION",

                      height=460,margin=dict(l=6,r=6,t=40,b=6),
                      xaxis_rangeslider_visible=False,showlegend=False,
                      plot_bgcolor='rgba(0,0,0,0)',paper_bgcolor='rgba(0,0,0,0)')
    return fig


def _render_level_atlas_v1(source_events,project_root):
    import pandas as pd
    import streamlit as st
    import reaction_structural_level_atlas as atlas
    project=Path(project_root)
    folder=project/atlas.lineage.ROOT_REL/atlas.OUTPUT
    st.markdown('### 🧱 Structural Level Atlas V1')
    st.caption('Niveles horizontales INDEPENDIENTES de las compresiones: no se requiere una caja previa. Zonas ATR congeladas al segundo pivote de 15m/30m. '
               'La etiqueta PRE utiliza únicamente cierres anteriores al retest; el toque de 1m es conocido solo después de su cierre.')
    if not (folder/'summary.json').is_file():
        st.info('Primero ejecutá `python3 reaction_structural_level_atlas.py --plan`, '
                'después `--run` y `--audit` desde la raíz del proyecto. No descarga históricos.')
        return
    @st.cache_data(show_spinner=False,max_entries=3)
    def cached(path,mtime):return load(Path(path))
    try:frames,events,meta=cached(str(project),(folder/'summary.json').stat().st_mtime_ns)
    except Exception as exc:
        st.error(f'Atlas: {exc}');return
    # Outcome is injected only after structural features are fixed and checked.
    chosen=source_events[['event_key','outcome','side','date_utc','symbol']].drop_duplicates('event_key')
    x=events.drop(columns=['side','date_utc','symbol']).merge(chosen,on='event_key',how='inner',validate='one_to_one')
    if x.empty:
        st.info('Los filtros superiores no contienen eventos de la muestra evaluada en Atlas.');return
    q=st.columns(4)
    q[0].metric('REACTIONs procesadas',f"{meta['events']:,}")
    q[1].metric('En los filtros',f'{len(x):,}')
    q[2].metric('Con nivel detectado',f"{int(x.has_level.sum()):,}")
    q[3].metric('Cobertura 15m+30m completa',f"{int(x.coverage_all.sum()):,}")
    st.warning('Investigación histórica exploratoria, sin ventaja validada. '
        'El nivel se elige sin conocer TP/SL; la revisión reiterada de los días posteriores impide usarlos como test independiente.')
    if meta['cohort']=='live-stratified':
        st.caption('Cohorte repartida por fecha/dirección: los porcentajes de prevalencia no representan el universo real sin ponderar.')
    if not x.coverage_all.eq(1).all():
        st.info('Algunas estructuras tienen cobertura parcial o insuficiente. No contarlas como ausencia de niveles.')
    a,b,c=st.tabs(['Estados y resultados','Cruces y robustez','Inspector de nivel'])
    with a:
        min_n=st.slider('Mínimo casos resueltos para mostrar porcentajes',10,150,30,10,key='sla_min_n')
        st.markdown('#### Estados causales ANTES del retest')
        st.dataframe(summarize(x,'selection_state_pre',min_n),hide_index=True,use_container_width=True)
        st.markdown('#### Origen del nivel')
        st.dataframe(summarize(x,'selection_level_origin',min_n),hide_index=True,use_container_width=True)
        st.markdown('#### Cercanía ATR al nivel')
        st.dataframe(summarize(x,'selection_pre_near_level',min_n),hide_index=True,use_container_width=True)
        st.markdown('#### Estado conocido AL CERRAR el retest (no preentrada)')
        st.dataframe(summarize(x,'selection_state_known',min_n),hide_index=True,use_container_width=True)
    with b:
        st.markdown('#### Cruce estado previo × LONG/SHORT')
        z=x[x.outcome.isin(['TP_FIRST','SL_FIRST'])]
        if z.empty:st.info('Sin TP/SL resueltos con estos filtros.')
        else:
            counts=z.groupby(['side','selection_state_pre']).outcome.agg(
                n='size',tp=lambda y:int(y.eq('TP_FIRST').sum()),sl=lambda y:int(y.eq('SL_FIRST').sum())).reset_index()
            counts['tp_pct']=100*counts.tp/counts.n
            counts.loc[counts.n<min_n,'tp_pct']=float('nan')
            st.dataframe(counts,hide_index=True,use_container_width=True)
            days=sorted(z.date_utc.unique().tolist())
            if len(days)>=4:
                boundary=days[max(1,int(len(days)*.6))-1]
                z=z.copy();z['período']=z.date_utc.map(lambda d:'Anterior' if d<=boundary else 'Posterior')
                st.markdown('#### Evaluación temporal descriptiva (no holdout independiente)')
                for period,gg in z.groupby('período'):
                    st.caption(period)
                    st.dataframe(summarize(gg,'selection_state_pre',min_n),hide_index=True,use_container_width=True)
            st.markdown('#### Comparaciones por día y dirección')
            matched=diagnostic_comparison(z)
            st.dataframe(matched,hide_index=True,use_container_width=True)
            st.caption('Cada fila compara la categoría con la tasa TP del mismo día/dirección. '
                       'No es un efecto causal: grupos y símbolos pueden estar correlacionados.')
            st.markdown('#### Distancia a zona y defensas anteriores')
            cols=['event_key','symbol','side','date_utc','selection_state_pre','outcome',
                  'selection_pre_distance_atr','selection_defenses','selection_acceptance_last4']
            st.dataframe(x[cols],hide_index=True,use_container_width=True,height=350)
    with c:
        tf=st.selectbox('Timeframe',['15m','30m'],key='sla_tf')
        options=frames[(frames.timeframe==tf)&frames.event_key.isin(x.event_key)].copy()
        options=options[options.level_price.notna()].sort_values('retest_ts',ascending=False)
        if options.empty:st.info('Sin nivel estructural en este timeframe con los filtros actuales.');return
        options['label']=options.symbol+' · '+options.side+' · '+options.date_utc+' · '+options.state_pre
        keys=options.event_key.tolist()
        key=st.selectbox('REACTION',keys,format_func=lambda v:options.loc[options.event_key==v,'label'].iloc[0],key='sla_event')
        r=options[options.event_key==key].iloc[0]
        st.dataframe(pd.DataFrame([r.to_dict()]),hide_index=True,use_container_width=True)
        fig=chart_level(project,r,r)
        if fig is not None:st.plotly_chart(fig,use_container_width=True)
        st.caption('Velas HTF cerradas ANTES del retest; triángulo = precio de cierre de la vela 1m exacta de REACTION (KNOWN después de cerrar). Para el recorrido posterior usá el Inspector por REACTION.')
        st.download_button('Exportar eventos y niveles de estos filtros',x.to_csv(index=False).encode('utf-8'),
                           file_name='structural_level_atlas_filtered.csv',mime='text/csv',key='sla_export')


def render_level_atlas(source_events,project_root):
    """Version chooser: V2 is independent and V1 remains reproducible."""
    import streamlit as st
    import reaction_structural_level_atlas_v2 as v2
    from dashboard.views.reaction_structural_level_atlas_v2_view import render_v2
    folder=Path(project_root)/v2.v1.lineage.ROOT_REL/v2.OUTPUT
    choices=['V2 · Mapa multizona y desenlaces','V1 · Detector original']
    selection=st.radio('Structural Atlas',choices,index=0 if (folder/'summary.json').exists() else 1,
        horizontal=True,key='sla_version_picker')
    if selection==choices[0]:
        render_v2(source_events,project_root)
    else:
        _render_level_atlas_v1(source_events,project_root)
