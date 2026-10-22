"""Structural Level Atlas V2. Read-only UI; multi-level PRE and POST separated."""
from __future__ import annotations
import json
from pathlib import Path
from contextlib import closing


def load(project):
    import pandas as pd
    import reaction_structural_level_atlas_v2 as m
    folder=Path(project)/m.v1.lineage.ROOT_REL/m.OUTPUT
    meta=json.loads((folder/'summary.json').read_text(encoding='utf-8'))
    if meta['version']!=m.VERSION:raise RuntimeError('V2 esquema anterior, repetir --run')
    if any(m.v1.lineage.hash_file(folder/file)!=digest for file,digest in meta['files_sha256'].items()):
        raise RuntimeError('Hash V2 inválido: ejecutar --audit')
    levels=pd.read_csv(folder/'candidate_levels.csv',low_memory=False)
    events=pd.read_csv(folder/'event_map.csv',low_memory=False)
    if len(events)!=meta['events'] or len(levels)!=meta['candidate_levels'] or events.event_key.duplicated().any():
        raise RuntimeError('Cantidad de filas o evento duplicado en Atlas V2')
    return levels,events,meta


def _stats(df,field,min_n):
    import pandas as pd
    records=[]
    for name,g in df.groupby(field,dropna=False):
        tp=int(g.outcome.eq('TP_FIRST').sum());sl=int(g.outcome.eq('SL_FIRST').sum());n=tp+sl
        if n:
            p=tp/n;w=1.96;den=1+w*w/n
            center=(p+w*w/(2*n))/den
            spread=w*((p*(1-p)/n+w*w/(4*n*n))**.5)/den
            low=100*max(0,center-spread);high=100*min(1,center+spread)
        else:low=high=None
        records.append({'Grupo':str(name),'REACTIONs':len(g),'TP':tp,'SL':sl,
            'NO_HIT':int(g.outcome.eq('NO_HIT').sum()),'AMBIGUOUS':int(g.outcome.eq('AMBIGUOUS').sum()),
            'Otros/sin path':int(len(g)-n-g.outcome.eq('NO_HIT').sum()-g.outcome.eq('AMBIGUOUS').sum()),
            'TP entre resueltos (%)':round(100*tp/n,1) if n else None,
            'Wilson 95% min':round(low,1) if n else None,
            'Wilson 95% max':round(high,1) if n else None,
            'Muestra suficiente':n>=min_n,'N resueltos':n,'Días':g.date_utc.nunique(),'Símbolos':g.symbol.nunique()})
    return pd.DataFrame(records).sort_values('REACTIONs',ascending=False) if records else pd.DataFrame()


def _read_structural_figure(project,item,zones,lookback=96,display_tf=None,max_zones=4):
    import pandas as pd
    import plotly.graph_objects as go
    import reaction_structural_level_atlas_v2 as m
    p=Path(project);root=p/m.v1.lineage.ROOT_REL
    cache=root/m.v1.lineage.HISTORY_NAME
    hist=m.v1.lineage.ro(cache) if cache.exists() else None
    try:
        with closing(m.v1.lineage.ro(root/m.v1.lineage.CTX_NAME)) as con:
            df15,df30,_=m.v1.v4._merge_tf(con,hist,str(item.symbol))
            exact=con.execute("SELECT open,high,low,close FROM candles WHERE symbol=? AND timeframe='1m' AND open_ts=?",
                              (str(item.symbol),int(item.retest_ts))).fetchone()
    finally:
        if hist is not None:hist.close()
    tf=str(display_tf or (item.primary_timeframe if pd.notna(item.primary_timeframe) else '15m'))
    step=m.v1.STEP[tf];r=int(item.retest_ts)
    df=(df15 if tf=='15m' else df30).reset_index()
    df=df[(df.timestamp+step<=r)].tail(lookback)
    if df.empty:return None
    x=pd.to_datetime(df.timestamp,unit='ms',utc=True)
    fig=go.Figure(go.Candlestick(x=x,open=df.open,high=df.high,low=df.low,close=df.close,
         name=tf,increasing_line_color='#16a085',decreasing_line_color='#d97779'))
    zz=zones[zones.timeframe.eq(tf)].sort_values('gap_to_zone_atr').head(max_zones)
    for j,(_,z) in enumerate(zz.iterrows()):
        selected=z.zone_id==item.primary_zone_id
        primary_color='#18ab91' if selected else '#92a1b5'
        fig.add_hrect(y0=float(z.zone_low),y1=float(z.zone_high),
            fillcolor=primary_color,opacity=.17 if selected else .07,
            line_width=0)
        # No pasar annotation_text=None: Plotly puede dibujar la etiqueta 'new text'.
        kw={'line_dash':'dash' if selected else 'dot',
            'line_color':primary_color,'opacity':1 if selected else .35}
        if selected:
            kw['annotation_text']='Nivel principal'
        fig.add_hline(y=float(z.level_price),**kw)
    react_stamp=pd.to_datetime(r+m.v1.lineage.M,unit='ms',utc=True)
    if exact:
        fig.add_trace(go.Scatter(x=[react_stamp],y=[float(exact[3])],mode='markers+text',
            marker=dict(symbol='triangle-up' if item.side=='LONG' else 'triangle-down',
                color='#16a085' if item.side=='LONG' else '#e87985',size=13),
            text=['REACTION'],textposition='top center',
            hovertemplate='REACTION: cierre 1m observado después de cerrar: %{y:.8g}<extra></extra>'))
    fig.add_vline(x=react_stamp,line_dash='dash',line_color='#e87985')
    fig.update_layout(height=470,showlegend=False,xaxis_rangeslider_visible=False,
        title=f'{item.symbol} {item.side} · {tf} · niveles congelados antes del retest',
        margin=dict(l=5,r=5,t=45,b=5),plot_bgcolor='rgba(0,0,0,0)',paper_bgcolor='rgba(0,0,0,0)')
    fig.update_xaxes(range=[x.iloc[0],react_stamp+pd.Timedelta(minutes=20)])
    return fig


def _read_post(project,item):
    """Use only archived 360x1m Auto Paths, validate chronological continuity."""
    import reaction_structural_level_atlas_v2 as m
    from reaction_lab_auto_paths import decode_path
    db=Path(project)/m.v1.lineage.ROOT_REL/'auto_paths_v1.sqlite3'
    with closing(m.v1.lineage.ro(db)) as con:
        row=con.execute('SELECT entry_ts,state,count,path_blob FROM events WHERE event_key=?',
            (str(item.event_key),)).fetchone()
    if row is None:return [],'MISSING'
    entry_ts,state,n,blob=row
    if state!='COMPLETE' or int(n)!=360 or int(entry_ts)!=int(item.retest_ts)+60_000:
        return [],str(state)
    bars=decode_path(blob)
    if len(bars)!=360 or any(int(b[0])!=int(entry_ts)+j*60_000 for j,b in enumerate(bars)):
        return [],'INVALID_PATH_CONTINUITY'
    return bars,'COMPLETE'


def _post_figure(item,bars,n_minutes,tp_pct,sl_pct):
    import pandas as pd
    import plotly.graph_objects as go
    part=bars[:n_minutes]
    if not part:return None
    x=pd.to_datetime([r[0] for r in part],unit='ms',utc=True)
    fig=go.Figure(go.Candlestick(x=x,open=[b[1] for b in part],high=[b[2] for b in part],
       low=[b[3] for b in part],close=[b[4] for b in part],
       increasing_line_color='#17b491',decreasing_line_color='#d87679',name='1m POST'))
    entry_ts=getattr(item,'entry_ts',None);price=getattr(item,'entry_price',None)
    if pd.notna(entry_ts) and pd.notna(price) and float(price)>0:
        price=float(price);long=item.side=='LONG'
        ts=pd.to_datetime(int(entry_ts),unit='ms',utc=True)
        fig.add_hline(y=price,line_color='#94a3b8',line_dash='dot',annotation_text='Entrada replay')
        fig.add_hline(y=price*(1+tp_pct/100 if long else 1-tp_pct/100),line_color='#16a085',
                      line_dash='dash',annotation_text=f'TP {tp_pct:g}%')
        fig.add_hline(y=price*(1-sl_pct/100 if long else 1+sl_pct/100),line_color='#e87985',
                      line_dash='dash',annotation_text=f'SL {sl_pct:g}%')
        fig.add_vline(x=ts,line_dash='dot',line_color='#94a3b8')
    minute=getattr(item,'first_touch_minute',None)
    if pd.notna(minute) and float(minute)<=n_minutes:
        # First-touch minute is one-indexed from retest+1m, not entry-relative.
        ts=x[int(minute)-1] if int(minute)>=1 else None
        if ts is not None:
            fig.add_vline(x=ts,line_color='#cba35a',line_dash='dot')
    fig.update_layout(height=435,showlegend=False,xaxis_rangeslider_visible=False,
        title=f'Recorrido POST de Auto Paths 1m · {str(item.outcome)} · {n_minutes} minutos',
        margin=dict(l=5,r=5,t=45,b=5),plot_bgcolor='rgba(0,0,0,0)',paper_bgcolor='rgba(0,0,0,0)')
    return fig


def render_v2(source_events,project_root,tp_pct=1.0,sl_pct=0.5,outcome_horizon=180,entry_mode='OBSERVED_OPEN'):
    import pandas as pd
    import streamlit as st
    import reaction_structural_level_atlas_v2 as m
    project=Path(project_root)
    folder=project/m.v1.lineage.ROOT_REL/m.OUTPUT
    st.markdown('### 🧭 Structural Level Atlas V2 — mapa y relevancia')
    st.caption('Todos los niveles horizontales de pivotes confirmados, 15m/30m. Distancia al nivel y espacio hasta obstáculo contrario ANTES del retest. La entrada/resultado posterior se añaden después; no modifican niveles.')
    if not (folder/'summary.json').exists():
        st.info('Ejecutá `python3 reaction_structural_level_atlas_v2.py --plan`, `--run` y `--audit` desde la raíz. V1 permanece intacto.')
        return
    @st.cache_data(max_entries=2,show_spinner=False)
    def cached(base,mtime):return load(Path(base))
    try:levels,events,summary=cached(str(project),(folder/'summary.json').stat().st_mtime_ns)
    except Exception as exc:st.error(f'Atlas V2: {exc}');return
    df=events.merge(source_events[['event_key','outcome','entry_ts','entry_price','first_touch_minute']].drop_duplicates('event_key'),
       on='event_key',how='inner',validate='one_to_one')
    if df.empty:
        st.info('Los filtros superiores no contienen ninguna REACTION de la muestra Atlas V2.');return
    a,b,c,d=st.columns(4)
    a.metric('REACTIONs V2',len(events));b.metric('En filtros',len(df))
    c.metric('Niveles congelados',len(levels));d.metric('Histórico completo',int(df.all_covered.sum()))
    st.caption('Cohorte estratificada y análisis retrospectivo: prevalencias y diferencias NO validadas fuera de muestra. Cierres del retest (KNOWN) nunca son predictores PRE.')
    tabs=st.tabs(['🎯 Cerca del nivel principal','Mapa y distancias','Familias y resultados','Espacio estructural','Inspector + desenlace'])
    min_n=st.slider('N mínimo para considerar la tasa interpretable (las tasas pequeñas se muestran igual)',5,100,20,5,key='level_v2_min')
    st.caption('Las tasas y Wilson se muestran cuando N>0; “Muestra suficiente” indica si alcanza el mínimo seleccionado.')
    with tabs[0]:
        from dashboard.views.reaction_principal_level_filter_view import render_principal_level_filter
        render_principal_level_filter(
            df,levels,project,tp_pct=tp_pct,sl_pct=sl_pct,
            outcome_horizon=outcome_horizon,entry_mode=entry_mode,
        )
    with tabs[1]:
        st.markdown('#### Nivel directamente relacionado o histórico distante')
        st.dataframe(_stats(df,'primary_contact_band_pre',min_n),use_container_width=True,hide_index=True)
        st.dataframe(df[['symbol','side','date_utc','primary_timeframe','pre_family','primary_state_pre',
            'primary_distance_atr','primary_distance_pct','n_zones_total','outcome']].sort_values('primary_distance_atr'),
            use_container_width=True,hide_index=True,height=350)
    with tabs[2]:
        st.dataframe(_stats(df,'pre_family',min_n),use_container_width=True,hide_index=True)
        st.markdown('#### Cortes de robustez: dirección y fecha')
        st.dataframe(_stats(df,'side',min_n),use_container_width=True,hide_index=True)
        by_day=df.groupby(['date_utc','pre_family'],dropna=False).outcome.agg(
            total='size',tp=lambda y:int(y.eq('TP_FIRST').sum()),
            sl=lambda y:int(y.eq('SL_FIRST').sum())).reset_index()
        by_day['n_resolved']=by_day.tp+by_day.sl
        by_day['tp_rate_pct']=100*by_day.tp/by_day.n_resolved.replace(0,float('nan'))
        by_day.loc[by_day.n_resolved<min_n,'tp_rate_pct']=float('nan')
        st.dataframe(by_day,use_container_width=True,hide_index=True,height=240)
        st.caption('Estos días ya fueron inspeccionados repetidamente: el corte cronológico es diagnóstico, NO validación fuera de muestra.')
        st.markdown('#### Defensas independientes del mismo nivel')
        st.dataframe(df[['symbol','side','date_utc','primary_defenses','primary_defense_episodes',
                   'primary_distance_atr','outcome']],hide_index=True,use_container_width=True,height=250)
        st.caption('Los episodios separan visitas distanciadas; varias velas contiguas cuentan como una visita.')
        st.markdown('#### Etiqueta solo conocida AL CERRAR retest — no PRE')
        st.dataframe(_stats(df,'known_family',min_n),use_container_width=True,hide_index=True)
        st.markdown('#### Control negativo: nivel desplazado 3 ATR (NO es un nivel detectado)')
        st.dataframe(_stats(df,'placebo_near_pre',min_n),use_container_width=True,hide_index=True)
        st.caption('Un placebo desplazado es solamente una comprobación de sanidad; NO sustituye un control sin REACTION o validación independiente.')
    with tabs[3]:
        st.markdown('#### Distancia hasta la zona contraria más cercana')
        st.dataframe(_stats(df,'primary_orientation',min_n),use_container_width=True,hide_index=True)
        st.dataframe(df[['symbol','side','pre_family','support_distance_atr',
              'space_to_opposition_pct','space_to_opposition_atr','outcome']].sort_values('space_to_opposition_pct'),
             use_container_width=True,hide_index=True,height=380)
        st.caption('Sin obstáculo detectado NO significa espacio libre garantizado. El mapa depende de la cobertura histórica y de los pivotes confirmados.')
    with tabs[4]:
        labels={r.event_key:f'{r.symbol} {r.side} · {r.date_utc} · {r.pre_family} · {r.outcome}'
                for r in df.sort_values('retest_ts',ascending=False).itertuples(index=False)}
        key=st.selectbox('Evento estructural',list(labels),format_func=labels.get,key='level_v2_pick')
        item=df[df.event_key==key].iloc[0]
        zones=levels[levels.event_key==key].copy()
        st.markdown('**Zonas históricas conservadas (no seleccionadas por TP/SL)**')
        show=['timeframe','origin_kind','level_price','formation_closed_ts','level_state_pre',
              'gap_to_zone_atr','orientation','defenses_pre','zone_id']
        st.dataframe(zones[show].sort_values('gap_to_zone_atr'),use_container_width=True,hide_index=True,height=240)
        if str(item.pre_family)=='DISTANT_LEVEL_ONLY':
            st.warning('El Atlas identificó un nivel histórico DISTANTE. No significa que la REACTION haya defendido esa zona.')
        shown_tf=st.radio('Timeframe del mapa histórico',['15m','30m'],
            index=0 if str(item.primary_timeframe)=='15m' else 1,
            horizontal=True,key='level_v2_inspect_tf')
        from dashboard.views.reaction_outcome_overlay import (
            get_post_bars, overlay_future, outcome_explanation,
        )
        max_zones=8 if st.checkbox('Mostrar 8 zonas históricas (puede saturar el gráfico)',
                              value=False,key='level_v2_show_eight') else 4
        h1,h2=st.columns(2)
        with h1:
            display_minutes=st.select_slider('POST en el MISMO gráfico (min)',
                options=[15,30,60,180,360],value=180,key='level_v2_post_h')
        with h2:
            pre_hours=st.select_slider('Horas previas visibles',
                options=[6,12,24,48],value=12,key='level_v2_pre_h')
        try:
            bars,path_state=get_post_bars(project,item.event_key,item.retest_ts)
            fig=_read_structural_figure(project,item,zones,display_tf=shown_tf,max_zones=max_zones)
            if fig is not None:
                if bars:
                    fig=overlay_future(fig,bars,retest_ts=item.retest_ts,
                        side=item.side,entry_ts=item.entry_ts,entry_price=item.entry_price,
                        outcome=item.outcome,first_touch_minute=item.first_touch_minute,
                        tp_pct=tp_pct,sl_pct=sl_pct,outcome_horizon=outcome_horizon,
                        display_minutes=display_minutes,pre_hours=pre_hours)
                st.plotly_chart(fig,use_container_width=True,key='level_v2_pre_fig')
            st.info(outcome_explanation(item.outcome,item.first_touch_minute,
                tp_pct=tp_pct,sl_pct=sl_pct,outcome_horizon=outcome_horizon,
                display_minutes=display_minutes,path_state=path_state,entry_mode=entry_mode))
            if bars:
                with st.expander('Ampliar solamente las velas 1m posteriores',expanded=False):
                    detail=_post_figure(item,bars,display_minutes,tp_pct,sl_pct)
                    if detail is not None:
                        st.plotly_chart(detail,use_container_width=True,key='level_v2_post_fig')
        except Exception as exc:
            st.error(f'No se pudo reconstruir el gráfico con POST: {exc}')
        st.caption(f'Estado PRE: {item.pre_family} · estado retest CERRADO: {item.known_family} · '
                   f'outcome del replay filtrado: {item.outcome}. '
                   'La entrada observable no es igual al cierre del retest. NO_HIT no es beneficio ni pérdida.')
    st.download_button('Exportar muestra Atlas V2',df.to_csv(index=False).encode('utf-8'),
        file_name='structural_level_atlas_v2_filtrado.csv',mime='text/csv',key='level_v2_download')
