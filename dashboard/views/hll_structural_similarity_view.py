"""Panel opcional HLL · Buscar estructuras similares a la huella PRE seleccionada.

Cualquier corte #N puede ser referencia; índice V1 = primer intento de cada episodio.
El ranking usa distancias descriptivas entre familias, NO señales ni etiquetas A/B/C.
"""
from __future__ import annotations

from pathlib import Path
import math

import pandas as pd

import hll_structural_fingerprint_v1 as fp
import hll_structural_fingerprint_labels_v1 as labels
import hll_structural_similarity_v1 as similarity


INDEX_FOLDER = similarity.DEFAULT_OUTPUT


def candidate_meta(candidate):
    """Adaptar solo metadata PRE verificada para el dibujante ORIGINAL del Discovery."""
    if candidate.get('status') != 'COMPLETE_PRE_96_288':
        raise ValueError('No se puede dibujar un corte incompleto')
    required = ('attempt_id', 'episode_id', 'symbol', 'level_id', 'pre_closed_ts',
                'zone_low_report_only', 'zone_high_report_only', 'atr_pre_report_only')
    if any(candidate.get(k) is None for k in required):
        raise ValueError('Candidato sin metadata PRE verificable')
    return {'attempt_id':candidate['attempt_id'], 'episode_id':candidate['episode_id'],
            'symbol':candidate['symbol'], 'level_id':candidate['level_id'],
            'pre_closed_ts':int(candidate['pre_closed_ts']),
            'zone_low':float(candidate['zone_low_report_only']),
            'zone_high':float(candidate['zone_high_report_only']),
            'atr_pre':float(candidate['atr_pre_report_only']), 'status':'COMPLETE_PRE'}


def summary_table(result):
    """Datos fieles al ranking; distancia no equivale a una probabilidad o calidad."""
    rows=[]
    for i, item in enumerate(result['results'], 1):
        c=item['candidate']
        row={'#':i,'Símbolo':c['symbol'], 'Intento': '#1', 'Attempt ID':c['attempt_id'],
             'Episode ID':c['episode_id'],
             'Corte PRE UTC':pd.Timestamp(int(c['pre_closed_ts']),unit='ms',tz='UTC'),
             'Distancia':round(item['distance'],4)}
        for fam in result['selected_families']:
            row[fp.FAMILIES[fam]]=round(item['family_distances'][fam],4)
        rows.append(row)
    return pd.DataFrame(rows)


def feature_explanations(item, *, best=True, top=5):
    """Contrast between reference and candidate for comparable features.

    Normalized distances are never used as A/B/C labels. A small difference
    means numeric resemblance, not that a variable is desirable.
    """
    paired=[]
    for family, features in item['paired'].items():
        for key,reference,candidate,clipped,raw in features:
            paired.append({'Familia':fp.FAMILIES[family],
                           'Variable':labels.dual_label(key),
                           'ID técnico':key,
                           'Referencia':reference,'Candidato':candidate,
                           'Unidad':fp.FEATURES[key][1],
                           'Diferencia normalizada':round(raw,4),
                           'Lectura contextual':labels.reading(key)})
    ordered=sorted(paired,key=lambda r:(r['Diferencia normalizada'],r['ID técnico']),reverse=not best)
    return pd.DataFrame(ordered[:top])


def source_row_for_reference(selected_row):
    """Keep metadata of exact selected CROSS_UP, NOT episode-first substitute."""
    required=('attempt_id','episode_id','level_id','symbol','pre_closed_ts','zone_low','zone_high','atr_pre')
    missing=[k for k in required if k not in selected_row or selected_row[k] is None]
    if missing: raise ValueError('La selección no trae metadata PRE: '+','.join(missing))
    source={k:selected_row[k] for k in required}
    source['episode_ordinal']=int(selected_row.get('episode_ordinal',1))
    source['status']=selected_row.get('status','COMPLETE_PRE')
    return source


def _load_index_cached(index_path,mtime_csv,mtime_norm,mtime_manifest):
    # Decorated through Streamlit at runtime; pure tests can call it normally.
    return similarity.load_index(index_path)


def render_structural_similarity(selected_row, candles, root):
    """Render a search expander after the original candle chart. No dashboard mutation.

    selected_row: precisely same Attempt ID shown in the preceding candlestick chart.
    candles: full 30m OHLC dict for that symbol; reference slices only PRE.
    """
    import streamlit as st
    import hll_structural_approach_ohlc_pre as ohlc
    from dashboard.views.hll_structural_discovery_view import chart_for_attempt

    root=Path(root)
    directory=root/INDEX_FOLDER
    with st.expander('🔎 Buscar estructuras similares · Huella Estructural PRE',expanded=False):
        st.caption('Elegí qué propiedades conservar de la estructura que estás viendo. '
                   'La referencia es el intento seleccionado, NO se sustituye por #1. '
                   'Las coincidencias son semejanzas numéricas, no pronósticos ni notas de calidad.')
        if not all((directory/name).is_file() for name in ('manifest.json','fingerprints_first_pre.csv','normalizers.json')):
            st.info('Falta construir el índice universal. Ejecutá primero en el VPS: '
                    '`python3 hll_structural_similarity_v1.py --plan` y '
                    '`python3 -u hll_structural_similarity_v1.py --run`.')
            return
        try:
            # Cache keyed by exact source path and mtimes. Index integrity is
            # checked by sha256 before admitting ANY row to search.
            csvp=directory/'fingerprints_first_pre.csv'
            normp=directory/'normalizers.json'
            manp=directory/'manifest.json'
            cached=st.cache_data(show_spinner='Cargando índice PRE de episodios')(_load_index_cached)
            candidates,norm,manifest=cached(str(directory),csvp.stat().st_mtime_ns,
                                            normp.stat().st_mtime_ns,manp.stat().st_mtime_ns)
        except (ValueError,KeyError,FileNotFoundError,OSError) as exc:
            st.error(f'El índice no pasó la verificación: {exc}')
            return
        base=source_row_for_reference(selected_row)
        base_id=base['attempt_id']
        # Clear navigation override when the actual selected chart changes.
        state_key='hll_similarity_origin_attempt_v1'
        if st.session_state.get(state_key)!=base_id:
            st.session_state[state_key]=base_id
            st.session_state.pop('hll_similarity_override_attempt_v1',None)
            st.session_state.pop('hll_similarity_search_results_v1',None)
        override=st.session_state.get('hll_similarity_override_attempt_v1')
        override_row=next((r for r in candidates if r['attempt_id']==override),None) if override else None
        if override and override_row is None:
            st.session_state.pop('hll_similarity_override_attempt_v1',None)
            override_row=None
        if override_row is not None:
            reference=override_row
            st.info(f'Nueva referencia de búsqueda: {override_row["symbol"]} · '
                    f'`{override_row["attempt_id"]}` (primer intento de su episodio). '
                    'El gráfico principal de arriba sigue mostrando la selección original.')
            if st.button('Volver a la referencia del gráfico',key='hll_similarity_reset_reference_v1'):
                st.session_state.pop('hll_similarity_override_attempt_v1',None)
                st.session_state.pop('hll_similarity_search_results_v1',None)
                st.rerun()
        else:
            try:
                reference=similarity.build_reference(base,candles,scale_atr=1.0)
            except (ValueError,TypeError,KeyError) as exc:
                st.warning(f'No se puede construir la huella del corte seleccionado: {exc}')
                return
        if reference.get('status')!='COMPLETE_PRE_96_288':
            st.warning('Este corte necesita 96 y 288 velas PRE completas para buscar similares.')
            return
        st.markdown(f'**Referencia:** {reference["symbol"]} · '
                    f'`{reference["attempt_id"]}` · '
                    f'corte #{int(reference["episode_ordinal_report_only"])}')
        st.caption(f'{len(candidates):,} episodios indexados; {manifest["complete_288"]:,} completos. '
                   'V1: se comparan los PRIMEROS intentos de otros episodios. '
                   'El índice se obtuvo sobre el histórico completo; es una búsqueda exploratoria retrospectiva.')
        with st.form('hll_similarity_search_form_v1',clear_on_submit=False):
            names=list(fp.FAMILIES)
            chosen=st.multiselect('Familias a conservar de la huella',names,
                default=names,format_func=lambda fam:fp.FAMILIES[fam],
                key='hll_similarity_families_v1')
            c1,c2,c3=st.columns(3)
            mode=c1.radio('Modo de similitud',('Equilibrada','Estricta'),horizontal=False,
                          help='Equilibrada: promedio igual por familia. Estricta: pesa la peor coincidencia.',
                          key='hll_similarity_mode_v1')
            top=c2.selectbox('Cantidad de candidatos',[5,10,20,30],index=1,
                             key='hll_similarity_top_v1')
            distinct=c3.checkbox('Priorizar símbolos distintos',value=True,
                                 key='hll_similarity_distinct_v1')
            before=st.checkbox('Solo cortes de candidatos anteriores a la referencia',value=False,
                 help='Filtro temporal del catálogo. La normalización del índice global sigue siendo retrospectiva.',
                 key='hll_similarity_before_v1')
            submitted=st.form_submit_button('🔎 Buscar estructuras similares',use_container_width=True)
        if submitted:
            if not chosen:
                st.warning('Elegí al menos una familia de variables.')
                st.session_state.pop('hll_similarity_search_results_v1',None)
            else:
                filtered=[r for r in candidates if (not before or int(r['pre_closed_ts'])<=int(reference['pre_closed_ts']))]
                with st.spinner('Comparando huellas PRE...'):
                    ranked=similarity.rank(reference,filtered,norm,chosen,
                         mode='strict' if mode=='Estricta' else 'balanced',top=top,
                         distinct_symbols=distinct)
                st.session_state['hll_similarity_search_results_v1']={
                    'result':ranked,'reference_id':reference['attempt_id'],
                    'index_sha256':manifest['fingerprints_sha256'],
                    'filters':{'before':before},
                }
        current=st.session_state.get('hll_similarity_search_results_v1')
        if not current or current['reference_id']!=reference['attempt_id'] or current['index_sha256']!=manifest['fingerprints_sha256']:
            return
        result=current['result']
        st.success(f'{len(result["results"])} coincidencias mostradas · '
                   f'{result["eligible_candidates"]} episodios comparables · '
                   f'{result["insufficient_candidates"]} con datos insuficientes.')
        st.caption('Distancia menor = más parecido EN LAS FAMILIAS ELEGIDAS. '
                   'Los números no representan probabilidad de ruptura, calidad, rentabilidad ni recomendación operativa.')
        ranked_table=summary_table(result)
        st.dataframe(ranked_table,hide_index=True,use_container_width=True)
        st.download_button('Descargar ranking CSV',data=ranked_table.to_csv(index=False).encode('utf-8-sig'),
            file_name=f'hll_similares_{reference["symbol"]}_{reference["attempt_id"]}.csv',
            mime='text/csv',key='hll_similarity_download_v1')
        if not result['results']:return
        ids=[r['candidate']['attempt_id'] for r in result['results']]
        choice=st.selectbox('Inspeccionar coincidencia',ids,
                format_func=lambda aid:next(f'{x["candidate"]["symbol"]} · distancia {x["distance"]:.3f} · {aid}'
                                            for x in result['results'] if x['candidate']['attempt_id']==aid),
                key='hll_similarity_candidate_selected_v1')
        item=next(x for x in result['results'] if x['candidate']['attempt_id']==choice)
        candidate=item['candidate']
        st.markdown(f'**{candidate["symbol"]}** · Attempt ID `{choice}` · '
                    f'Episode ID `{candidate["episode_id"]}`')
        st.caption('Comparación por familia: cada familia pesa igual, independientemente de cuántas variables contenga.')
        famtable=pd.DataFrame([{
            'Familia':fp.FAMILIES[fam],
            'Distancia normalizada':round(dist,4),
            'Cobertura':f'{item["family_coverage"][fam]:.0%}',
        } for fam,dist in item['family_distances'].items()])
        st.dataframe(famtable,hide_index=True,use_container_width=True)
        t1,t2=st.tabs(['Variables más parecidas','Diferencias más grandes'])
        with t1:
            st.dataframe(feature_explanations(item,best=True),hide_index=True,use_container_width=True)
        with t2:
            st.dataframe(feature_explanations(item,best=False),hide_index=True,use_container_width=True)
        if st.button('Usar este candidato como nueva referencia',key='hll_similarity_use_candidate_v1'):
            st.session_state['hll_similarity_override_attempt_v1']=choice
            st.session_state.pop('hll_similarity_search_results_v1',None)
            st.rerun()
        # Load only ONE requested chart, without loading 290 OHLC files into memory.
        chart_path=root/similarity.DEFAULT_CANDLES/f'{candidate["symbol"]}_30m.csv'
        if not chart_path.is_file():
            st.warning('No está disponible el archivo de velas para este candidato: '+str(chart_path))
            return
        try:
            @st.cache_data(show_spinner=False,max_entries=12)
            def load_candidate_bars(path,mtime):
                return ohlc.load_candles(Path(path),timestamp_kind='open')[0]
            chart_bars=load_candidate_bars(str(chart_path),chart_path.stat().st_mtime_ns)
            plot,actual_pre,_=chart_for_attempt(candidate_meta(candidate),chart_bars,1.0,288,0,True)
        except (ValueError,TypeError,KeyError,OSError) as exc:
            st.warning(f'No se pudo dibujar el candidato: {exc}')
            return
        st.plotly_chart(plot,use_container_width=True,key='hll_similarity_candidate_chart_v1')
        st.caption(f'Gráfico del candidato: {actual_pre}/288 velas PRE, POST oculto. '
                   'Pivotes confirmados hasta su propio corte. '
                   'Agrupación de episodios: heurística por nivel 24/96.')
