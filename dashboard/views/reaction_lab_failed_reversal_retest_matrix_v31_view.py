"""Read-only 6x5 TP/SL matrix for FAILED V3, embedded in Runner Discovery."""
from __future__ import annotations

import json
from pathlib import Path

from reaction_lab_failed_reversal_retest_matrix_v31 import (
    SCHEMA, TARGETS, STOPS, matrix_dir, stop_dir,
)
from reaction_lab_failed_reversal_retest_v3 import MODES, DEFAULT_DEPTHS
from dashboard.views.reaction_lab_failed_reversal_retest_v3_view import (
    MODE_LABELS, plot_reversal_retest, _filtered_rows,
)
from dashboard.views.reaction_lab_failed_reclaim_audit_v1_view import _read_selected_path


METRICS = {
    'Neto conservador por señal (%)': 'net_worst_mean_per_signal_pct',
    'Neto conservador por evento evaluable (%)': 'net_mean_per_eligible_pct',
    'Neto optimista por señal (límite superior, NO validado) (%)': 'net_best_mean_per_signal_pct',
    'TP seguros / señales (%)': 'tp_rate_signalled_pct',
}


def scenario_grid(frame, mode, depth, field):
    """Return fixed six-row/five-column matrix; do not infer missing scenarios."""
    import pandas as pd
    group = frame.loc[(frame['group_type'] == 'ALL') &
             (frame['mode'] == mode) &
             pd.to_numeric(frame['depth_pct'],errors='coerce').eq(depth)].copy()
    for col in ('target_pct','sl_pct',field):
        group[col] = pd.to_numeric(group[col], errors='coerce')
    if group.duplicated(['target_pct','sl_pct']).any():
        raise ValueError('Duplicate cells in V3.1 matrix')
    piv = group.pivot(index='target_pct', columns='sl_pct', values=field)
    piv = piv.reindex(index=TARGETS,columns=STOPS)
    if piv.isna().any().any():
        raise ValueError('Incomplete TP/SL matrix for selected mode/depth')
    return piv


def render_failed_reversal_matrix(project):
    import pandas as pd
    import streamlit as st
    import plotly.graph_objects as go
    project = Path(project).resolve()
    root = matrix_dir(project)
    st.markdown('#### ▦ FAILED V3.1 · Matriz completa TP × SL')
    st.caption('TP +1% a +6% × SL −1% a −5%; mismas reglas causales y universo congelado. '
       'La matriz contiene 420 escenarios con dos sweeps y siete entradas. '
       'No son 420 pruebas independientes ni rentabilidad de cartera.')
    mf = root/'manifest.json'
    sf = root/'summary.csv'
    if not (mf.is_file() and sf.is_file()):
        st.info('La matriz ampliada aún no fue generada. Ejecutá '
                '`reaction_lab_failed_reversal_retest_matrix_v31.py --run` en el VPS.')
        return
    try:
        manifest = json.loads(mf.read_text(encoding='utf-8'))
        if (manifest.get('schema') != SCHEMA or
            tuple(manifest.get('targets_pct',[])) != TARGETS or
            tuple(manifest.get('stops_pct',[])) != STOPS or
            manifest.get('scenario_cells') != 420):
            raise ValueError('Manifest is not the frozen full matrix')
        @st.cache_data(show_spinner=False,max_entries=3)
        def load_summary(path,stamp):
            return pd.read_csv(path,low_memory=False)
        rows = load_summary(str(sf),sf.stat().st_mtime_ns)
    except (OSError,ValueError,KeyError,pd.errors.ParserError) as exc:
        st.error(f'Matriz incompleta o ilegible: {exc}')
        return
    c1,c2,c3 = st.columns([1.4,1,2])
    mode = c1.selectbox('Regla de entrada',MODES,index=4,format_func=lambda x: MODE_LABELS[x],
                        key='fv31_mode')
    depth = c2.selectbox('Sweep adverso (%)',list(DEFAULT_DEPTHS),
                         format_func=lambda x:f'{x:g}%',key='fv31_depth')
    metric_label = c3.selectbox('Métrica del mapa',list(METRICS),key='fv31_metric')
    metric = METRICS[metric_label]
    try:
        grid = scenario_grid(rows,mode,depth,metric)
    except (ValueError,KeyError) as exc:
        st.error(f'No se puede mostrar una matriz completa: {exc}')
        return
    fig=go.Figure(go.Heatmap(
        x=[f'−{x}%' for x in STOPS], y=[f'+{x}%' for x in TARGETS],
        z=grid.values, text=[[f'{v:+.3f}%' for v in line] for line in grid.values],
        texttemplate='%{text}',textfont={'size':12},
        colorscale='RdYlGn',zmid=0,
        hovertemplate='TP %{y} · SL %{x}<br>Resultado %{z:+.3f}%<extra></extra>',
        colorbar={'title':'%'}))
    fig.update_layout(height=420,margin=dict(l=10,r=10,t=20,b=20),
                      xaxis_title='Stop Loss',yaxis_title='Take Profit')
    st.plotly_chart(fig,use_container_width=True,key='fv31_heatmap',
                    config={'displaylogo':False})
    st.caption(f"Cohorte {manifest['cohort_total']:,}, evaluables {manifest['coverage_common']:,}. "
       'Entradas límite: resultado conservador y límite superior intravela por separado. '
       'Una celda positiva no implica una ventaja fuera de muestra.')
    a,b=st.columns(2)
    tp = a.selectbox('TP para detalle',list(TARGETS),index=1,
                     format_func=lambda x:f'+{x}%',key='fv31_detail_tp')
    sl = b.selectbox('SL para detalle',list(STOPS),index=2,
                     format_func=lambda x:f'−{x}%',key='fv31_detail_sl')
    selected = rows.loc[(rows['group_type']=='ALL') &
        pd.to_numeric(rows['depth_pct'],errors='coerce').eq(depth) &
        pd.to_numeric(rows['target_pct'],errors='coerce').eq(tp) &
        pd.to_numeric(rows['sl_pct'],errors='coerce').eq(sl)]
    show = ['mode','eligible_common','signal_n','no_sweep_n','no_reversal_n',
        'no_fill_n','tp_first_n','sl_first_n','ambiguous_n','time_exit_n',
        'net_worst_mean_per_signal_pct','net_best_mean_per_signal_pct',
        'net_mean_per_eligible_pct']
    st.markdown(f'**Comparación entre entradas: TP +{tp}% / SL −{sl}% · sweep {depth:g}%**')
    st.dataframe(selected.loc[:,show],hide_index=True,use_container_width=True)
    with st.expander('Inspeccionar episodios y velas 1m de esta celda',expanded=False):
        file=stop_dir(project,sl)/'event_outcomes.csv'
        evfile=stop_dir(project,sl)/'events.csv'
        if not (file.is_file() and evfile.is_file()):
            st.warning('Faltan los eventos individuales del SL seleccionado.')
            return
        @st.cache_data(show_spinner=False,max_entries=5)
        def load_case_rows(path,stamp,mode,depth,tp):
            return _filtered_rows(Path(path),mode,depth,tp)
        @st.cache_data(show_spinner=False,max_entries=5)
        def load_events(path,stamp):
            return pd.read_csv(path,low_memory=False)
        cases = load_case_rows(str(file),file.stat().st_mtime_ns,mode,depth,tp)
        events = load_events(str(evfile),evfile.stat().st_mtime_ns)
        state = st.selectbox('Estado de la entrada',['TODOS','SIGNAL','NO_SIGNAL'],key='fv31_state')
        if state!='TODOS':
            cases=cases.loc[cases['signal_state'].eq(state)]
        st.dataframe(cases[['date_utc','symbol','side','signal_state','outcome',
            'entry_price','stop_distance_pct','net_worst_pct','net_best_pct','touch_key']].head(300),
            hide_index=True,use_container_width=True)
        st.download_button('Exportar casos de esta celda',
            cases.to_csv(index=False).encode('utf-8-sig'),
            file_name=f'failed_v31_tp{tp}_sl{sl}_{mode}_{depth:g}.csv',
            mime='text/csv',key='fv31_export')
        if cases.empty:return
        few=cases.sort_values('date_utc',ascending=False).head(150)
        keys=few['touch_key'].astype(str).tolist()
        labels={r['touch_key']:f"{r['date_utc']} · {r['symbol']} {r['side']} · {r['outcome']}"
                for _,r in few.iterrows()}
        chosen = st.selectbox('Episodio',keys,format_func=lambda x:labels.get(x,x),key='fv31_case')
        candle_count = st.select_slider('Velas POST',[60,120,180,240,360],value=240,key='fv31_window')
        if st.button('Ver gráfico verificado',key='fv31_show_plot'):
            try:
                event = events.loc[events['touch_key'].astype(str).eq(chosen)]
                case = few.loc[few['touch_key'].astype(str).eq(chosen)]
                if len(event)!=1 or len(case)!=1:
                    raise ValueError('Identidad no única')
                _,bars=_read_selected_path(project,chosen)
                chart=plot_reversal_retest(bars,event.iloc[0],case.iloc[0],candle_count)
                st.plotly_chart(chart,use_container_width=True,key='fv31_candles',
                    config={'displaylogo':False})
            except Exception as exc:
                st.error(f'Error al abrir velas verificadas: {exc}')
