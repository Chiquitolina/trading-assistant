"""Opt-in, read-only Inspector Universal de Resistencia inside Discovery PRE.

Never modifies PRE rank, HLL detectors or entry records. The only relevant
'future' state is deliberately revealed POST for retrospective research.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd

from hll_resistance_universal_audit_v1 import price_behavior, replay_frozen, VERSION

LEVELS = Path('reports/horizontal_level_structure_lab_v26/runs/sep290_frozen_v26_roundtrip/levels_features.csv')
EVENT_LABEL = {
    'CONTACTO_FISICO':'Contacto físico con zona',
    'CONTACTO_DESDE_ARRIBA':'Contacto desde arriba (candidato)',
    'DOS_CIERRES_ENCIMA':'Dos cierres consecutivos sobre zona',
    'PERDIDA_ZONA':'Pérdida: cierre bajo la zona',
    'RECUPERACION_CIERRE':'Recuperación: cierre sobre la zona',
    'DEFENSA_DESCRIPTIVA':'Defensa descriptiva (NO Flip V2.6)',
}
EVENT_COLOR = {
    'CONTACTO_DESDE_ARRIBA':'#ecc15c',
    'PERDIDA_ZONA':'#ec7272',
    'RECUPERACION_CIERRE':'#59b4e6',
    'DEFENSA_DESCRIPTIVA':'#21be94',
    'DOS_CIERRES_ENCIMA':'#a897e4',
}
EVENT_SHAPE = {
    'CONTACTO_DESDE_ARRIBA':'triangle-up',
    'PERDIDA_ZONA':'x',
    'RECUPERACION_CIERRE':'diamond',
    'DEFENSA_DESCRIPTIVA':'star',
    'DOS_CIERRES_ENCIMA':'circle',
}


def level_formation_atr(path, symbol, level_id, lo, hi):
    """Read ATR *at formation* from V2.6: never substitute CROSS_UP atr_pre."""
    path=Path(path)
    if not path.is_file():
        raise FileNotFoundError('Falta levels_features.csv de V2.6; replay Flip no verificable')
    found=[]
    with path.open(encoding='utf-8-sig',newline='') as handle:
        for row in csv.DictReader(handle):
            if row.get('symbol') == str(symbol) and row.get('level_id') == str(level_id):
                found.append(row)
    if len(found)!=1:
        raise ValueError(f'Nivel V2.6 ambiguo/ausente: {symbol} / {level_id} ({len(found)})')
    row=found[0]
    l,h,atr=map(float,(row['zone_low'],row['zone_high'],row['atr_at_formation']))
    if atr<=0 or abs(l-float(lo))>max(abs(l),1e-12)*1e-10 or abs(h-float(hi))>max(abs(h),1e-12)*1e-10:
        raise ValueError('Zona congelada V2.6 distinta de Discovery: abortar replay')
    return atr


def prepare(root, selected, candles, overlay_records, *, visible_post_bars):
    """Build inspector for exact selected Attempt ID; no CSV writes or reranking."""
    root=Path(root)
    pre=int(selected['pre_closed_ts'])
    breakout=int(selected['breakout_closed_ts'])
    zlo,zhi,pre_atr=(float(selected[k]) for k in ('zone_low','zone_high','atr_pre'))
    # Behavior must be independent of detector source availability. If the
    # frozen level-formation ATR cannot be verified, contacts/losses/recoveries
    # remain observable; ATR-dependent defense and replay are unavailable.
    formation=None
    unavailable=None
    try:
        formation=level_formation_atr(root/LEVELS,selected['symbol'],selected['level_id'],zlo,zhi)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        unavailable=f'ATR de formación V2.6 no verificable: {exc}'
    b=price_behavior(candles,pre_closed_ts=pre,breakout_closed_ts=breakout,
        zone_low=zlo,zone_high=zhi,formation_atr=formation,horizon=int(visible_post_bars))
    detector=None
    if overlay_records is None:
        unavailable=(unavailable + '; ' if unavailable else '') + 'Overlay de mecanismos no disponible'
    elif formation is not None:
        detector=replay_frozen(candles,breakout_closed_ts=breakout,zone_low=zlo,
            zone_high=zhi,formation_atr=formation,atr_pre=pre_atr,
            frozen=overlay_records)
    return {'version':VERSION,'attempt_id':str(selected['attempt_id']),
            'behavior':b,'detector':detector,'detector_unavailable_reason':unavailable,
            'formation_atr':formation,'pre_atr':pre_atr,'breakout_closed_ts':breakout}


def add_behavior_markers(fig, events, pre_closed_ts, visible_post_bars, *, visible_types=None):
    """Add optional, non-signal markers ONLY inside revealed POST horizon."""
    import plotly.graph_objects as go
    start=int(pre_closed_ts)
    end=start+int(visible_post_bars)*1_800_000
    types=set(EVENT_COLOR if visible_types is None else visible_types)
    for kind in types:
        if kind not in EVENT_COLOR:continue
        values=[v for v in events if v['event']==kind and
                start<=int(v['known_closed_ts'])<=end][:18]
        if not values:continue
        fig.add_trace(go.Scatter(
            x=[pd.Timestamp(int(v['known_closed_ts'])-900_000,unit='ms',tz='UTC') for v in values],
            y=[float(v['price']) for v in values], mode='markers',
            marker=dict(color=EVENT_COLOR[kind],symbol=EVENT_SHAPE[kind],size=10),
            name=EVENT_LABEL[kind],
            customdata=[[str(pd.Timestamp(int(v['known_closed_ts']),unit='ms',tz='UTC'))] for v in values],
            hovertemplate=('%{customdata[0]}<br>Precio: %{y:.8g}'
                           '<br>Auditoría descriptiva, NO señal de entrada<extra></extra>')))
    return fig


def render_audit(result):
    """Show dual audit + comparison, post-only and explicitly descriptive."""
    import streamlit as st
    behavior=result['behavior']
    detector=result['detector']
    with st.expander('🔍 Inspector Universal de Resistencia · comportamiento + detectores',expanded=True):
        st.caption('Auditoría histórica del Attempt ID exacto: '+result['attempt_id']+'. '
            'Solo se muestra después de Revelar POST. No modifica huellas, filtros, ranking, '
            'detectores originales ni LIVE. Contacto físico ≠ retest confirmado ≠ entrada.')
        a,b,c=st.tabs(['Comportamiento del precio','Detector original','Comparación'])
        with a:
            x=behavior['summary']
            cols=st.columns(4)
            cols[0].metric('Contactos desde arriba',x['touches_from_above'])
            cols[1].metric('Pérdidas de zona',x['loss_crosses'])
            cols[2].metric('Recuperaciones',x['recovery_crosses'])
            cols[3].metric('Defensas descriptivas',x['descriptive_defenses'])
            if not x['breakout_close_above_zone_high']:
                st.warning('El CROSS_UP original no cerró por encima de zone_high: no inferir aceptación inicial.')
            st.caption(f'POST observado {x["observed_post_bars"]}/{x["requested_post_bars"]} velas 30m. '
                + ('Ventana completa.' if x['complete_window'] else 'Ventana CENSURADA: falta una vela.'))
            st.dataframe(pd.DataFrame([dict(
                Evento=EVENT_LABEL.get(e['event'],e['event']),
                UTC=pd.Timestamp(e['known_closed_ts'],unit='ms',tz='UTC'),
                Barra=e['bar_offset'], Precio=e['price'], Naturaleza=e['note'])
                for e in behavior['events']]),hide_index=True,use_container_width=True)
            with st.expander('Traza completa, vela por vela',expanded=False):
                st.dataframe(pd.DataFrame(behavior['bars']),hide_index=True,use_container_width=True)
            st.caption(('ATR de formación no disponible: defensas descriptivas no evaluadas. ' if result['formation_atr'] is None else '') + 'Una defensa descriptiva se mide después de un contacto, con hasta cuatro cierres; '
                'no sustituye la aceptación temprana ni la regla congelada del Flip V2.6.')
        with b:
            if detector is None:
                st.warning('Auditoría del detector no disponible: '+str(result.get('detector_unavailable_reason') or 'faltan fuentes verificables')+'.')
                st.caption('No se interpreta como NO_SIGNAL. Las observaciones de precio siguen disponibles.')
            else:
                if not detector['all_match']:
                    st.error('REPLAY MISMATCH: los detectores originales y su reproducción difieren. '
                             'No interpretar las puertas de rechazo como diagnóstico cerrado.')
                else:
                    st.success('Los tres estados del replay coinciden con las versiones congeladas.')
                st.dataframe(pd.DataFrame(detector['rows']),hide_index=True,use_container_width=True)
                st.markdown('**Aceptación temprana Flip V2.6 · primeras 4 velas**')
                st.caption('La primera vela es el CROSS_UP original. Dos cierres por encima de zone_high '
                           'deben ocurrir dentro de la ventana congelada; el contacto físico posterior '
                           'no reabre automáticamente un intento descartado.')
                st.dataframe(pd.DataFrame([{
                    'barra':b['bar_offset'],'cierre_UTC':pd.Timestamp(b['known_closed_ts'],unit='ms',tz='UTC'),
                    'precio_cierre':b['close'],'relacion_zona':b['relation'],
                    'dos_cierres_encima':b['two_closes_above'],
                    'contacto_fisico':b['physical_touch']}
                    for b in behavior['bars'][:4]]),hide_index=True,use_container_width=True)
                st.caption('V2.6 replay: first_4b_status = '
                           +str(detector['v26_transitions'].get('first_4b_status','SIN_DATO')))
                st.markdown('**Condiciones observadas de Continuation V2.9 · primeras 12 velas**')
                st.dataframe(pd.DataFrame(detector['gate_rows']),hide_index=True,use_container_width=True)
                st.caption('Reclaim depende de la falla V2.6 original; no es elegible '
                           'sin esa falla. Cobertura de entrada FIRST_PER_LEVEL se informa por separado.')
        with c:
            x=behavior['summary']
            st.markdown('**Dos lecturas sobre el mismo corte PRE**')
            st.write('**Precio observable:** '+str(x['touches_from_above'])+' contactos desde arriba, '
                +str(x['loss_crosses'])+' pérdidas de zona, '+str(x['recovery_crosses'])+
                ' recuperaciones y '+str(x['descriptive_defenses'])+' defensas descriptivas.')
            if detector is None:
                st.warning('Contraste pendiente: '+str(result.get('detector_unavailable_reason') or 'no hay mecanismos validados accesibles')+'.')
            else:
                st.dataframe(pd.DataFrame(detector['rows'])[['route','original_status','entry_coverage',
                    'replay_equal']],hide_index=True,use_container_width=True)
                if x['descriptive_defenses'] and not any(r['route']=='FLIP_1M' and
                         str(r['original_status']).startswith('FLIP_') for r in detector['rows']):
                    st.info('Hay defensa descriptiva sin Flip confirmado V2.6: candidato para '
                            'auditoría de falso negativo, NO flip demostrado.')
            st.caption('Comparación exploratoria POST. Un evento favorable posterior no demuestra '
                'entrada capturable ni rentabilidad. No se utiliza para seleccionar vecinos PRE.')
