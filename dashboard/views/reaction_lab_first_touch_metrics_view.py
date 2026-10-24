"""Read-only MFE/MAE and TP/SL views for persistent Continuous V1 first touches.

Never computes missing 1m bars, never includes incomplete horizons in averages.
Use identical retest-close baseline to Legacy for LEGACY_ORIGINAL; executable
proxies use the next contiguous 1m OPEN. No transaction costs included.
"""
from __future__ import annotations
from contextlib import closing
from pathlib import Path
import sqlite3
from collections import defaultdict
from statistics import median, mean

HORIZONS=(15,30,60,120,180,360)
MODES={
 'Original · comparable con Legacy':'LEGACY_ORIGINAL',
 'Original · OPEN siguiente 1m':'ORIGINAL_OPEN',
 'Inversa · OPEN siguiente 1m':'INVERSE',
}
STATUS_LABELS={'REACTION':'REACTION','TOUCH_FAILED':'FAILED','TOUCH_INDECISIVE':'INDECISIVE'}

def follow_db(project):
    return Path(project)/'reports/reaction_lab/first_touch_paths_v2.sqlite3'


def connection(journal,following):
    if not Path(journal).is_file() or not Path(following).is_file():
        raise FileNotFoundError('Falta journal de primeros toques o SQLite de recorridos 1m')
    db=sqlite3.connect(Path(journal).resolve().as_uri()+'?mode=ro',uri=True,timeout=10)
    try:
        db.execute('ATTACH DATABASE ? AS fp',(Path(following).resolve().as_uri()+'?mode=ro',))
        db.execute('PRAGMA query_only=ON')
        return db
    except Exception:
        db.close();raise


def where_filters(symbols=None,status=None,side=None,symbol=None,origin=None,scope='filtered'):
    if status is not None and status not in STATUS_LABELS:raise ValueError('Invalid status')
    if side is not None and side not in ('LONG','SHORT'):raise ValueError('Invalid side')
    if origin is not None and origin not in ('LIVE_OBSERVED','RECOVERED_WINDOW'):raise ValueError('Invalid origin')
    if scope not in ('all','last','filtered'):raise ValueError('Invalid scope')
    parts=[];params=[]
    allowed=tuple(sorted(set(str(s).upper() for s in symbols))) if symbols is not None else None
    if allowed is not None:
        if len(allowed)>900:raise ValueError('Too many allowed symbols')
        if not allowed:parts.append('1=0')
        else:
            parts.append('f.symbol IN ('+','.join('?' for _ in allowed)+')');params.extend(allowed)
    if scope=='filtered':
        for field,val in [('status',status),('side',side),('symbol',symbol),('observation_type',origin)]:
            if val is not None:parts.append(f'f.{field}=?');params.append(val)
    if scope=='last':
        parts.append('f.first_seen_ms=(SELECT ended_ms FROM cycles ORDER BY cycle_id DESC LIMIT 1)')
    return (' WHERE '+' AND '.join(parts)) if parts else '',params


def overview(journal,following,*,symbols=None,status=None,side=None,symbol=None,origin=None,scope='filtered',mode='LEGACY_ORIGINAL',horizon=60):
    if mode not in MODES.values():raise ValueError('Invalid perspective')
    if int(horizon) not in HORIZONS:raise ValueError('Invalid horizon')
    where,params=where_filters(symbols,status,side,symbol,origin,scope)
    join_and=where.replace(' WHERE ',' AND ',1) if where else ''
    with closing(connection(journal,following)) as con:
        total=con.execute('SELECT COUNT(*) FROM first_touches f'+where,params).fetchone()[0]
        srows=con.execute('''SELECT f.status,m.horizon,m.mfe_pct,m.mae_pct,m.return_pct
          FROM fp.metrics m JOIN first_touches f ON m.touch_key=f.touch_key
          WHERE m.perspective=?'''+join_and,[mode,*params]).fetchall()
        erows=con.execute('''SELECT f.status,COUNT(*) FROM first_touches f'''+where+''' GROUP BY f.status''',params).fetchall()
        states=con.execute('''SELECT p.state,COUNT(*) FROM fp.paths p JOIN first_touches f ON p.touch_key=f.touch_key'''+where+''' GROUP BY p.state''',params).fetchall()
        outcomes=con.execute('''SELECT f.status,o.tp,o.sl,o.outcome,COUNT(*)
              FROM fp.outcomes o JOIN first_touches f ON o.touch_key=f.touch_key
              WHERE o.perspective=? AND o.horizon=?'''+join_and+''' GROUP BY f.status,o.tp,o.sl,o.outcome''',
              ['ORIGINAL_OPEN' if mode=='LEGACY_ORIGINAL' else mode,int(horizon),*params]).fetchall()
    groups=defaultdict(list)
    for st,h,mfe,mae,ret in srows:
        groups[(st,h)].append((mfe,mae,ret))
        groups[('TODOS',h)].append((mfe,mae,ret))
    metrics=[]
    for (st,h),values in sorted(groups.items()):
        mfes=[x[0] for x in values]; maes=[x[1] for x in values]; rets=[x[2] for x in values]
        metrics.append(dict(status=st,horizon=h,N=len(values),
                            mfe_avg=mean(mfes),mfe_med=median(mfes),
                            mae_avg=mean(maes),mae_med=median(maes),
                            return_avg=mean(rets),return_med=median(rets),
                            positive_close_pct=100*sum(x>0 for x in rets)/len(rets)))
    totals=defaultdict(int)
    for st,tp,sl,oc,n in outcomes:
        totals[(tp,sl,oc)]+=n
    outcomes.extend(('TODOS',tp,sl,oc,n) for (tp,sl,oc),n in totals.items())
    return {'total':total,'status_counts':dict(erows),'path_states':dict(states),
            'metrics':metrics,'outcomes':outcomes}


def page_metrics(journal,following,keys,horizon=60,mode='LEGACY_ORIGINAL',tp=1.0,sl=1.0):
    if not keys:return {}
    if len(keys)>500:raise ValueError('Max 500 keys per page')
    markers=','.join('?' for _ in keys)
    out={key:{} for key in keys}
    with closing(connection(journal,following)) as con:
        for k,h,p,m,a,r in con.execute('''SELECT touch_key,horizon,perspective,mfe_pct,mae_pct,return_pct FROM fp.metrics
               WHERE horizon=? AND perspective=? AND touch_key IN ('''+markers+')',[horizon,mode,*keys]):
            out[k].update(MFE_pct=m,MAE_pct=a,Return_pct=r)
        for k,side,st,count,err in con.execute('''SELECT touch_key,side,state,count,last_error FROM fp.paths
               WHERE touch_key IN ('''+markers+')',keys):
            out[k].update(path_state=st,bars_1m=count)
        for k,oc,minute,gross in con.execute('''SELECT touch_key,outcome,exit_minute,gross_return_pct
             FROM fp.outcomes WHERE horizon=? AND perspective=? AND tp=? AND sl=? AND touch_key IN ('''+markers+')',
             [horizon,'ORIGINAL_OPEN' if mode=='LEGACY_ORIGINAL' else mode,tp,sl,*keys]):
            out[k].update(TP_SL=oc,exit_minute=minute,gross_pct=gross)
    return out


def touch_metrics(journal,following,key):
    with closing(connection(journal,following)) as con:
        rows=con.execute('''SELECT perspective,horizon,mfe_pct,mae_pct,return_pct,reference_price,entry_open
            FROM fp.metrics WHERE touch_key=? ORDER BY perspective,horizon''',(key,)).fetchall()
        outcomes=con.execute('''SELECT perspective,horizon,tp,sl,outcome,exit_minute,gross_return_pct
            FROM fp.outcomes WHERE touch_key=?''',(key,)).fetchall()
        state=con.execute('SELECT state,count,last_error,path_blob FROM fp.paths WHERE touch_key=?',(key,)).fetchone()
    return rows,outcomes,state


def render_overview(project,journal,*,symbols,status,side,symbol,origin):
    import pandas as pd
    import streamlit as st
    db=follow_db(project)
    st.markdown('#### 📐 MFE / MAE · Todos los primeros retesteos')
    st.caption('Horizontes completos 15/30/60/120/180/360m. '
               'MFE y MAE positivos; retorno firmado. **Los eventos sin cobertura 1m suficiente no entran en los promedios.**')
    if not db.is_file():
        st.warning('Aún falta el archivo de recorridos first_touch_paths_v2.sqlite3. '
                   'Activá el colector reaction_lab_first_touch_paths_v2.py; el scanner guarda eventos, no sus velas futuras.')
        return None
    c1,c2,c3=st.columns([2,2,2])
    with c1:
        scope_label=st.selectbox('Universo del resumen',['Histórico completo','Última tanda nueva','Selección filtrada'],
                                 index=2,key='ft_metrics_scope_v2')
    with c2:
        mode_label=st.selectbox('Dirección / referencia',list(MODES),key='ft_metrics_mode_v2')
    with c3:
        horizon=st.selectbox('Horizonte para la tabla individual',[15,30,60,120,180,360],index=2,
                             key='ft_metrics_horizon_v2')
    mode=MODES[mode_label];scope={'Histórico completo':'all','Última tanda nueva':'last','Selección filtrada':'filtered'}[scope_label]
    try:
        s=overview(journal,db,symbols=symbols,status=status,side=side,symbol=symbol,origin=origin,
                   scope=scope,mode=mode,horizon=horizon)
    except (sqlite3.Error,OSError,ValueError) as exc:
        st.error(f'Error leyendo métricas persistidas: {exc}');return None
    x,y,z=st.columns(3)
    x.metric('Eventos en el universo',s['total'])
    selected=[r for r in s['metrics'] if r['horizon']==horizon]
    n=next((r['N'] for r in selected if r['status']=='TODOS'),0)
    y.metric(f'Evaluables · {horizon}m',n)
    z.metric('Sin horizonte completo',max(0,s['total']-n))
    st.caption(f'Estados del seguimiento: {s["path_states"]}. '
               f'**Última tanda nueva** = eventos insertados en el último ciclo del scanner, '
               'no toda la ventana reescaneada.')
    if selected:
        st.markdown('**Resumen de la tanda / universo por estado**')
        table=pd.DataFrame(s['metrics'])
        table['coverage_%']=table.apply(lambda r:round(100*r['N']/max(1,s['status_counts'].get(r['status'],0)),1),axis=1)
        table['total_eventos']=table['status'].map(dict(s['status_counts'],TODOS=s['total']))
        st.dataframe(table[['status','horizon','total_eventos','N','coverage_%','mfe_avg','mfe_med','mae_avg','mae_med','return_avg','return_med','positive_close_pct']].round(4),
                     hide_index=True,use_container_width=True)
        st.download_button('⬇ Exportar métricas por estado/horizonte',table.to_csv(index=False).encode('utf-8-sig'),
                           file_name='continuous_v1_mfe_mae_resumen.csv',mime='text/csv',key='ft_metrics_summary_csv_v2')
    else:st.info('Aún no hay ventanas completas para este universo. Esto NO equivale a MFE/MAE cero.')
    if mode=='LEGACY_ORIGINAL':
        st.caption('**MFE/MAE comparables con Legacy:** referencia `retest_close`; extremos HIGH/LOW posteriores '
                   'desde la siguiente vela 1m contigua. El TP/SL de abajo utiliza **OPEN siguiente 1m** '
                   '(no el cierre del retesteo), como una simulación separada.')
    else:
        st.caption('**Simulación estructural:** referencia OPEN siguiente 1m.')
    st.caption('TP/SL bruto, sin comisión, slippage ni funding; ambos niveles en una vela => SL_AMBIGUOUS.')
    if s['outcomes']:
        src=pd.DataFrame(s['outcomes'],columns=['status','TP_%','SL_%','outcome','n'])
        piv=src.pivot_table(index=['status','TP_%','SL_%'],columns='outcome',values='n',aggfunc='sum',fill_value=0).reset_index()
        for col in ['TP_FIRST','SL_FIRST','SL_AMBIGUOUS','TIME_EXIT']:
            if col not in piv:piv[col]=0
        piv['N']=piv[['TP_FIRST','SL_FIRST','SL_AMBIGUOUS','TIME_EXIT']].sum(axis=1)
        piv['TP_first_%']=(100*piv['TP_FIRST']/piv['N']).round(2)
        piv['SL_y_ambiguo_%']=(100*(piv['SL_FIRST']+piv['SL_AMBIGUOUS'])/piv['N']).round(2)
        st.markdown(f'**TP primero / SL primero · {horizon}m**')
        st.dataframe(piv[['status','TP_%','SL_%','N','TP_FIRST','SL_FIRST','SL_AMBIGUOUS','TIME_EXIT',
                          'TP_first_%','SL_y_ambiguo_%']],hide_index=True,use_container_width=True)
    return mode,horizon


def enrich_page(project,journal,rows,mode,horizon):
    import pandas as pd
    db=follow_db(project)
    if not db.is_file():return pd.DataFrame(rows)
    info=page_metrics(journal,db,[r['touch_key'] for r in rows],horizon,mode)
    merged=[dict(row,**info.get(row['touch_key'],{})) for row in rows]
    frame=pd.DataFrame(merged)
    for col in ('MFE_pct','MAE_pct','Return_pct','TP_SL','path_state','bars_1m'):
        if col not in frame:frame[col]=None
    return frame


def render_touch(project,journal,key,mode,horizon,touch):
    import pandas as pd
    import streamlit as st
    db=follow_db(project)
    if not db.is_file():return
    try:rows,outcomes,state=touch_metrics(journal,db,key)
    except (sqlite3.Error,ValueError,OSError) as exc:
        st.warning(f'No se pudo leer seguimiento 1m: {exc}');return
    st.markdown('##### 📉 Seguimiento individual · MFE / MAE')
    if not state:
        st.info('Todavía no fue registrado por el colector de recorridos 1m.');return
    st.caption(f'Estado: **{state[0]}** · Velas 1m contiguas guardadas: **{state[1]}/360**'
               +(f' · {state[2]}' if state[2] else ''))
    if rows:
        df=pd.DataFrame(rows,columns=['perspectiva','minutos','MFE_%','MAE_%','retorno_%','referencia','open_siguiente'])
        st.dataframe(df.round(5),use_container_width=True,hide_index=True)
        st.download_button('⬇ CSV métricas de este evento',df.to_csv(index=False).encode('utf-8-sig'),
                           file_name='continuous_v1_evento_mfe_mae.csv',mime='text/csv',key='ft_event_metrics_csv_v2')
    else:st.info('Este evento todavía no tiene un horizonte completo de 15 minutos.')
    if outcomes:
        odf=pd.DataFrame(outcomes,columns=['perspectiva','minutos','TP_%','SL_%','resultado','min_salida','retorno_bruto_%'])
        structural_mode='ORIGINAL_OPEN' if mode=='LEGACY_ORIGINAL' else mode
        st.dataframe(odf[(odf.perspectiva==structural_mode)&(odf.minutos==horizon)].round(4),
                     use_container_width=True,hide_index=True)
    if state[3] and st.checkbox('Ver velas 1m guardadas de este evento',key='ft_event_saved_1m_v2'):
        import plotly.graph_objects as go
        from reaction_lab_first_touch_paths_v2 import decode
        bars=decode(state[3]);df=pd.DataFrame(bars,columns=['timestamp','open','high','low','close'])
        x=pd.to_datetime(df.timestamp,unit='ms',utc=True).dt.tz_convert('America/Argentina/Buenos_Aires')
        fig=go.Figure(data=[go.Candlestick(x=x,open=df.open,high=df.high,low=df.low,close=df.close)])
        try:
            swing=float(touch.get('swing_price'))
            if swing>0:fig.add_hline(y=swing,line_dash='dash',annotation_text='Swing')
        except (ValueError,TypeError):pass
        fig.update_layout(title='Velas posteriores 1m realmente archivadas',xaxis_rangeslider_visible=False,
                          height=470,margin={'l':5,'r':5,'t':50,'b':5})
        st.plotly_chart(fig,use_container_width=True,key='ft_saved_1m_candles_v2')
