"""Read-only V2.9.6 entry-level audit panel for the existing HLL Streamlit inspector."""
from pathlib import Path
import pandas as pd

from dashboard.views.hll_v296_entry_audit import OUTPUT, ROUTES, PRESPEC


def render_v296_audit(root:Path, selection:pd.DataFrame):
    import streamlit as st
    root=Path(root)
    folder=root/OUTPUT
    if not (folder/'manifest.json').is_file():
        st.caption('Auditoría por operación V2.9.6 sin generar. '
                   'Ejecutá `python3 dashboard/views/hll_v296_entry_audit.py --run` para crearla.')
        return
    with st.expander('V2.9.6 · Auditoría individual de entradas y comparaciones predefinidas',expanded=False):
        st.caption('Una fila por intento/ruta verificada y 25 desenlaces por entrada/horizonte. '
                   'Resultados descriptivos, no una cartera ni un holdout externo.')
        import json
        manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
        st.caption(f"Cohorte congelada: {manifest['scope']} · "
                   f"{manifest['selected_attempts']} intentos · "
                   f"{manifest['verified_entries']} entradas verificadas · "
                   f"{manifest['scenario_rows']} escenarios · "
                   f"coste total {manifest['commission_roundtrip_pct']+manifest['slippage_roundtrip_pct']:.2f}%")
        if st.button('Cargar auditoría por entrada V2.9.6',key='hll296_load'):
            st.session_state['hll296_load_requested']=True
        if not st.session_state.get('hll296_load_requested'):return
        entries=pd.read_csv(folder/'entry_events.csv',low_memory=False)
        detail=pd.read_csv(folder/'entry_scenarios_25.csv',low_memory=False)
        coverage=pd.read_csv(folder/'attempt_route_coverage.csv',low_memory=False)
        if entries.duplicated(['attempt_id','route']).any() or detail.duplicated(
                ['entry_id','horizon_min','tp_pct','sl_pct']).any():
            st.error('Auditoría V2.9.6 contiene duplicados; revisá la corrida.')
            return
        ids=set(selection.attempt_id.astype(str))
        relevant_coverage=coverage[coverage.attempt_id.astype(str).isin(ids)]
        subset=entries[entries.attempt_id.astype(str).isin(ids)]
        st.markdown('**Cobertura según selección visible**')
        counts=relevant_coverage.status.value_counts()
        attempts_with_entry=set(subset.attempt_id.astype(str))
        total_attempts=int(relevant_coverage.attempt_id.nunique())
        unverified=int(relevant_coverage.status.ne('NO_SIGNAL').sum()-len(subset))
        c1,c2,c3,c4=st.columns(4)
        c1.metric('Intentos con entrada',len(attempts_with_entry))
        c2.metric('Intentos sin entrada',total_attempts-len(attempts_with_entry))
        c3.metric('Entradas verificadas',len(subset))
        c4.metric('Señales sin OPEN',unverified)
        st.caption(f"{int(counts.get('NO_SIGNAL',0))} combinaciones intento × ruta sin confirmación; "
                   'una estructura puede tener más de una entrada, nunca se cuentan como intentos distintos.')
        if subset.empty:
            st.info('No hay entradas V2.9.6 en los intentos seleccionados. '
                    'La corrida por defecto contiene solo los primeros intentos por nivel.')
            return
        route=st.selectbox('Ruta auditada',list(ROUTES),key='hll296_route')
        horizons=sorted(set(int(x) for x in manifest['horizons_min']))
        horizon=st.selectbox('Horizonte V2.9.6 (minutos)',horizons,
                             format_func=lambda n:f'{n//60} horas',key='hll296_horizon')
        tp,sl=st.selectbox('Escenario fijado antes de comparar',list(PRESPEC),
                            format_func=lambda x:f'TP +{x[0]}% / SL −{x[1]}%',
                            key='hll296_scenario')
        selected=subset[subset.route.eq(route)]
        records=detail[(detail.entry_id.isin(selected.entry_id))&detail.route.eq(route)&
            detail.horizon_min.eq(horizon)&detail.tp_pct.eq(tp)&detail.sl_pct.eq(sl)].copy()
        if len(records)!=len(selected):
            st.error('Faltan filas del escenario para una o más entradas verificadas.')
            return
        wanted=['symbol','entry_utc','attempt_id','entry_id','route','mechanism_pre_48',
                'pre_pattern_48','entry_price','tp_pct','sl_pct','tp_price','sl_price',
                'result','path_state','first_hit_min','first_hit_minute_utc',
                'exit_minute_utc','minutes_to_exit','gross_pct','cost_roundtrip_pct',
                'net_pct','entry_month','symbol_day']
        st.dataframe(records[[x for x in wanted if x in records]].sort_values('entry_utc'),
                     hide_index=True,use_container_width=True)
        st.download_button('Descargar estas operaciones verificables (CSV)',
                           records.to_csv(index=False).encode('utf-8'),
                           'hll_v296_operaciones_individuales.csv','text/csv',key='hll296_download')
        st.markdown('**Resultados por mes (sin elegir la mejor celda)**')
        known=records[records.result.isin(('TP_FIRST','SL_FIRST','TIMEOUT'))]
        by_month=records.groupby('entry_month').agg(entradas=('entry_id','nunique'),
                          simbolos=('symbol','nunique')).reset_index()
        if not known.empty:
            av=known.groupby('entry_month').agg(evaluables=('entry_id','nunique'),
                neto_medio_solo_evaluables_pct=('net_pct','mean')).reset_index()
            by_month=by_month.merge(av,on='entry_month',how='left')
        else:
            by_month['evaluables']=0;by_month['neto_medio_solo_evaluables_pct']=float('nan')
        by_month['censuradas_o_ambiguas']=by_month.entradas-by_month.evaluables.fillna(0).astype(int)
        st.dataframe(by_month,hide_index=True,use_container_width=True)
        st.warning('Solo retornos de trayectoria conocida: las censuras pueden cambiar los promedios. '
                   'TP/SL son fills teóricos al umbral; no se incluye slippage variable, financiación, '
                   'concurrencia ni ejecución real. La clasificación PRE y las señales no se modifican.')
