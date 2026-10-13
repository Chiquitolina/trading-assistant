"""Read-only Runner Analyzer for ALL Continuous V1 Base REACTIONs.

It compares causal context to later outcomes over the SAME fully observed,
contiguous 1m horizon. Not a candidate filter, trading signal, or backtest.
BTC price-correlation indicators are used ONLY if already frozen per event.
"""
from __future__ import annotations

from contextlib import closing
import math
from pathlib import Path
import sqlite3
import zlib
import struct

MINUTE_MS = 60_000
HORIZONS = (30, 60, 120, 180, 360)
TARGETS = (1.0, 2.0, 3.0, 5.0, 10.0)

# Fixed, predeclared buckets; choosing these AFTER seeing the outcomes would
# result in exploration rather than an out-of-sample predictive test.
NUMERIC_FACTORS = {
    'HTF Room mínimo': ('nearest_opposing_room_pct', (-math.inf, .5, 1., 2., 3., 5., math.inf)),
    'HTF Room 30m': ('opposing_room_30m_pct', (-math.inf, .5, 1., 2., 3., 5., math.inf)),
    'HTF Room 1h': ('opposing_room_1h_pct', (-math.inf, .5, 1., 2., 3., 5., math.inf)),
    'HTF Room 4h': ('opposing_room_4h_pct', (-math.inf, .5, 1., 2., 3., 5., math.inf)),
    'Strength BTC 1h (direccional)': ('side_adjusted_strength_vs_btc_1h', (-math.inf, -2., -.5, 0., .5, 2., math.inf)),
    'Strength BTC 4h (direccional)': ('side_adjusted_strength_vs_btc_4h', (-math.inf, -2., -.5, 0., .5, 2., math.inf)),
    'BTC retorno 1h': ('btc_return_pct_1h', (-math.inf, -2., -.5, 0., .5, 2., math.inf)),
    'BTC retorno 4h': ('btc_return_pct_4h', (-math.inf, -2., -.5, 0., .5, 2., math.inf)),
    'Breadth 1h': ('market_breadth_1h', (-math.inf, 20., 40., 60., 80., math.inf)),
    'Breadth 4h': ('market_breadth_4h', (-math.inf, 20., 40., 60., 80., math.inf)),
    'Delta breadth 1h': ('breadth_delta_1h', (-math.inf, -20., -5., 0., 5., 20., math.inf)),
    'Delta breadth 4h': ('breadth_delta_4h', (-math.inf, -20., -5., 0., 5., 20., math.inf)),
    'Volumen relativo 1m': ('rel_volume_1m', (-math.inf, .5, 1., 2., 4., math.inf)),
    'Volumen relativo 5m': ('rel_volume_5m', (-math.inf, .5, 1., 2., 4., math.inf)),
    'Volumen relativo 15m': ('rel_volume_15m', (-math.inf, .5, 1., 2., 4., math.inf)),
    'Volumen relativo QUOTE 4h': ('relative_volume_4h', (-math.inf, .5, 1., 2., 4., math.inf)),
    'RSI 1m': ('rsi14_1m', (-math.inf, 20., 30., 40., 50., 60., 70., 80., math.inf)),
    'RSI 15m': ('rsi14_15m', (-math.inf, 20., 30., 40., 50., 60., 70., 80., math.inf)),
    'RSI 1h': ('rsi14_1h', (-math.inf, 20., 30., 40., 50., 60., 70., 80., math.inf)),
    'Rank retorno 4h': ('return_rank_pct_4h', (-math.inf, 20., 40., 60., 80., math.inf)),
    'Rank volumen 4h': ('volume_rank_pct_4h', (-math.inf, 20., 40., 60., 80., math.inf)),
    'Fuerza sector vs BTC 4h': ('sector_strength_vs_btc_4h', (-math.inf, -2., -.5, 0., .5, 2., math.inf)),
    'Volumen sector 4h': ('sector_relative_volume_4h', (-math.inf, .5, 1., 2., 4., math.inf)),
    'Departure pre-retest': ('max_departure_pct', (-math.inf, .5, 1., 2., 3., 5., math.inf)),
    'Distancia del retest al nivel': ('retest_distance_pct', (-math.inf, .025, .05, .1, .2, .5, math.inf)),
    'Edad del nivel confirmado (min)': ('confirmed_level_age_min', (-math.inf, 30., 60., 120., 240., 360., math.inf)),
    'Confluencias HTF cercanas': ('htf_confluence_count_0_50', (-math.inf, 1., 2., 3., 4., math.inf)),
    # Existing legacy BTC correlation fields: strictly read-only, never backfilled
    # from a post-event market path or the current Redis buffer.
    'BTC Corr 5m / 1h': ('btc_corr_5m_1h', (-math.inf, -.75, -.3, .3, .75, math.inf)),
    'BTC Corr 5m / 4h': ('btc_corr_5m_4h', (-math.inf, -.75, -.3, .3, .75, math.inf)),
    'BTC Corr 5m / 24h': ('btc_corr_5m_24h', (-math.inf, -.75, -.3, .3, .75, math.inf)),
    'BTC Beta 5m / 4h': ('btc_beta_5m_4h', (-math.inf, -.5, 0., .5, 1., 1.5, math.inf)),
    'BTC R² 5m / 4h': ('btc_r2_5m_4h', (-math.inf, .2, .5, .8, math.inf)),
    'BTC Corr 15m': ('btc_corr_15m', (-math.inf, -.75, -.3, .3, .75, math.inf)),
    'BTC Corr 1h': ('btc_corr_1h', (-math.inf, -.75, -.3, .3, .75, math.inf)),
    'BTC Corr 4h': ('btc_corr_4h', (-math.inf, -.75, -.3, .3, .75, math.inf)),
    'BTC Beta 1h': ('btc_beta_1h', (-math.inf, -.5, 0., .5, 1., 1.5, math.inf)),
    'BTC R² 1h': ('btc_r2_1h', (-math.inf, .2, .5, .8, math.inf)),
    'BTC residual 1h': ('btc_residual_move_1h_pct', (-math.inf, -2., -.5, 0., .5, 2., math.inf)),
    'BTC velocidad 15m (%)': ('btc_velocity_15m', (-math.inf, .2, .5, .8, 1.2, 2., math.inf)),
    'BTC velocidad 1h (%)': ('btc_velocity_1h', (-math.inf, .5, 1., 1.5, 2.5, math.inf)),
    'BTC movimiento con signo 15m': ('btc_signed_move_15m_pct', (-math.inf, -1., -.5, 0., .5, 1., math.inf)),
    'BTC movimiento con signo 1h': ('btc_signed_move_1h_pct', (-math.inf, -2., -1., 0., 1., 2., math.inf)),
    'BTC residual 15m': ('btc_residual_move_15m_pct', (-math.inf, -2., -.5, 0., .5, 2., math.inf)),
}

CATEGORICAL_FACTORS = {
    'Market Flow': 'Market alignment',
    'Flow 1h': 'Market alignment 1h',
    'Flow 4h': 'Market alignment 4h',
    'Breadth dirección 1h': 'breadth_direction_1h',
    'Breadth dirección 4h': 'breadth_direction_4h',
    'Sector': 'primary_sector',
    'Lado': 'signal',
    'Sesión UTC': 'utc_session',
    'Día de semana UTC': 'utc_weekday',
    'Tipo de observación': 'observation_type',
    'BTC relationship': 'btc_relationship_label',
    'BTC trade risk': 'btc_trade_risk_state',
    'BTC direction alignment': 'btc_direction_alignment',
    'BTC velocity state': 'btc_velocity_state',
    'BTC trade alignment': 'btc_trade_alignment',
    'BTC context status': 'btc_context_status',
}

FACTORS = {name: column for name, (column, _) in NUMERIC_FACTORS.items()}
FACTORS.update(CATEGORICAL_FACTORS)


def observed_horizon(bars, *, retest_timestamp, retest_close, side, horizon):
    """Exactly H consecutive closed 1m bars AFTER retest, with retest-close baseline.

    Invalid/gapped/truncated prefix returns None: it must NOT be a negative.
    Intrabar high/low describes reachable extremes, not necessarily executable PnL.
    """
    if horizon not in HORIZONS:
        raise ValueError('Unsupported horizon')
    try:
        baseline = float(retest_close)
        stamp = int(retest_timestamp)
    except (TypeError, ValueError, OverflowError):
        return None
    side = str(side).upper()
    if side not in ('LONG', 'SHORT') or not math.isfinite(baseline) or baseline <= 0:
        return None
    if bars is None or len(bars) < horizon:
        return None
    mfe, mae = 0.0, 0.0
    cross = {target: None for target in TARGETS}
    peak_ts = None
    for i, candle in enumerate(bars[:horizon]):
        try:
            ts, o, hi, lo, c = candle[:5]
            ts = int(ts)
            o, hi, lo, c = (float(x) for x in (o, hi, lo, c))
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if (ts != stamp + (i+1)*MINUTE_MS or
            any(not math.isfinite(v) or v <= 0 for v in (o, hi, lo, c)) or
            lo > min(o, c) or hi < max(o, c) or hi < lo):
            return None
        favor = (hi/baseline - 1)*100 if side == 'LONG' else (1-lo/baseline)*100
        against = (1-lo/baseline)*100 if side == 'LONG' else (hi/baseline-1)*100
        if favor > mfe:
            mfe, peak_ts = favor, ts
        mae = max(mae, against)
        for threshold in TARGETS:
            if cross[threshold] is None and favor > threshold + 1e-9:
                cross[threshold] = i+1
    output = {'run_horizon_pct':mfe, 'mae_horizon_pct':mae,
              'run_peak_ts':peak_ts, 'observed_horizon_min':horizon}
    for threshold in TARGETS:
        output[f'hit_{str(threshold).replace(".", "_")}pct'] = cross[threshold] is not None
        output[f'minutes_to_{str(threshold).replace(".", "_")}pct'] = cross[threshold]
    return output


def load_eligible_outcomes(project, events, horizon):
    """Read every Continuous Base event's path, NOT just runners archive.

    Returns (outcomes, quality counts) and never writes. Works during PM2 WAL
    writes using a read-only SQLite connection. Batches under SQLite bind limit.
    """
    import pandas as pd
    from reaction_lab_auto_paths import decode_path
    if events is None or events.empty:
        return pd.DataFrame(), {'events': 0, 'eligible':0}
    if 'event_key' not in events or 'cohort' not in events:
        raise ValueError('Need Event ID and cohort')
    base = events.loc[events['cohort'].eq('Continuous V1')].copy()
    if base['event_key'].duplicated().any():
        raise ValueError('Duplicate Event IDs in source cohort')
    keys = {str(k): (ts, close, side) for k, ts, close, side in zip(
        base['event_key'], base['retest_timestamp'], base['retest_close'], base['signal'])}
    quality = {'events':len(keys), 'eligible':0, 'no_path':len(keys),
               'short_path':0, 'invalid_or_gapped':0, 'read_errors':0}
    db = Path(project)/'reports/reaction_lab/auto_paths_v1.sqlite3'
    if not db.is_file():
        return pd.DataFrame(), quality
    outputs = []
    with closing(sqlite3.connect(f'file:{db.resolve().as_posix()}?mode=ro',uri=True,timeout=30)) as con:
        ordered = list(keys)
        for index in range(0, len(ordered), 350):
            part = ordered[index:index+350]
            placeholders = ','.join('?' for _ in part)
            for key, blob, count in con.execute(
                f'SELECT event_key,path_blob,count FROM events WHERE event_key IN ({placeholders})',part):
                quality['no_path'] -= 1
                if not blob or int(count) < int(horizon):
                    quality['short_path'] += 1
                    continue
                try:
                    data = decode_path(blob)
                    if len(data) != int(count):
                        quality['invalid_or_gapped'] += 1
                        continue
                    metric = observed_horizon(data, retest_timestamp=keys[key][0],
                                              retest_close=keys[key][1], side=keys[key][2],
                                              horizon=horizon)
                except (ValueError, TypeError, IndexError, OverflowError, zlib.error, struct.error):
                    quality['read_errors'] += 1
                    continue
                if metric is None:
                    quality['invalid_or_gapped'] += 1
                    continue
                metric['event_key'] = key
                outputs.append(metric)
    quality['eligible'] = len(outputs)
    return pd.DataFrame(outputs), quality


def runner_analysis_frame(all_events, outcome_frame):
    """Merge strictly evaluated equal-horizon outcomes with frozen as-of features."""
    import pandas as pd
    if outcome_frame is None or outcome_frame.empty:
        return pd.DataFrame()
    if all_events['event_key'].duplicated().any() or outcome_frame['event_key'].duplicated().any():
        raise ValueError('Duplicate Event IDs')
    filtered = all_events.loc[all_events['cohort'].eq('Continuous V1')].copy()
    filtered = filtered.drop(columns=[c for c in outcome_frame if c != 'event_key' and c in filtered],errors='ignore')
    result = filtered.merge(outcome_frame,on='event_key',how='inner',validate='one_to_one')
    # Time-of-day and confirmation-age features were known at event time.
    if 'known_ts' in result:
        dt = pd.to_datetime(pd.to_numeric(result['known_ts'],errors='coerce'),unit='ms',utc=True,errors='coerce')
        result['utc_session'] = dt.dt.hour.map(lambda h: ('00-07 UTC' if h<8 else '08-15 UTC' if h<16 else '16-23 UTC') if pd.notna(h) else None)
        result['utc_weekday'] = dt.dt.day_name()
    if 'known_ts' in result and 'actionable_timestamp' in result:
        known=pd.to_numeric(result['known_ts'],errors='coerce')
        confirm=pd.to_numeric(result['actionable_timestamp'],errors='coerce')
        age=(known-confirm)/MINUTE_MS
        result['confirmed_level_age_min']=age.where(age.ge(0))
    return result


def _group_bucket(values, name):
    import pandas as pd
    if name in NUMERIC_FACTORS:
        column, bins = NUMERIC_FACTORS[name]
        return pd.cut(pd.to_numeric(values[column], errors='coerce'),
                      bins=bins, right=False, include_lowest=True).astype('object').map(str).replace('nan',None)
    column=CATEGORICAL_FACTORS[name]
    return values[column].astype('string').str.strip().replace('',pd.NA)


def factor_coverage(frame):
    import pandas as pd
    if frame is None or frame.empty:
        return pd.DataFrame(columns=['Factor','Campo','Con dato','Total','Cobertura %'])
    rows=[]
    for name, field in FACTORS.items():
        valid = int(pd.to_numeric(frame[field],errors='coerce').notna().sum()) if name in NUMERIC_FACTORS and field in frame else (int(frame[field].notna().sum()) if field in frame else 0)
        rows.append({'Factor':name,'Campo':field,'Con dato':valid,
                     'Total':len(frame),'Cobertura %':round(100*valid/len(frame),1)})
    return pd.DataFrame(rows)


def wilson_bounds(hits,n,z=1.96):
    if n <= 0:
        return (math.nan,math.nan)
    p = hits/n
    den=1+z*z/n
    centre=(p+z*z/(2*n))/den
    margin=z*math.sqrt((p*(1-p)+z*z/(4*n))/n)/den
    return (100*(centre-margin),100*(centre+margin))


def factor_buckets(frame, factor, target=2.0, min_n=15):
    """Fixed bucket rate/lift vs a matched, equal-horizon eligible universe."""
    import pandas as pd
    if factor not in FACTORS:raise ValueError('Unknown factor')
    if target not in TARGETS:raise ValueError('Unsupported target')
    if frame is None or frame.empty or FACTORS[factor] not in frame:
        return pd.DataFrame()
    label=f'hit_{str(target).replace(".","_")}pct'
    if label not in frame:raise ValueError('Missing horizon outcome')
    bucket = _group_bucket(frame, factor)
    valid = bucket.notna() & frame[label].notna()
    selected = frame.loc[valid, [label,'run_horizon_pct']].copy()
    selected['Bucket'] = bucket.loc[valid].astype(str).to_numpy()
    if selected.empty:return pd.DataFrame()
    group=selected.groupby('Bucket',dropna=False,sort=True)
    out=group.agg(n=(label,'size'),hits=(label,'sum'),
                  median_run_pct=('run_horizon_pct','median')).reset_index()
    out=out.loc[out['n']>=min_n].copy()
    if out.empty:return out
    baseline=float(selected[label].mean())
    out['hit_rate_pct']=100*out['hits']/out['n']
    out['base_rate_pct']=100*baseline
    out['lift']=out['hit_rate_pct']/(100*baseline) if baseline>0 else float('nan')
    limits=out.apply(lambda row: wilson_bounds(int(row['hits']),int(row['n'])),axis=1)
    out['wilson_low_pct']=[x[0] for x in limits]
    out['wilson_high_pct']=[x[1] for x in limits]
    return out.sort_values(['hit_rate_pct','n'],ascending=[False,False],kind='stable').reset_index(drop=True)


def cohort_target_summary(frame):
    import pandas as pd
    if frame is None or frame.empty:return pd.DataFrame()
    rows=[]
    for target in TARGETS:
        col=f'hit_{str(target).replace(".","_")}pct'
        if col in frame:
            winners=frame.loc[frame[col]]
            rows.append({'Umbral %':target,'Elegibles':len(frame),'Alcanzaron':len(winners),
                         'Tasa %':round(100*len(winners)/len(frame),2),
                         'Mediana hasta umbral (min)':winners[f'minutes_to_{str(target).replace(".","_")}pct'].median()
                         if not winners.empty else float('nan')})
    return pd.DataFrame(rows)


def factor_pair_matrix(frame, first, second, target=2., min_n=10):
    """2D contingency: never infer outcomes for partial paths."""
    import pandas as pd
    if first not in FACTORS or second not in FACTORS or first==second:
        raise ValueError('Choose two different supported factors')
    if FACTORS[first] not in frame or FACTORS[second] not in frame:return pd.DataFrame()
    label=f'hit_{str(target).replace(".","_")}pct'
    working=pd.DataFrame({'a':_group_bucket(frame,first),
                          'b':_group_bucket(frame,second),'hit':frame[label]})
    working=working.dropna(subset=['a','b','hit'])
    if working.empty:return pd.DataFrame()
    grouped=working.groupby(['a','b'],observed=True).agg(n=('hit','size'),
        hits=('hit','sum')).reset_index()
    grouped=grouped.loc[grouped.n>=min_n].copy()
    if grouped.empty:return grouped
    grouped['hit_rate_pct']=(100*grouped.hits/grouped.n).round(2)
    return grouped.sort_values(['hit_rate_pct','n'],ascending=[False,False])


def spearman_feature_table(frame, min_n=25):
    """Exploratory rank correlation between frozen features and future run.

    Not BTC-price correlation; separate original BTC-correlations only if
    explicitly persisted. These are descriptive, and overlapping events may
    heavily correlate across symbols/timestamps.
    """
    import pandas as pd
    if frame is None or frame.empty:return pd.DataFrame()
    y=pd.to_numeric(frame['run_horizon_pct'],errors='coerce')
    records=[]
    for factor,(col,_) in NUMERIC_FACTORS.items():
        if col not in frame:continue
        x=pd.to_numeric(frame[col],errors='coerce')
        mask=x.notna()&y.notna()
        if mask.sum()<min_n or x[mask].nunique()<2 or y[mask].nunique()<2:continue
        # Spearman is Pearson correlation of average ranks.  Computing the
        # ranks directly avoids pandas' optional SciPy dependency while
        # preserving tie handling and the original pairwise non-null mask.
        corr=x[mask].rank(method='average').corr(
            y[mask].rank(method='average'),method='pearson')
        records.append({'Factor':factor,'N':int(mask.sum()),'Spearman con Run':round(float(corr),4)})
    if not records:return pd.DataFrame()
    return pd.DataFrame(records).sort_values('Spearman con Run',key=lambda s:s.abs(),ascending=False)


def winner_control_medians(frame, target=2.0, min_each=10):
    """Compare causal metric distributions among winners and non-runners."""
    import pandas as pd
    if frame is None or frame.empty:return pd.DataFrame()
    label=f'hit_{str(target).replace(".","_")}pct'
    records=[]
    for factor,(col,_) in NUMERIC_FACTORS.items():
        if col not in frame:continue
        value=pd.to_numeric(frame[col],errors='coerce')
        yes=value[frame[label]&value.notna()]
        no=value[~frame[label]&value.notna()]
        if min(len(yes),len(no))<min_each:continue
        records.append({'Factor':factor,'Ganadores N':len(yes),'Controles N':len(no),
                        'Mediana ganadores':round(float(yes.median()),4),
                        'Mediana controles':round(float(no.median()),4),
                        'Diferencia medianas':round(float(yes.median()-no.median()),4)})
    if not records:return pd.DataFrame()
    return pd.DataFrame(records)


def time_split_summary(frame, target=2., min_events=30):
    """Forward chronological split by FULL UTC days. No random leak across day.

    Describes holdout prevalence only; does not fit or validate a strategy.
    """
    import pandas as pd
    if frame is None or frame.empty or 'known_ts' not in frame:return pd.DataFrame()
    dates=pd.to_datetime(pd.to_numeric(frame['known_ts'],errors='coerce'),unit='ms',utc=True,errors='coerce').dt.floor('D')
    days=sorted(dates.dropna().unique())
    if len(days)<4:return pd.DataFrame()
    cutoff=days[max(1,int(len(days)*.7))]
    label=f'hit_{str(target).replace(".","_")}pct'
    periods=[('Exploración (días anteriores)', dates<cutoff),
             ('Validación temporal (días posteriores)', dates>=cutoff)]
    records=[]
    for name, mask in periods:
        part=frame.loc[mask]
        if len(part)<min_events:return pd.DataFrame()
        records.append({'Período':name,'Eventos':len(part),'Aciertos':int(part[label].sum()),
                        'Tasa %':round(100*part[label].mean(),2),
                        'Desde UTC':pd.to_datetime(part['known_ts'].min(),unit='ms',utc=True).strftime('%Y-%m-%d'),
                        'Hasta UTC':pd.to_datetime(part['known_ts'].max(),unit='ms',utc=True).strftime('%Y-%m-%d')})
    return pd.DataFrame(records)


def render_runner_analyzer(project, source_events):
    """Streamlit-only UI. Source events must include ALL Base REACTIONs pre runner-filter."""
    import pandas as pd
    import streamlit as st
    if source_events is None or source_events.empty:
        return
    st.markdown('### 🏃 Runner Analyzer · Base vs runners')
    st.caption('Compara TODAS las REACTIONs Base con el MISMO horizonte completo. '
               'Los runners no se seleccionan primero: +1/+2/+3/+5% son etiquetas futuras, '
               'no señales disponibles al retest. Sin órdenes ni portfolios.')
    with st.expander('Abrir Runner Analyzer — comparaciones, buckets, BTC y volumen',expanded=False):
        enabled=st.checkbox('Calcular Runner Analyzer con paths archivados',value=False,
                            key='runner_analyzer_enable',
                            help='Lectura solamente. Procesa los paths completos del horizonte elegido; '
                                 'el catálogo de runners NO define el grupo de control.')
        a,b,c=st.columns(3)
        horizon=a.selectbox('Horizonte comparable (minutos)',list(HORIZONS),
                            index=3,key='runner_analyzer_horizon')
        target=b.selectbox('Runner si supera (%)',list(TARGETS),
                           index=1,key='runner_analyzer_target')
        min_n=c.number_input('Mínimo de eventos por bucket',min_value=5,
                             max_value=500,value=20,step=5,
                             key='runner_analyzer_min_n')
        observation=st.selectbox('Origen de las señales',
            ['Todos','LIVE_OBSERVED','RECOVERED_WINDOW'],
            key='runner_analyzer_observation')
        context_filter=st.selectbox('Calidad del contexto',
            ['Todos','READY','PARTIAL'],key='runner_analyzer_context')
        if not enabled:
            st.info('Activá el cálculo para ver los grupos de control y factores. No escribe en SQLite.')
            return
        events=source_events.loc[source_events['cohort'].eq('Continuous V1')].copy()
        if observation!='Todos':events=events.loc[events['observation_type'].eq(observation)].copy()
        if context_filter!='Todos':events=events.loc[events['context_state'].eq(context_filter)].copy()
        if events.empty:
            st.warning('No hay REACTIONs Base para estos filtros.');return

        @st.cache_data(ttl=120,max_entries=12,show_spinner=False)
        def _get_paths(root, event_keys, retests, baselines, directions, window):
            incoming=pd.DataFrame({'event_key':event_keys,'cohort':['Continuous V1']*len(event_keys),
                'retest_timestamp':retests,'retest_close':baselines,'signal':directions})
            return load_eligible_outcomes(root,incoming,window)

        # Stable Event ID order; cache expires for ACTIVE paths/late arrivals.
        selected=events.sort_values('event_key')
        with st.spinner('Leyendo paths 1m persistidos y validando horizonte continuo…'):
            outcome,quality=_get_paths(str(project),tuple(selected['event_key'].astype(str)),
                tuple(selected['retest_timestamp']),tuple(selected['retest_close']),
                tuple(selected['signal']),int(horizon))
        cols=st.columns(4)
        cols[0].metric('Base REACTIONs',quality['events'])
        cols[1].metric(f'Paths ≥ {horizon}m válidos',quality['eligible'])
        cols[2].metric('Sin path',quality['no_path'])
        cols[3].metric('Path corto / inválido',quality['short_path']+quality['invalid_or_gapped']+quality['read_errors'])
        if outcome.empty:
            st.warning('Ninguna REACTION tiene aún un path continuo de este horizonte. '
                       'No clasificamos paths incompletos como perdedores.')
            return
        full=runner_analysis_frame(events,outcome)
        label=f'hit_{str(target).replace(".","_")}pct'
        hits=int(full[label].sum())
        st.metric(f'Runners > {target:g}% / {horizon}m',f'{hits} / {len(full)}',
                  f'{100*hits/len(full):.1f}% de los elegibles',delta_color='off')
        st.caption('Los outcomes miden el máximo HIGH/LOW intravela desde el CLOSE del retest. '
                   'No equivalen a rentabilidad ejecutable ni consideran spreads, slippage o comisiones. '
                   'Múltiples señales pueden compartir el mismo contexto BTC y el mismo movimiento general.')
        st.markdown('#### Frecuencia por objetivo y tiempo hasta alcanzarlo')
        st.dataframe(cohort_target_summary(full),hide_index=True,use_container_width=True)
        trend=time_split_summary(full,target,min_events=30)
        if not trend.empty:
            st.markdown('#### Corte cronológico exploración / validación')
            st.dataframe(trend,hide_index=True,use_container_width=True)
            st.caption('Separación por fechas UTC, sin mezclar días. Todavía no es validación fuera de muestra de una estrategia.')
        else:
            st.caption('Sin suficientes días y eventos para un corte temporal útil (mínimo 4 días).')
        st.markdown('#### ¿Qué datos causales tenemos?')
        coverage=factor_coverage(full)
        st.dataframe(coverage,hide_index=True,use_container_width=True,height=260)
        btc_cov=coverage.loc[coverage['Factor'].str.startswith('BTC Corr')]
        if not btc_cov.empty and int(btc_cov['Con dato'].sum())==0:
            st.warning('BTC Correlation histórico (Pearson/Beta/R²) NO está congelado por Event ID '
                       'en esta cohorte. Sí están BTC retorno 1h/4h, Strength vs BTC y Market Flow. '
                       'No calculamos correlaciones con velas posteriores o Redis actual.')
        available=coverage.loc[coverage['Con dato']>=int(min_n),'Factor'].tolist()
        if not available:
            st.info('No hay factores con muestras suficientes para agrupar.');return
        st.markdown('#### Buckets contra todas las REACTIONs evaluables')
        left,right=st.columns(2)
        preferred='Strength BTC 4h (direccional)'
        factor=left.selectbox('Primer factor',available,
                    index=available.index(preferred) if preferred in available else 0,
                    key='runner_analyzer_factor')
        bucket=factor_buckets(full,factor,target,min_n=int(min_n))
        if bucket.empty:
            st.info('Los buckets del factor no alcanzan el mínimo de eventos.')
        else:
            st.dataframe(bucket,hide_index=True,use_container_width=True)
            st.bar_chart(bucket.set_index('Bucket')[['hit_rate_pct','base_rate_pct']],
                         use_container_width=True)
            st.caption('Lift = tasa en bucket / tasa global con dato del mismo factor. '
                       'Wilson 95% describe incertidumbre binomial aproximada; señales simultáneas no son independientes.')
        next_choices=[x for x in available if x!=factor]
        if next_choices:
            second=right.selectbox('Cruzar con',next_choices,index=0,
                                    key='runner_analyzer_second_factor')
            crossed=factor_pair_matrix(full,factor,second,target,int(min_n))
            if crossed.empty:
                st.info('El cruce 2D no tiene suficientes eventos completos por celda.')
            else:
                st.markdown(f'#### Cruce: {factor} × {second}')
                st.dataframe(crossed,hide_index=True,use_container_width=True)
                import plotly.graph_objects as go
                heat=crossed.pivot(index='a',columns='b',values='hit_rate_pct')
                counts=crossed.pivot(index='a',columns='b',values='n').reindex(index=heat.index,columns=heat.columns)
                plot=go.Figure(go.Heatmap(z=heat.values,x=list(heat.columns),
                    y=list(heat.index),text=counts.values,
                    hovertemplate='%{y} · %{x}<extra></extra><br>Tasa: %{z:.1f}%<br>N: %{text}',
                    zmin=0,zmax=100,colorbar=dict(title='Éxito %')))
                plot.update_layout(height=370,margin=dict(l=5,r=5,t=15,b=10),
                    xaxis_title=second,yaxis_title=factor)
                st.plotly_chart(plot,use_container_width=True,key='runner_analyzer_heatmap')
        st.markdown('#### Ganadores vs controles — contexto previo')
        medians=winner_control_medians(full,target,min_each=max(5,int(min_n)//2))
        if medians.empty:st.info('Todavía no hay ganadores y controles con muestras suficientes.')
        else:st.dataframe(medians,hide_index=True,use_container_width=True,height=340)
        st.markdown('#### Correlación estadística con Run (exploratoria)')
        corr=spearman_feature_table(full,min_n=max(25,int(min_n)))
        if corr.empty:st.info('No hay suficientes magnitudes numéricas.')
        else:st.dataframe(corr,hide_index=True,use_container_width=True,height=280)
        st.caption('Spearman aquí significa asociación entre UN FACTOR PREVIO y RUN POSTERIOR. '
                   'No es correlación del precio de la moneda con BTC. Rankings y buckets son exploratorios: '
                   'no hay evidencia de ventaja operativa hasta probarlos sobre datos futuros independientes.')
        export=full.copy()
        for col in export.columns:
            if export[col].map(lambda x:isinstance(x,(list,tuple,dict))).any():
                import json
                export[col]=export[col].map(lambda x:json.dumps(x,ensure_ascii=False) if isinstance(x,(list,tuple,dict)) else x)
        st.download_button('Exportar universo elegible: winners + controles con contexto y outcomes (CSV)',
              export.to_csv(index=False).encode('utf-8-sig'),
              file_name=f'continuous_runner_analyzer_{horizon}m_{target:g}pct.csv',
              mime='text/csv', key='runner_analyzer_export')
