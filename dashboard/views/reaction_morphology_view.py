"""Interactive morphology atlas; read-only, with strict PRE/KNOWN/POST phases."""
from __future__ import annotations

from collections import Counter

NAME={
 'FAST_APPROACH':'Llegada rápida y adversa',
 'SLOW_FADING':'Llegada lenta con agotamiento',
 'OTHER_PRE':'Otras llegadas / sin patrón',
 'MISSING_PRE':'Contexto incompleto',
 'FAST_REJECTION':'Impulso adverso + rechazo en retest',
 'SLOW_EXHAUSTION':'Agotamiento + cierre de retest',
 'DEEP_BREAK_OUTSIDE':'Ruptura profunda, cierre fuera del swing',
 'NO_SWING_LEVEL_OTHER':'Otro / sin nivel swing',
 'OTHER_KNOWN':'Otra morfología conocida',
 'NOT_KNOWN':'Vela del retest no disponible',
 'POST_LATE_RECLAIM_3_TO_15M':'Recuperación tardía (3–15m)',
 'POST_EARLY_RECLAIM_1_TO_2M':'Recuperación temprana (1–2m)',
 'POST_NO_RECLAIM_15M':'No recupera nivel en 15m',
 'NOT_DEEP_BREAK':'No hubo ruptura profunda',
 'NO_VALID_POST':'Sin trayectoria posterior válida',
}


def _wilson(tp, n, z=1.96):
    import math
    if n<=0:return (float('nan'),float('nan'))
    p=tp/n; zz=z*z
    center=(p+zz/(2*n))/(1+zz/n)
    radius=z*math.sqrt(p*(1-p)/n+zz/(4*n*n))/(1+zz/n)
    return center-radius,center+radius


def summarize_morphology(frame,stage,min_group=20):
    """Per-REACTION denominator, no repeated scenario rows. Zero-hit explicit."""
    import pandas as pd
    if stage not in ('pre_morphology','known_morphology','post_morphology'):
        raise ValueError('Unexpected morphology stage')
    if 'event_key' in frame and frame.event_key.duplicated().any():
        raise ValueError('Duplicate event keys would bias the rates')
    out=[]
    for name,group in frame.groupby(stage,dropna=False):
        count=group.outcome.value_counts()
        tp=int(count.get('TP_FIRST',0));sl=int(count.get('SL_FIRST',0));n=tp+sl
        lo,hi=_wilson(tp,n)
        out.append({'morfologia':NAME.get(str(name),str(name)),'codigo':str(name),
                    'oportunidades':len(group),'resueltos':n,'tp':tp,'sl':sl,
                    'no_hit':int(count.get('NO_HIT',0)),
                    'ambiguos':int(count.get('AMBIGUOUS',0)),
                    'sin_path':int(group.outcome.isna().sum()),
                    'tp_rate_pct':round(100*tp/n,2) if n>=min_group else float('nan'),
                    'ci95_bajo_pct':round(100*lo,2) if n>=min_group else float('nan'),
                    'ci95_alto_pct':round(100*hi,2) if n>=min_group else float('nan'),
                    'n_dias':int(group.date_utc.nunique())})
    return pd.DataFrame(out).sort_values('oportunidades',ascending=False).reset_index(drop=True)


def compare_dates(frame,stage,split=0.6):
    """Dates chosen purely chronologically; no fitting on the later sample."""
    import pandas as pd
    dates=sorted(frame.date_utc.dropna().astype(str).unique())
    if len(dates)<4:return pd.DataFrame(),[],[]
    cut=max(1,min(len(dates)-1,round(len(dates)*split)))
    train,held=dates[:cut],dates[cut:]
    parts=[]
    for key,days in [('Anteriores',train),('Posteriores',held)]:
        sub=frame[frame.date_utc.isin(days)]
        desc=summarize_morphology(sub,stage,min_group=15)
        desc.insert(0,'periodo',key)
        parts.append(desc)
    return pd.concat(parts,ignore_index=True),train,held


def prototype_curves(project, frame, stage, selected, max_items=14):
    """Centered PRE 20-bar closing paths; ignores POST and outcomes for geometry.

    One deterministic representative per symbol to reduce apparent repetition.
    """
    import pandas as pd
    from contextlib import closing
    from reaction_retest_quality_lab import ro, rows_for, BASE, MIN
    sub=frame[frame[stage].astype(str)==str(selected)].sort_values(['retest_ts','event_key'])
    sub=sub.drop_duplicates('symbol').head(max_items)
    curves=[]
    with closing(ro(project/BASE/'reaction_full_context_v1.sqlite3')) as ctx:
        for e in sub.itertuples(index=False):
            ts=int(e.retest_ts);side=e.side
            rows=rows_for(ctx,e.symbol,'1m',ts-21*MIN,ts)
            if len(rows)!=21 or any(int(b[0])!=ts-21*MIN+i*MIN for i,b in enumerate(rows)):
                continue
            price=rows[0][4]
            if not price:continue
            sign=1 if side=='LONG' else -1
            curves.append({'event_key':e.event_key,'symbol':e.symbol,'side':side,
                           'y':[100*sign*(b[4]/price-1) for b in rows]})
    return curves


def render_morphology_atlas(frame,project,requested_tp=1.0,requested_sl=.5):
    import pandas as pd
    import plotly.graph_objects as go
    import streamlit as st
    from dashboard.services.reaction_reverse_engineering import one_per_symbol_wave
    st.markdown('### 🧬 Atlas de morfologías del retest')
    st.caption('Hipótesis predefinidas · 7.631 identidades · grupos sin forzar · información separada por momento causal.')
    st.markdown('''**Tres mecanismos investigados:** llegada adversa rápida que rechaza al cerrar el retest;
    acercamiento gradual que pierde impulso y volumen; ruptura profunda del swing que puede
    recuperarse minutos después. **Esa recuperación posterior NO se utiliza para predecir una entrada.**''')
    stage_label={'PRE_RETEST':'pre_morphology','RETEST_CERRADO':'known_morphology',
                 'POST_SOLO_EXPLICATIVO':'post_morphology'}
    controls=st.columns([1.4,1,1])
    stage_name=controls[0].selectbox('Momento del conocimiento',list(stage_label),key='rql_morph_stage',
          format_func=lambda x:{'PRE_RETEST':'1. Solo llegada anterior al retest',
              'RETEST_CERRADO':'2. Retest 1m ya cerrado',
              'POST_SOLO_EXPLICATIVO':'3. Resultado posterior (NO predictivo)'}[x])
    stage=stage_label[stage_name]
    only_live=controls[1].checkbox('Solo señales LIVE',True,key='rql_morph_live')
    purge=controls[2].selectbox('Un evento por ola', [180,60,360,0],key='rql_morph_wave',
                                   format_func=lambda x:f'{x} minutos' if x else 'Sin depurar')
    if stage_name=='POST_SOLO_EXPLICATIVO':
        st.error('POST se calcula con velas posteriores. Es solo una etiqueta de ingeniería inversa. Está PROHIBIDO usarla en señales o como predictor causal.')
    elif stage_name=='RETEST_CERRADO':
        st.info('El rechazo del retest recién se conoce al cerrar la vela. Cualquier entrada por confirmación debe simularse DESPUÉS de ese cierre.')
    else:
        st.success('Las formas PRE solo utilizan 1m previos ya cerrados; pueden examinarse antes del retest.')
    data=frame.copy()
    if only_live:data=data.loc[data.observation_type.eq('LIVE_OBSERVED')].copy()
    if purge:
        before=len(data);data,removed=one_per_symbol_wave(data,minutes=purge)
        st.caption(f'Control de episodios: {removed} registros cercanos excluidos, sin consultar el resultado futuro.')
    desc=summarize_morphology(data,stage,min_group=20)
    st.markdown(f'#### Frecuencia de cada forma — TP +{requested_tp:g}% / SL −{requested_sl:g}%')
    st.dataframe(desc,hide_index=True,use_container_width=True)
    if not desc.empty:
        bars=go.Figure()
        bars.add_trace(go.Bar(y=desc['morfologia'],x=desc['tp'],name='TP primero',orientation='h',marker_color='#16a085'))
        bars.add_trace(go.Bar(y=desc['morfologia'],x=desc['sl'],name='SL primero',orientation='h',marker_color='#d26b76'))
        bars.add_trace(go.Bar(y=desc['morfologia'],x=desc['no_hit'],name='Sin toque',orientation='h',marker_color='#bba36d'))
        bars.update_layout(barmode='stack',height=max(270,len(desc)*54),margin=dict(l=0,r=0,t=10,b=0),yaxis=dict(autorange='reversed'))
        st.plotly_chart(bars,use_container_width=True)
    st.caption('TP% y Wilson 95% calculados **solo entre resueltos**. Sin toque, ambiguos, sin trayectoria y no observables aparecen por separado. Sin costes ni cierre por tiempo.')
    periods,earlier,later=compare_dates(data,stage)
    st.markdown('#### Repetibilidad por fecha')
    if len(periods):
        st.caption('Separación por días: '+', '.join(earlier)+' → '+', '.join(later)+'. Clasificación fija, sin ajuste de umbrales.')
        st.dataframe(periods[['periodo','morfologia','oportunidades','tp','sl','tp_rate_pct','ci95_bajo_pct','ci95_alto_pct','n_dias']],hide_index=True,use_container_width=True)
    st.markdown('#### Ejemplos visuales y mecanismos')
    options=list(desc['codigo']) if not desc.empty else []
    if options:
        selected=st.selectbox('Forma para inspeccionar',options,format_func=lambda x:NAME.get(x,x),key='rql_morph_selected')
        curves=prototype_curves(project,data,stage,selected,max_items=14)
        if curves:
            fig=go.Figure()
            for curve in curves:
                fig.add_trace(go.Scatter(x=list(range(-21,0)),y=curve['y'],mode='lines',opacity=.40,
                    line=dict(width=1.5),name=curve['symbol']+' '+curve['side']))
            fig.add_vline(x=-1,line_dash='dash',line_color='#8699aa')
            fig.update_layout(title='Llegada al retest: últimos 21 cierres 1m, retorno ajustado por lado (%)',
                height=370,margin=dict(l=0,r=0,t=35,b=0),xaxis_title='Minutos antes del retest',
                yaxis_title='Retorno ajustado al lado desde inicio (%)')
            st.plotly_chart(fig,use_container_width=True)
        example=data[data[stage].eq(selected)].sort_values(['retest_ts','event_key']).head(80)
        columns=['event_key','date_utc','symbol','side','pre_morphology','known_morphology',
                 'post_morphology','outcome','pre_adverse_5m_pct','pre_adverse_deceleration_pct',
                 'pre_quote_recent5_vs_previous5','known_swing_penetration_pct','post_reclaim_minute_15m']
        st.dataframe(example[[x for x in columns if x in example]],hide_index=True,use_container_width=True)
    st.markdown('#### Cruce causal: llegada PRE × comportamiento al cierre del retest')
    cross=pd.crosstab(data.pre_morphology,data.known_morphology)
    st.dataframe(cross,use_container_width=True)
    st.caption('No sumamos una supuesta ventaja por mirar los casos con recuperación POST; para simular una entrada en recuperación hay que esperar su cierre y volver a calcular TP/SL desde la entrada posterior.')
    st.download_button('Exportar resumen por morfología',desc.to_csv(index=False).encode('utf-8'),
        'atlas_morfologias_reaction.csv','text/csv',key='rql_morph_download')
