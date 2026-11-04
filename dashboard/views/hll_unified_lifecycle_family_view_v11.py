"""Panel SOLO LECTURA bajo el gráfico Discovery original: geometría × familia × entrada.

El reporte debe estar previamente generado offline y verificado; nunca recalcula
clasificadores ni toma decisiones de trading. POST oculto hasta revelar POST.
"""
from __future__ import annotations

#da

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from hll_unified_family_lifecycle_v11 import sha, exact_ts, VERSION

REPORT='reports/horizontal_level_unified_lifecycle_v11/pilot15_seed20261103_v11'
FAMILY_LABELS={
 'ASCENSO_ESCALONADO':'Ascenso escalonado',
 'RECONSTRUCCION_ENTRE_CONTACTOS':'Reconstrucción entre contactos',
 'OSCILACION_EN_RESISTENCIA':'Oscilación en resistencia',
 'REBOTE_TRAS_CORRECCION':'Rebote tras corrección',
 'DETERIORO_DESDE_MINIMO':'Deterioro desde mínimo',
 'CORRECCION_DOMINANTE':'Corrección dominante',
 'SECUENCIA_MIXTA_INDETERMINADA':'Secuencia mixta / indeterminada',
 'INDETERMINADA_SIN_MINIMO':'Sin mínimo confirmado',
}
REQUIRED=('unified_attempts_family_v11.csv','unified_entries_family_v11.csv',
          'entry_outcomes_family_v11.csv','by_geometry_family_route_outcomes_v11.csv',
          'episode_family_evolution_v11.csv')


def read_csv(path):
    with Path(path).open('r',encoding='utf-8-sig',newline='') as f:
        return list(csv.DictReader(f))


def validated_load(folder):
    folder=Path(folder)
    m=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    if m.get('version')!=VERSION or m.get('outcomes_copied_no_recompute') is not True:
        raise ValueError('No es HLL Unified Family V1.1 validado')
    hashes=m.get('output_sha256',{})
    if not set(REQUIRED)<=set(hashes):raise ValueError('Manifiesto sin archivos obligatorios')
    for name,ref in hashes.items():
        path=folder/name
        if not path.is_file() or sha(path)!=ref:
            raise ValueError(f'El reporte V1.1 no coincide con su manifiesto: {name}')
    data={name:read_csv(folder/name) for name in REQUIRED}
    index={}
    for r in data['unified_attempts_family_v11.csv']:
        if not r['attempt_id'] or r['attempt_id'] in index:raise ValueError('Attempt ID duplicado')
        index[r['attempt_id']]=r
    for e in data['unified_entries_family_v11.csv']:
        if e['attempt_id'] not in index:raise ValueError('Entrada sin intento')
    for o in data['entry_outcomes_family_v11.csv']:
        if o['attempt_id'] not in index:raise ValueError('Resultado sin intento')
    data['by_attempt']=index
    return data


def find_selected(data, selected):
    aid=str(selected.get('attempt_id',''))
    row=data['by_attempt'].get(aid)
    if row is None:return None
    for k in ('symbol','level_id','episode_id'):
        if k in selected and selected.get(k) is not None and str(selected.get(k))!='' and str(selected.get(k))!=row[k]:
            raise ValueError(f'El intento seleccionado no corresponde al {k} del reporte')
    if 'pre_closed_ts' in selected and selected.get('pre_closed_ts') is not None:
        if exact_ts(selected.get('pre_closed_ts'))!=exact_ts(row['pre_closed_ts']):
            raise ValueError('El corte PRE del intento no coincide con el reporte V1.1')
    return row



def matrix_rows_for_attempt(data, selected, route, *, include_other_agreements=False):
    """Matriz exacta: nunca mezclar SPLIT/ruta/geom/familia/escala.

    El CSV agrupa adicionalmente por scale_agreement. Se devuelve explícito
    para que dos filas del mismo escenario no parezcan duplicadas.
    """
    required=('split','pre_pattern_96','family_1p0','scale_agreement')
    if any(k not in selected for k in required):
        raise ValueError('Intento seleccionado sin atributos PRE exactos')
    out=[]
    for m in data['by_geometry_family_route_outcomes_v11.csv']:
        if (m['split']!=selected['split'] or m['route']!=route or
            m['scale']!='1p0' or m['family']!=selected['family_1p0'] or
            m['pre_pattern_96']!=selected['pre_pattern_96']):
            continue
        same=m['scale_agreement']==selected['scale_agreement']
        if same or include_other_agreements:
            out.append({**m,'same_agreement_as_selected':same})
    return sorted(out,key=lambda x:(x['scenario'],not x['same_agreement_as_selected'],x['scale_agreement']))


def episode_timeline(data, selected):
    """Cronología retrospectiva visible solo tras Revelar POST.

    No copia la huella del primer intento ni mueve cortes PRE: cada fila
    conserva su familia/geometría exactas.
    """
    eid=selected['episode_id']; symbol=selected['symbol']; level=selected['level_id']
    rows=[a for a in data['unified_attempts_family_v11.csv'] if a['episode_id']==eid]
    out=[]; prev=None; ordinals=[]
    for a in sorted(rows,key=lambda x:(exact_ts(x['pre_closed_ts']),int(x['episode_ordinal']),x['attempt_id'])):
        if a['symbol']!=symbol or a['level_id']!=level:
            raise ValueError('Episodio cruza símbolo o nivel')
        ordinal=int(a['episode_ordinal']); ordinals.append(ordinal)
        ms=exact_ts(a['pre_closed_ts'])
        instant=datetime.fromtimestamp(ms/1000,tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
        fam=a['family_1p0']
        out.append({'intento':ordinal, 'corte_PRE_UTC':instant,
                    'geometria_96':a['pre_pattern_96'],
                    'familia_1_ATR':FAMILY_LABELS.get(fam,fam or 'Sin datos'),
                    'cambio_vs_anterior':'—' if prev is None else ('Sí' if fam!=prev else 'No'),
                    'seleccionado':'◀' if a['attempt_id']==selected['attempt_id'] else ''})
        prev=fam
    if ordinals!=list(range(1,len(ordinals)+1)):
        raise ValueError('Orden de intentos incompleto o inconsistente')
    if sum(bool(x['seleccionado']) for x in out)!=1:
        raise ValueError('Intento seleccionado ausente de episodio')
    return out


def render(root, selected, *, reveal_post=False):
    """Renderizar dentro del Discovery, sin cambiar ranking ni dibujar señales."""
    import pandas as pd
    import streamlit as st
    folder=Path(root)/REPORT
    if not (folder/'manifest.json').is_file():
        st.caption('Unified Lifecycle V1.1 todavía no generado: se conserva el Discovery original.')
        return
    @st.cache_data(show_spinner=False,max_entries=3)
    def cached(folder_str,manifest_mtime):
        return validated_load(folder_str)
    try:
        data=cached(str(folder),(folder/'manifest.json').stat().st_mtime_ns)
        row=find_selected(data,selected)
    except (OSError,ValueError,KeyError,TypeError) as exc:
        st.error(f'Lifecycle V1.1 no verificable: {exc}. No se muestran resultados del reporte.')
        return
    if row is None:
        st.caption('Lifecycle V1.1: este intento está fuera de los 15 símbolos del piloto; no se infieren entradas.')
        return
    with st.expander('🧩 Lifecycle unificado V1.1 · geometría × familia × OPEN',expanded=False):
        st.caption('Dos taxonomías PRE complementarias; etiquetas del corte exacto. Universo condicionado a CROSS_UP. '
                   'No es un escáner prospectivo ni un ranking de calidad.')
        c1,c2=st.columns(2)
        with c1:
            st.markdown('**Geometría estructural PRE**')
            st.write('96 velas: `'+row['pre_pattern_96']+'`')
            st.caption('24: '+row['pre_pattern_24']+' · 48: '+row['pre_pattern_48'])
            st.caption('Huella 35 variables: '+row['fingerprint_status']+' (no se hereda del primer corte)')
        with c2:
            st.markdown('**Familia de movimiento PRE**')
            for f,label in (('family_0p5','0,5 ATR'),('family_1p0','1,0 ATR'),('family_1p5','1,5 ATR')):
                st.write(label+': '+FAMILY_LABELS.get(row[f],row[f] or 'Sin datos'))
            st.caption('Acuerdo entre escalas: '+row['scale_agreement'])
        st.caption('Split: '+row['split']+' · Ordinal dentro del episodio: '+row['episode_ordinal']+'. '
                   'Las familias posteriores de un mismo episodio no son conocidas en el primer corte.')
        if row['split']=='HOLDOUT_SYMBOL':
            st.warning('Símbolo HOLDOUT: estos resultados son para validación, NO para elegir familias, umbrales ni filtros.')
        if not reveal_post:
            st.info('Revelá POST en el gráfico para ver entradas y resultados históricos. '
                    'La clasificación PRE se muestra sin utilizar resultados futuros.')
            return
        coverage={x:row['coverage_'+x] for x in ('FLIP_1M','V29_CONTINUATION_30M','V29_RECLAIM_30M')}
        st.markdown('**Cobertura y entradas · intento exacto**')
        st.dataframe(pd.DataFrame([{'Ruta':k,'Cobertura':v} for k,v in coverage.items()]),
                     hide_index=True,use_container_width=True)
        entries=[e for e in data['unified_entries_family_v11.csv'] if e['attempt_id']==row['attempt_id']]
        outcomes=[o for o in data['entry_outcomes_family_v11.csv'] if o['attempt_id']==row['attempt_id']]
        if entries:
            st.dataframe(pd.DataFrame([{'entry_id':e['entry_id'],'ruta':e['route'],
                 'OPEN_1m_ts':e['entry_ts'],'precio_OPEN_1m':e['entry_price']}
                 for e in entries]),hide_index=True,use_container_width=True)
            st.markdown('**Escenarios históricos desde el OPEN verificado**')
            st.dataframe(pd.DataFrame([{'entry_id':o['entry_id'],'ruta':o['route'],
                'escenario':o['scenario'],'estado':o['state'],
                'mfe_pct':o['mfe_pct'],'mae_pct':o['mae_pct'],
                'net_pct_teorico':o['net_pct']} for o in outcomes]),
                hide_index=True,use_container_width=True)
        else:
            st.info('No existe OPEN 1m verificado para este intento en FIRST_PER_LEVEL. '
                    'NOT_COVERED no equivale a NO_SIGNAL.')
        st.markdown('**Matriz descriptiva · misma geometría y familia**')
        st.caption('Agrupada también por acuerdo entre las tres escalas ATR. Los resultados pertenecen a otros '
                   'intentos del mismo grupo PRE, NO necesariamente al seleccionado.')
        route=st.selectbox('Ruta de la matriz',
            ['FLIP_1M','V29_CONTINUATION_30M','V29_RECLAIM_30M'],
            key='hll_v11_route_'+row['attempt_id'])
        include_other=st.checkbox('Comparar también otros acuerdos entre escalas ATR',value=False,
            key='hll_v11_other_agree_'+row['attempt_id'])
        matrix=matrix_rows_for_attempt(data,row,route,include_other_agreements=include_other)
        if matrix:
            st.caption('Acuerdo del intento seleccionado: '+row['scale_agreement']+
                       '. Cada escenario puede contener varios acuerdos, que NO deben confundirse.')
            st.dataframe(pd.DataFrame([{
                'escenario':x['scenario'],
                'acuerdo_ATR':x['scale_agreement'],
                'mismo_acuerdo':('Sí' if x['same_agreement_as_selected'] else 'No'),
                'entradas':x['entries'],'episodios':x['episodes'], 'símbolos':x['symbols'],
                'TP':x['tp_first'],'SL':x['sl_first'],
                'sin_resolver':int(x['timeout'])+int(x['ambiguous'])+int(x['censored']),
                'TP_sobre_resueltos_%':x['tp_pct_resolved']
            } for x in matrix]), hide_index=True,use_container_width=True)
            st.caption('Muestras pequeñas y episodios dependientes: los porcentajes son descriptivos, '
                       'no probabilidades para el intento seleccionado.')
        else:
            st.caption('No hay entradas con esta combinación exacta de geometría, familia y acuerdo ATR en el piloto.')
        ep=[x for x in data['episode_family_evolution_v11.csv'] if x['episode_id']==row['episode_id']]
        if ep:
            st.markdown('**Evolución entre intentos · RETROSPECTIVA**')
            timeline=episode_timeline(data,row)
            st.caption(f'{len(timeline)} intentos del episodio. Cada fila es el corte PRE de ese intento; '
                       'los intentos futuros NO estaban disponibles en el seleccionado.')
            st.dataframe(pd.DataFrame(timeline),hide_index=True,use_container_width=True)
            st.caption('La cronología completa solo aparece tras Revelar POST; '
                       'no se utiliza como criterio PRE del primer intento.')
        st.caption('TP/SL son simulaciones históricas V1 sobre OHLC 1m; no fills reales. '
                   'Se preservan HOLDOUT y DISCOVERY separados; sin inferencia de rentabilidad futura.')
