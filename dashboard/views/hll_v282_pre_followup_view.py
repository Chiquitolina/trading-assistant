"""V2.8.2: one stable PRE cohort, POST outcomes drawn on the *same* episode.

Standalone block imported by the existing V2.8 Streamlit view. No trading code.
"""
from __future__ import annotations
import json
import hashlib
from pathlib import Path
import pandas as pd

TF=1_800_000
RUNS=Path('reports/horizontal_level_structure_quality_v282/runs')
CANDLES=Path('reports/horizontal_level_lifecycle_v21/candles_aug20_oct25_core15_btc')
V29_EVENTS=Path('reports/horizontal_level_continuation_v29/runs/core15_continuation_v29/continuation_events.csv')
V291_AUDIT=Path('reports/horizontal_level_structure_integrity_v291/runs/core15_trendline_v291')
PATTERNS={
    'ASCENDING_CLEAN':'A · Mínimos ascendentes limpios',
    'ASCENDING_CHOPPY':'B · Mínimos ascendentes con cruces desordenados',
    'HORIZONTAL_BASE':'C · Base horizontal bajo resistencia',
    'MULTI_CROSS_RANGE':'D · Múltiples cruces / rango errático',
    'IMPULSIVE_APPROACH':'E · Aproximación impulsiva al techo',
    'DESCENDING_LOW_SEQUENCE':'F · Mínimos descendentes',
    'OTHER':'G · Mixta / otra estructura',
    'UNKNOWN':'Sin contexto suficiente',
    'ASCENDING_TREND_BROKEN':'H · Tendencia ascendente rota (PRE)',
}
STATES={
    'FLIP_CONFIRMADO':'Flip confirmado',
    'SIN_FLIP_EN_VENTANA':'Sin flip dentro del seguimiento V2.6',
    'INDETERMINADO':'No resuelto / censurado',
}


def load_cohort(folder):
    file=folder/'pre_cohort.csv'
    df=pd.read_csv(file,low_memory=False)
    required={'attempt_id','symbol','post_outcome','breakout_closed_ts',
              'pre_closed_ts','flip_confirmed_closed_ts','retest_closed_ts',
              'failed_closed_ts','zone_low','zone_high','v26_status'}
    for w in (24,48,96):
        required.update({f'pre_pattern_{w}',f'pre{w}_pivot_lows_json',f'flip_pattern_{w}'})
    if required-set(df):raise ValueError('V2.8.2: columnas ausentes: '+', '.join(sorted(required-set(df))))
    if df.attempt_id.isna().any() or df.attempt_id.duplicated().any():
        raise ValueError('V2.8.2: attempt_id duplicado o vacío')
    if df.post_outcome.isna().any() or not set(df.post_outcome).issubset(STATES):
        raise ValueError('V2.8.2: estado POST inválido')
    if (df.loc[df.post_outcome.eq('FLIP_CONFIRMADO'),'flip_confirmed_closed_ts'].isna().any()
            or df.loc[~df.post_outcome.eq('FLIP_CONFIRMADO'),'flip_confirmed_closed_ts'].notna().any()):
        raise ValueError('V2.8.2: fecha de confirmación inconsistente')
    return df


def filtered(df, window, pattern='TODAS', symbol='Todos', state='TODOS', pattern_field=None):
    field=pattern_field or f'pre_pattern_{int(window)}'
    if field not in df.columns:
        raise ValueError('Clasificación ausente: '+field)
    choices=df.copy()
    if pattern!='TODAS':choices=choices[choices[field].eq(pattern)]
    if symbol!='Todos':choices=choices[choices.symbol.eq(symbol)]
    if state!='TODOS':choices=choices[choices.post_outcome.eq(state)]
    return choices


def _clock(value):
    if value is None or pd.isna(value):return None
    n=int(value)
    return n if n>0 else None


def _date(ms):
    return pd.Timestamp(int(ms),unit='ms',tz='UTC')



def _validate_visible_ohlc(frame):
    """Raise on malformed OHLC, rather than rendering an invented long wick."""
    import numpy as np
    required=('timestamp','open','high','low','close')
    if set(required)-set(frame):
        raise ValueError('Faltan columnas OHLC para graficar')
    vals=frame[list(required[1:])].apply(pd.to_numeric,errors='coerce')
    if not np.isfinite(vals.to_numpy()).all():
        raise ValueError('OHLC incompleto o no finito en el episodio')
    invalid=(vals.high<vals[['open','close','low']].max(axis=1)) | (vals.low>vals[['open','close','high']].min(axis=1)) | (vals.low<=0)
    if invalid.any():
        ts=frame.loc[invalid,'timestamp'].iloc[0]
        raise ValueError(f'OHLC inconsistente en vela con timestamp {int(ts)}; revisar CSV histórico')
    if frame.timestamp.duplicated().any():
        raise ValueError('Velas 30m duplicadas en episodio')


def episode_markers(row, bars, show_post):
    """Time-anchored marker data; no full-height lines that resemble wicks."""
    if not show_post:return []
    events=[('Breakout CROSS_UP','breakout_closed_ts','triangle-up','#459df5'),
            ('Contacto retest','retest_closed_ts','circle-open','#f0bd5e'),
            ('FLIP confirmado','flip_confirmed_closed_ts','diamond','#16c48a'),
            ('Nivel perdido / intento fallido','failed_closed_ts','x','#f26872')]
    out=[]
    for label,field,symbol,color in events:
        clock=_clock(row.get(field))
        if clock is None:continue
        target=bars.loc[bars.timestamp.eq(clock-TF)]
        if len(target)!=1:
            raise ValueError(f'{label}: falta vela cerrada en {_date(clock)}')
        candle=target.iloc[0]
        out.append(dict(label=label,time=clock,open_ts=int(candle.timestamp),
                        close=float(candle.close),low=float(candle.low),high=float(candle.high),
                        symbol=symbol,color=color))
    return out


def load_v29_events(path):
    """Optional exploratory overlay, keyed exclusively by the frozen attempt ID."""
    events=pd.read_csv(path,low_memory=False)
    required={'attempt_id','branch','status','confirmed_ts','signal_close','signal_open_ts'}
    if required-set(events):
        raise ValueError('V2.9 sin columnas: '+', '.join(sorted(required-set(events))))
    if events.attempt_id.isna().any():
        raise ValueError('V2.9 contiene IDs nulos')
    for field in ('attempt_id','branch','status'):
        events[field] = events[field].astype(str).str.strip()
    if events[['attempt_id','branch']].duplicated().any() or events.attempt_id.isna().any():
        raise ValueError('V2.9 con IDs repetidos o vacíos')
    if events.loc[events.status.eq('CONFIRMED'),'confirmed_ts'].isna().any():
        raise ValueError('V2.9 confirmado sin hora conocida')
    return events


def load_v291_audit(cohort_path, folder, cohort):
    """Optional PRE-only integrity layer; fail visibly if a different cohort was used."""
    source=folder/'integrity_audit.csv'
    if not source.is_file():
        return None
    manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    original_hash=hashlib.sha256(cohort_path.read_bytes()).hexdigest()
    if original_hash != manifest.get('original_sha256'):
        raise ValueError('Auditoría V2.9.1 generada sobre otro archivo PRE')
    audit=pd.read_csv(source,low_memory=False)
    required={'attempt_id'}|{f'pre_pattern_v291_{w}' for w in (24,48,96)}
    if required-set(audit) or audit.attempt_id.isna().any() or audit.attempt_id.duplicated().any():
        raise ValueError('Auditoría V2.9.1 incompleta / con IDs duplicados')
    if set(audit.attempt_id)!=set(cohort.attempt_id) or len(audit)!=len(cohort):
        raise ValueError('Auditoría V2.9.1 y V2.8.2 tienen universos distintos')
    return audit


def make_chart(row,bars,window,show_post=False,show_flip_pivots=False,continuation_events=None,post_bars=96):
    import plotly.graph_objects as go
    pre=int(row.pre_closed_ts)
    breakout=int(row.breakout_closed_ts)
    flip=_clock(row.flip_confirmed_closed_ts)
    retest=_clock(row.retest_closed_ts)
    failed=_clock(row.failed_closed_ts)
    extra_clocks=[_clock(e.get('confirmed_ts')) for e in (continuation_events or []) if e.get('status')=='CONFIRMED']
    last_event=max([breakout]+[v for v in [flip,retest,failed]+extra_clocks if v is not None])
    # DIRECT_NO_RETEST_24B cannot be established until 24 retest candles have elapsed.
    # Extend chart farther than the first few post-breakout bars, without altering status.
    if str(row.v26_status)=='DIRECT_NO_RETEST_24B':
        last_event=max(last_event,breakout+26*TF)
    if post_bars not in (24,48,96,160,240):
        raise ValueError("Ventana POST inválida")
    # A minimum of POST bars after breakout, even if no flip/retest event exists.
    # Never fabricate missing candles: actual coverage remains limited by CSV.
    end=pre if not show_post else max(last_event+12*TF, breakout+int(post_bars)*TF)
    start=pre-(int(window)+8)*TF
    visible=bars.loc[(bars.timestamp>=start)&(bars.timestamp+TF<=end)].copy()
    if visible.empty:raise ValueError('No hay velas 30m para PRE/POST')
    if not bars.timestamp.eq(pre-TF).any():
        raise ValueError('Falta vela al corte PRE')
    _validate_visible_ohlc(visible)
    fig=go.Figure()
    x=pd.to_datetime(visible.timestamp,unit='ms',utc=True)
    fig.add_trace(go.Candlestick(x=x,open=visible.open,high=visible.high,
                                 low=visible.low,close=visible.close,name='Velas 30m',
                                 increasing_line_color='#17a883',decreasing_line_color='#d75f69',
                                 increasing_fillcolor='#17a883',decreasing_fillcolor='#d75f69'))
    lo=float(row.zone_low);hi=float(row.zone_high)
    fig.add_hrect(y0=lo,y1=hi,fillcolor='#d5a352',opacity=.17,line_width=0)
    fig.add_hline(y=lo,line_color='#bc9553',line_dash='dot')
    fig.add_hline(y=hi,line_color='#bc9553',line_dash='dash')
    pivot_field=f'pre{window}_pivot_lows_json'
    raw=row.get(pivot_field,'[]')
    pivots=json.loads(raw) if isinstance(raw,str) and raw else []
    if any(int(p['known_closed_ts'])>pre for p in pivots):
        raise ValueError('Lookahead: pivote PRE confirmado después del corte')
    if pivots:
        fig.add_trace(go.Scatter(x=[_date(p['open_ts']) for p in pivots],
            y=[float(p['low']) for p in pivots],mode='markers+lines',
            line=dict(color='#f9bc55',width=1.5,dash='dot'),
            marker=dict(size=9,color='#f9bc55',symbol='triangle-up'),
            name='Mínimos confirmados en PRE',
            text=[f"Confirmado: {_date(p['known_closed_ts'])}" for p in pivots],
            hovertemplate='%{text}<br>Precio: %{y}<extra></extra>'))
    # V2.9.1: trendline built from three 2x2 CONFIRMED pivot lows at PRE.
    # Historical original V2.8.2 classification remains immutable.
    first=_clock(row.get(f'trendline{window}_anchor_first_ts'))
    last=_clock(row.get(f'trendline{window}_anchor_last_ts'))
    lp_first=row.get(f'trendline{window}_line_price_first')
    lp_pre=row.get(f'trendline{window}_line_price_at_pre')
    if (first is not None and last is not None and pd.notna(lp_first) and
            pd.notna(lp_pre) and first<pre):
        fig.add_trace(go.Scatter(
            x=[_date(first),_date(pre-TF)],
            y=[float(lp_first),float(lp_pre)], mode='lines',
            line=dict(color='#f8a83a',width=2,dash='dash'),
            name='Directriz PRE (3 pivotes confirmados)',
            hovertemplate='Directriz investigada: %{y:.8f}<extra></extra>'))
        broken=_clock(row.get(f'trendline{window}_broken_closed_ts'))
        if broken is not None and broken<=pre:
            b=bars.loc[bars.timestamp.eq(broken-TF)]
            if len(b)==1:
                fig.add_trace(go.Scatter(
                    x=[_date(broken-TF)],y=[float(b.close.iloc[0])],mode='markers',
                    marker=dict(size=14,color='#f26d74',symbol='x',line=dict(width=2)),
                    name='Directriz rota en PRE (2 cierres)',
                    text=[f'Ruptura conocida: {_date(broken)}'],
                    hovertemplate='%{text}<br>Cierre: %{y}<extra></extra>'))
    # The ONLY full-height vertical line is the neutral PRE freeze boundary.
    fig.add_vline(x=_date(pre),line_color='#9aa6b7',line_dash='dash',line_width=1.2)
    if show_post:
        fig.add_vrect(x0=_date(breakout-TF),x1=_date(end),
                      fillcolor='#2b9c90',opacity=.04,line_width=0)
        for ev in episode_markers(row,bars,True):
            fig.add_trace(go.Scatter(x=[_date(ev['open_ts'])],y=[ev['close']],
                mode='markers',name=ev['label'],
                marker=dict(color=ev['color'],size=12,symbol=ev['symbol'],line=dict(width=1,color='#ffffff')),
                text=[f"{ev['label']} conocido al cierre {_date(ev['time'])}"],
                hovertemplate='%{text}<br>Cierre: %{y}<extra></extra>'))
        # New, independent V2.9 tracks. Their confirmation marks NEVER replace
        # or relabel the original V2.6 RETEST / FLIP outcomes.
        for ev in continuation_events or []:
            if ev.get('status')!='CONFIRMED':
                continue
            t=_clock(ev.get('confirmed_ts'))
            anchor=_clock(ev.get('signal_open_ts'))
            if t is None or anchor!=t-TF:
                raise ValueError('V2.9: timestamp inconsistente')
            candle=bars.loc[bars.timestamp.eq(anchor)]
            if len(candle)!=1:
                raise ValueError('V2.9: falta vela de confirmación')
            price=float(ev['signal_close'])
            if abs(float(candle.close.iloc[0])-price)>max(abs(price)*1e-7,1e-10):
                raise ValueError('V2.9: precio de señal no coincide con OHLC')
            reclaim=ev['branch']=='RECLAIM_CONTINUATION'
            contacted=bool(ev.get('contact_before_confirmation',False))
            label=('Reclaim confirmado V2.9' if reclaim else
                   'Continuación V2.9 (tras contacto)' if contacted else
                   'Continuación V2.9 (sin contacto)')
            fig.add_trace(go.Scatter(x=[_date(anchor)],y=[price],mode='markers',
                name=label,
                marker=dict(size=14,symbol='star' if reclaim else 'triangle-up',
                            color='#ad80ef' if reclaim else '#4ac7f5',
                            line=dict(width=1,color='#ffffff')),
                hovertemplate=(label+
                   ' confirmado V2.9 al cierre '+str(_date(t))+
                   '<br>Precio de señal: %{y}<extra></extra>')))
        if show_flip_pivots and flip is not None:
            field=f'flip{window}_pivot_lows_json'
            raw_flip=row.get(field,'[]'); fp=json.loads(raw_flip) if isinstance(raw_flip,str) else []
            fp=[p for p in fp if int(p['known_closed_ts'])<=flip]
            if fp:
                fig.add_trace(go.Scatter(x=[_date(p['open_ts']) for p in fp],
                  y=[float(p['low']) for p in fp],mode='markers',
                  marker=dict(color='#57a4f5',size=8,symbol='circle-open'),
                  name='Pivotes conocidos al FLIP (solo POST)'))
    fig.update_layout(height=630,xaxis_rangeslider_visible=False,
        margin=dict(l=12,r=10,t=22,b=15),xaxis_title='UTC',yaxis_title='Precio',showlegend=True)
    return fig


def render_hll_v282_pre_followup(root,blind=True):
    """Return True if V282 panel was rendered, False if its run isn't installed."""
    import streamlit as st
    root=Path(root)
    parent=root/RUNS
    runs=sorted([p for p in parent.iterdir() if (p/'pre_cohort.csv').is_file() and
                 (p/'manifest.json').is_file()],key=lambda p:p.stat().st_mtime,reverse=True) if parent.is_dir() else []
    if not runs:return False
    st.markdown('#### Inspector de candidatos PRE → desenlace del mismo intento · V2.8.2')
    folder=st.selectbox('Corrida de cohortes PRE',runs,format_func=lambda p:p.name,key='hll282_run')
    try:df=load_cohort(folder)
    except (OSError,ValueError,KeyError) as ex:
        st.error(f'V2.8.2 incompleto: {ex}')
        return True
    # V2.9.2 is read-only and optional, and NEVER joins outcomes into PRE classification.
    has_v292=False;v292_contacts=None;v292_survival=None
    try:
        from dashboard.views.hll_v292_inspector_overlay import (
            V292, LABELS, read_v292, overlay_pre_contacts, overlay_physical_clusters,
        )
    except ModuleNotFoundError:
        V292=None
    if V292 is not None and (root/V292/'mechanism_attempts.csv').is_file():
        try:
            extras,v292_contacts,v292_survival=read_v292(folder/'pre_cohort.csv',root/V292,df)
            df=df.merge(extras,on='attempt_id',how='left',validate='one_to_one')
            has_v292=True
        except (ValueError,KeyError,OSError) as ex:
            st.error(f'V2.9.2 incompatible: {ex}')
            st.stop()
    has_v291=False
    try:
        audit=load_v291_audit(folder/'pre_cohort.csv',root/V291_AUDIT,df)
        if audit is not None:
            df=df.merge(audit,on='attempt_id',how='left',validate='one_to_one')
            has_v291=True
    except (ValueError,KeyError,OSError) as ex:
        st.error(f'La auditoría V2.9.1 no coincide con el PRE original: {ex}')
        st.stop()
    if has_v291:
        taxonomy=st.radio('Clasificación morfológica',
            ['V2.9.1 · Integridad de la directriz PRE','V2.8.2 · Original, sin cambios'],
            horizontal=True,key='hll291_taxonomy')
        revised=taxonomy.startswith('V2.9.1')
        st.caption('V2.9.1 comprueba si una directriz de tres pivotes confirmados se rompió '
                   'por dos cierres consecutivos al menos 0,35 ATR por debajo. '
                   'Es una hipótesis geométrica auditable, no una señal de entrada.')
    else:
        revised=False
        st.caption('Auditoría de directriz V2.9.1 todavía no ejecutada. '
                   'Se mantienen las etiquetas originales V2.8.2.')
    a,b,c=st.columns([1,2,1.5])
    with a:window=st.selectbox('Ventana 30m',[24,48,96],index=1,key='hll282_window')
    with b:
        pattern_choices=[k for k in PATTERNS if revised or k!='ASCENDING_TREND_BROKEN']
        pattern=st.selectbox('Tipo de estructura PRE',['TODAS']+pattern_choices,
            format_func=lambda x: 'Todas las estructuras PRE' if x=='TODAS' else PATTERNS[x],key='hll282_pattern')
    with c:
        symbol=st.selectbox('Símbolo',['Todos']+sorted(df.symbol.unique().tolist()),key='hll282_symbol')
    if blind:
        st.caption('Auditoría ciega activa: los desenlaces están ocultos. Desactivala arriba para ver cuándo confirmó cada candidato.')
        state='TODOS'
    else:
        state=st.selectbox('Desenlace posterior',['TODOS']+list(STATES),
            format_func=lambda x:'Todos los desenlaces' if x=='TODOS' else STATES[x],key='hll282_state')
    class_field=(f'pre_pattern_v291_{window}' if revised else f'pre_pattern_{window}')
    sel=filtered(df,window,pattern,symbol,state,pattern_field=class_field)
    if has_v292:
        mechanism=st.selectbox('Mecanismo PRE V2.9.2',
            ['TODOS']+sorted(df[f'mechanism_{window}'].dropna().astype(str).unique().tolist()),
            format_func=lambda m: 'Todos los mecanismos' if m=='TODOS' else LABELS.get(m,m),
            key='hll292_mechanism')
        if mechanism!='TODOS':sel=sel[sel[f'mechanism_{window}'].eq(mechanism)]
        st.caption('Mecanismo PRE descriptivo: no utiliza desenlace POST. '
                   'Contacto V2.6 independiente ≠ cualquier vela que cruza la zona.')
        with st.expander('Comparación descriptiva V2.9.2 por mecanismo',expanded=False):
            summary_path=root/V292/'mechanism_summary.csv'
            if blind:
                pre_summary=(df.groupby(f'mechanism_{window}',dropna=False)
                      .agg(N_PRE=('attempt_id','size'),simbolos=('symbol','nunique'),
                           niveles=('level_id','nunique')).reset_index())
                st.dataframe(pre_summary,hide_index=True,use_container_width=True)
                st.caption('Auditoría ciega: solo cantidades PRE. No se muestran resultados POST.')
            elif summary_path.is_file():
                grouped=pd.read_csv(summary_path)
                scope=st.radio('Unidad de comparación',['FIRST_PER_LEVEL','ALL'],horizontal=True,
                               key='hll292_summary_scope')
                st.dataframe(grouped[(grouped.window==window)&(grouped.scope==scope)],
                             use_container_width=True,hide_index=True)
                st.caption('Un flip confirmado es un evento V2.6, no una operación rentable. '
                           'Las tasas son descriptivas y los niveles/símbolos están correlacionados.')
            pair_path=root/V292/'matched_controls_post.csv'
            if not blind and pair_path.is_file():
                pairs=pd.read_csv(pair_path)
                if pattern!='TODAS':pairs=pairs[pairs.pre_class.eq(pattern)]
                if mechanism!='TODOS':pairs=pairs[pairs.case_mechanism.eq(mechanism)]
                st.caption(f'{len(pairs)} pares caso-control: mismo mes y clase PRE; '
                           'comparación retrospectiva, no tasa de éxito ni prueba externa.')
                st.dataframe(pairs.head(250),hide_index=True,use_container_width=True)
    st.caption(f'{len(sel)} intentos originales V2.6 en esta selección; PRE se mantiene fijo. Cambiar el desenlace no reclasifica los intentos.')
    # V2.9.4: Count attempts with independently verified entry OPENs.
    # Same attempt with multiple routes counts ONCE in 'Con entrada'.
    entry_catalogue = None
    if not blind:
        try:
            from dashboard.views.hll_v294_entry_matrix import (
                load_catalogue_for_view, summarize_coverage, LABELS,
                aggregate_selection_matrix, episode_grid, matrix_table,
            )
            entry_catalogue = load_catalogue_for_view(root,folder,df.symbol.unique())
            coverage = summarize_coverage(sel,entry_catalogue)
            st.markdown('**Cobertura de entradas confirmadas — selección actual**')
            cells=st.columns(4)
            for c,(label,value) in zip(cells,[
                ('Intentos PRE',coverage['total']),
                ('Con entrada verificada',coverage['with_entry']),
                ('Sin entrada verificada',coverage['without_entry']),
                ('Señal sin OPEN verificable',coverage['confirmed_unverified']),
            ]):
                c.metric(label,value)
            st.caption(f"{coverage['no_signal']} intentos sin confirmación de ruta. "
                       'Cada intento se cuenta una sola vez, incluso cuando tiene varias entradas. '
                       'Una confirmación sin OPEN verificable NO cuenta como entrada.')
            st.dataframe(pd.DataFrame([{
                'Ruta':LABELS[key],
                'Señales confirmadas':coverage[key]['signal'],
                'Entradas verificadas':coverage[key]['verified'],
                'Señales sin OPEN':coverage[key]['unverified'],
            } for key in ('FLIP_1M','V29_CONTINUATION_30M','V29_RECLAIM_30M')]),
                hide_index=True,use_container_width=True)
            if not (root/V29_EVENTS).is_file():
                st.warning('No se encontró el CSV V2.9: los conteos de continuación/reclaim están incompletos.')
        except (ValueError,KeyError,OSError,TypeError) as exc:
            st.error(f'No se pudieron verificar las entradas de toda la selección: {exc}')
            entry_catalogue=None
    if not blind:
        counts=sel.post_outcome.value_counts()
        slots=st.columns(4)
        for ix,(label,n) in enumerate([('Detectados PRE',len(sel)),
             ('Flip confirmado',int(counts.get('FLIP_CONFIRMADO',0))),
             ('Sin flip en ventana',int(counts.get('SIN_FLIP_EN_VENTANA',0))),
             ('No resuelto',int(counts.get('INDETERMINADO',0)))]):
            slots[ix].metric(label,n)
        st.caption('"Sin flip" significa solo dentro del seguimiento definido en V2.6; no implica que nunca haya confirmado. Censurados y no resueltos se mantienen separados.')
        if state!='TODOS':
            st.caption('Atención: esta selección ya está filtrada por el desenlace. '
                       'Los cuatro contadores NO representan tasas de conversión del universo PRE.')
    if sel.empty:
        st.info('No hay casos bajo esos filtros.')
        return True
    sel=sel.sort_values('breakout_closed_ts',ascending=False).copy()
    sel['label']=(sel.symbol+' · '+pd.to_datetime(sel.breakout_closed_ts,unit='ms',utc=True).dt.strftime('%Y-%m-%d %H:%M UTC')+' · '+sel.attempt_id.astype(str).str[:10])
    ids=sel.attempt_id.tolist()
    idx=st.selectbox('Episodio PRE (ID original, no cambia por su resultado)',range(len(sel)),
        format_func=lambda i:sel.label.iloc[i],key='hll282_episode')
    row=sel.iloc[idx]
    show_post=st.checkbox('Mostrar en el mismo gráfico breakout, retest y desenlace POST',
        value=not blind,disabled=blind,key='hll282_show_post')
    post_bars=st.selectbox('Velas POST de 30m después del breakout',
        [24,48,96,160,240],index=2,disabled=not show_post,key='hll291_post_bars')
    show_flip_pivots=st.checkbox('Agregar pivotes conocidos recién al confirmar el flip',
        value=False,disabled=not show_post,key='hll282_flip_pivots')
    if not blind:
        st.markdown(f'**Este intento:** `{row.v26_status}` · **{STATES[row.post_outcome]}**')
        if row.v26_status=='DIRECT_NO_RETEST_24B':
            st.caption('Ruptura directa: el precio no volvió a tocar la zona durante las 24 barras estudiadas. Puede subir mucho sin cumplir un flip por retest.')
        elif row.v26_status in ('FAILED_BREAKOUT','RETEST_FAILED','ACCEPTED_THEN_FAILED_NO_CONTACT'):
            st.caption('Este intento se cerró como fallido en V2.6. Un ascenso posterior de precio no modifica retrospectivamente este evento.')
        st.caption('Marcas puntuales: triángulo azul = breakout; círculo amarillo = retest; rombo verde = flip; X roja = fallo. Son símbolos, NO mechas.')
    # Overlay exists only after a separately-run V2.9 offline analysis.
    continuation=[]
    continuation_all=[]
    if (root/V29_EVENTS).is_file():
        try:
            v29=load_v29_events(root/V29_EVENTS)
            if not blind:
                st.caption('V2.9 experimental: continuación del breakout (con o sin contacto previo) y recuperación posterior a fallo. Mantiene intactos los flips V2.6.')
                a29,b29=st.columns(2)
                matching=v29[v29.attempt_id.isin(sel.attempt_id)]
                a29.metric('Continuaciones V2.9 (selección)',int(((matching.branch=='BREAKOUT_CONTINUATION') & (matching.status=='CONFIRMED')).sum()))
                b29.metric('Recuperaciones V2.9 (selección)',int(((matching.branch=='RECLAIM_CONTINUATION') & (matching.status=='CONFIRMED')).sum()))
            show_v29=st.checkbox('Mostrar señales de continuación y reclaim V2.9 (experimental)',
                                 value=False,disabled=blind or not show_post,key='hll29_show_continuation')
            # Always keep the event clocks for actual entry markers, even when
            # the separate V2.9 *signal* markers are hidden.
            if show_post and not blind:
                selected_id=str(row.attempt_id).strip()
                continuation_all=v29.loc[v29.attempt_id.eq(selected_id)].to_dict('records')
                if not continuation_all:
                    st.warning(f'V2.9 no encontró eventos para el ID exacto {selected_id}; '
                               f'CSV: {root/V29_EVENTS}. No equivale a no tener señal.')
                if show_v29:
                    continuation=continuation_all
        except (ValueError,KeyError,OSError) as ex:
            st.warning(f'No se pudo cargar V2.9: {ex}')
    p=root/CANDLES/f'{row.symbol}_30m.csv'
    if not p.is_file():
        st.warning(f'Faltan velas originales: {p}')
        return True
    try:
        bars=pd.read_csv(p,usecols=['timestamp','open','high','low','close']).sort_values('timestamp').reset_index(drop=True)
        # Visual guides are strictly POST and independent of the frozen PRE labels.
        # The actual reference OPEN is selected explicitly; never infer an entry
        # from the zone or the signal-close price.
        from dashboard.views.hll_v292_percent_guides import overlay_visual_levels
        from dashboard.views.hll_v293_entry_markers import (
            get_entries, overlay_entry_markers,
        )
        plot_levels = False
        chosen_reference = None
        reference_key = None
        entry_markers = []
        show_entries = False
        include_breakout_reference = False
        all_entry_anchors = []
        references = {}
        if show_post and not blind:
            survival_row = (v292_survival.loc[
                v292_survival.attempt_id.astype(str).eq(str(row.attempt_id))]
                if has_v292 else None)
            all_entry_anchors = get_entries(
                row, bars, survival_row, continuation_all,
                minute_dir=(root / 'reports/horizontal_level_cross_research_v22/extended_1m_core15_v25'),
                include_breakout_reference=True,
            )
            if entry_catalogue is not None:
                from dashboard.views.hll_v294_entry_matrix import reconcile_entries_with_catalogue
                evidence=entry_catalogue.loc[
                    entry_catalogue.attempt_id.eq(str(row.attempt_id))]
                allowed=set(evidence.loc[evidence.status.eq('ENTRY_VERIFIED'),'route'])
                # Never let a weaker historical summary create an unverified
                # entry: the exact-source catalogue must validate it first.
                all_entry_anchors=[e for e in all_entry_anchors
                    if e['key']=='BREAKOUT_30M' or e['key'] in allowed]
                all_entry_anchors=reconcile_entries_with_catalogue(all_entry_anchors,evidence)
            # BREAKOUT_30M is a price reference, NEVER a confirmed trade entry.
            # It cannot enable either TP/SL guides or a first-touch matrix.
            references = {e['key']: dict(label=e['label'], price=e['price'],
                                         entry_ts=e['entry_ts']) for e in all_entry_anchors
                          if e['key'] != 'BREAKOUT_30M'}
            st.markdown('**Entradas de referencia realmente observadas (no órdenes ejecutadas)**')
            show_entries = st.checkbox('Marcar entradas de flip / continuación / reclaim en el gráfico',
                                       value=True, key='hll293_show_entry_markers')
            include_breakout_reference = st.checkbox('Agregar también OPEN posterior al CROSS_UP original (solo comparativa)',
                                       value=False, disabled=not show_entries,
                                       key='hll293_show_breakout_reference')
            st.markdown('**Guías TP/SL porcentuales — escenario LONG (solo visual)**')
            plot_levels = st.checkbox('Mostrar niveles punteados +1% a +5% / −1% a −5%',
                                      value=True, key='hll293_show_percent_levels')
            if plot_levels:
                if references:
                    if pd.notna(row.get('flip_confirmed_closed_ts')) and 'FLIP_1M' not in references:
                        st.caption('Sin OPEN de 1m observado para el reloj del flip; '
                                   'no se infiere entrada ni se usa el cierre de la señal.')
                    default = ('FLIP_1M' if 'FLIP_1M' in references else
                               'V29_CONTINUATION_30M' if 'V29_CONTINUATION_30M' in references else
                               next(iter(references)))
                    reference_key = st.selectbox('Precio base de los porcentajes',
                           list(references), index=list(references).index(default),
                           format_func=lambda k: references[k]['label'],
                           key='hll293_reference')
                    chosen_reference = references[reference_key]
                    max_pct = st.select_slider('Mostrar niveles hasta',
                            options=[1,2,3,4,5], value=5, key='hll293_max_pct')
                    lcol,rcol = st.columns(2)
                    with lcol:
                        show_tp = st.checkbox('Positivos: TP +%',value=True,key='hll293_tp')
                    with rcol:
                        show_sl = st.checkbox('Negativos: SL −%',value=True,key='hll293_sl')
                    if show_tp or show_sl:
                        chosen_reference = dict(chosen_reference, max_pct=max_pct,
                                                show_tp=show_tp, show_sl=show_sl)
                    else:
                        chosen_reference = None
                    st.caption('Son referencias de precio, no órdenes ni ganancias. '
                               'El OPEN posterior se conoce recién al comenzar esa vela. '
                               'Los resultados TP/SL requieren comprobar qué barrera se toca primero en 1m '
                               'y descontar comisiones/slippage.')
                else:
                    st.info('Este intento no tiene ninguna entrada confirmada con OPEN verificable. '
                            'No se dibujan TP/SL. El CROSS_UP es solo una referencia, no una operación.')
        # Independently render ALL confirmed entry paths. A signal's confirmation
        # marker and its next-open reference are different events.
        if show_post and not blind and show_entries:
            entry_markers = [e for e in all_entry_anchors
                             if include_breakout_reference or e['key'] != 'BREAKOUT_30M']
            if not entry_markers:
                confirmed_v29=[ev for ev in continuation_all if
                    str(ev.get('status','')).strip()=='CONFIRMED' and
                    str(ev.get('branch','')).strip() in
                    ('BREAKOUT_CONTINUATION','RECLAIM_CONTINUATION')]
                if confirmed_v29:
                    st.error('Hay V2.9 CONFIRMED para este intento, pero no se pudo dibujar '
                             'su entrada. Revisar integridad de los datos y compatibilidad del inspector.')
                else:
                    st.info('Ninguna ruta tiene todavía un OPEN de entrada verificable para este episodio.')
        fig=make_chart(row,bars,window,show_post,show_flip_pivots,continuation,post_bars)
        if chosen_reference is not None:
            fig=overlay_visual_levels(fig, chosen_reference,
                max_pct=chosen_reference['max_pct'],
                include_tp=chosen_reference['show_tp'],include_sl=chosen_reference['show_sl'])
        if show_entries:
            fig = overlay_entry_markers(fig, entry_markers, selected_reference=reference_key)
        if has_v292:
            local=v292_contacts[(v292_contacts.attempt_id.astype(str)==str(row.attempt_id)) &
                               (v292_contacts.window==window)]
            overlay_physical_clusters(fig,row,bars,window)
            overlay_pre_contacts(fig,local,int(row.pre_closed_ts))
        st.plotly_chart(fig,use_container_width=True,key='hll282_chart')
        if show_entries:
            st.caption('Las entradas son escenarios LONG medidos en el OPEN observado posterior a una confirmación: '
                       'flip V2.6 (+1m), continuación/reclaim V2.9 (siguiente 30m). '
                       'La referencia del breakout sin confirmación se presenta aparte. '
                       'No son órdenes ejecutadas ni resultados de TP/SL.')
            if entry_markers:
                st.dataframe(pd.DataFrame([{
                    'Ruta': e['short'],
                    'Confirmada UTC': _date(e['known_ts']).strftime('%Y-%m-%d %H:%M'),
                    'OPEN referencia UTC': _date(e['entry_ts']).strftime('%Y-%m-%d %H:%M'),
                    'Precio de entrada observado': e['price'],
                    'Precio base seleccionado para guías': e['key']==reference_key,
                } for e in entry_markers]), use_container_width=True, hide_index=True)
        # HLL V2.9.5: strictly verified entries, per-route net exploratory grid.
        # CROSS_UP alone stays a reference; it never activates a TP/SL grid.
        if show_post and not blind:
            st.markdown('**Costes de investigación LONG (estimaciones, no tarifas verificadas)**')
            fee_col, slip_col = st.columns(2)
            with fee_col:
                fee_pct = st.number_input('Comisión total ida y vuelta (%)',
                    min_value=0.0,max_value=1.0,value=0.10,step=0.01,
                    key='hll295_fee_roundtrip')
            with slip_col:
                slippage_pct = st.number_input('Deslizamiento total ida y vuelta (%)',
                    min_value=0.0,max_value=1.0,value=0.0,step=0.01,
                    key='hll295_slippage_roundtrip')
            st.caption('Las tasas son hipótesis configurables en puntos porcentuales sobre la entrada. '
                'No incluyen funding ni ejecución real de Binance. Las barreras se consideran '
                'ejecutadas al precio teórico: un gap o deslizamiento real puede empeorar el resultado.')
        if show_post and not blind and references:
            from dashboard.views.hll_v294_entry_matrix import read_minute_window, matrix_table
            from dashboard.views.hll_v295_net_matrix import first_touch_net
            st.markdown('#### Matriz TP/SL +1%…+5% / −1%…−5% · primera llegada en 1m')
            route_for_matrix=st.selectbox('Ruta para evaluar',list(references),
                format_func=lambda key:references[key]['label'],
                key='hll294_matrix_route')
            hmin=st.selectbox('Horizonte después de la entrada',[120,720,2880],
                format_func=lambda h:f'{h//60} horas',index=1,key='hll294_matrix_horizon')
            ref=references[route_for_matrix]
            minpath=root/'reports/horizontal_level_cross_research_v22/extended_1m_core15_v25'/f'{row.symbol}_1m.csv'
            window_1m,state_1m=read_minute_window(minpath,int(ref['entry_ts']),hmin)
            if not window_1m.empty:
                actual_open=float(window_1m.open.iloc[0])
                if abs(actual_open-float(ref['price']))>max(1e-10,abs(actual_open)*1e-7):
                    raise ValueError('Diferencia entre OPEN verificado y vela inicial 1m')
            matrix_results=first_touch_net(window_1m,float(ref['price']),state_1m,
                int(ref['entry_ts']),float(fee_pct),float(slippage_pct))
            grid=matrix_table(matrix_results)
            grid.index=[f'SL −{i}%' for i in grid.index]
            grid.columns=[f'TP +{i}%' for i in grid.columns]
            st.dataframe(grid,use_container_width=True)
            mcounts=matrix_results.result.value_counts().to_dict()
            st.caption(f'Cobertura: {state_1m} · TP primero: {mcounts.get("TP_FIRST",0)} · '
                f'SL primero: {mcounts.get("SL_FIRST",0)} · '
                f'Ambiguos: {mcounts.get("AMBIGUOUS_1M",0)} · '
                f'TIMEOUT: {mcounts.get("TIMEOUT",0)}. '
                'Primer toque expresado por minuto UTC; el segundo exacto dentro de esa vela se desconoce.')
            with st.expander('Detalle de primeras llegadas, minutos y retornos netos estimados'):
                st.dataframe(matrix_results,hide_index=True,use_container_width=True)
                st.download_button('Exportar las 25 combinaciones de este episodio',
                    matrix_results.to_csv(index=False).encode('utf-8'),
                    file_name='hll_v295_episodio_matrix.csv',mime='text/csv',
                    key='hll295_episode_download')
        elif show_post and not blind:
            st.caption('Matriz TP/SL no disponible: no existe entrada confirmada con OPEN '
                       'verificable en este episodio.')
        # Aggregate comparisons. One route is one distinct research clock; a
        # multi-route attempt is not an independent second market observation.
        if show_post and not blind and entry_catalogue is not None:
            with st.expander('Matriz estadística sobre la selección actual (solo entradas verificadas)'):
                st.caption('V2.9.5: expectativa neta estimada con 25 configuraciones por ruta. '
                    'Se evalúa la misma cohorte para cada ruta, pero una misma estructura puede '
                    'confirmar más de una ruta. Nunca se suman esos registros como trades independientes.')
                from dashboard.views.hll_v295_net_matrix import (
                    aggregate_net_matrix, first_attempt_ids,
                )
                compare_routes=st.selectbox('Rutas para el análisis',
                    ['TODAS','FLIP_1M','V29_CONTINUATION_30M','V29_RECLAIM_30M'],
                    format_func=lambda k:('Comparar las tres rutas' if k=='TODAS' else LABELS[k]),
                    key='hll295_route')
                agg_h=st.selectbox('Horizonte agregado',[120,720,2880],
                    format_func=lambda h:f'{h//60} horas',index=1,key='hll295_aggregate_horizon')
                unit=st.radio('Unidad de comparación',
                    ['FIRST_PER_LEVEL','ALL'],
                    format_func=lambda k:('Primer intento original por símbolo y nivel' if k=='FIRST_PER_LEVEL'
                                           else 'Todos los intentos; hay repetición de niveles'),
                    key='hll295_unit',horizontal=True)
                selected_ids=set(sel.attempt_id.astype(str))
                if unit=='FIRST_PER_LEVEL':
                    selected_ids &= first_attempt_ids(df)
                chosen=entry_catalogue[
                    entry_catalogue.attempt_id.isin(selected_ids) &
                    entry_catalogue.status.eq('ENTRY_VERIFIED') &
                    (entry_catalogue.route.isin(['FLIP_1M','V29_CONTINUATION_30M','V29_RECLAIM_30M'])
                     if compare_routes=='TODAS' else entry_catalogue.route.eq(compare_routes))
                ]
                counts_by_route=chosen.route.value_counts()
                st.caption(f'{len(selected_ids)} intentos PRE seleccionados bajo la unidad elegida; '
                    f'entradas verificadas por ruta: '
                    f'flip {int(counts_by_route.get("FLIP_1M",0))}, '
                    f'continuación {int(counts_by_route.get("V29_CONTINUATION_30M",0))}, '
                    f'reclaim {int(counts_by_route.get("V29_RECLAIM_30M",0))}. '
                    'Sin señal ni OPEN quedan fuera de la simulación, no del recuento anterior.')
                root_m=root/'reports/horizontal_level_cross_research_v22/extended_1m_core15_v25'
                # Time/price + file fingerprints prevent stale cached results in session.
                source_signature=tuple(sorted((str(f),f.stat().st_size,f.stat().st_mtime_ns)
                    for f in [root_m/f'{sym}_1m.csv' for sym in chosen.symbol.unique()] if f.is_file()))
                signature=(tuple(sorted((str(e.attempt_id),str(e.route),int(e.entry_ts),float(e.entry_price))
                    for e in chosen.itertuples(index=False))),compare_routes,unit,agg_h,
                    float(fee_pct),float(slippage_pct),source_signature)
                if st.button('Calcular matriz neta de los intentos seleccionados',
                             key='hll295_calc_selection'):
                    with st.spinner('Calculando primeras llegadas sobre históricos 1m...'):
                        result=aggregate_net_matrix(chosen,root_m,agg_h,
                            commission_roundtrip_pct=float(fee_pct),
                            slippage_roundtrip_pct=float(slippage_pct))
                    st.session_state['hll295_last_matrix']={
                        'signature':signature,'table':result}
                store=st.session_state.get('hll295_last_matrix')
                if store and store['signature']==signature:
                    summary=store['table']
                    if summary.empty:
                        st.info('No hay entradas verificadas en esta selección/ruta.')
                    else:
                        st.caption('Media neta = promedio de TP, SL y TIMEOUT con resultado observable; '
                            'excluye casos ambiguos y censurados. TIMEOUT usa cierre real al final del horizonte. '
                            'No es rendimiento de una cartera: no considera simultaneidad ni capacidad de ejecución.')
                        for rt in summary.route.unique():
                            part=summary.loc[summary.route.eq(rt)]
                            st.markdown(f'**{LABELS[rt]} — {int(part.entries.iloc[0])} entradas**')
                            net_grid=part.pivot(index='sl_pct',columns='tp_pct',values='net_mean_pct')
                            net_grid.index=[f'SL −{i}%' for i in net_grid.index]
                            net_grid.columns=[f'TP +{i}%' for i in net_grid.columns]
                            st.dataframe(net_grid,use_container_width=True)
                        st.markdown('**Tabla completa: cobertura, primera llegada, tiempos y retorno neto**')
                        st.dataframe(summary,hide_index=True,use_container_width=True)
                        st.download_button('Descargar matriz V2.9.5 (CSV)',
                            summary.to_csv(index=False).encode('utf-8'),
                            file_name='hll_v295_matriz_neta.csv',mime='text/csv',
                            key='hll295_download_matrix')
                        # Optional V2.9.4 descriptive comparison, no cost adjustment.
                        if st.checkbox('Mostrar también la matriz bruta V2.9.4 (TP-first, no rentabilidad)',
                                       value=False,key='hll295_legacy_compare'):
                            from dashboard.views.hll_v294_entry_matrix import aggregate_selection_matrix
                            legacy=aggregate_selection_matrix(chosen,root_m,agg_h)
                            st.dataframe(legacy,hide_index=True,use_container_width=True)
                        st.warning('Resultados exploratorios, no una optimización validada. '
                            'No seleccionar el mejor TP/SL de la misma muestra sin un test futuro independiente; '
                            'si la cobertura difiere entre celdas o rutas, las medias no son directamente comparables.')
        # Offline V2.9.6: show individual scenario rows only if its frozen run exists.
        if show_post and not blind:
            from dashboard.views.hll_v296_audit_view import render_v296_audit
            render_v296_audit(root,sel)
        if show_post:
            seen=int(((bars.timestamp>=int(row.breakout_closed_ts)) &
                     (bars.timestamp < int(row.breakout_closed_ts)+int(post_bars)*TF)).sum())
            if seen<post_bars:
                st.warning(f'Histórico POST incompleto: hay {seen} de {post_bars} velas pedidas. No se inventan velas faltantes.')
    except (OSError,ValueError,KeyError,TypeError) as ex:
        st.error(f'No se pudo mostrar el gráfico PRE → POST: {ex}')
        return True
    if show_post:
        if continuation:
            st.dataframe(pd.DataFrame([{'Ruta V2.9':e['branch'],
                'Estado':e['status'],
                'Contacto previo':e.get('contact_before_confirmation',False),
                'Conocido UTC':str(_date(e['confirmed_ts'])) if _clock(e.get('confirmed_ts')) else 'No confirmó',
                'Señal al cierre':e.get('signal_close')} for e in continuation]),
                hide_index=True,use_container_width=True)
        points=episode_markers(row,bars,True)
        if points:
            st.dataframe(pd.DataFrame([{'Evento':p['label'],
                'Conocido UTC':_date(p['time']).strftime('%Y-%m-%d %H:%M'),
                'Cierre 30m':p['close']} for p in points]),
                use_container_width=True,hide_index=True)
    if has_v292:
        st.markdown('**Diagnóstico PRE V2.9.2 — contactos e integridad**')
        st.caption(f"Mecanismo: **{LABELS.get(row[f'mechanism_{window}'],row[f'mechanism_{window}'])}** · "
            f"Tests independientes V2.6: **{int(row[f'touch_v26_independent_{window}'])}** · "
            f"Grupos de velas que solapan la zona: **{int(row[f'physical_overlap_clusters_{window}'])}**. "
            'Los tests se numeran en el gráfico; sus resoluciones futuras se ocultan al corte PRE.')
        cols=['confirmed_pivots','median_pivot_gap_bars','pivot_slope_atr_per_bar',
              'progress_low_atr','close_efficiency','early_drawdown_atr',
              'late_recovery_atr','pullback_depth_ratio']
        st.dataframe(pd.DataFrame([{'Factor PRE':c,'Valor':row.get(f'{c}_{window}')} for c in cols]),
                     use_container_width=True,hide_index=True)
        if blind:
            st.caption('Revisión geométrica ciega: registrar calidad SIN conocer el resultado posterior.')
            if 'hll292_reviews' not in st.session_state:
                st.session_state['hll292_reviews']={}
            key=f'{row.attempt_id}:{window}'
            grades=['PENDIENTE','CORRECTA','DUDOSA','INCORRECTA']
            current=st.session_state['hll292_reviews'].get(key,{}).get('calidad','PENDIENTE')
            grade=st.radio('Clasificación visual del episodio PRE',grades,
                          index=grades.index(current) if current in grades else 0,
                          horizontal=True,key='hll292_grade_'+str(row.attempt_id)+str(window))
            notes=st.text_input('Observación PRE (sin ver desenlace)',
                          value=st.session_state['hll292_reviews'].get(key,{}).get('nota',''),
                          key='hll292_note_'+str(row.attempt_id)+str(window))
            st.session_state['hll292_reviews'][key]=dict(attempt_id=str(row.attempt_id),
                          ventana_30m=int(window),calidad=grade,nota=notes)
            manual=pd.DataFrame(list(st.session_state['hll292_reviews'].values()))
            st.download_button('Descargar revisiones ciegas (CSV)',
                  manual.to_csv(index=False).encode('utf-8'),
                  file_name='hll292_revisiones_ciegas.csv',mime='text/csv',key='hll292_download')
        elif show_post:
            h=v292_survival[v292_survival.attempt_id.astype(str)==str(row.attempt_id)]
            if not h.empty:
                st.markdown('**Supervivencia después del flip — 1m**')
                st.dataframe(h[['horizon_min','path_state','entry_open','mfe_pct','mae_pct',
                    'close_return_pct','survived_without_close_loss','first_close_loss_ts',
                    'first_reclaim_ts','time_to_max_min']],hide_index=True,use_container_width=True)
                st.caption('Entrada de referencia: OPEN de 1m posterior a un minuto tras el cierre de FLIP; '
                    'MFE/MAE informativos, no PnL. Si falta cualquier minuto del horizonte, las métricas quedan vacías.')
    old_label=PATTERNS.get(row[f'pre_pattern_{window}'],'Sin clasificación')
    current_label=PATTERNS.get(row[class_field],'Sin clasificación')
    st.info(f'Original PRE V2.8.2: **{old_label}**. '
            f'Clasificación mostrada: **{current_label}**. '
            'Ambas se fijan antes del breakout; el desenlace POST nunca reclasifica.')
    if has_v291 and bool(row.get(f'trendline{window}_broken',False)):
        when=_clock(row.get(f'trendline{window}_broken_closed_ts'))
        if when is not None:
            st.warning(f'Directriz alcista comprometida antes del breakout: '
                       f'ruptura conocida desde {_date(when).strftime("%Y-%m-%d %H:%M UTC")}. '
                       'Dos cierres bajo la línea, con margen de 0,35 ATR. Revisar visualmente el trazo.')
    if show_post:
        st.markdown(f'**Desenlace V2.6:** {STATES[row.post_outcome]} · `{row.v26_status}`')
        if row.post_outcome=='FLIP_CONFIRMADO':
            st.success(f"Confirmación conocida: **{_date(row.flip_confirmed_closed_ts).strftime('%Y-%m-%d %H:%M UTC')}** "
                       f"· Geometría al FLIP: {PATTERNS.get(row[f'flip_pattern_{window}'],'Sin contexto')}.")
            if _clock(row.failed_closed_ts) is not None:
                st.warning('Atención: el nivel se perdió posteriormente; esto no elimina la confirmación que ya ocurrió.')
        elif row.post_outcome=='INDETERMINADO':
            st.warning('Seguimiento censurado/no resuelto. No debe computarse como fracaso.')
        elif row.post_outcome=='SIN_FLIP_EN_VENTANA':
            st.caption('No se confirmó un flip bajo las reglas y horizonte de V2.6.')
    st.caption('Las marcas verdes/rojas son desenlaces posteriores; NO se conocen al corte PRE y no deben usarse para construir las etiquetas originales.')
    return True
