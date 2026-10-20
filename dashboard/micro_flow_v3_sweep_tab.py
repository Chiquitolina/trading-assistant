"""Read-only visual 1m/5m swing sweep discovery. Does not control LIVE or PM2."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from dashboard.micro_flow_v3_confluence_tab import render_confluence_study
from dashboard.services.micro_flow_v3_multi_session import load_replay_sessions
from research.micro_flow_v3.swing_sweep_discovery import (
    discover, save_immutable_run,
)
from research.micro_flow_v3.sweep_entry_research import (
    analyze_sweep_entries, save_entry_research_run,
)
from research.micro_flow_v3.aggression_exhaustion_discovery import discover_exhaustion
from research.micro_flow_v3.exhaustion_entry_research import (
    evaluate_exhaustion_entries, save_exhaustion_entry_run,
)


def _fmt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%d/%m %H:%M:%S UTC')


def _table(events, reveal):
    out = []
    for e in events:
        row = {
            'Evento':e['event_id'], 'UTC':_fmt(e['sweep']['available_ms']),
            'Swing':e['swing_side'], 'Sesgo hipotético':e['hypothesis_direction'],
            'Nivel':e['level'], 'Vol. relativo':e['aggressive_volume_relative'],
            'Imbalance':e['aggressive_imbalance'],
            'Penetración inicial %':e['initial_sweep_penetration_pct'],
        }
        if reveal:
            row.update({'Penetración máx. posterior %':e['max_penetration_before_reclaim_pct'],
                        'Estado':e['status'],
                        'Recuperación UTC':_fmt(e['reclaim']['available_ms']) if e['reclaim'] else None,
                        'Retest UTC':_fmt(e['retest']['available_ms']) if e['retest'] else None,
                        'Retorno dir. 180s %':e.get('outcome_180s_return_directional_pct')})
        out.append(row)
    return pd.DataFrame(out)


def _inspect_chart(session, result, event, before_minutes, after_minutes, reveal, entry_cases=None):
    sweep_ms = event['sweep']['available_ms']
    start = sweep_ms - before_minutes * 60_000
    finish = sweep_ms + after_minutes * 60_000 if reveal else sweep_ms
    seg = event['segment_id']
    candles = [c for c in result['candles'] if c['segment_id'] == seg and
               start <= c['timestamp'] <= finish and
               c['available_ms'] <= finish]
    buckets = [b for b in session['buckets'] if b['segment_id'] == seg and
               start <= int(b['second_start_ms']) <= finish and
               int(b['available_wall_ns']) <= finish * 1_000_000]
    if not buckets:
        return None
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=.035,
                        row_heights=[.57,.22,.21],
                        subplot_titles=(f"{session['summary']['symbol']} · velas observadas {event['timeframe_minutes']}m",
                                        'Delta agresivo · ETH · Replay 1s',
                                        'CVD del segmento · ETH'))
    if candles:
        d = pd.to_datetime([c['timestamp'] for c in candles], unit='ms', utc=True)
        fig.add_trace(go.Candlestick(
            x=d, open=[c['open'] for c in candles], high=[c['high'] for c in candles],
            low=[c['low'] for c in candles], close=[c['close'] for c in candles],
            increasing_line_color='#16a34a', decreasing_line_color='#dc2626',
            name='OHLC observado'),row=1,col=1)
    fig.add_hline(y=event['level'],line=dict(color='#f59e0b',dash='dash',width=1.3),row=1,col=1)
    swings = [s for s in result['swings'] if s['segment_id']==seg and
              s['confirmed_available_ms'] <= (finish if reveal else sweep_ms) and
              start <= s['pivot_ms'] <= finish]
    if swings:
        fig.add_trace(go.Scatter(x=pd.to_datetime([s['pivot_ms'] for s in swings],unit='ms',utc=True),
                     y=[s['level'] for s in swings],
                     mode='markers', name='Swings (solo confirmados)',
                     marker=dict(symbol=['triangle-down' if s['side']=='HIGH' else 'triangle-up'
                                         for s in swings],size=9,color='#a78bfa'),
                     customdata=[[s['confirmed_available_ms'],s['side']] for s in swings],
                     hovertemplate='Pivote %{x}<br>Precio %{y}<br>Confirmado (ms) %{customdata[0]}<extra></extra>'),
                     row=1,col=1)
    bdt=pd.to_datetime([b['second_start_ms'] for b in buckets],unit='ms',utc=True)
    fig.add_trace(go.Scatter(x=bdt, y=[float(b['close']) for b in buckets],
                             line=dict(color='#38bdf8',width=1.1),
                             name='Precio 1s observado'),row=1,col=1)
    deltas=[float(b['delta_qty']) for b in buckets]
    fig.add_trace(go.Bar(x=bdt,y=deltas,name='Delta BUY−SELL',
               marker_color=['#16a34a' if v>=0 else '#dc2626' for v in deltas]),row=2,col=1)
    fig.add_trace(go.Scatter(x=bdt,y=[float(b['cvd_qty']) for b in buckets],
               mode='lines',line=dict(color='#f472b6',width=1.3),name='CVD'),row=3,col=1)
    timeline=[('swing_confirm_available_ms','Swing conocido','#a78bfa'),
              ('sweep','Barrido publicado','#f59e0b')]
    if reveal:
        timeline += [('reclaim','Recuperación','#22c55e'),
                     ('departure','Alejamiento','#0ea5e9'),('retest','Retest','#e879f9')]
    for key,label,color in timeline:
        stage = event.get(key)
        stamp = stage if isinstance(stage,int) else (stage.get('available_ms') if stage else None)
        if stamp is None or stamp < start or stamp > finish:
            continue
        x=pd.to_datetime(stamp,unit='ms',utc=True)
        fig.add_vline(x=x,line_color=color,line_dash='dot',line_width=1.3,row=1,col=1)
        fig.add_annotation(x=x,y=1.025,xref='x',yref='paper',text=label,
                  font=dict(color=color,size=10),showarrow=False,
                  bgcolor='rgba(0,0,0,.6)')
    if reveal and entry_cases:
        for case, color in zip(entry_cases, ('#2dd4bf', '#e879f9')):
            ref = case.get('entry_reference')
            if not ref:
                continue
            stamp = ref['entry_reference_second_start_ms']
            if start <= stamp <= finish:
                x = pd.to_datetime(stamp,unit='ms',utc=True)
                fig.add_vline(x=x,line_color=color,line_dash='dash',line_width=1,row=1,col=1)
                fig.add_annotation(x=x,y=.96,xref='x',yref='paper',
                   text=f"Referencia {case['variant']} · NO fill",showarrow=False,
                   font=dict(color=color,size=9),bgcolor='rgba(0,0,0,.6)')
    fig.update_layout(height=790,hovermode='x unified',showlegend=True,
                      legend=dict(orientation='h',y=-.1),
                      margin=dict(l=10,r=10,t=85,b=30))
    fig.update_xaxes(rangeslider_visible=False)
    return fig



def _exhaustion_frame(events, reveal):
    """Avoid revealing later classification/outcomes until explicitly requested."""
    out=[]
    for e in events:
        snap=e['impulse_features']
        row={
            'ID':e['event_id'], 'Impulso UTC':_fmt(e['impulse']['available_ms']),
            'Presión':e['pressure_side'],'Hipótesis':e['hypothesis_direction'],
            'Volumen relativo':snap['relative_volume'],
            'Imbalance':snap['imbalance'], 'Delta ETH':snap['delta_qty'],
            'Movimiento presión %':snap['pressure_signed_displacement_pct'],
            'Segundos observados':snap['observed_seconds'],
        }
        if reveal:
            row.update({
                'Debilitamiento':_fmt(e['weakening']['available_ms']) if e['weakening'] else None,
                'Fracaso':_fmt(e['failure']['available_ms']) if e['failure'] else None,
                'Estado observación':e['followup_state'],
                'Referencia de entrada':e['entry_state'],
                'Retorno 180s %':e['outcomes'].get('180s',{}).get('return_directional_pct'),
                'MFE 180s %':e['outcomes'].get('180s',{}).get('mfe_directional_pct'),
                'MAE 180s %':e['outcomes'].get('180s',{}).get('mae_directional_pct'),
            })
        out.append(row)
    return pd.DataFrame(out)


def _exhaustion_chart(session, ev, *, before, after, reveal, entry_cases=None):
    seg=ev['segment_id']
    ts=ev['impulse']['available_ms']
    start=ts-before*60_000
    end=ts+after*60_000 if reveal else ts
    rows=[r for r in session['buckets']
          if r['segment_id']==seg and start<=r['second_start_ms']<=end and
          r['available_wall_ns']<=end*1_000_000]
    if not rows:
        return None
    x=pd.to_datetime([r['second_start_ms'] for r in rows],unit='ms',utc=True)
    fig=make_subplots(rows=3,cols=1,shared_xaxes=True,vertical_spacing=.045,
                      row_heights=[.55,.23,.22],
                      subplot_titles=('Precio observado 1s','Delta agresivo ETH','CVD observado ETH'))
    fig.add_trace(go.Scatter(x=x,y=[float(r['close']) for r in rows],
                   mode='lines',name='Precio 1s',line=dict(color='#38bdf8')),row=1,col=1)
    fig.add_trace(go.Bar(x=x,y=[float(r['delta_qty']) for r in rows],
                   name='Delta ETH',marker_color=['#16a34a' if float(r['delta_qty'])>=0 else '#dc2626' for r in rows]),row=2,col=1)
    fig.add_trace(go.Scatter(x=x,y=[float(r['cvd_qty']) for r in rows],
                   mode='lines',name='CVD',line=dict(color='#f472b6')),row=3,col=1)
    timeline=[('impulse','Impulso','#f59e0b')]
    if reveal:
        timeline += [('weakening','Menor eficacia','#a78bfa'),
                     ('failure','Fracaso confirmado','#22c55e')]
    for field,label,color in timeline:
        stage=ev.get(field)
        if stage and start<=stage['available_ms']<=end:
            t=pd.to_datetime(stage['available_ms'],unit='ms',utc=True)
            fig.add_vline(x=t,line_color=color,line_dash='dot',row=1,col=1)
            fig.add_annotation(x=t,y=1.015,yref='paper',text=label,showarrow=False,
                               font=dict(size=10,color=color),bgcolor='rgba(0,0,0,.7)')
    # Only show post-impulse structure and signal references when future is revealed.
    if reveal and entry_cases:
        for case in entry_cases:
            if case['variant'] == 'FAILURE_SWING_BREAK' and case.get('preknown_swing'):
                level=case['preknown_swing']['level']
                fig.add_hline(y=level,line=dict(color='#f59e0b',dash='dash',width=1),row=1,col=1)
            stage=case.get('signal')
            if stage and start<=stage['available_ms']<=end:
                color='#06b6d4' if case['variant']=='FAILURE' else '#e879f9'
                t=pd.to_datetime(stage['available_ms'],unit='ms',utc=True)
                fig.add_vline(x=t,line_color=color,line_dash='dot',row=1,col=1)
                fig.add_annotation(x=t,y=.95 if case['variant']=='FAILURE' else .89,
                    yref='paper',showarrow=False,bgcolor='rgba(0,0,0,.7)',
                    text='Señal '+case['variant'],font=dict(color=color,size=9))
    fig.update_layout(height=680,hovermode='x unified',margin=dict(l=10,r=10,t=75,b=20),
                      legend=dict(orientation='h',y=-.12))
    return fig


def _is_closed(path):
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True, timeout=2) as db:
        last=db.execute('SELECT kind,reason FROM tape ORDER BY seq DESC LIMIT 1').fetchone()
        return last == ('MARKER','PROCESS_STOP')


def render_swing_sweep_discovery(root, tapes):
    root=Path(root)
    st.subheader('🧭 Swing Sweep Discovery · 1m / 5m')
    st.caption('Research-only: swings confirmados + barridos + presión AggTrades + '
               'reclaim + alejamiento + retest. No hay entradas ejecutables ni órdenes.')
    if not tapes:
        st.info('No hay tapes V3 para estudiar.'); return
    closed=[]
    for tape in tapes:
        try:
            if _is_closed(tape):
                closed.append(tape)
        except Exception:
            # Zero-byte/truncated/archive invalid explicitly unavailable.
            pass
    if not closed:
        st.warning('No hay tapes completos y cerrados con Replay causal.'); return
    with st.form('mfv3_sweep_form'):
        selected = st.selectbox('Tape cerrado de ETHUSDT', closed,
                               format_func=lambda p:p.name, key='mfv3_sweep_tape')
        a,b,c,d=st.columns(4)
        with a:
            tf=st.selectbox('Estructura', [1,5],format_func=lambda v:f'{v}m',index=0)
        with b:
            swing=st.selectbox('Swing L×R',[2,3,5],index=1,format_func=lambda n:f'{n}x{n}')
        with c:
            age=st.number_input('Edad máxima swing (min)',5,120,30,5)
        with d:
            reclaim=st.number_input('Ventana reclaim (s)',30,900,300,30)
        a,b,c=st.columns(3)
        with a:
            departure=st.number_input('Alejamiento mínimo %',.0,1.0,.03,.01,format='%.3f')
        with b:
            tolerance=st.number_input('Tolerancia retest %',.0,1.0,.02,.01,format='%.3f')
        with c:
            retest=st.number_input('Ventana retest (s)',60,1800,600,60)
        submitted=st.form_submit_button('Descubrir barridos en Replay completo',type='primary')
    settings=(str(selected),int(tf),int(swing),int(age),int(reclaim),float(departure),
              float(tolerance),int(retest))
    if submitted:
        st.session_state.pop('mfv3_sweep_discovery',None)
        try:
            with st.spinner('Verificando SHA256 del Replay y detectando estructuras causales...'):
                session=load_replay_sessions(root,[selected],max_sessions=1)[0]
                result=discover(session['buckets'],timeframe_minutes=tf,
                     swing_bars=swing,max_swing_age_minutes=age,
                     reclaim_seconds=reclaim,departure_pct=departure,
                     retest_tolerance_pct=tolerance,retest_seconds=retest)
                entries=analyze_sweep_entries(session['buckets'],result)
                exhaustion=discover_exhaustion(session['buckets'])
                exhaustion_entries=evaluate_exhaustion_entries(
                    session['buckets'], exhaustion, result['swings'])
                st.session_state['mfv3_sweep_discovery']=(
                    settings,session,result,entries,exhaustion,exhaustion_entries)
        except Exception as exc:
            st.error(f'No fue posible completar el estudio: {exc}'); return
    data=st.session_state.get('mfv3_sweep_discovery')
    if not data:
        st.info('Seleccioná un tape cerrado y ejecutá el detector. Los 1s originales no se modifican.')
        return
    if len(data) != 6:
        st.session_state.pop('mfv3_sweep_discovery',None)
        st.info('La sesión del inspector es anterior a Fase 3. Ejecutá nuevamente Descubrir barridos.')
        return
    saved_settings,session,result,entry_study,exhaustion,exhaustion_entries=data
    if saved_settings != settings:
        st.warning('Cambiaron los parámetros o la sesión. Ejecutá nuevamente el análisis.'); return
    s=result['summary']
    columns=st.columns(5)
    for col,label,val in zip(columns,('Velas completas','Swings','Barridos','Recuperaciones','Retests'),
                                      (s['candles'],s['swings'],s['sweeps'],s['reclaimed'],s['retested'])):
        col.metric(label,f'{val:,}')
    st.caption('Cada swing puede producir su primer barrido: los eventos pueden solaparse, NO son trades independientes. '
               'Todos los barridos se conservan aunque NO recuperen el nivel o NO retesteen. '
               'Datos 1s observados, sin rellenar segundos/minutos vacíos; '
               'swings disponibles después del cierre real de la vela confirmadora.')
    if not result['events']:
        st.warning('No hay barridos con esta configuración. La confluencia VE + AE puede estudiarse igual.')
        render_confluence_study(root,session,exhaustion)
        return
    reveal=st.checkbox('Revelar etapas posteriores y outcomes (modo exploratorio)',
                       value=False,key='mfv3_sweep_future')
    table=_table(result['events'],reveal)
    st.dataframe(table,use_container_width=True,hide_index=True)
    st.download_button('Exportar tabla de barridos (CSV)',table.to_csv(index=False),
                       file_name='swing_sweep_discovery.csv',mime='text/csv')
    ids=[e['event_id'] for e in result['events']]
    key=st.selectbox('Seleccionar barrido para inspeccionar',ids,
                      format_func=lambda k: k.split(':SWEEP:')[-1]+' · '+
                        next(e['hypothesis_direction'] for e in result['events'] if e['event_id']==k)+
                        ' · '+k.split(':')[1],key='mfv3_sweep_selected')
    ev=next(e for e in result['events'] if e['event_id']==key)
    before=st.slider('Minutos anteriores',5,90,20,5,key='mfv3_sweep_before')
    after=st.slider('Minutos posteriores (solo si se revelan)',5,30,15,5,key='mfv3_sweep_after')
    # Pure analysis over already-verified, complete Replay. No source writes.
    event_cases = [c for c in entry_study['entries'] if c['event_id']==key]
    event_effort = next(d for d in entry_study['effort_diagnostics'] if d['event_id']==key)
    chart=_inspect_chart(session,result,ev,before,after,reveal,event_cases)
    if chart is not None:
        st.plotly_chart(chart,use_container_width=True)
    st.caption(f"Swing conocido: {_fmt(ev['swing_confirm_available_ms'])} · "
               f"Barrido publicado: {_fmt(ev['sweep']['available_ms'])} · "
               f"Segmento causal: {ev['segment_id']} · "
               'Las líneas de nivel no implican órdenes ejecutadas.')
    if reveal:
        st.json(ev)
        st.warning('Los outcomes posteriores son descriptivos, sin fills, spread ni costos. '
                   'El retest requiere observación posterior al alejamiento.')
    st.divider()
    st.markdown('#### Sweep Entry Research · A/B + esfuerzo agresivo')
    st.caption('A: primer segundo posterior al reclaim. B: primer segundo posterior al retest. '
        'Solo referencias de precio observadas, NO fills. Sin spread, fees ni órdenes. '
        'Cada barrido permanece en ambas cohortes aun cuando la señal no aparezca.')
    totals = entry_study['summary']['variants']
    cols=st.columns(4)
    for col,variant,metric in (
        (cols[0],'RECLAIM','signal_seen'), (cols[1],'RECLAIM','observed_180s'),
        (cols[2],'RETEST','signal_seen'), (cols[3],'RETEST','observed_180s')):
        col.metric(f'{variant}: '+('señales' if metric=='signal_seen' else 'resultados 180s'),
                   totals[variant][metric])
    # Effort diagnostics at sweep-time can be shown with the future HIDDEN.
    st.markdown('**Presión observada en este barrido (sin mirar el futuro)**')
    st.write({k:event_effort.get(k) for k in (
        'high_effort_candidate','delta_qty','signed_price_displacement_pct',
        'signed_impact_pct_per_100eth_delta','recovery_from_window_extreme_fraction',
        'observed_seconds','sweep_relative_volume','sweep_imbalance')})
    st.caption('"High effort" = presión alineada, volumen ≥2x e |imbalance| ≥0,70 '
               '(solo referencia exploratoria). No prueba agotamiento ni absorción.')
    if reveal:
        st.markdown('**Referencias de entrada del barrido seleccionado**')
        selected_rows=[]
        for case in event_cases:
            ref=case['entry_reference'] or {}
            horizon=case['outcomes'].get('180s',{})
            selected_rows.append({
                'Variante':case['variant'], 'Señal':case['signal_state'],
                'Referencia':case['entry_state'],
                'Instante señal UTC':_fmt(case['signal_available_ms']) if case['signal_available_ms'] else None,
                'Precio 1s open (NO fill)':ref.get('entry_reference_price'),
                'Primer segundo observado UTC':_fmt(ref['entry_reference_second_start_ms']) if ref else None,
                'Estado 180s':horizon.get('state'),
                'Retorno bruto 180s %':horizon.get('return_directional_pct'),
                'MFE 180s %':horizon.get('mfe_directional_pct'),
                'MAE 180s %':horizon.get('mae_directional_pct'),
            })
        st.dataframe(pd.DataFrame(selected_rows),use_container_width=True,hide_index=True)
        st.markdown('**Todas las observaciones · sin seleccionar ganadoras**')
        all_rows=[]
        for case in entry_study['entries']:
            p=case.get('entry_reference') or {}
            h=case['outcomes'].get('180s',{})
            all_rows.append({'Evento':case['event_id'],'Variante':case['variant'],
                'Sesgo':case['hypothesis_direction'],'Señal':case['signal_state'],
                'Referencia':case['entry_state'],
                'Precio ref. NO fill':p.get('entry_reference_price'),
                'Estado 180s':h.get('state'),'Retorno 180s %':h.get('return_directional_pct'),
                'MFE 180s %':h.get('mfe_directional_pct'),
                'MAE 180s %':h.get('mae_directional_pct'),
                'Cobertura 180s':h.get('coverage_fraction')})
        frame=pd.DataFrame(all_rows)
        st.dataframe(frame,use_container_width=True,hide_index=True)
        st.download_button('Exportar A/B (todos los casos)',frame.to_csv(index=False),
                           file_name='sweep_entry_research_ab.csv',mime='text/csv')
        effort_frame=pd.DataFrame(entry_study['effort_diagnostics'])
        st.download_button('Exportar presión / posible agotamiento',
                 effort_frame.to_csv(index=False),file_name='sweep_aggression_effort.csv',
                 mime='text/csv')
        st.caption('Los outcomes se censuran ante huecos de observación o fin de sesión. '
          'Las variantes no son operaciones independientes ni un backtest ejecutable. '
          'Que haya rally más adelante no implica un retorno favorable a 180s.')
    else:
        st.info('El futuro sigue oculto. Activá Revelar etapas posteriores para '
                'comparar entradas A/B y los resultados observados.')
    if st.button('Guardar investigación A/B + agotamiento (verificando RAW)',
                 key='mfv3_sweep_entry_save'):
        try:
            with st.spinner('Verificando fuente RAW y SHA256 del Replay...'):
                saved=save_entry_research_run(root,session,result,entry_study,
                                              exhaustion=exhaustion,verify_raw=True)
            st.success(f'Corrida de investigación A/B conservada: {saved}')
        except Exception as exc:
            st.error(f'No se guardó la nueva corrida: {exc}')
    st.divider()
    st.markdown('#### Aggression Exhaustion Discovery · independiente de swings')
    st.caption('Detecta impulsos BUY/SELL por volumen e imbalance observados; después evalúa '
      'si la presión pierde eficacia y si el precio cruza el comienzo del impulso en '
      'sentido contrario. NO prueba absorción ni implica una entrada en el máximo/mínimo.')
    es=exhaustion['summary']
    aa,bb,cc=st.columns(3)
    aa.metric('Impulsos BUY',es['buy_impulses'])
    bb.metric('Impulsos SELL',es['sell_impulses'])
    if reveal:
        cc.metric('Fracaso confirmado después',es['failure_observed'])
        st.caption(f"Debilitamiento observado: {es['weakening_observed']} · "
                   f"Seguimientos censurados: {es['censored_followup']}")
    else:
        cc.metric('Impulsos totales',es['impulses'])
    exhaustion_table=_exhaustion_frame(exhaustion['events'],reveal)
    st.dataframe(exhaustion_table,use_container_width=True,hide_index=True)
    if exhaustion['events']:
        exhaustion_id=st.selectbox('Inspeccionar impulso independiente',
             [e['event_id'] for e in exhaustion['events']],key='mfv3_exhaustion_selected')
        inspected=next(e for e in exhaustion['events'] if e['event_id']==exhaustion_id)
        inspected_cases=[c for c in exhaustion_entries['entries']
                         if c['event_id']==exhaustion_id]
        fig=_exhaustion_chart(session,inspected,before=before,after=after,
                              reveal=reveal,entry_cases=inspected_cases)
        if fig is not None:
            st.plotly_chart(fig,use_container_width=True,key='mfv3_exhaustion_chart')
        if reveal:
            st.json(inspected)
    if reveal:
        st.download_button('Exportar todos los impulsos de agotamiento (CSV)',
            exhaustion_table.to_csv(index=False),file_name='aggression_exhaustion_v1.csv',
            mime='text/csv')
    st.markdown('#### Exhaustion Entry Research · confirmación vs ruptura de swing')
    st.caption('A FAILURE: referencia posterior al fracaso. B FAILURE_SWING_BREAK: '
        'tras el fracaso, cierre posterior más allá de un swing confirmado ANTES '
        'del impulso (timeframe y pivote seleccionados arriba). '
        'Todos los impulsos permanecen en las dos variantes. '
        'Los precios son OPEN 1s retrospectivos: NO son fills ejecutables.')
    cases=exhaustion_entries['entries']
    summary=exhaustion_entries['summary']
    if reveal:
        for variant in ('FAILURE','FAILURE_SWING_BREAK'):
            side_summary=summary['by_variant'][variant]
            n_signals=sum(row['signals'] for row in side_summary.values())
            n_refs=sum(row['entry_references'] for row in side_summary.values())
            st.write(f'**{variant}** · señales: {n_signals} · referencias observadas: {n_refs}')
        all_rows=[]
        for case in cases:
            ref=case.get('entry_reference') or {}
            known=case.get('preknown_swing') or {}
            row={
                'ID':case['event_id'], 'Variante':case['variant'],
                'Presión':case['pressure_side'],
                'Sesgo':case['hypothesis_direction'],
                'Estado señal':case['signal_state'],
                'Señal UTC':_fmt(case['signal']['available_ms']) if case['signal'] else None,
                'Swing conocido':known.get('swing_id'),
                'Nivel conocido':known.get('level'),
                'Referencia':case['entry_state'],
                'Precio ref. NO fill':ref.get('entry_reference_price'),
                'Ref disponible UTC':_fmt(ref['entry_reference_available_ms']) if ref else None,
            }
            for horizon in exhaustion_entries['parameters']['horizons_seconds']:
                h=case['outcomes'].get(f'{horizon}s',{})
                row[f'Estado {horizon}s']=h.get('state')
                row[f'Bruto {horizon}s %']=h.get('return_directional_pct')
                row[f'Neto ilustrativo {horizon}s %']=h.get('net_after_illustrative_cost_pct')
                row[f'MFE {horizon}s %']=h.get('mfe_directional_pct')
                row[f'MAE {horizon}s %']=h.get('mae_directional_pct')
                row[f'Cobertura {horizon}s']=h.get('coverage_fraction')
            all_rows.append(row)
        study_frame=pd.DataFrame(all_rows)
        st.dataframe(study_frame,use_container_width=True,hide_index=True)
        st.download_button('Exportar todas las entradas de agotamiento A/B (CSV)',
            study_frame.to_csv(index=False),
            file_name='exhaustion_entry_research_v1.csv',mime='text/csv',
            key='mfv3_exhaustion_entries_csv')
        if exhaustion['events']:
            st.markdown('**Caso seleccionado: ambas entradas y todos los horizontes**')
            st.dataframe(study_frame[study_frame['ID']==exhaustion_id],
                         use_container_width=True,hide_index=True)
        st.markdown('**Resumen por dirección y horizonte (solo outcomes completos)**')
        report=[]
        for variant, sides in summary['by_variant'].items():
            for side, metrics in sides.items():
                for horizon, outcome in metrics['horizons'].items():
                    report.append({'Variante':variant,'Presión':side,'Horizonte':horizon,
                        'Impulsos':metrics['impulses'],'Señales':metrics['signals'],
                        'Referencias':metrics['entry_references'],
                        'Outcomes completos':outcome['observed'],
                        'Sin outcome completo':outcome['censored_or_no_signal'],
                        'Bruto medio %':outcome['mean_gross_pct'],
                        'Neto ilustrativo medio %':outcome['mean_net_illustrative_pct'],
                        'MFE medio %':outcome['mean_mfe_pct'],
                        'MAE medio %':outcome['mean_mae_pct']})
        st.dataframe(pd.DataFrame(report),use_container_width=True,hide_index=True)
        st.write('**Comparación A/B en la misma cohorte de impulsos**',summary['paired'])
        st.warning('Los 12 bps son un costo fijo ILUSTRATIVO, no spread/fees/slippage '
                   'medidos. Las entradas no son fills; señales y ventanas pueden '
                   'solaparse y los grupos A/B no son aleatorios. Esto NO demuestra edge.')
    else:
        st.info('Resultados posteriores ocultos. Activá Revelar etapas para ver '
                'rupturas, entradas A/B, MFE/MAE y retornos.')
    if st.button('Guardar corrida Fase 3 · entradas agotamiento + hashes RAW',
                 key='mfv3_exhaustion_entry_save'):
        try:
            with st.spinner('Verificando SHA256 Replay, digest lógico RAW y guardando...'):
                target=save_exhaustion_entry_run(root,session,exhaustion,
                                               result['swings'],exhaustion_entries,
                                               verify_raw=True)
            st.success(f'Corrida Fase 3 inmutable: {target}')
        except Exception as exc:
            st.error(f'No se guardó ninguna corrida válida: {exc}')
    st.caption('Las fases temporales están separadas. El resultado a 180s se mide solo '
      'después de una posible referencia posterior al fracaso, y se censura sin cobertura. '
      'No hay órdenes ni fills.')
    st.divider()
    st.markdown('#### Persistencia reproducible')
    st.caption('Replay SHA256 verificado al cargar; guardar exige verificar también el '
               'digest lógico del SQLite original y genera un directorio nuevo, '
               'sin reemplazar RAW, Replay ni investigaciones anteriores.')
    if st.button('Guardar corrida nueva + hashes con verificación RAW',key='mfv3_sweep_save'):
        try:
            with st.spinner('Verificando digest lógico del tape original y guardando artefactos...'):
                path=save_immutable_run(root,session,result,verify_raw=True)
            st.success(f'Investigación guardada: {path}')
        except Exception as exc:
            st.error(f'No se guardó: {exc}')

    # Phase 4: optional, separate VE x AE study; no changes to prior detectors.
    render_confluence_study(root,session,exhaustion)
