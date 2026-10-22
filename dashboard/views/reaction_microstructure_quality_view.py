"""Streamlit view: read-only microstructure/retest quality exploration.

Uses audited frozen context, Auto Paths and first-touch results. The only
write action is explicitly building a disposable, derived feature CSV.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from contextlib import closing

from reaction_retest_quality_lab import BASE, OUTPUT, FEATURES, ro, rows_for, digest
from reaction_retest_morphology import MORPH_NUMERIC, MORPH_FIELDS

PALETTE={'TP_FIRST':'#19a78d','SL_FIRST':'#d97879','NO_HIT':'#dcab62',
         'AMBIGUOUS':'#8493a5','NOT_ENTERED':'#68778b'}
FEATURE_LABELS={
 'pre_range5_pct':'Rango 5m (%)', 'pre_range15_pct':'Rango 15m (%)',
 'pre_range30_pct':'Rango 30m (%)','pre_realized_vol60_pct':'Volatilidad 60m (%)',
 'pre_signed_return5_pct':'Retorno direccional 5m (%)',
 'pre_signed_return15_pct':'Retorno direccional 15m (%)',
 'pre_signed_return60_pct':'Retorno direccional 60m (%)',
 'pre_quote_relative5_to_previous30':'Volumen relativo 5m vs 30m',
 'pre_trades_relative5_to_previous30':'Trades relativos 5m vs 30m',
 'pre_taker_side_imbalance5':'Desequilibrio taker direccional 5m',
 'pre_close_strength_side_last1m':'Fuerza cierre 1m previa',
 'pre_wick_rejection_share_last1m':'Mecha rechazo 1m previa',
 'pre_efficiency15':'Eficiencia de movimiento 15m',
 'pre_quote_relative1_to_previous30':'Volumen 1m / media 30m',
 'pre_trades_relative1_to_previous30':'Trades 1m / media 30m',
 'pre_taker_side_imbalance1':'Desequilibrio taker previo 1m',
 'pre_taker_side_imbalance15':'Desequilibrio taker previo 15m',
 'pre_rejection_mean5':'Mecha de rechazo media 5m',
 'pre_close_strength_mean5':'Cierre favorable medio 5m',
 'pre_signed_body_mean5_pct':'Cuerpo direccional medio 5m (%)',
 'pre_range_contraction5_over30':'Contracción rango 5m / 30m',
 'pre_price_location_15m_side':'Posición en rango local 15m',
 'pre15m_range1h_pct':'Rango HTF 1h (%)',
 'pre15m_range4h_pct':'Rango HTF 4h (%)',
 'pre15m_atr14_pct':'ATR 15m 14 (%)',
 'pre15m_side_return1h_pct':'Retorno HTF dir. 1h (%)',
 'pre15m_side_return4h_pct':'Retorno HTF dir. 4h (%)',
 'pre15m_vs_btc4h_pct':'Fuerza vs BTC 4h (%)',
 'btc_pre_signed_return5_pct':'Retorno BTC ajustado 5m (%)',
 'btc_pre_signed_return15_pct':'Retorno BTC ajustado 15m (%)',
 'btc_pre_signed_return60_pct':'Retorno BTC ajustado 60m (%)',
 'pre_strength_vs_btc15_pct':'Fortaleza vs BTC 15m (%)',
 'pre_strength_vs_btc60_pct':'Fortaleza vs BTC 60m (%)',
 'known_retest_range_pct':'Rango vela retest (%)',
 'known_retest_close_strength_side':'Cierre favorable retest',
 'known_retest_rejection_wick_share':'Mecha rechazo retest',
 'known_retest_signed_body_pct':'Cuerpo direccional retest (%)',
 'known_retest_relative_quote30':'Volumen relativo retest / 30m',
 'known_retest_taker_side_imbalance':'Taker direccional retest',
 'known_swing_level':'Nivel swing 15m',
 'known_retest_distance_to_swing_pct':'Distancia swing-retet (%)',
 'known_swing_age_min':'Edad del swing (min)',
 'known_departure_pct':'Alejamiento máximo swing (%)',
 'known_retest_distance_pct':'Distancia retest scanner (%)',
 'known_relative_volume_scanner':'Volumen relativo scanner',
 'known_swing_prominence_pct':'Prominencia swing (%)',
 'known_pivot_to_confirmation_pct':'Movimiento pivote-confirmación (%)',
 'known_penetration_pct':'Penetración del nivel (%)',
 'known_pivot_to_retest_min':'Pivote a retest (min)',
 'known_triggering_swing_count':'Swings que disparan reacción',
 'pre_adverse_5m_pct':'Llegada adversa 5m (%)',
 'pre_adverse_15m_pct':'Llegada adversa 15m (%)',
 'pre_approach_speed_ratio':'Velocidad llegada 5m vs 15m',
 'pre_adverse_previous5m_pct':'Llegada adversa previa 5m (%)',
 'pre_adverse_deceleration_pct':'Desaceleración adversa entre bloques 5m',
 'pre_quote_recent5_vs_previous5':'Volumen reciente 5m vs anterior 5m',
 'pre_trades_recent5_vs_previous5':'Operaciones reciente 5m vs anterior 5m',
 'pre_taker_side_recent5':'Taker direccional reciente 5m',
 'pre_taker_side_previous5':'Taker direccional anterior 5m',
 'pre_taker_relief5':'Alivio presión taker 5m',
 'known_swing_penetration_pct':'Penetración mecha vs swing confirmado (%)',
 'known_retest_closes_beyond_level':'Retest termina fuera del swing (0/1)',
 'post_reclaim_minute_15m':'Minuto recuperación swing POST (NO predictor)',
}


def load_frame(project):
    import pandas as pd
    features=project/BASE/OUTPUT/'retest_quality_features.csv'
    outcomes=project/BASE/'reaction_first_touch_lab_v1'/'first_touch_scenarios.csv'
    summary=project/BASE/OUTPUT/'summary.json'
    replay=project/BASE/'reaction_first_touch_lab_v1'/'summary.json'
    if not all(p.is_file() for p in (features,outcomes,summary,replay)):
        raise FileNotFoundError('Ejecutá reaction_retest_quality_lab.py --build y First-Touch --run.')
    m=json.loads(summary.read_text(encoding='utf-8'))
    o=json.loads(replay.read_text(encoding='utf-8'))
    if m['frozen_cohort_sha256']!=o['frozen_cohort_sha256']:
        raise RuntimeError('Las cohortes no coinciden; se bloquea el cruce.')
    if digest(features)!=m['features_sha256'] or digest(outcomes)!=o['file_sha256'][outcomes.name]:
        raise RuntimeError('Checksum de features o outcomes inválido. No mostrar datos inconsistentes.')
    f=pd.read_csv(features,low_memory=False)
    r=pd.read_csv(outcomes,low_memory=False)
    if len(f)!=m['events'] or len(r)!=o['scenario_rows']:
        raise RuntimeError('Conteos CSV distintos de los reportes auditados')
    return f,r,m


def available_first_touch_horizons(outcomes):
    """Offer only horizons actually stored (and SHA-audited) in First Touch.

    In particular, an old 180m CSV must not silently masquerade as 240/300/360m.
    """
    all_horizons=(5,15,30,60,180,240,300,360)
    return [h for h in all_horizons if
            f'result_{h}m' in outcomes.columns and f'minute_{h}m' in outcomes.columns]


def outcome_subset(r,mode,tp,sl,horizon):
    sub=r[(r.entry_mode==mode) & r.tp_pct.eq(tp) & r.sl_pct.eq(sl)].copy()
    col=f'result_{int(horizon)}m'
    if col not in sub.columns or f'minute_{int(horizon)}m' not in sub.columns:
        raise ValueError(f'First Touch no tiene resultados calculados para {horizon}m. '
                         'Ejecutá --run y --audit de reaction_first_touch_lab.py.')
    sub['outcome']=sub[col].astype(str)
    sub['first_touch_minute']=sub[f'minute_{int(horizon)}m']
    return sub[['event_key','entry_ts','entry_price','entry_delay_min','outcome','first_touch_minute','mfe_180_pct','mae_180_pct']]


def score_binary(frame,feature):
    """Rank AUC descriptiva, not fit/holdout validation. Higher predicts TP."""
    import pandas as pd
    if feature not in frame.columns:
        return None
    z=frame.loc[frame['outcome'].isin(['TP_FIRST','SL_FIRST']),[feature,'outcome']].dropna()
    good=z.outcome.eq('TP_FIRST')
    if not good.any() or good.all() or len(z)<20:return None
    ranks=z[feature].rank(method='average')
    pos=int(good.sum());neg=int((~good).sum())
    auc=(ranks[good].sum()-pos*(pos+1)/2)/(pos*neg)
    return {'n':len(z),'tp_n':pos,'sl_n':neg,'auc_higher_is_tp':float(auc),
     'tp_median':float(z.loc[good,feature].median()),
     'sl_median':float(z.loc[~good,feature].median())}


def matrix(frame,fx,fy,n_bins=4,min_bin=30):
    import pandas as pd
    if fx not in frame.columns or fy not in frame.columns:
        return None,None
    z=frame.loc[frame['outcome'].isin(['TP_FIRST','SL_FIRST']),[fx,fy,'outcome']].dropna().copy()
    if z.empty:return None,None
    z['x']=pd.qcut(z[fx],q=n_bins,duplicates='drop')
    z['y']=pd.qcut(z[fy],q=n_bins,duplicates='drop')
    tab=z.groupby(['y','x'],observed=False)['outcome'].agg(
        n='size',tp=lambda col:col.eq('TP_FIRST').sum()).reset_index()
    tab['rate']=100*tab.tp/tab.n
    tab.loc[tab.n<min_bin,'rate']=float('nan')
    return tab,z


def _fetch_detail(project,key):
    from reaction_lab_auto_paths import decode_path
    folder=project/BASE
    with closing(ro(folder/'reaction_full_context_v1.sqlite3')) as ctx:
        row=ctx.execute('''SELECT symbol,side,retest_ts,known_ts,first_seen_ms,observation_type,causal_json
                FROM events WHERE event_key=?''',(key,)).fetchone()
        if row is None:raise KeyError('Event ID not found')
        sym,side,r,k,seen,obs,raw=row
        pre=rows_for(ctx,sym,'1m',r-180*60_000,r)
        retest=rows_for(ctx,sym,'1m',r,r+60_000)
        closed15=(r//900_000)*900_000
        pre15=rows_for(ctx,sym,'15m',closed15-96*900_000,closed15)
        btc=rows_for(ctx,'BTCUSDT','1m',r-180*60_000,r)
        # All three frozen causal anchors can be examined independently.
        observed_end=(seen//60_000)*60_000
        observed_asset=rows_for(ctx,sym,'1m',observed_end-180*60_000,observed_end)
        observed_btc=rows_for(ctx,'BTCUSDT','1m',observed_end-180*60_000,observed_end)
        known_end=(k//60_000)*60_000
        known_asset=rows_for(ctx,sym,'1m',known_end-180*60_000,known_end)
    with closing(ro(folder/'auto_paths_v1.sqlite3')) as ap:
        path=ap.execute('SELECT state,count,entry_ts,path_blob FROM events WHERE event_key=?',(key,)).fetchone()
    bars=[];status='MISSING'
    if path:
        status=path[0]
        if path[0]=='COMPLETE' and path[1]==360 and path[2]==r+60_000:
            bars=decode_path(path[3])
            if len(bars)!=360 or any(int(bar[0])!=r+(i+1)*60_000 for i,bar in enumerate(bars)):
                bars=[];status='INVALID_CONTINUITY'
    try:causal=json.loads(raw)
    except (ValueError,TypeError):causal={'raw':str(raw)[:500]}
    return {'symbol':sym,'side':side,'retest_ts':r,'known_ts':k,'first_seen_ms':seen,'observation':obs,
      'path_status':status,'pre':pre,'retest':retest,'pre15':pre15,'btc':btc,'post':bars,'causal':causal,
      'observed_asset':observed_asset,'observed_btc':observed_btc,'known_asset':known_asset}


def aggregate_display_5m(rows):
    """Only full, contiguous UTC-aligned 5x1m buckets; visual display only."""
    from collections import defaultdict
    buckets=defaultdict(list)
    for r in rows:buckets[(int(r[0])//300_000)*300_000].append(r)
    out=[]
    for ts,b in sorted(buckets.items()):
        b=sorted(b,key=lambda x:x[0])
        if len(b)!=5 or any(int(r[0])!=ts+60_000*i for i,r in enumerate(b)):
            continue
        out.append((ts,b[0][1],max(v[2] for v in b),min(v[3] for v in b),b[-1][4]))
    return out


def candles_plot(bars,title,verticals=None,horizontal=None,volume=False):
    import plotly.graph_objects as go
    import pandas as pd
    if not bars:return None
    x=pd.to_datetime([x[0] for x in bars],unit='ms',utc=True)
    fig=go.Figure()
    fig.add_trace(go.Candlestick(x=x,open=[v[1] for v in bars],high=[v[2] for v in bars],
        low=[v[3] for v in bars],close=[v[4] for v in bars],name='OHLC',
        increasing_line_color='#1bb89b',decreasing_line_color='#e27378'))
    if volume and len(bars[0])>=7:
        # Volume is in a separate panel to avoid mixing incomparable price axes.
        pass
    for stamp,label,color in (verticals or []):
        xstamp=pd.Timestamp(stamp,unit='ms',tz='UTC').to_pydatetime()
        fig.add_shape(type='line',xref='x',yref='paper',x0=xstamp,x1=xstamp,y0=0,y1=1,
                      line=dict(color=color,dash='dot',width=1))
        fig.add_annotation(x=xstamp,y=1,xref='x',yref='paper',text=label,
                           showarrow=False,font=dict(color=color,size=10),yshift=12)
    for price,label,color in (horizontal or []):
        if price is not None:
            fig.add_hline(y=price,line_dash='dash',line_color=color,annotation_text=label)
    fig.update_layout(title=title,height=430,margin=dict(l=5,r=5,t=43,b=0),
       xaxis_rangeslider_visible=False,plot_bgcolor='rgba(0,0,0,0)',
       paper_bgcolor='rgba(0,0,0,0)',xaxis=dict(showgrid=False),
       yaxis=dict(gridcolor='rgba(128,128,128,.14)'),showlegend=False)
    return fig


def _render_compatible_research_tab(renderer, source_events, project_root, *,
                                    label, tp_pct, sl_pct, outcome_horizon,
                                    entry_mode):
    """Call either legacy (frame, root) or newer scenario-aware research tabs.

    Do not quietly claim the global outcome controls were propagated to a
    legacy renderer: report that its own/default scenario may apply.
    """
    import inspect
    import streamlit as st

    requested = {
        'tp_pct': tp_pct,
        'sl_pct': sl_pct,
        'outcome_horizon': outcome_horizon,
        'entry_mode': entry_mode,
    }
    parameters = inspect.signature(renderer).parameters
    accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD
                         for p in parameters.values())
    accepted = {
        name: value for name, value in requested.items()
        if accepts_kwargs or (name in parameters and parameters[name].kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ))
    }
    missing = [name for name in requested if name not in accepted]
    if missing:
        st.warning(
            f'{label}: el módulo instalado usa una firma anterior y no acepta '
            f'{", ".join(missing)}. Se puede abrir con su interfaz original, '
            'pero sus estadísticas pueden no coincidir con el TP/SL, horizonte '
            'y modo de entrada seleccionados arriba. Para comparaciones, '
            'actualizá esa vista a la versión compatible con los controles globales.'
        )
    return renderer(source_events, project_root, **accepted)


def render_microstructure_quality_lab(project_root):
    import pandas as pd
    import plotly.graph_objects as go
    import streamlit as st
    from reaction_retest_quality_lab import build, validate_sources
    project=Path(project_root).resolve()
    out=project/BASE/OUTPUT
    st.markdown('## 🔬 Reaction Microstructure & Retest Quality')
    st.caption('Catálogo completo congelado · contexto causal 1m/15m y BTC · trayectorias posteriores 360×1m · Research, sin órdenes ni Redis')
    fpath=out/'retest_quality_features.csv'
    if not fpath.exists():
        st.info('Las bases están auditadas; falta generar el CSV derivado para el dashboard (no descarga velas).')
        if st.button('Construir variables de las 7.631 REACTIONs',key='rql_build'):
            try:
                with st.spinner('Calculando features desde las velas crudas, sin modificar el histórico...'):
                    result=build(project)
                st.success(f"Se calcularon {result['events']} eventos, datos originales intactos.")
                st.rerun()
            except Exception as exc:st.error(str(exc))
        return
    @st.cache_data(show_spinner='Cargando análisis auditado...',max_entries=2)
    def get_cached(root,ft,ot,mt):
        return load_frame(Path(root))
    try:
        f,r,meta=get_cached(str(project),fpath.stat().st_mtime_ns,
                            (project/BASE/'reaction_first_touch_lab_v1'/'first_touch_scenarios.csv').stat().st_mtime_ns,
                            (out/'summary.json').stat().st_mtime_ns)
    except Exception as exc:
        st.error(f'No se puede abrir el Lab: {exc}');return
    c1,c2,c3,c4=st.columns(4)
    c1.metric('REACTIONs congeladas',f'{len(f):,}')
    c2.metric('Contextos completos',f"{meta['feature_quality'].get('COMPLETE',0):,}")
    c3.metric('Trayectorias completas',f"{int(f.path_quality.eq('COMPLETE_VALID').sum()):,}")
    c4.metric('Señales LIVE',f"{int(f.observation_type.eq('LIVE_OBSERVED').sum()):,}")
    st.caption('PRE: velas cerradas anteriores al retest. KNOWN: vela del retest solo tras su cierre. POST: Auto Paths 360m; sin datos futuros de volumen en esos paths.')
    with st.expander('Integridad y procedencia de los datos'):
        st.json({'version':meta['version'],'cohort_sha256':meta['frozen_cohort_sha256'],
             'features_sha256':meta['features_sha256'],
             'fuentes':['reaction_full_context_v1.sqlite3','auto_paths_v1.sqlite3','reaction_first_touch_lab_v1'],
             'advertencias':['Análisis retrospectivo, no validación prospectiva',
               'RECOVERED_WINDOW no equivale a ejecución LIVE',
               'TP/SL same-candle es ambiguo; NO_HIT no equivale a P&L cero']})

    st.markdown('### Cohorte y replay')
    v=st.columns([1.2,1.2,1.0,1.0,1.0,1.0],gap='small')
    obs=v[0].selectbox('Observabilidad',['LIVE_OBSERVED','RECOVERED_WINDOW','TODAS'],key='rql_obs')
    mode=v[1].selectbox('Entrada',['OBSERVED_OPEN','STRUCTURAL_OPEN'],key='rql_mode')
    pairs=[(0.5,0.5),(1.0,0.5),(1.0,1.0),(1.5,0.75),(2.0,1.0)]
    pair=v[2].selectbox('TP / SL',pairs,index=1,format_func=lambda x:f'+{x[0]:g}% / −{x[1]:g}%',key='rql_pair')
    available_horizons=available_first_touch_horizons(r)
    if not available_horizons:
        st.error('First Touch no tiene columnas de resultado/minuto coherentes. Revisá su auditoría.')
        return
    horizon=v[3].selectbox('Horizonte (min)',available_horizons,
                           index=(available_horizons.index(180) if 180 in available_horizons else len(available_horizons)-1),
                           key='rql_horizon')
    if 360 not in available_horizons:
        st.info('Para habilitar 240/300/360m, actualizá First Touch y ejecutá '
                '`python3 reaction_first_touch_lab.py --run` y luego `--audit`. '
                'Hasta entonces mostramos solo horizontes realmente calculados.')
    side=v[4].selectbox('Dirección',['TODAS','LONG','SHORT'],key='rql_side')
    day=v[5].selectbox('Fecha UTC',['TODAS']+sorted(f.date_utc.dropna().astype(str).unique().tolist()),key='rql_day')
    if mode=='STRUCTURAL_OPEN':st.warning('STRUCTURAL_OPEN usa entrada histórica teórica no ejecutable. Para causalidad de observación, preferí OBSERVED_OPEN.')
    if obs=='RECOVERED_WINDOW' and mode=='OBSERVED_OPEN':
        st.info('Las ventanas recuperadas no tienen entrada observable: se clasifican como NOT_ENTERED.')
    available=outcome_subset(r,mode,pair[0],pair[1],horizon)
    merged=f.merge(available,on='event_key',how='left',validate='one_to_one')
    if obs!='TODAS':merged=merged[merged.observation_type.eq(obs)]
    if side!='TODAS':merged=merged[merged.side.eq(side)]
    if day!='TODAS':merged=merged[merged.date_utc.eq(day)]
    symbols=['TODOS']+sorted(merged.symbol.dropna().unique().tolist())
    symbol=st.selectbox('Símbolo',symbols,key='rql_symbol')
    if symbol!='TODOS':merged=merged[merged.symbol.eq(symbol)]
    statuses=['TODOS','TP_FIRST','SL_FIRST','NO_HIT','AMBIGUOUS','NOT_ENTERED','SIN_PATH']
    result=st.selectbox('Resultado',statuses,key='rql_result')
    analysis_before_result_filter = merged.copy()  # independent TP/SL denominator for reverse engineering
    if result=='SIN_PATH':merged=merged[merged.outcome.isna()]
    elif result!='TODOS':merged=merged[merged.outcome.eq(result)]
    if merged.empty:st.warning('Sin registros para estos filtros');return
    counts=merged.outcome.fillna('SIN_PATH').value_counts()
    k=st.columns(5)
    for box,label,code in zip(k,['Oportunidades','TP primero','SL primero','Sin toque','Ambiguas'],[None,'TP_FIRST','SL_FIRST','NO_HIT','AMBIGUOUS']):
        box.metric(label,f"{len(merged):,}" if code is None else f"{counts.get(code,0):,}")
    tp=int(counts.get('TP_FIRST',0));sl=int(counts.get('SL_FIRST',0))
    st.caption(f'TP sobre resueltas (sin ambiguas): {100*tp/(tp+sl):.1f}%' if tp+sl else 'Sin casos resueltos')

    available_features=[c for c in FEATURES if c in merged.columns]
    available_morph=[c for c in MORPH_NUMERIC if not c.startswith('post_') and c in merged.columns]
    all_rankable=available_features+available_morph
    missing_features=[c for c in FEATURES if c not in merged.columns]
    missing_morph=[c for c in MORPH_NUMERIC if not c.startswith('post_') and c not in merged.columns]
    if missing_features or missing_morph:
        missing_preview=(missing_features+missing_morph)[:12]
        st.warning(
            'Hay columnas esperadas que no están en el CSV derivado actual. '
            'Se ocultan para evitar que el dashboard falle. '
            'Probablemente haya desalineación entre la vista y retest_quality_features.csv. '
            f'Missing (muestra): {", ".join(missing_preview)}'
        )

    tabs=st.tabs(['📊 Variables y cruces','🧩 Matrices','🕯️ Inspector por REACTION','📋 Eventos','🧠 Ingeniería inversa','🧬 Morfologías','🗜️ Compression Lineage','🧱 Breakout Retest V4','🧱 Structural Level Atlas','🧭 Estructuras independientes','🟩 Horizontal Level Rich Card'])
    with tabs[0]:
        st.markdown('#### Ranking descriptivo TP-first frente a SL-first')
        st.caption('AUC por variable en la cohorte filtrada: 0,50 ≈ azar; NO es un resultado validado fuera de muestra. KNOWN no es previo al retest.')
        ranks=[]
        for col in all_rankable:
            s=score_binary(merged,col)
            if s:ranks.append({'feature':FEATURE_LABELS.get(col,col),'campo':col,
                             'N':s['n'],'AUC ↑TP':round(s['auc_higher_is_tp'],3),
                             'AUC separación':round(max(s['auc_higher_is_tp'],1-s['auc_higher_is_tp']),3),
                             'Mediana TP':round(s['tp_median'],5),'Mediana SL':round(s['sl_median'],5)})
        ranking=pd.DataFrame(ranks)
        if not ranking.empty:
            st.dataframe(ranking.sort_values('AUC separación',ascending=False),use_container_width=True,hide_index=True)
        else:st.info('Se necesitan suficientes casos TP y SL para comparar las variables.')
        if all_rankable:
            feature=st.selectbox('Distribución por resultado',all_rankable,
                 format_func=lambda x:FEATURE_LABELS.get(x,x),key='rql_distfeature')
            z=merged[merged.outcome.isin(['TP_FIRST','SL_FIRST'])].dropna(subset=[feature])
            if len(z)>1:
                import plotly.express as px
                fig=px.box(z,x='outcome',y=feature,color='outcome',points=False,
                   color_discrete_map=PALETTE,labels={'outcome':'Primer toque',feature:FEATURE_LABELS.get(feature,feature)})
                fig.update_layout(height=350,showlegend=False,margin=dict(l=0,r=0,t=10,b=0))
                st.plotly_chart(fig,use_container_width=True)
        else:
            st.info('No hay variables numéricas disponibles en el CSV derivado actual para mostrar distribuciones.')
    with tabs[1]:
        st.markdown('#### Matriz de interacciones (TP primero entre resueltas)')
        if len(available_features) < 2:
            st.info('Se necesitan al menos dos variables disponibles en el CSV derivado para construir matrices.')
        else:
            cols=st.columns([1,1,1])
            default_fx=min(3,len(available_features)-1)
            fx=cols[0].selectbox('Variable X',available_features,index=default_fx,
               format_func=lambda x:FEATURE_LABELS.get(x,x),key='rql_x')
            fy_options=[x for x in available_features if x!=fx]
            default_fy=min(3,len(fy_options)-1)
            fy=cols[1].selectbox('Variable Y',fy_options,index=default_fy,
               format_func=lambda x:FEATURE_LABELS.get(x,x),key='rql_y')
            min_n=cols[2].slider('Mínimo eventos por celda',10,100,30,10,key='rql_min_n')
            cells,z=matrix(merged,fx,fy,n_bins=4,min_bin=min_n)
            if cells is None or cells.empty or z['x'].nunique()<2 or z['y'].nunique()<2:
                st.info('Se necesitan al menos dos cuantiles distintos en cada eje.')
            else:
                import plotly.express as px
                xs=list(z['x'].cat.categories);ys=list(z['y'].cat.categories)
                namesx=[f'Q{i+1}' for i in range(len(xs))];namesy=[f'Q{i+1}' for i in range(len(ys))]
                rate=[];txt=[]
                for y in ys:
                    rr=[];tt=[]
                    for x in xs:
                        sub=cells[(cells.x==x)&(cells.y==y)]
                        if len(sub):
                            n=int(sub.n.iloc[0]);val=float(sub.rate.iloc[0]);rr.append(val)
                            tt.append(f'{val:.1f}% · n={n}' if val==val else f'n={n} · insuficiente')
                        else:rr.append(float('nan'));tt.append('n=0')
                    rate.append(rr);txt.append(tt)
                fig=px.imshow(rate,x=namesx,y=namesy,color_continuous_scale='Teal',
                    zmin=0,zmax=100,labels={'color':'TP primero (%)'})
                fig.update_traces(text=txt,texttemplate='%{text}',hovertemplate='%{text}<extra></extra>')
                fig.update_layout(height=390,margin=dict(l=0,r=0,t=0,b=0))
                st.plotly_chart(fig,use_container_width=True)
                st.caption('Q1–Q4: cuartiles calculados en la cohorte filtrada. No hay corrección por múltiples pruebas; las celdas con pocas observaciones quedan sin tasa.')
    with tabs[2]:
        st.markdown('#### Gráficos y metadatos de cada evento')
        pick=merged.sort_values('retest_ts',ascending=False).copy()
        pick['option']=pick['symbol'].astype(str)+' · '+pick['side']+' · '+pd.to_datetime(pick.retest_ts,unit='ms',utc=True).dt.strftime('%d/%m %H:%M UTC')+' · '+pick.outcome.fillna('SIN_PATH')
        opts=list(pick[['event_key','option']].itertuples(index=False,name=None))
        labels=dict(opts)
        key=st.selectbox('Evento',options=[k for k,_ in opts],format_func=lambda k:labels.get(k,k),key='rql_event')
        @st.cache_data(show_spinner='Consultando velas archivadas...',max_entries=15)
        def read_one(base,event_key):return _fetch_detail(Path(base),event_key)
        try:ev=read_one(str(project),key)
        except Exception as exc:st.error(str(exc));return
        selected=merged[merged.event_key==key].iloc[0]
        st.caption(f"{ev['symbol']} {ev['side']} · Estado Auto Paths: {ev['path_status']} · Observación: {ev['observation']}")
        from dashboard.views.reaction_morphology_view import NAME as MORPH_NAME
        mcols=st.columns(3)
        for col,key_m,label_m in zip(mcols,('pre_morphology','known_morphology','post_morphology'),
                ('Antes del retest','Retest cerrado','Recuperación POST · explicativa')):
            code=str(selected.get(key_m,'SIN_CALCULAR'))
            col.metric(label_m,MORPH_NAME.get(code,code))
        st.caption('La recuperación POST se observa DESPUÉS del retest y NO puede usarse como predictor de una entrada anterior.')
        plotted_pre=st.slider('Velas 1m anteriores',30,180,90,10,key='rql_pre')
        plotted_post=st.slider('Velas 1m posteriores',30,360,180,30,key='rql_post')
        bars=ev['pre'][-plotted_pre:]+ev['retest']+ev['post'][:plotted_post]
        tf_chart=st.radio('Velas del gráfico principal',['1m','5m derivadas'],horizontal=True,key='rql_chart_tf')
        if tf_chart=='5m derivadas':bars=aggregate_display_5m(bars)
        horizontal=[]
        entry=selected.get('entry_price')
        if pd.notna(entry) and float(entry)>0:
            price=float(entry);islong=ev['side']=='LONG';tp_,sl_=pair
            horizontal=[(price,'Entrada teórica','#93a8be'),
                (price*(1+tp_/100 if islong else 1-tp_/100),'TP','#16a085'),
                (price*(1-sl_/100 if islong else 1+sl_/100),'SL','#df7676')]
        for name,chunks in [(tf_chart+': antes → retest → después',bars)]:
            fig=candles_plot(chunks,name,verticals=[(ev['retest_ts'],'RETEST','#daa654'),
                (ev['first_seen_ms'],'Visto LIVE','#7b9ddd')] if ev['observation']=='LIVE_OBSERVED' else [(ev['retest_ts'],'RETEST','#daa654')],
                horizontal=horizontal)
            if fig:st.plotly_chart(fig,use_container_width=True)
        if not ev['post']:
            st.warning('Esta REACTION no tiene Auto Paths COMPLETE válido; solo se muestran las velas anteriores.')
        else:
            idx=selected.get('entry_delay_min')
            ep=selected.get('entry_price')
            if pd.notna(idx) and pd.notna(ep):
                idx=int(idx);ep=float(ep)
                metrics=[]
                for h in [5,15,30,60,120,180,360]:
                    used=ev['post'][idx:h] if idx<h else []
                    if not used:continue
                    if ev['side']=='LONG':
                        mfe=(max(v[2] for v in used)/ep-1)*100
                        mae=(1-min(v[3] for v in used)/ep)*100
                    else:
                        mfe=(1-min(v[3] for v in used)/ep)*100
                        mae=(max(v[2] for v in used)/ep-1)*100
                    metrics.append({'Horizonte desde retest (min)':h,
                         'MFE (%)':round(max(0,mfe),3),
                         'MAE (%)':round(max(0,mae),3)})
                if metrics:
                    st.markdown('**Escaleras de recorrido posterior desde la entrada elegida**')
                    st.dataframe(pd.DataFrame(metrics),hide_index=True,use_container_width=True)
        # Activity can be graphed only before/at the signal; Auto Paths lacks volume.
        raw=ev['pre']+ev['retest']
        if raw:
            import plotly.graph_objects as go
            vx=pd.to_datetime([x[0] for x in raw],unit='ms',utc=True)
            vfig=go.Figure(go.Bar(x=vx,y=[x[6] for x in raw],marker_color='#4298b5',
                                 name='Quote volume 1m'))
            vfig.update_layout(title='Volumen USDT 1m anterior + vela del retest',height=230,
               margin=dict(l=0,r=0,t=38,b=0),xaxis=dict(showgrid=False),
               paper_bgcolor='rgba(0,0,0,0)',plot_bgcolor='rgba(0,0,0,0)')
            st.plotly_chart(vfig,use_container_width=True)

        anchor_mode=st.radio('Comparar información por momento causal',
              ['PRE_RETEST','PRE_KNOWN','PRE_OBSERVED'],horizontal=True,key='rql_anchor')
        at_anchor={'PRE_RETEST':ev['pre'],'PRE_KNOWN':ev['known_asset'],
                   'PRE_OBSERVED':ev['observed_asset']}[anchor_mode]
        anchor_title={'PRE_RETEST':'Previas al retest (sin su vela)',
                      'PRE_KNOWN':'Conocidas tras cerrar vela retest',
                      'PRE_OBSERVED':'Cerradas al momento de observación efectiva'}[anchor_mode]
        if anchor_mode!='PRE_RETEST':
            st.caption('Estas velas pueden incluir movimiento posterior al retest; no usarlas como predictores PRE_RETEST.')
        ca=candles_plot(at_anchor,'Asset 1m · '+anchor_title)
        if ca:st.plotly_chart(ca,use_container_width=True)
        g1,g2=st.columns(2)
        with g1:
            fig=candles_plot(ev['pre15'],'15m previas cerradas: originales de Binance')
            if fig:st.plotly_chart(fig,use_container_width=True)
        with g2:
            btc_anchor=ev['observed_btc'] if anchor_mode=='PRE_OBSERVED' else ev['btc']
            fig=candles_plot(btc_anchor,'BTC 1m · '+('antes de observación' if anchor_mode=='PRE_OBSERVED' else 'previas retest'))
            if fig:st.plotly_chart(fig,use_container_width=True)
        st.markdown('**Características de la señal**')
        st.dataframe(pd.DataFrame([{'variable':FEATURE_LABELS.get(k,k),'valor':selected.get(k)} for k in (*FEATURES,*MORPH_FIELDS)]),
          use_container_width=True,hide_index=True)
        with st.expander('JSON causal original congelado'):
            st.json(ev['causal'])
        st.caption('Líneas TP/SL respecto a entrada simulada; no representan órdenes ni fills. El gráfico 5m se deriva de velas 1m y es solo visual. En POST solo hay OHLC 1m; volumen futuro no está incluido en Auto Paths.')
    with tabs[3]:
        cols=['event_key','date_utc','symbol','side','observation_type','path_quality','outcome','entry_delay_min',
              'first_touch_minute','mfe_180_pct','mae_180_pct',
              'pre_realized_vol60_pct','pre_quote_relative5_to_previous30',
              'known_retest_rejection_wick_share','pre_strength_vs_btc60_pct']
        st.dataframe(merged[cols].sort_values('date_utc',ascending=False),use_container_width=True,hide_index=True,height=440)
        st.download_button('Descargar selección CSV',data=merged[cols].to_csv(index=False).encode('utf-8'),
          file_name='reaction_retest_quality_seleccion.csv',mime='text/csv',key='rql_download')

    with tabs[4]:
        from dashboard.services.reaction_reverse_engineering import (
            screen_rules, evaluate_rule_days, describe_rule,
        )
        st.markdown('### 🧠 Ingeniería inversa de runners limpios')
        st.caption(
            'Buscar primero en días anteriores y comprobar SIN REENTRENAR en días posteriores. '
            'Agrupa eventos repetidos del mismo símbolo dentro de 180m; NO equivale a validación prospectiva. '
            'Controles comparables: otras REACTIONs, NO instantes sin REACTION.'
        )
        if mode != 'OBSERVED_OPEN':
            st.warning('Para ingeniería inversa ejecutable, seleccioná OBSERVED_OPEN. '
                       'La entrada estructural puede preceder el conocimiento del retest.')
        if obs != 'LIVE_OBSERVED':
            st.warning('Se analizan SOLO LIVE_OBSERVED; RECOVERED_WINDOW no es entrada ejecutable.')
        if day != 'TODAS' or symbol != 'TODOS':
            st.warning('El análisis se limita a los filtros de día/símbolo aplicados arriba. '
                       'Para el estudio temporal completo elegí TODAS las fechas y TODOS los símbolos.')
        corpus = analysis_before_result_filter.loc[
            analysis_before_result_filter.observation_type.eq('LIVE_OBSERVED')
        ].copy()
        if mode != 'OBSERVED_OPEN':
            st.info('Cambiá Entrada a OBSERVED_OPEN para ejecutar la búsqueda de reglas.')
        else:
            kind = st.radio(
                'Momento en que existe cada predictor',
                ['PRE_RETEST · solo velas anteriores', 'PRE_KNOWN · incluye vela del retest cerrada'],
                horizontal=True, key='rql_reverse_stage',
            )
            pre_fields=[name for name in (*FEATURES,*MORPH_NUMERIC) if name.startswith(('pre_','pre15m_','btc_'))]
            if kind.startswith('PRE_KNOWN'):
                pre_fields += [name for name in (*FEATURES,*MORPH_NUMERIC) if name.startswith('known_')]
            controls=st.columns([1,1,1])
            cost=controls[0].number_input('Coste total supuesto (pp)', min_value=0.0,
                 max_value=1.0,value=0.12,step=0.01,key='rql_reverse_cost')
            minimum=controls[1].slider('Mínimo TRAIN resueltos',30,250,70,10,key='rql_reverse_min')
            prune=controls[2].selectbox('Separar olas del mismo símbolo',
                 [180,60,360,0],format_func=lambda x:f'{x} min' if x else 'Sin agrupación',
                 key='rql_reverse_episode')
            st.caption('Criterio: TP y SL de la tabla principal; los NO_HIT/AMBIGUOUS quedan '
               'fuera del proxy de rentabilidad. Reglas y umbrales se eligen SOLO en TRAIN. '
               'No se usa MFE/MAE posterior para elegir un predictor.')
            if corpus.date_utc.nunique() < 4:
                st.info('Seleccioná al menos cuatro días para separar TRAIN y período posterior.')
            else:
                @st.cache_data(show_spinner='Analizando umbrales, cruces, días y controles comparables...',max_entries=3)
                def _run_reverse(data,fields,tp,sl,cost_value,min_train,wave):
                    return screen_rules(data,fields,tp_pct=tp,sl_pct=sl,
                        cost_pct=cost_value,min_train=min_train,purge_minutes=wave)
                try:
                    result_table, stats, train, held = _run_reverse(
                        corpus, tuple(pre_fields), float(pair[0]), float(pair[1]),
                        float(cost), int(minimum), int(prune))
                except Exception as exc:
                    st.error(f'No se puede evaluar la partición temporal: {exc}')
                else:
                    st.caption('DISCOVERY: '+', '.join(stats['train_days'])+' · POSTERIOR: '+
                               ', '.join(stats['held_days'])+' · '
                               f"excluidas por ola: {stats['excluded_nearby']} · "
                               f"reglas 1D: {stats['searched_single_rules']} · "
                               f"cruces: {stats['searched_pair_rules']}")
                    met=st.columns(4)
                    met[0].metric('TRAIN resueltas',f"{stats['train_events']:,}")
                    met[1].metric('Período posterior',f"{stats['held_events']:,}")
                    met[2].metric('TP-first TRAIN',f"{100*stats['train_baseline']['tp_rate']:.1f}%")
                    met[3].metric('TP-first posterior',f"{100*stats['held_baseline']['tp_rate']:.1f}%")
                    if result_table.empty:
                        st.warning('Ninguna regla superó la criba mínima de discovery: '
                                   'no forzamos un patrón de los datos.')
                    else:
                        display=result_table.drop(columns=['rule']).copy()
                        for col in ('train_tp_rate','held_tp_rate'):
                            display[col]=100*display[col]
                        display=display.rename(columns={
                            'regla':'Condición congelada', 'train_n':'N TRAIN',
                            'train_tp_rate':'TP TRAIN (%)', 'train_proxy_pct':'Proxy TRAIN (pp)',
                            'held_n':'N posterior', 'held_tp_rate':'TP posterior (%)',
                            'held_proxy_pct':'Proxy posterior (pp)',
                            'held_delta_base_pp':'Δ vs base posterior (pp)',
                            'held_matched_delta_pp':'Δ control comparable (pp)',
                            'held_days_above_base':'Días superiores a base',
                            'held_days_present':'Días con casos',
                            'held_coverage_pct':'Cobertura posterior (%)',
                        })
                        cols=['Condición congelada','etapas','N TRAIN','TP TRAIN (%)',
                              'N posterior','TP posterior (%)','Δ vs base posterior (pp)',
                              'Δ control comparable (pp)','Días superiores a base','Días con casos',
                              'Proxy posterior (pp)','Cobertura posterior (%)']
                        st.markdown('#### Reglas candidatas (ordenadas por solidez SOLO en TRAIN)')
                        st.dataframe(display[cols].head(40),hide_index=True,use_container_width=True,height=470)
                        st.caption('El control comparable ajusta por día, lado y cuartil de volatilidad '
                                   'de TRAIN. Solo usa estratos con al menos 3 seleccionadas '
                                   'y 3 no seleccionadas. Puede quedar vacío por falta de controles.')
                        sel = st.selectbox('Examinar condición / días posteriores',
                              list(range(min(25,len(result_table)))),
                              format_func=lambda i:result_table.iloc[i]['regla'],
                              key='rql_reverse_sel')
                        rule=result_table.iloc[sel]['rule']
                        st.markdown('**' + describe_rule(rule,FEATURE_LABELS) + '**')
                        byday=evaluate_rule_days(held,rule,float(pair[0]),float(pair[1]),float(cost))
                        st.dataframe(byday,hide_index=True,use_container_width=True)
                        st.download_button('Descargar resultados del screening CSV',
                            data=display.to_csv(index=False).encode('utf-8'),
                            file_name='retest_inverse_screen_exploratorio.csv',
                            mime='text/csv',key='rql_reverse_export')
                    st.warning('No es una ventaja operativa demostrada: la exploración repetida de '
                        'este período posterior destruye su independencia; se requiere '
                        'congelar reglas, simular costes y salidas por tiempo, incorporar '
                        'controles SIN REACTION y evaluar señales nuevas.')

    with tabs[5]:
        from dashboard.views.reaction_morphology_view import render_morphology_atlas
        render_morphology_atlas(analysis_before_result_filter,project,requested_tp=float(pair[0]),requested_sl=float(pair[1]))

    with tabs[6]:
        from dashboard.views.reaction_compression_lineage_view import render_lineage_atlas
        _render_compatible_research_tab(
            render_lineage_atlas, analysis_before_result_filter, project,
            label='Compression Lineage', tp_pct=float(pair[0]), sl_pct=float(pair[1]),
            outcome_horizon=int(horizon), entry_mode=mode)

    with tabs[7]:
        from dashboard.views.reaction_compression_breakout_retest_v4_view import render_v4
        _render_compatible_research_tab(
            render_v4, analysis_before_result_filter, project,
            label='Breakout Retest V4', tp_pct=float(pair[0]), sl_pct=float(pair[1]),
            outcome_horizon=int(horizon), entry_mode=mode)

    with tabs[8]:
        from dashboard.views.reaction_structural_level_atlas_view import render_level_atlas
        _render_compatible_research_tab(
            render_level_atlas, analysis_before_result_filter, project,
            label='Structural Level Atlas', tp_pct=float(pair[0]), sl_pct=float(pair[1]),
            outcome_horizon=int(horizon), entry_mode=mode)

    with tabs[9]:
        from dashboard.views.reaction_structure_independent_view import render_independent_structures
        _render_compatible_research_tab(
            render_independent_structures, analysis_before_result_filter, project,
            label='Estructuras independientes', tp_pct=float(pair[0]), sl_pct=float(pair[1]),
            outcome_horizon=int(horizon), entry_mode=mode)

    with tabs[10]:
        from dashboard.views.reaction_horizontal_level_rich_card_view import render_horizontal_level_rich_card
        render_horizontal_level_rich_card(analysis_before_result_filter,project,
            tp_pct=float(pair[0]),sl_pct=float(pair[1]),
            outcome_horizon=int(horizon),entry_mode=mode)
