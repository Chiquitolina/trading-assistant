"""Minimal Horizontal Level Rich Card in the Retest Quality dashboard.

Uses audited Atlas V2 levels + frozen pre-retest 1m candles, not a new
support/resistance detector. PRE, retest candle KNOWN and POST separated.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

import reaction_horizontal_level_card as logic

SETUP_LABELS={
    'DEFENSA_POTENCIAL':'Defensa potencial',
    'RUPTURA_POTENCIAL':'Ruptura potencial',
    'EN_ZONA_SIN_ORIGEN':'En zona · sin origen claro',
    'DESCONOCIDO':'Indeterminado · datos insuficientes',
}
KNOWN_LABELS={
    'RECHAZO_FAVORABLE_1M':'Rechazo favorable de la vela 1m',
    'NIVEL_PERDIDO_EN_CIERRE':'Nivel perdido al cerrar 1m',
    'TOQUE_SIN_DEFENSA_CONFIRMADA':'Contacto sin defensa confirmada',
    'SIN_CONTACTO_1M':'Sin contacto en la vela',
    'CRUCE_INICIAL_1M':'Cruce inicial de la zona (NO ruptura aceptada)',
    'PRUEBA_SIN_QUIEBRE':'Prueba sin cierre de ruptura',
    'SALE_POR_ARRIBA_1M':'Cierra por encima de la zona',
    'SALE_POR_ABAJO_1M':'Cierra por debajo de la zona',
    'DENTRO_O_INDEFINIDO_1M':'Cierra dentro / sin confirmación',
    'SIN_VELA_CERRADA':'Vela 1m no disponible',
    'SIN_HISTORICO':'Histórico incompleto',
}


def _pretty(n,places=5):
    try:
        if pd.notna(n):return f'{float(n):.{places}g}'
    except (TypeError,ValueError):pass
    return '—'


def _explain(side,setup):
    if setup=='DEFENSA_POTENCIAL':
        return ('SHORT: resistencia por encima; posible rechazo bajista.' if side=='SHORT'
                else 'LONG: soporte por debajo; posible rebote alcista.')
    if setup=='RUPTURA_POTENCIAL':
        return ('SHORT: soporte por debajo; posible quiebre bajista. No lo confundimos con defensa.'
                if side=='SHORT' else 'LONG: resistencia por encima; posible ruptura alcista.')
    if setup=='EN_ZONA_SIN_ORIGEN':
        return 'La llegada lleva varias velas dentro de la zona: no se puede afirmar defensa ni ruptura.'
    return 'No hay datos continuos para reconstruir el lado de llegada; no se atribuye dirección.'


def render_horizontal_level_rich_card(source_events,project_root,*,tp_pct=1.0,sl_pct=0.5,
                                      outcome_horizon=180,entry_mode='OBSERVED_OPEN'):
    import streamlit as st
    from dashboard.views.reaction_structural_level_atlas_v2_view import load,_read_structural_figure
    from dashboard.views.reaction_outcome_overlay import get_post_bars,overlay_future,outcome_explanation

    project=Path(project_root).resolve()
    import reaction_structural_level_atlas_v2 as atlas
    from dashboard.views.reaction_horizontal_rich_chart import enhance_pre_candles, diagnostic_text
    out=project/atlas.v1.lineage.ROOT_REL/atlas.OUTPUT
    st.markdown('### 🟩 Horizontal Level Rich Card')
    st.caption('Una zona principal ya confirmada · una REACTION · su desenlace. '
               'La línea verde es un NIVEL, no automáticamente una defensa.')
    if not (out/'summary.json').is_file():
        st.info('Primero generá Structural Level Atlas V2 (--run y --audit).');return

    @st.cache_data(max_entries=2,show_spinner=False)
    def cached(base,meta_mtime,source_mtime):
        zones,ev,summary=load(Path(base))
        annotated=logic.enrich_events(Path(base),ev)
        return zones,annotated,summary

    try:
        context_path=project/logic.ROOT/'reaction_full_context_v1.sqlite3'
        zones,events,summary=cached(str(project),(out/'summary.json').stat().st_mtime_ns,
                                   context_path.stat().st_mtime_ns)
        expected=['event_key','outcome','entry_ts','entry_price','first_touch_minute']
        miss=[c for c in expected if c not in source_events.columns]
        if miss:raise ValueError('Faltan columnas del First-Touch: '+', '.join(miss))
        source=source_events[expected].drop_duplicates('event_key')
        selected=events.merge(source,on='event_key',how='inner',validate='one_to_one')
    except Exception as exc:
        st.error(f'Horizontal Rich Card: no se pudieron validar las fuentes: {exc}');return

    if selected.empty:
        st.info('No hay REACTIONs del Atlas dentro de los filtros superiores.');return

    total=len(selected)
    all_def=int(selected.setup_pre.eq('DEFENSA_POTENCIAL').sum())
    all_break=int(selected.setup_pre.eq('RUPTURA_POTENCIAL').sum())
    all_unknown=total-all_def-all_break
    k=st.columns(4)
    k[0].metric('REACTIONs',total)
    k[1].metric('Defensa potencial',all_def)
    k[2].metric('Ruptura potencial',all_break)
    k[3].metric('Indeterminadas',all_unknown)

    st.caption('PRE = qué lado traía el precio antes de la vela 1m del retest. '
               'KNOWN = cierre de esa vela. POST = replay histórico. '
               f'{entry_mode} · TP +{tp_pct:g}% / SL −{sl_pct:g}% · {outcome_horizon}m.')
    opts=st.columns([1.2,1,1],gap='small')
    setup=opts[0].selectbox('Situación previa',('TODAS','DEFENSA_POTENCIAL','RUPTURA_POTENCIAL',
                       'EN_ZONA_SIN_ORIGEN','DESCONOCIDO'),
                       format_func=lambda v:'Todas' if v=='TODAS' else SETUP_LABELS[v],key='hlrc_setup')
    direction=opts[1].selectbox('Dirección',('TODAS','LONG','SHORT'),key='hlrc_side')
    result=opts[2].selectbox('Desenlace (solo inspector)',
                   ('TODOS','TP_FIRST','SL_FIRST','NO_HIT','SIN_PATH'),key='hlrc_outcome')
    pre_filtered=selected.copy()
    if setup!='TODAS':pre_filtered=pre_filtered[pre_filtered.setup_pre.eq(setup)]
    if direction!='TODAS':pre_filtered=pre_filtered[pre_filtered.side.eq(direction)]

    st.markdown('**Comparación sencilla — sin seleccionar ganadoras para calcular tasas**')
    table=logic.groups_summary(pre_filtered)
    if not table.empty:st.dataframe(table,hide_index=True,use_container_width=True)
    st.caption('TP% = TP / (TP + SL) entre resueltas. NO_HIT, ambiguas y faltantes quedan afuera '
               'del denominador, pero se muestran. Los grupos con pocos casos no demuestran una ventaja.')

    for_inspector=pre_filtered
    if result=='SIN_PATH':
        for_inspector=for_inspector[for_inspector.outcome.isna()]
    elif result!='TODOS':
        for_inspector=for_inspector[for_inspector.outcome.eq(result)]
    if for_inspector.empty:
        st.info('No hay eventos para estos filtros; probá con “TODOS”.');return

    rows=for_inspector.sort_values('retest_ts',ascending=False)
    labels={r.event_key:f'{r.symbol} {r.side} · {r.date_utc} · '
                           f'{SETUP_LABELS.get(r.setup_pre,r.setup_pre)} · {r.outcome}'
            for r in rows.itertuples(index=False)}
    event_key=st.selectbox('Abrir REACTION',list(labels),format_func=lambda k:labels[k],key='hlrc_event')
    item=rows.loc[rows.event_key.eq(event_key)].iloc[0]
    st.markdown(f'#### {item.symbol} · {item.side} · nivel {item.primary_timeframe}')

    cards=st.columns(3,gap='small')
    with cards[0]:
        st.markdown('**1 · ANTES**')
        st.info(SETUP_LABELS.get(item.setup_pre,item.setup_pre))
        st.caption(_explain(str(item.side),str(item.setup_pre)))
        if pd.notna(item.pre_contacts_5):
            st.caption(f'Contacto con la zona: {int(item.pre_contacts_5)}/5 velas anteriores. '
                       +('Presión repetida (exploratoria, NO predice ruptura).'
                         if bool(item.pressure_pre) else 'No confirma ruptura.'))
    with cards[1]:
        st.markdown('**2 · VELA REACTION CERRADA**')
        st.info(KNOWN_LABELS.get(item.known_candle_label,item.known_candle_label))
        st.caption('Solo disponible después de cerrar 1m. Un cruce inicial no es aceptación.')
    with cards[2]:
        st.markdown('**3 · DESPUÉS**')
        st.info(str(item.outcome) if pd.notna(item.outcome) else 'SIN_PATH')
        st.caption('Resultado del escenario First-Touch elegido arriba; no intervino en PRE.')

    st.caption('Zona verde: '+_pretty(item.primary_zone_low,8)+'–'+_pretty(item.primary_zone_high,8)
              +' · distancia previa: '+_pretty(item.primary_distance_atr,3)+' ATR.'
              +' · nivel original: '+str(item.primary_state_pre)+'.')
    if pd.notna(item.primary_distance_atr) and float(item.primary_distance_atr)>1.5:
        st.warning('El nivel principal está distante (>1,5 ATR): no atribuirle la REACTION actual.')
    if pd.isna(item.primary_zone_id):
        st.warning('Sin nivel principal confirmado.');return
    available=zones[zones.event_key.eq(event_key)]
    c1,c2=st.columns(2)
    post_n=c1.select_slider('Velas POST 1m (min)',[15,30,60,180,360],value=180,key='hlrc_post')
    tf=c2.radio('Velas previas',['15m','30m'],index=0 if str(item.primary_timeframe)=='15m' else 1,
                horizontal=True,key='hlrc_tf')
    try:
        fig=_read_structural_figure(project,item,available,display_tf=tf,max_zones=2)
        bars,status=get_post_bars(project,item.event_key,item.retest_ts)
        if fig is None:
            st.warning('No hay velas previas disponibles para este gráfico.');return
        fig,pre_diagnostic=enhance_pre_candles(fig,tf,show_higher_lows=True,visible_hours=12)
        if bars:
            fig=overlay_future(fig,bars,retest_ts=item.retest_ts,side=item.side,
                entry_ts=item.entry_ts,entry_price=item.entry_price,outcome=item.outcome,
                first_touch_minute=item.first_touch_minute,tp_pct=tp_pct,sl_pct=sl_pct,
                outcome_horizon=outcome_horizon,display_minutes=post_n,pre_hours=12)
        st.plotly_chart(fig,use_container_width=True,key='hlrc_chart')
        st.caption(diagnostic_text(pre_diagnostic,tf))
        st.caption(outcome_explanation(item.outcome,item.first_touch_minute,tp_pct=tp_pct,
            sl_pct=sl_pct,outcome_horizon=outcome_horizon,display_minutes=post_n,
            path_state=status,entry_mode=entry_mode))
    except Exception as exc:
        st.error(f'No se pudo dibujar el gráfico: {exc}')
    st.caption('La reacción se señala al CIERRE de su vela 1m: no es necesariamente precio de entrada. '
               'Si una ruptura requiere aceptación en velas futuras, esa aceptación NO existe todavía en PRE.')
