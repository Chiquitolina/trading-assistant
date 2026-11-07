"""Embedded Streamlit panel for ALL first touches, separate from Runner Discovery V2.

All inputs are read-only. The optional refresh writes only derived CSV/JSON.
"""
from __future__ import annotations

import json
from pathlib import Path

from reaction_lab_runner_discovery_all_touches_v1 import (
    build_dataset, RESULT_DIR, iter_events, recovery_index, ro_connect,
)

STATES = ('REACTION','TOUCH_INDECISIVE','TOUCH_FAILED')


def _select_outcome(result,minute,horizon):
    if result is None or str(result) in ('','nan','None'):
        return 'SIN_DATOS'
    if str(result)=='NO_HIT':
        return 'NO_HIT'
    try:
        when=float(minute)
    except (ValueError,TypeError):
        return 'SIN_DATOS'
    return str(result) if when<=horizon else 'NO_HIT'


def make_summary(frame, *, horizon, tp, basis):
    """Requires path_count>=horizon. Return grouped descriptive rates only."""
    import pandas as pd
    status='first_touch_status'
    result=[]
    for group in STATES:
        sub=frame.loc[frame[status].eq(group)]
        valid=sub.loc[sub['path_quality'].eq('CONTIGUOUS_VALID') &
                      sub['path_count'].ge(horizon) &
                      sub['retest_close' if basis=='retest' else 'next_open'].gt(0)].copy()
        hit=pd.to_numeric(valid[f'{basis}_time_to_run_{tp}pct'],errors='coerce')
        n_runner=int(hit.le(horizon).sum())
        outcomes=[_select_outcome(r,m,horizon) for r,m in zip(
            valid[f'{basis}_tp{tp}_result'],valid[f'{basis}_tp{tp}_minute'])]
        n_tp=sum(x=='TP_FIRST' for x in outcomes)
        result.append({'Clasificación inicial':group,'Total en catálogo':len(sub),
                       'Con cobertura':len(valid),'Sin cobertura':len(sub)-len(valid),
                       f'Runners +{tp}%':n_runner,
                       'Frecuencia runners %':round(100*n_runner/len(valid),2) if len(valid) else None,
                       'TP antes de SL −1%':n_tp,
                       'TP-first %':round(100*n_tp/len(valid),2) if len(valid) else None})
    return pd.DataFrame(result)


def render_all_first_touches(project):
    import pandas as pd
    import streamlit as st

    project=Path(project).resolve()
    folder=project/RESULT_DIR
    csv_path=folder/'all_first_touches.csv'
    manifest_path=folder/'manifest.json'
    st.markdown('#### 🌐 Runners de TODOS los primeros retests')
    st.caption('Incluye REACTION, TOUCH_INDECISIVE y TOUCH_FAILED. La clasificación inicial permanece congelada. '
               'El recorrido favorable no implica TP-first, ni entrada operable.')
    info,action=st.columns([4,1.35],vertical_alignment='center')
    if manifest_path.is_file():
        try:
            m=json.loads(manifest_path.read_text(encoding='utf-8'))
            info.caption('Dataset offline: '+str(m.get('created_utc',''))[:16].replace('T',' ')+' UTC')
        except (OSError,ValueError):
            info.caption('Dataset offline disponible')
    else:
        info.caption('Todavía no se generó el dataset de todos los primeros retests')
    if action.button('↻ Actualizar TODOS',key='rdall_refresh',use_container_width=True):
        try:
            with st.spinner('Calculando resultados desde las trayectorias 1m verificadas...'):
                build_dataset(project)
            st.success('Dataset completo regenerado. Sin modificar SQLite ni procesos PM2.')
        except Exception as exc:
            st.error(f'No se pudo generar el dataset: {type(exc).__name__}: {exc}')
            return
    if not csv_path.is_file():
        st.info('Presioná «Actualizar TODOS» para generar el catálogo desde First Touch V2 + Auto Paths V1.')
        return

    @st.cache_data(show_spinner=False,max_entries=2)
    def _load(path,mtime):
        return pd.read_csv(path,low_memory=False)
    try:
        frame=_load(str(csv_path),csv_path.stat().st_mtime_ns)
    except (OSError,ValueError,pd.errors.ParserError) as exc:
        st.error(f'Error leyendo CSV: {exc}')
        return
    for col in ('path_count','retest_close','next_open','retest_ts'):
        frame[col]=pd.to_numeric(frame[col],errors='coerce')
    if frame.empty:
        st.warning('Catálogo vacío.')
        return
    a,b,c,d=st.columns([1.1,1.1,1.15,1.55],gap='small')
    tp=int(a.selectbox('Recorrido favorable',['+2%','+3%','+5%'],key='rdall_tp')[1:-1])
    horizon=int(b.selectbox('Horizonte',['15','30','60','120','180','360'],index=4,
                            format_func=lambda s:s+' min',key='rdall_horizon'))
    label=c.selectbox('Base',['Cierre retest','OPEN siguiente 1m'],key='rdall_basis')
    basis='retest' if label=='Cierre retest' else 'open'
    chosen=d.selectbox('Estado inicial',['TODOS',*STATES],key='rdall_status')
    st.caption('Denominador: únicamente eventos con velas 1m continuas hasta el horizonte seleccionado. '
               'Las trayectorias ausentes o recientes se contabilizan aparte, NO como no-runners.')
    summary=make_summary(frame,horizon=horizon,tp=tp,basis=basis)
    st.dataframe(summary,hide_index=True,use_container_width=True)
    group=frame if chosen=='TODOS' else frame.loc[frame['first_touch_status'].eq(chosen)]
    eligible=group.loc[group['path_quality'].eq('CONTIGUOUS_VALID') &
                       group['path_count'].ge(horizon) &
                       group['retest_close' if basis=='retest' else 'next_open'].gt(0)].copy()
    hit_column=f'{basis}_time_to_run_{tp}pct'
    hit=pd.to_numeric(eligible[hit_column],errors='coerce')
    runners=eligible.loc[hit.le(horizon)].copy()
    nohit=eligible.loc[~hit.le(horizon)]
    m1,m2,m3,m4=st.columns(4)
    m1.metric('Primeros retests',f'{len(group):,}'.replace(',','.'))
    m2.metric('Evaluables',f'{len(eligible):,}'.replace(',','.'))
    m3.metric(f'Runners +{tp}%',f'{len(runners):,}'.replace(',','.'))
    m4.metric('Frecuencia',f'{100*len(runners)/len(eligible):.1f}%' if len(eligible) else '—')
    st.caption(f'Sin recorrido completo o referencia válida: {len(group)-len(eligible):,}. '
               f'Con cobertura, pero sin +{tp}% en {horizon}m: {len(nohit):,}. '
               'La cobertura histórica fue reconstruida; sigue siendo una comparación descriptiva, no una señal de trading.')
    e1,e2,e3=st.columns([1.3,1.1,1.3])
    only_runners=e1.checkbox('Solo runners',value=True,key='rdall_onlyrunners')
    search=e2.text_input('Símbolo',key='rdall_symbol',placeholder='Ej. BTC').strip().upper()
    from_day=e3.selectbox('Desde fecha', ['Todo el período',*sorted(frame['date_utc'].dropna().astype(str).unique())],
                          key='rdall_from_day')
    listing=runners if only_runners else group
    if search:
        listing=listing.loc[listing['symbol'].astype(str).str.upper().str.contains(search,regex=False)]
    if from_day!='Todo el período':
        listing=listing.loc[listing['date_utc'].astype(str).ge(from_day)]
    cols=['date_utc','symbol','side','first_touch_status','path_source','path_count',
          'path_quality',hit_column,f'{basis}_tp{tp}_result',f'{basis}_tp{tp}_minute','touch_key']
    table=listing.loc[:,cols].sort_values(['date_utc','symbol'],ascending=[False,True])
    st.dataframe(table.head(1000),hide_index=True,use_container_width=True)
    st.caption(f'Se muestran hasta 1.000 filas de {len(table):,}. El CSV exporta todas las filas del filtro.')
    st.download_button('Descargar CSV del filtro',data=table.to_csv(index=False).encode('utf-8-sig'),
                       file_name=f'first_touch_all_runners_{chosen.lower()}_{tp}pct_{horizon}m.csv',
                       mime='text/csv',key='rdall_export')
    st.caption('Cobertura por categoría: consultar el denominador evaluable del informe actual. '
               'Los eventos recientes o sin horizonte completo nunca se cuentan como perdedores.')
    st.divider()
    render_all_first_touch_chart(project,table,tp=tp,horizon=horizon,basis=basis)
    st.divider()
    from dashboard.views.reaction_lab_failed_reclaim_audit_v1_view import render_failed_reclaim_audit
    render_failed_reclaim_audit(project)
    st.divider()
    try:
        from dashboard.views.reaction_lab_failed_pullback_entry_v2_view import render_failed_pullback_entry
        render_failed_pullback_entry(project)
    except ImportError as exc:
        st.info(f'FAILED Sweep V2 todavía no está instalado: {exc}')


def _all_first_touch_path(project,key):
    root=Path(project)/'reports/reaction_lab/first_touch_historical_recovery_v11'
    hist,_=recovery_index(root)
    matches=list(iter_events(project,recovery=hist,include_bars=True,only_touch_key=key))
    if len(matches)!=1:raise ValueError('No existe un único primer retest para esta identidad')
    row,bars=matches[0]
    if row['path_quality']!='CONTIGUOUS_VALID' or not bars:
        raise ValueError('El episodio no tiene OHLC verificado')
    return row,bars


def _level_from_journal(project,key):
    import json
    journal=Path(project)/'reports/reaction_lab/scanner_first_touches_v1.sqlite3'
    with ro_connect(journal) as db:
        record=db.execute('SELECT status,causal_json FROM first_touches WHERE touch_key=?',
                          (key,)).fetchone()
    if record is None:return None
    try:
        meta=json.loads(record[1])
        if not isinstance(meta,dict):return None
        value=float(meta.get('swing_price'))
        return value if value>0 else None
    except (ValueError,TypeError):return None


def plot_all_first_touch_case(bars,row,*,tp=2,sl=1.,n=180,basis='open',
                              level=None,sweep_depth=1.):
    import plotly.graph_objects as go
    import pandas as pd
    seq=bars[:min(n,len(bars))]
    if not seq:raise ValueError('Missing 1m bars')
    xs=pd.to_datetime([b[0] for b in seq],unit='ms',utc=True)
    fig=go.Figure(go.Candlestick(x=xs,open=[b[1] for b in seq],
        high=[b[2] for b in seq],low=[b[3] for b in seq],close=[b[4] for b in seq],
        increasing_line_color='#16856a',decreasing_line_color='#dc5b4d',name='POST 1m'))
    side=row['side'];price=float(seq[0][1]) if basis=='open' else float(row['retest_close'])
    fig.add_hline(y=price,line_dash='dot',line_color='#a68b59',annotation_text='Base seleccionada')
    if level is not None:
        fig.add_hline(y=level,line_dash='dash',line_color='#64748b',annotation_text='Nivel original')
    tp_price=price*(1+tp/100 if side=='LONG' else 1-tp/100)
    sl_price=price*(1-sl/100 if side=='LONG' else 1+sl/100)
    fig.add_hline(y=tp_price,line_dash='dot',line_color='#16856a',annotation_text=f'TP +{tp:g}%')
    fig.add_hline(y=sl_price,line_dash='dot',line_color='#cd544b',annotation_text=f'SL −{sl:g}%')
    if row['first_touch_status']=='TOUCH_FAILED':
        fail_close=float(row['retest_close'])
        sweep=fail_close*(1-sweep_depth/100 if side=='LONG' else 1+sweep_depth/100)
        fig.add_hline(y=sweep,line_dash='dash',line_color='#b77ed8',
                      annotation_text=f'Sweep adverso {sweep_depth:g}%')
    fig.update_layout(height=490,margin=dict(l=12,r=20,t=25,b=25),
        xaxis_rangeslider_visible=False,xaxis_title='POST 1m, UTC',
        yaxis_title='Precio',hovermode='x unified')
    return fig


def render_all_first_touch_chart(project,listing,*,tp,horizon,basis):
    import streamlit as st
    st.markdown('**📈 Gráfico del episodio seleccionado (velas verificadas)**')
    st.caption('Ahora aparece directamente en Runner Discovery. Seleccioná un caso para ver '
               'las velas POST y el nivel original; no requiere descargar un CSV ni otro dashboard.')
    if listing.empty:
        st.info('Sin eventos para graficar con estos filtros.');return
    items=listing.head(250)
    keys=items['touch_key'].astype(str).tolist()
    labels={r['touch_key']:f"{r['date_utc']} · {r['symbol']} {r['side']} · {r['first_touch_status']}"
            for _,r in items.iterrows()}
    key=st.selectbox('Episodio entre las primeras 250 filas',keys,
                     format_func=lambda k:labels.get(k,k),key='rdall_chart_key')
    c1,c2,c3=st.columns(3)
    visible=c1.select_slider('Velas POST',options=[60,120,180,240,360],
             value=min(360,max(60,horizon)),key='rdall_chart_bars')
    sl_choice=c2.selectbox('SL visual (%)',[1.,3.,4.,5.,'OTRO'],
           format_func=lambda x: 'Otro…' if x=='OTRO' else f'−{x:g}%',key='rdall_visual_sl_choice')
    sl=float(sl_choice) if sl_choice!='OTRO' else float(c2.number_input(
        'SL visual personalizado (%)',min_value=.25,max_value=10.,value=2.,
        step=.25,format='%.2f',key='rdall_visual_sl_custom'))
    sweep_depth=c3.number_input('Sweep visual (%)',min_value=.25,max_value=10.,value=1.,
                       step=.25,format='%.2f',key='rdall_visual_sweep')
    if sl!=1.:
        st.warning('El CSV agregado de TP-first usa SL −1%. Cambiar el SL VISUAL '
                   'solo mueve la línea del gráfico; para otras simulaciones usá FAILED Sweep V2.')
    try:
        source,bars=_all_first_touch_path(project,key)
        level=_level_from_journal(project,key)
        fig=plot_all_first_touch_case(bars,source,tp=tp,sl=sl,n=visible,
                  basis=basis,level=level,sweep_depth=sweep_depth)
        st.plotly_chart(fig,use_container_width=True,key='rdall_verified_candles',
                        config={'displaylogo':False})
        st.caption(f"Fuente verificada: {source['path_source']} · "
                   f"{source['path_count']} velas de 1 minuto disponibles. "
                   'El nivel original se dibuja solo cuando consta en el diario causal.')
    except Exception as exc:
        st.error(f'No se pudo abrir el gráfico: {type(exc).__name__}: {exc}')
