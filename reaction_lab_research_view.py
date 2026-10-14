"""Read-only cohort-wide research explorer built from scanner/context/path SQLite.

Legacy research engines remain separate.  Context values are frozen as-of known_ts;
followups are later observations, never entry-time features.  This viewer does
not impersonate the historical Fast Candidate portfolio/materialization engine.
"""
from __future__ import annotations
import json
from contextlib import closing
import sqlite3
from pathlib import Path


def _readonly(path):
    return sqlite3.connect(f'file:{Path(path).resolve().as_posix()}?mode=ro',uri=True,timeout=10)


def continuous_research_frame(project, mode='Continuous V1', max_events=15000):
    import pandas as pd
    from reaction_lab_cohort_view import load_reaction_cohort
    root=Path(project)
    cohort=load_reaction_cohort(root,mode=mode)
    rows=cohort['rows'] if max_events is None else cohort['rows'][:max_events]
    if not rows:return pd.DataFrame(),cohort
    ctx_db=root/'reports/reaction_lab/causal_context_v1.sqlite3'
    contexts={};followups={};v1_regimes={};btc_contexts={}
    if ctx_db.is_file():
        with closing(_readonly(ctx_db)) as con:
            contexts={k:(state,notes,json.loads(payload)) for k,state,notes,payload in
                      con.execute('SELECT event_key,status,notes_json,context_json FROM contexts')}
            followups={k:(state,json.loads(payload)) for k,state,payload in
                       con.execute('SELECT event_key,path_state,followup_json FROM followups')}
            # Optional, append-only canonical V1 enrichment. Existing DBs remain readable.
            tables={name for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'v1_regime_events' in tables:
                v1_regimes={k:(state,json.loads(payload)) for k,state,payload in
                    con.execute('SELECT event_key,status,metrics_json FROM v1_regime_events')}
    # Independent prospective BTC sidecar. It does not rewrite the core
    # causal-context JSON or retroactively label old REACTIONs.
    btc_db=root/'reports/reaction_lab/btc_context_v1.sqlite3'
    if btc_db.is_file():
        with closing(_readonly(btc_db)) as con:
            btc_contexts={key:(state,json.loads(payload)) for key,state,payload in
                          con.execute('SELECT event_key,status,features_json FROM btc_events')}
    all_rows=[]
    protected={'event_key','symbol','signal','known_ts','retest_timestamp','observation_type','cohort'}
    for row in rows:
        record=dict(row);key=row['event_key']
        state,notes,ctx=contexts.get(key,('NO_CONTEXT','[]',{}))
        record['context_state']=state
        record['context_notes']=notes
        record.update({field:value for field,value in ctx.items() if field not in protected})
        regime_status,regime=v1_regimes.get(key,('PENDING',{}))
        record['v1_regime_status']=regime_status
        # Keep precision/lineage of already captured core fields; fill only
        # missing optional V1 metrics. No future outcomes enter causal context.
        for field,value in regime.items():
            if field not in protected and (field not in record or record[field] is None):
                record[field]=value
        if regime_status in ('READY','SECTOR_UNAVAILABLE'):
            record['sector_context_available']=regime_status=='READY'
            record['sector_context_status']='READY' if regime_status=='READY' else 'SECTOR_UNAVAILABLE'
        btc_state,btc_metrics=btc_contexts.get(key,('NOT_CAPTURED',{}))
        record['btc_context_status']=btc_state
        # BTC feature names are namespaced and frozen at known_ts. No outcomes
        # from the runner path are used here, and existing core context wins.
        for field,value in btc_metrics.items():
            if field not in protected and (field not in record or record[field] is None):
                record[field]=value
        _state,followup=followups.get(key,(None,{}))
        record.update(followup)
        all_rows.append(record)
    frame=pd.DataFrame(all_rows)
    return frame,cohort


# Research runner exports read the causal context already persisted in
# causal_context_v1.sqlite3 and canonical V1 regime sidecars. They NEVER
# recalculate these fields from current Redis or label outcomes as entry inputs.
RUNNER_ID_FIELDS = (
    'event_key', 'cohort', 'symbol', 'signal', 'observation_type',
    'retest_timestamp', 'known_ts', 'pivot_timestamp', 'actionable_timestamp',
    'departure_timestamp', 'swing_price', 'retest_close', 'retest_price',
    'max_departure_pct', 'retest_distance_pct', 'context_state',
    'v1_regime_status', 'sector_context_status', 'btc_context_status', 'context_notes',
    'path_state', 'path_bars', 'runner_path_state',
)
RUNNER_CAUSAL_FIELDS = (
    'nearest_opposing_room_pct', 'nearest_opposing_swing_tf',
    'nearest_opposing_swing_price', 'nearest_opposing_swing_actionable_timestamp',
    'opposing_room_30m_pct', 'opposing_room_1h_pct', 'opposing_room_4h_pct',
    'same_swing_dist_30m_pct', 'same_swing_dist_1h_pct', 'same_swing_dist_4h_pct',
    'nearest_htf_same_swing_pct', 'htf_confluence_count_0_50',
    'rsi14_1m', 'rsi14_5m', 'rsi14_15m', 'rsi14_1h',
    'aligned_rsi_extreme_count',
    'rel_volume_1m', 'rel_volume_5m', 'rel_volume_15m',
    'relative_volume_4h', 'quote_volume_4h', 'median_quote_volume_4h',
    'return_pct_1h', 'return_pct_4h',
    'btc_return_pct_1h', 'btc_return_pct_4h',
    'btc_return_delta_1h', 'btc_return_delta_4h',
    'symbol_strength_vs_btc_1h', 'symbol_strength_vs_btc_4h',
    'side_adjusted_strength_vs_btc_1h', 'side_adjusted_strength_vs_btc_4h',
    'market_breadth_1h', 'market_breadth_4h',
    'breadth_delta_1h', 'breadth_delta_4h',
    'breadth_direction_1h', 'breadth_direction_4h',
    'positive_symbols_1h', 'positive_symbols_4h',
    'market_coverage_1h', 'market_coverage_4h',
    'market_median_return_pct_1h', 'market_median_return_pct_4h',
    'Market alignment', 'Market alignment 1h', 'Market alignment 4h',
    'flow_1h_candle_timestamp', 'flow_4h_candle_timestamp',
    'primary_sector', 'sector', 'return_rank_pct_4h', 'volume_rank_pct_4h',
    'sector_return_pct_4h', 'sector_breadth_4h',
    'sector_relative_volume_4h', 'sector_return_rank_pct_4h',
    'sector_strength_vs_btc_4h', 'symbol_strength_vs_sector_4h',
    'sector_coverage_pct', 'sector_valid_symbols', 'sector_configured_symbols',
    'market_context_available', 'core_context_available',
    'sector_context_available', 'flow_candle_timestamp',
    'context_schema_version', 'context_helper_fingerprint',
    'btc_corr_5m_1h', 'btc_corr_5m_4h', 'btc_corr_5m_24h',
    'btc_corr_5m', 'btc_corr_15m', 'btc_corr_30m', 'btc_corr_1h', 'btc_corr_4h',
    'btc_beta_5m_4h', 'btc_beta_15m', 'btc_beta_1h', 'btc_beta_4h',
    'btc_r2_5m_4h', 'btc_r2_15m', 'btc_r2_1h', 'btc_r2_4h',
    'btc_residual_move_15m_pct', 'btc_residual_move_1h_pct', 'btc_residual_move_4h_pct',
    'btc_velocity_15m', 'btc_velocity_1h',
    'btc_signed_move_15m_pct', 'btc_signed_move_1h_pct',
    'btc_direction_alignment', 'btc_velocity_state',
    'btc_trade_alignment', 'btc_trade_risk_state', 'btc_relationship_label',
    'btc_context_known_ts', 'btc_context_source_hash',
)
RUNNER_OUTCOME_FIELDS = (
    'peak_run_pct', 'reaction_mfe_pct', 'runner_observed_min',
    'reaction_observed_min', 'runner_max_adverse_pct',
    'threshold_cross_ts', 'runner_peak_ts',
    'mfe_1m_pct', 'mfe_3m_pct', 'mfe_5m_pct', 'mfe_15m_pct',
    'mfe_30m_pct', 'mfe_60m_pct', 'mfe_120m_pct',
    'mfe_180m_pct', 'mfe_360m_pct',
    'mae_1m_pct', 'mae_3m_pct', 'mae_5m_pct', 'mae_15m_pct',
    'mae_30m_pct', 'mae_60m_pct', 'mae_120m_pct',
    'mae_180m_pct', 'mae_360m_pct',
    'tp_0_5_sl_3_h180_outcome', 'tp_0_5_sl_3_h180_exit_ts',
    'tp_sl_same_bar_ambiguous',
)


def runner_research_export_frame(frame):
    """One row per Event ID, fully flattened, no arbitrary UI batch limit.

    Causal measurements and future outcomes are preserved in distinctly ordered
    groups. Missing values stay missing. Unknown/nested fields are serialized
    as JSON rather than silently discarded. Caller has already applied the
    requested cohort/side/runner filters and loaded the frozen V1 sidecar.
    """
    import pandas as pd
    if frame is None or frame.empty:
        return pd.DataFrame()
    if 'event_key' not in frame.columns:
        raise ValueError('Runner research export needs Event IDs')
    if frame['event_key'].duplicated().any():
        raise ValueError('Runner research export contains duplicated Event IDs')
    preferred = RUNNER_ID_FIELDS + RUNNER_CAUSAL_FIELDS + RUNNER_OUTCOME_FIELDS
    columns = [key for key in dict.fromkeys(preferred) if key in frame.columns]
    columns += [key for key in frame.columns if key not in columns]
    out = frame.loc[:, columns].copy()
    for key in out.columns:
        if out[key].map(lambda value: isinstance(value, (list, tuple, dict))).any():
            out[key] = out[key].map(
                lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True)
                if isinstance(value, (list, tuple, dict)) else value
            )
    return out.reset_index(drop=True)


def runner_context_table_columns(frame):
    """Compact in-app context comparison; full export retains all fields."""
    fields = (
        'symbol', 'signal', 'peak_run_pct', 'runner_observed_min',
        'context_state', 'v1_regime_status', 'btc_context_status',
        'btc_corr_5m_4h', 'btc_beta_5m_4h', 'btc_r2_5m_4h',
        'btc_velocity_15m', 'btc_velocity_1h', 'btc_velocity_state',
        'btc_trade_risk_state', 'btc_relationship_label',
        'nearest_opposing_room_pct',
        'opposing_room_30m_pct', 'opposing_room_1h_pct', 'opposing_room_4h_pct',
        'side_adjusted_strength_vs_btc_1h', 'side_adjusted_strength_vs_btc_4h',
        'btc_return_pct_1h', 'btc_return_pct_4h',
        'market_breadth_1h', 'market_breadth_4h',
        'breadth_delta_1h', 'breadth_delta_4h',
        'Market alignment 1h', 'Market alignment 4h',
        'relative_volume_4h', 'return_rank_pct_4h', 'volume_rank_pct_4h',
        'primary_sector', 'sector_strength_vs_btc_4h',
        'threshold_cross_ts', 'runner_peak_ts', 'event_key',
    )
    return [field for field in fields if field in frame.columns]


def selected_event_path(project,key):
    from reaction_lab_auto_paths import read_auto_paths_dataframe
    path=Path(project)/'reports/reaction_lab/auto_paths_v1.sqlite3'
    return read_auto_paths_dataframe(path,event_keys={key})



# Continuous inspector uses the original, immutable event and OHLC snapshots.
# It does not run Redis backtests or change the Legacy inspector.
def _valid_number(value):
    import math
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def persisted_room_fields(row):
    """Use frozen causally available HTF fields; never recompute from live Redis."""
    price = _valid_number(row.get('retest_close'))
    if price is None:
        price = _valid_number(row.get('retest_price'))
    swing_tf = row.get('nearest_opposing_swing_tf')
    if not isinstance(swing_tf, str) or not swing_tf.strip():
        swing_tf = None
    rsi_ready = all(_valid_number(row.get('rsi14_' + tf)) is not None
                    for tf in ('1m', '5m', '15m', '1h'))
    return {
        'reaction_price': price,
        'room_pct': _valid_number(row.get('nearest_opposing_room_pct')),
        'swing_tf': swing_tf,
        'swing_price': _valid_number(row.get('nearest_opposing_swing_price')),
        'swing_confirmed': _valid_number(row.get('nearest_opposing_swing_confirmed_timestamp')),
        'swing_actionable': _valid_number(row.get('nearest_opposing_swing_actionable_timestamp')),
        'aligned_rsi_count': (_valid_number(row.get('aligned_rsi_extreme_count')) if rsi_ready else None),
    }


def frozen_closed_15m(candles, known_ts, limit):
    """Only snapshot 15m candles that were FULLY closed at event-known time."""
    import pandas as pd
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(candles)
    required = {'timestamp', 'open', 'high', 'low', 'close'}
    if not required.issubset(df.columns):
        return pd.DataFrame()
    for col in required:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    known = _valid_number(known_ts)
    if known is None:
        return pd.DataFrame()
    return (
        df.dropna(subset=list(required))
        .loc[lambda x: x['timestamp'] + 900_000 <= known]
        .sort_values('timestamp')
        .drop_duplicates(subset=['timestamp'], keep='last')
        .tail(max(1, int(limit)))
        .reset_index(drop=True)
    )


def persisted_retest_markers(row):
    """Markers saved by the canonical structural scanner (no synthetic pivots)."""
    result = []
    for label, timestamp_field, price_fields in (
        ('Pivot', 'pivot_timestamp', ('swing_price', 'pivot_price')),
        ('Confirmed', 'actionable_timestamp', ('entry_price', 'swing_price')),
        ('Departure', 'departure_timestamp', ('departure_price',)),
        ('REACTION', 'retest_timestamp', ('retest_price', 'retest_close')),
    ):
        stamp = _valid_number(row.get(timestamp_field))
        price = next((_valid_number(row.get(k)) for k in price_fields
                      if _valid_number(row.get(k)) is not None), None)
        if stamp is not None and price is not None:
            result.append((label, int(stamp), price))
    return result



def render_cohort_research(project,cohort_mode,view_name,symbols,*,shared_retest_inspector=None,cohort_timezone='UTC'):
    import pandas as pd
    import streamlit as st
    import plotly.graph_objects as go
    df,cohort=continuous_research_frame(
        project,cohort_mode,
        max_events=None if 'REACTION / Retests' in view_name else 15000,
    )
    st.subheader(f'{view_name} · {cohort_mode}')
    st.caption('Un mismo conjunto de Event IDs; sin mezclar el contexto causal con los resultados posteriores.')
    for error in cohort['errors']:
        st.warning('Cohort audit: '+str(error))
    if df.empty:
        st.info('No hay eventos persistidos en esta cohorte.');return
    col1,col2,col3,col4=st.columns(4)
    col1.metric('REACTIONs',len(df))
    col2.metric('Contexto capturado',int(df['context_state'].isin(['READY','PARTIAL']).sum()))
    col3.metric('Paths completos',int(df['path_state'].eq('COMPLETE').sum()))
    col4.metric('Live observed',int(df['observation_type'].eq('LIVE_OBSERVED').sum()))
    st.caption(f"Régimen V1 canónico: {int(df['v1_regime_status'].eq('READY').sum())} completo · "
               f"{int(df['v1_regime_status'].eq('SECTOR_UNAVAILABLE').sum())} sin sector · "
               f"{int(df['v1_regime_status'].eq('PENDING').sum())} pendiente. "
               'Valores de rango/sector calculados as-of con velas 4h cerradas, no observados en vivo.')
    a,b,c=st.columns(3)
    with a: sel_symbol=st.selectbox('Símbolo',['Todos']+sorted(df['symbol'].dropna().unique().tolist()),key='cohort_global_symbol')
    with b:sel_side=st.selectbox('Dirección',['Todos','LONG','SHORT'],key='cohort_global_side')
    with c:sel_status=st.selectbox('Contexto',['Todos','READY','PARTIAL','NO_CONTEXT','ERROR'],key='cohort_global_context_status')
    if sel_symbol!='Todos':df=df[df['symbol'].eq(sel_symbol)]
    if sel_side!='Todos':df=df[df['signal'].eq(sel_side)]
    if sel_status!='Todos':df=df[df['context_state'].eq(sel_status)]
    if df.empty:
        st.info('Sin coincidencias.');return

    if 'REACTION / Retests' in view_name and cohort_mode != 'Legacy':
        # Preserve the full Base cohort BEFORE applying the runner-only filter.
        from reaction_lab_runner_analyzer import render_runner_analyzer
        render_runner_analyzer(project, df.copy())

    if 'REACTION / Retests' in view_name:
        # Exactly Legacy's observable post-REACTION MFE semantics, but using
        # the Continuous 1m archive (up to 360 closed bars), not live Redis.
        # These values describe FUTURE development, not causal entry features.
        from reaction_lab_unified_adapter import (
            attach_observed_reaction_runs, sort_retests_by_run,
        )
        from reaction_lab_unified_adapter import read_persisted_runners
        only_runners=st.checkbox(
            'Solo runners históricos persistidos (run > 1%)',
            value=True,key='cohort_continuous_runners_only',
            help='No depende de la ventana ni del lote visible. MFE posterior al retest, no señal causal.'
        )
        if only_runners:
            archive=read_persisted_runners(project)
            if archive.empty:
                st.info('Todavía no hay runners archivados. Ejecutá primero reaction_lab_runners.py --once y validá su SQLite.')
                return
            min_run=st.number_input('Recorrido mínimo observado (%)',
                                    min_value=1.0,max_value=100.0,value=1.0,step=0.5,
                                    key='cohort_runner_min_run')
            df=df.merge(archive,on='event_key',how='inner',validate='one_to_one')
            df=df.loc[df['peak_run_pct']>float(min_run)].copy()
            if df.empty:
                st.info('La selección actual no contiene runners >1% en el archivo permanente.')
                return
            df['reaction_mfe_pct']=df['peak_run_pct']
            df['reaction_observed_min']=df['runner_observed_min']
            df=sort_retests_by_run(df)
            st.caption(f'Archivo permanente: {len(df)} runners en los filtros actuales. '
                       f'Con run >=2%: {int(df["peak_run_pct"].ge(2).sum())}. '
                       'El máximo observado se actualiza hasta completar el path 1m; '
                       'el registro no desaparece tras 360 minutos.')
        else:
            df = sort_retests_by_run(attach_observed_reaction_runs(project, df))

    if 'Candidate Research' in view_name:
        st.markdown('#### Candidate V1/V2 — contexto y variantes')
        st.caption('Elegibilidad de investigación basada en campos disponibles; NO constituye ejecución ni portfolio congelado.')
        if 'side_adjusted_strength_vs_btc_4h' in df:
            threshold=st.number_input('Strong Strength 4h ≥ %',value=0.50,step=0.10,key='cohort_strength_threshold')
            strength=pd.to_numeric(df['side_adjusted_strength_vs_btc_4h'],errors='coerce')
            flow=df.get('Market alignment',pd.Series('UNKNOWN',index=df.index)).astype(str)
            variants=pd.DataFrame({'Variant':['Base','Strength > 0','Strong Strength','Strength + Flow'],
                                   'Events':[len(df),int((strength>0).sum()),
                                             int((strength>=threshold).sum()),
                                             int(((strength>0)&flow.eq('TAILWIND')).sum())]})
            st.dataframe(variants,hide_index=True,use_container_width=True)
        else:st.warning('Strength todavía no fue capturada para los eventos seleccionados.')
        for name,col in [('HTF Room','nearest_opposing_room_pct'),('Strength 4h','side_adjusted_strength_vs_btc_4h'),
                         ('Breadth 4h','market_breadth_4h'),('Breadth 1h','market_breadth_1h')]:
            if col in df:
                vals=pd.to_numeric(df[col],errors='coerce')
                if vals.notna().any():st.caption(f'{name}: {vals.notna().sum()} con dato · mediana {vals.median():.3f}')
    elif 'REACTION / Retests' in view_name:
        st.markdown('#### Retests persistidos')
        st.caption('Scanner continuo fijo: 15m 3x3, retest 0.10%, departure 0.20%. Otros detectores siguen siendo estudios independientes.')
        st.caption('Orden: mayor recorrido favorable observado desde el cierre de la REACTION (run/MFE post-retest). '
                   'No confundir con departure previo al retest ni con rendimiento de una operación. '
                   'Mide hasta 360 minutos de path 1m archivado; los eventos recientes pueden crecer y '
                   'los que no tienen velas consecutivas válidas aparecen al final sin un porcentaje inventado.')
    elif 'Pivots / Confirmations' in view_name:
        st.markdown('#### Swing → confirmación → retest')
        st.caption('Timestamps causales congelados por Event ID. No incluye todos los pivots sin retest.')
    elif 'Event swing structure' in view_name:
        st.markdown('#### Estructura y HTF Room causal')
    elif 'Volume' in view_name:
        st.markdown('#### Relación con volumen y RSI')
        st.caption('El universo Volume Exhaustion independiente no se redefine: se estudia su relación con REACTIONs de esta cohorte.')
    else:st.markdown('#### Registro auditado de REACTIONs')

    def columns_for_mode():
        base=['cohort','symbol','signal','retest_timestamp','observation_type','path_state','path_bars','context_state','v1_regime_status']
        if 'Candidate Research' in view_name:
            extra=['nearest_opposing_room_pct','side_adjusted_strength_vs_btc_4h','market_breadth_4h',
                   'relative_volume_4h','return_rank_pct_4h','volume_rank_pct_4h',
                   'sector_strength_vs_btc_4h','sector_relative_volume_4h',
                   'market_breadth_1h','Market alignment','mfe_180m_pct','tp_0_5_sl_3_h180_outcome']
        elif 'Pivots' in view_name: extra=['pivot_timestamp','actionable_timestamp','pivot_price','departure_timestamp','retest_price']
        elif 'Event swing' in view_name: extra=['nearest_opposing_room_pct','opposing_room_30m_pct','opposing_room_1h_pct','opposing_room_4h_pct','htf_confluence_count_0_50']
        elif 'Volume' in view_name: extra=['rel_volume_1m','rel_volume_5m','rel_volume_15m','rsi14_1m','rsi14_15m','aligned_rsi_extreme_count']
        else:extra=['reaction_mfe_pct','reaction_observed_min','pivot_timestamp','swing_price','max_departure_pct','retest_distance_pct','mfe_180m_pct']
        return [c for c in base+extra if c in df.columns]
    display_cols=columns_for_mode()
    if 'REACTION / Retests' in view_name:
        display_cols += [col for col in (
            'peak_run_pct','threshold_cross_ts','runner_peak_ts',
            'runner_max_adverse_pct','runner_path_state'
        ) if col in df.columns and col not in display_cols]
    if 'REACTION / Retests' in view_name and only_runners:
        # Both the in-app table and the full CSV use the already frozen context
        # and V1 sector sidecar joined by Event ID in continuous_research_frame.
        # The path archive is NOT reread or truncated for this export.
        display_cols += [col for col in runner_context_table_columns(df)
                         if col not in display_cols]
        with st.expander('Investigación de runners · Room / BTC / Breadth / Flow / Sectores', expanded=False):
            st.caption('Variables causales congeladas a known_ts. Run, MFE/MAE y TP/SL son '
                       'observaciones posteriores, nunca filtros conocidos al entrar. '
                       'PARTIAL y SECTOR_UNAVAILABLE permanecen en el estudio como valores faltantes.')
            st.dataframe(df[runner_context_table_columns(df)],
                         hide_index=True, use_container_width=True)
            complete = int(df['context_state'].eq('READY').sum())
            regime_ready = int(df['v1_regime_status'].eq('READY').sum())
            st.caption(f'Contexto causal READY: {complete}/{len(df)} · '
                       f'Régimen sectorial READY: {regime_ready}/{len(df)}')
        export = runner_research_export_frame(df)
        st.download_button(
            'Exportar runners con TODO el contexto y outcomes (CSV)',
            data=export.to_csv(index=False).encode('utf-8-sig'),
            file_name='continuous_v1_runners_full_context.csv',
            mime='text/csv',
            key='cohort_runners_full_context_export',
            help='Todas las filas filtradas; no se recortan por los lotes del scanner. '
                 'Los valores sin evidencia siguen vacíos. Incluye Event ID para auditorías.'
        )
    # Streamlit virtualizes rows; never truncate the runner archive to the latest batch.
    st.dataframe(df[display_cols] if 'REACTION / Retests' in view_name else df[display_cols].head(2500),
                 hide_index=True,use_container_width=True)
    keys=df['event_key'].astype(str).tolist()
    is_retest_view = 'REACTION / Retests' in view_name
    if is_retest_view:
        # Reuse the EXACT same Legacy component, never a look-alike inspector.
        from reaction_lab_unified_adapter import (
            inspector_rows, frozen_room, combined_archived_15m,
            archived_event_candles, load_selected_path,
        )
        if shared_retest_inspector is None:
            st.error('Falta el inspector canónico compartido de app.py; no se dibuja un sustituto.')
            return
        original=inspector_rows(df, tz=cohort_timezone)
        chartable=original.dropna(subset=[
            'pivot_timestamp','actionable_timestamp','departure_timestamp',
            'swing_price','departure_price','retest_price','retest_timestamp',
        ]).copy()
        if chartable.empty:
            st.warning('Eventos presentes, pero sin timestamps/precios suficientes para el inspector.')
            return
        st.caption(
            'Inspector original compartido con Legacy. La tabla muestra toda la '
            'cohorte; el gráfico requiere identidad estructural completa. '
            'Los análisis Legacy sin métricas comparables todavía no se ejecutan '
            'sobre resultados parciales.'
        )
        def _candles(event, limit):
            eid=str(event['event_key'])
            stored=archived_event_candles(project,eid)
            path=load_selected_path(project,eid)
            return combined_archived_15m(
                stored.get('15m',[]),path,event.get('known_ts'),limit
            )
        selected_event=shared_retest_inspector(
            chartable, candles_provider=_candles,
            room_provider=frozen_room,ui_key_prefix='continuous_',
        )
        if selected_event is None:return
        selected=str(selected_event['event_key'])
        row=df.loc[df['event_key'].eq(selected)].iloc[0]
        st.caption('REACTION Event ID: '+selected)
    else:
        selected = st.selectbox('Inspeccionar Event ID', keys,
                                key='cohort_global_event_inspector')
        row=df.loc[df['event_key'].eq(selected)].iloc[0]
    context_db=Path(project)/'reports/reaction_lab/causal_context_v1.sqlite3'
    recorded={}
    if context_db.is_file():
        with closing(_readonly(context_db)) as con:
            candle_row=con.execute('SELECT candles_json FROM contexts WHERE event_key=?',(selected,)).fetchone()
        if candle_row:
            recorded=json.loads(candle_row[0])
    with st.expander('Contexto completo, calidad y outcomes',expanded=not is_retest_view):
        value=row.to_dict()
        st.json({str(k):None if not isinstance(v,(list,dict)) and pd.isna(v) else v
                 for k,v in value.items() if not isinstance(v,(list,dict))})
        if row.get('context_state')!='READY':
            st.warning('Contexto no completamente validado. Las métricas ausentes no deben tratarse como ceros.')
    # Event-level deep research: available for EVERY Continuous Base REACTION,
    # independently of runner outcome. No mutation of existing research data.
    if 'REACTION / Retests' in view_name and cohort_mode != 'Legacy':
        try:
            from reaction_lab_deep_collector import load_deep_frame, load_deep_outcomes
            deep_event=load_deep_frame(project,[selected])
            with st.expander('🧬 Deep Research · departure, retest, volatilidad, flujo y ejecución',expanded=False):
                if deep_event.empty:
                    st.info('Este Event ID es anterior a la captura Deep Research, o está pendiente. '
                            'No completamos variables históricas usando el mercado actual.')
                else:
                    deep_values=deep_event.iloc[0].to_dict()
                    st.caption('Contexto causal congelado al known_ts. '
                               'Las fuentes externas faltantes se muestran como nulas, no como cero.')
                    groups={
                        'Departure y retest': ('departure_','retest_'),
                        'ATR y volatilidad': ('atr_','realized_vol_','retest_volatility_shock'),
                        'Breadth, Flow y densidad': ('breadth_','deep_market_','reaction_density_','sector_reaction_'),
                        'Open Interest, agresores y liquidez': ('oi_','funding_','taker_','cvd_','orderbook_','liquidations_','telemetry_'),
                    }
                    for label,prefixes in groups.items():
                        values={k:v for k,v in deep_values.items() if k.startswith(prefixes)}
                        if values:
                            st.markdown('**'+label+'**')
                            st.json({k:(None if pd.isna(v) else v) for k,v in values.items()})
                    if deep_values.get('microstructure_status') != 'OBSERVED':
                        st.warning('Microestructura incompleta: OI/CVD/funding/libro/liquidaciones '
                                   'pueden carecer de una fuente observada antes de la señal.')
                    horizons=st.selectbox('Outcome operativo posterior (minutos)',
                        [30,60,120,180,360],index=3,key='cohort_deep_outcome_horizon')
                    result=load_deep_outcomes(project,[selected],int(horizons))
                    if result.empty:
                        st.info('El horizonte todavía no tiene path 1m completo, '
                                'o el recolector Deep Research aún no actualizó este outcome.')
                    else:
                        val=result.iloc[0].to_dict()
                        st.json({k:v for k,v in val.items() if k!='event_key'})
                        st.caption('La simulación usa OPEN siguiente y orden SL primero si TP/SL '
                                   'se tocan en la misma vela. No supone ejecución real ni incluye fees.')
        except ImportError:
            pass
    # Exact as-of HTF evidence, independent of Redis. 15m uses the
    # dedicated inspector above in REACTION / Retests; other modes unchanged.
    if recorded:
            available=[tf for tf in ('15m','30m','1h','4h','5m','1m')
                       if recorded.get(tf) and not (is_retest_view and tf=='15m')]
            if available:
                active_tf=st.selectbox('Velas históricas congeladas · contexto as-of',available,key='cohort_global_htf_chart')
                historic=pd.DataFrame(recorded[active_tf])
                if {'timestamp','open','high','low','close'}.issubset(historic.columns):
                    times=pd.to_datetime(historic['timestamp'],unit='ms',utc=True)
                    chart=go.Figure(data=go.Candlestick(x=times,open=historic['open'],high=historic['high'],
                               low=historic['low'],close=historic['close'],name=active_tf))
                    for label,field in [('Pivot','pivot_timestamp'),('Confirmación','actionable_timestamp'),
                                        ('Retest','retest_timestamp')]:
                        v=pd.to_numeric(row.get(field),errors='coerce')
                        if pd.notna(v):
                            dt=pd.to_datetime(int(v),unit='ms',utc=True)
                            if not historic.empty and int(historic['timestamp'].min())<=int(v)<=int(historic['timestamp'].max())+4*3600000:
                                chart.add_shape(type='line',x0=dt.isoformat(),x1=dt.isoformat(),yref='paper',y0=0,y1=1,line=dict(dash='dot',width=1))
                    chart.update_layout(height=430,xaxis_rangeslider_visible=False)
                    st.plotly_chart(chart,use_container_width=True,key='cohort_global_htf_plot')
                    st.caption(f'Imagen causal: {len(historic)} velas {active_tf}, solo cierres anteriores al conocimiento de la REACTION.')

    path=selected_event_path(project,selected)
    if not path.empty:
        path=path.sort_values('timestamp')
        plot=go.Figure(data=go.Candlestick(x=pd.to_datetime(path['timestamp'],unit='ms',utc=True),
                        open=path['open'],high=path['high'],low=path['low'],close=path['close'],name='Path 1m'))
        plot.update_layout(height=430,xaxis_rangeslider_visible=False)
        st.plotly_chart(plot,use_container_width=True)
        st.caption(f'{len(path)} velas 1m persistidas de este Event ID.')
    else:st.info('Este evento todavía no tiene velas 1m persistidas.')
