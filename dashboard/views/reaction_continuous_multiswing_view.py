"""Read-only Continuous V1 swing viewer and causal multi-swing coincidence study.

Uses persisted first-touch identities, and the *original frozen* candles_json
from causal_context_v1.sqlite3. Never substitutes current Redis OHLC for old events.
A pivot marker is historical geometry; only points confirmed by known_ts are
eligible as causal evidence for the selected REACTION.
"""
from __future__ import annotations

from contextlib import closing
from functools import lru_cache
import time as _perf_time
import json
import math
from pathlib import Path
import sqlite3

from engine.live.research.swing_detector import SwingDetector

TF_MS = {'1m': 60_000, '5m': 300_000, '15m': 900_000, '30m': 1_800_000, '1h': 3_600_000}
DEFAULT_WINDOWS = {'1m': 5, '5m': 5, '15m': 3, '30m': 3, '1h': 2}
FIRST_TOUCH_DB = 'reports/reaction_lab/scanner_first_touches_v1.sqlite3'
CONTEXT_DB = 'reports/reaction_lab/causal_context_v1.sqlite3'


def _num(value):
    try:
        val = float(value)
        return val if math.isfinite(val) else None
    except (ValueError, TypeError):
        return None


def normalized_snapshot(records, timeframe, known_ts):
    """Validate a frozen TF candle series. No padding and no open/future bars."""
    import pandas as pd
    cols = ['timestamp', 'open', 'high', 'low', 'close']
    if isinstance(records, pd.DataFrame):
        records = records.to_dict('records')
    if not isinstance(records, list) or not records:
        return pd.DataFrame(columns=cols)
    f = pd.DataFrame(records)
    if not set(cols).issubset(f.columns):
        return pd.DataFrame(columns=cols)
    for col in cols:
        f[col] = pd.to_numeric(f[col], errors='coerce')
    if f[cols].isna().any().any():
        return pd.DataFrame(columns=cols)
    if f.timestamp.duplicated().any():
        return pd.DataFrame(columns=cols)
    f = f.sort_values('timestamp').reset_index(drop=True)
    tf_ms = TF_MS[timeframe]
    f = f[f.timestamp.mod(tf_ms).eq(0) & (f.timestamp + tf_ms <= int(known_ts))].copy()
    if f.empty:
        return pd.DataFrame(columns=cols)
    good = ((f[cols[1:]] > 0).all(axis=1)
            & (f.high >= f[['open','close','low']].max(axis=1))
            & (f.low <= f[['open','close','high']].min(axis=1)))
    if not good.all():
        return pd.DataFrame(columns=cols)
    return f.reset_index(drop=True)


def _runs_of_contiguous(frame, timeframe):
    """Segment gaps: detector never sees invented neighbors across missing bars."""
    if frame.empty:
        return []
    stamps = frame.timestamp.astype('int64').tolist()
    step = TF_MS[timeframe]
    spans = []
    start = 0
    for idx in range(1, len(stamps)):
        if stamps[idx] != stamps[idx-1] + step:
            spans.append(frame.iloc[start:idx].copy())
            start = idx
    spans.append(frame.iloc[start:].copy())
    return spans


def confirmed_points(frozen, timeframe, known_ts, bars, prominence_pct=0.0):
    """Same SwingDetector as Legacy, with explicit closed-bar availability."""
    frame = normalized_snapshot(frozen, timeframe, known_ts)
    detector = SwingDetector(left_bars=int(bars), right_bars=int(bars),
                             min_prominence_pct=float(prominence_pct))
    found = []
    for run in _runs_of_contiguous(frame, timeframe):
        if len(run) <= 2 * bars:
            continue
        points = detector.detect_all(run.to_dict('records'))
        found.extend(point for point in points
                     if int(point.confirmed_timestamp) + TF_MS[timeframe] <= int(known_ts))
    return frame, sorted(found, key=lambda p: (p.pivot_timestamp, p.side, p.price))


def confluence_matches(points_by_tf, *, pivot_ts, pivot_price, side,
                       known_ts, tolerance_pct=0.10, time_bucket='15m'):
    """One closest match per TF: comparable side, price, temporal bucket, and known.

    This is *coincidence of representations across timeframes*, not independent
    evidence of multiple buyers/sellers; 1m/5m/15m often describe one extremum.
    """
    if time_bucket not in ('5m', '15m'):
        raise ValueError('unsupported coincidence bucket')
    if side not in ('HIGH', 'LOW') or pivot_price <= 0:
        raise ValueError('invalid canonical pivot')
    width = TF_MS[time_bucket]
    anchor_bucket = int(pivot_ts) // width
    matches = []
    for timeframe, points in points_by_tf.items():
        candidates = []
        for point in points:
            if point.side != side or point.confirmed_timestamp + TF_MS[timeframe] > known_ts:
                continue
            if int(point.pivot_timestamp) // width != anchor_bucket:
                continue
            distance_pct = abs(point.price / float(pivot_price) - 1.0) * 100.0
            if distance_pct > float(tolerance_pct):
                continue
            candidates.append((distance_pct, point))
        if candidates:
            distance, best = min(candidates, key=lambda v:(v[0], abs(v[1].pivot_timestamp-pivot_ts)))
            matches.append({'timeframe': timeframe, 'side': best.side,
                            'pivot_ts': int(best.pivot_timestamp), 'price': float(best.price),
                            'confirmed_available_ts': int(best.confirmed_timestamp + TF_MS[timeframe]),
                            'distance_pct': float(distance), 'prominence_pct':best.prominence_pct})
    return sorted(matches, key=lambda v: TF_MS[v['timeframe']])


def _frozen_db_version(path: Path):
    """Detect SQLite DB and WAL updates; never cache a mutable journal blindly."""
    result = []
    for item in (path, Path(str(path) + '-wal')):
        try:
            stat = item.stat()
        except FileNotFoundError:
            result.append(None)
        else:
            result.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns))
    return tuple(result)


@lru_cache(maxsize=64)
def _cached_original_context(path_str: str, event_key: str, known_ts: int, version):
    """Read-only frozen JSON, invalidated on any DB/WAL file change."""
    with closing(sqlite3.connect(Path(path_str).resolve().as_uri()+'?mode=ro',
                                 uri=True, timeout=10)) as con:
        con.execute('PRAGMA query_only=ON')
        row = con.execute('SELECT known_ts, status, candles_json FROM contexts WHERE event_key=?',
                          (str(event_key),)).fetchone()
    if row is None:
        return None, 'NO_FROZEN_CONTEXT_FOR_EVENT'
    if int(row[0]) != int(known_ts):
        return None, 'KNOWN_TS_MISMATCH'
    try:
        snapshots = json.loads(row[2])
    except (ValueError, TypeError):
        return None, 'INVALID_FROZEN_JSON'
    if not isinstance(snapshots, dict):
        return None, 'INVALID_FROZEN_JSON'
    return snapshots, str(row[1])


def read_frozen_event(project, event_key, known_ts):
    """Read original event snapshot, reusing only identical frozen DB versions.

    No Redis, exchange or current-price fallback. Snapshot dict is read-only for
    downstream callers, who build their own Pandas copies.
    """
    path = Path(project) / CONTEXT_DB
    if not path.is_file():
        return None, 'NO_CONTEXT_DB'
    return _cached_original_context(str(path.resolve()), str(event_key),
                                    int(known_ts), _frozen_db_version(path))


def simultaneous_journal_pivots(project, symbol, retest_ts):
    """All distinct first-touch pivot identities in the very same 1m retest.

    The Continuous REACTION scanner's legacy key can collapse these into one
    Event ID, but the independent first-touch journal preserves each touch_key.
    """
    path = Path(project) / FIRST_TOUCH_DB
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=10)) as con:
        con.execute('PRAGMA query_only=ON')
        rows=con.execute('SELECT touch_key, side, status, pivot_ts, actionable_ts, '
                         'retest_ts, observation_type FROM first_touches '
                         'WHERE symbol=? AND retest_ts=? ORDER BY side, pivot_ts, touch_key',
                         (str(symbol).upper(),int(retest_ts))).fetchall()
    cols=('touch_key','side','status','pivot_ts','actionable_ts','retest_ts','observation_type')
    return [dict(zip(cols,row)) for row in rows]


@lru_cache(maxsize=48)
def _cached_post_path(project_root: str, event_key: str, known_ts: int,
                      max_bars: int, signature):
    """Descriptive POST cache, invalidated when offline auto-path DB/WAL changes."""
    import pandas as pd
    project = Path(project_root)
    key = str(event_key)
    try:
        from reaction_lab_auto_paths import read_auto_paths_dataframe
        frame = read_auto_paths_dataframe(
            Path(project) / 'reports/reaction_lab/auto_paths_v1.sqlite3',
            event_keys={str(key)})
    except (ImportError, OSError, ValueError, sqlite3.Error):
        return pd.DataFrame()
    required = {'timestamp','open','high','low','close'}
    if frame is None or frame.empty or not required.issubset(frame.columns):
        return pd.DataFrame()
    out = frame.copy()
    for col in required:
        out[col] = pd.to_numeric(out[col], errors='coerce')
    out = out.dropna(subset=list(required)).sort_values('timestamp')
    out = out.loc[out.timestamp >= int(known_ts)].head(int(max_bars))
    # No invented bars; show only the consecutive prefix.
    if out.empty or int(out.timestamp.iloc[0]) != int(known_ts):
        return pd.DataFrame()
    stamps = out.timestamp.astype('int64').tolist()
    end = 1
    while end < len(stamps) and stamps[end] == stamps[end-1] + 60_000:
        end += 1
    return out.iloc[:end].copy()



def _post_path(project, key, known_ts, max_bars):
    """Optional POST, never a swing-detector input; refresh on DB/WAL change."""
    import pandas as pd
    if int(max_bars) <= 0:
        return pd.DataFrame()
    db = Path(project) / 'reports/reaction_lab/auto_paths_v1.sqlite3'
    result = _cached_post_path(str(Path(project).resolve()), str(key),
        int(known_ts), int(max_bars), _frozen_db_version(db))
    return result.copy()


def assemble_chart_1m(pre, post, known_ts, before, after):
    """A finite, event-anchored 1m view: no present-day market data."""
    import pandas as pd
    x = normalized_snapshot(pre, '1m', int(known_ts)).tail(int(before))
    if x.empty:
        return x
    if post is None or post.empty or int(after) <= 0:
        return x
    future = post.head(int(after)).copy()
    return pd.concat([x, future], ignore_index=True).sort_values('timestamp').drop_duplicates('timestamp')


def render_continuous_multiswing_view(project, *, timezone='UTC',
                                      chart_builder=None, mode_override=None):
    """Read-only event or BTC view; parent can preselect mode without extra rerun."""
    import streamlit as st
    view_started = _perf_time.perf_counter()
    choices = ('Evento REACTION · Continuous', 'Mapa BTC 60D · 6 TF')
    if mode_override is None:
        # Compatibility with direct callers and older dashboards.
        modality = st.selectbox(
            'Modo de investigacion Multi-Swing', choices,
            key='rcms_study_mode_v1',
        )
    else:
        if mode_override not in choices:
            raise ValueError('Unsupported Multi-Swing mode')
        modality = mode_override
    if modality == 'Mapa BTC 60D · 6 TF':
        # Avoid even importing Pandas/Plotly and the frozen-event journal
        # when routing to the stand-alone BTC 60D research path.
        from dashboard.views.reaction_multiswing_btc60d_view_v1 import (
            render_btc_multiswing_60d_view,
        )
        render_btc_multiswing_60d_view(project)
        return
    import pandas as pd
    import plotly.graph_objects as go
    from dashboard.views.reaction_lab_first_touches_view import (
        get_page, get_touch, get_symbols,
    )
    st.markdown('### 🔬 Multi-Swing Confluence · Continuous V1')
    st.caption('Mismo detector de Legacy sobre snapshots 1m/5m/15m originales. '
               'Se inspeccionan REACTIONs persistidas; la coincidencia se cuenta '
               'únicamente con swings ya confirmados al known_ts. Solo lectura.')
    db = Path(project) / FIRST_TOUCH_DB
    if not db.is_file():
        st.warning('Falta el journal First Touches persistido.')
        return
    try:
        symbols = get_symbols(db)
    except (sqlite3.Error, OSError, ValueError) as exc:
        st.warning(f'No se pudo leer el journal: {exc}')
        return
    a,b,c = st.columns([2,1,1])
    with a:
        symbol = st.selectbox('Símbolo REACTION', symbols, key='rcms_symbol')
    with b:
        side = st.selectbox('Dirección', ['ALL','LONG','SHORT'], key='rcms_side')
    with c:
        origin = st.selectbox('Origen', ['ALL','LIVE_OBSERVED','RECOVERED_WINDOW'],key='rcms_origin')
    if not symbol:
        st.info('No hay símbolos registrados.')
        return
    page = st.number_input('Página de REACTIONs (250 por página)',min_value=1,value=1,step=1,key='rcms_page')
    try:
        total, rows = get_page(db,status='REACTION',symbol=symbol,
                               side=None if side=='ALL' else side,
                               origin=None if origin=='ALL' else origin,
                               limit=250,offset=(int(page)-1)*250)
    except (sqlite3.Error, OSError, ValueError) as exc:
        st.warning(f'No se pudieron leer los eventos: {exc}')
        return
    if not rows:
        st.info(f'Sin REACTIONs para estos filtros o página. Total: {total}.')
        return
    options={r['touch_key']:r for r in rows}
    selected=st.selectbox('Primer retest persistido',list(options),
        format_func=lambda k:(f"{pd.to_datetime(options[k]['retest_ts'],unit='ms',utc=True)} "
                              f"· {options[k]['side']} · {options[k]['status']} · {k[:20]}"),
        key='rcms_touch')
    try:
        touch=get_touch(db,selected)
    except (sqlite3.Error, ValueError, OSError) as exc:
        st.warning(f'No se pudo recuperar el evento: {exc}')
        return
    if not touch:
        st.warning('No se encontró la identidad original.')
        return
    symbol=str(touch['symbol'])
    known=int(options[selected]['known_ts'])
    retest=int(touch['retest_timestamp'])
    pivot_ts=int(touch['pivot_timestamp'])
    side=str(touch['signal'])
    target_side='HIGH' if side=='SHORT' else 'LOW'
    price=_num(touch.get('swing_price')) or _num(touch.get('pivot_price'))
    if price is None:
        st.warning('Pivot price no está persistido. No inferimos un nuevo nivel.')
        return
    event_key=str(touch.get('legacy_event_key') or f'{symbol}|{side}|{retest}')
    snapshots_started = _perf_time.perf_counter()
    snapshots, state=read_frozen_event(project,event_key,known)
    snapshots_ms = (_perf_time.perf_counter() - snapshots_started) * 1000
    if not snapshots:
        st.warning(f'No hay snapshots causales 1m/5m/15m para este Event ID: {state}. '
                   'No se sustituyen por velas de Redis de hoy.')
        return
    tf_options=['1m','5m','15m','30m','1h']
    selected_tfs=st.multiselect('Swing timeframes',tf_options,default=['1m','5m','15m'],key='rcms_timeframes')
    prominence=st.number_input('Min swing prominence %',min_value=0.0,value=0.0,step=0.05,
                              format='%.2f',key='rcms_prominence')
    default = {tf: DEFAULT_WINDOWS[tf] for tf in selected_tfs}
    detectors={}
    cols=st.columns(max(1,len(selected_tfs)))
    for i,tf in enumerate(selected_tfs):
        with cols[i]:
            names=['2x2','3x3','5x5']
            detectors[tf]=int(st.selectbox(f'{tf} detector',names,
                    index=names.index(f'{default[tf]}x{default[tf]}'),key=f'rcms_detector_{tf}')[0])
    d1,d2,d3=st.columns(3)
    with d1: candle_count=st.slider('Velas 1m PRE',min_value=60,max_value=400,value=240,step=20,key='rcms_pre_count')
    with d2: post_count=st.slider('Velas 1m POST (visual)',min_value=0,max_value=180,value=0,step=15,key='rcms_post_count')
    with d3: tolerance=st.number_input('Proximidad precio %',min_value=0.0,max_value=5.0,
                                     value=0.10,step=0.05,format='%.2f',key='rcms_tolerance')
    buck=st.selectbox('Coincidencia temporal', ['15m','5m'], key='rcms_bucket')
    detector_started = _perf_time.perf_counter()
    points={};frames={};diag=[]
    for tf in selected_tfs:
        f, p=confirmed_points(snapshots.get(tf,[]),tf,known,detectors[tf],prominence)
        points[tf]=p;frames[tf]=f
        diag.append({'tf':tf,'closed_bars':len(f),'confirmed_swings':len(p),
                     'contiguous_segments':len(_runs_of_contiguous(f,tf))})
    detectors_ms = (_perf_time.perf_counter() - detector_started) * 1000
    pre=normalized_snapshot(snapshots.get('1m',[]),'1m',known)
    post=_post_path(project,event_key,known,post_count)
    chart_frame=assemble_chart_1m(pre,post,known,candle_count,post_count)
    if chart_frame.empty:
        st.warning('Sin velas históricas 1m íntegramente cerradas para visualizar.')
    else:
        if chart_builder is not None:
            fig=chart_builder(candles=chart_frame,
                event_row=pd.Series({'symbol':symbol,'candle_open_timestamp':float('nan'),
                                     'potential_side':side,'close':price}),
                swing_points_by_timeframe=points,
                swing_candles_by_timeframe=frames)
        else:
            fig=go.Figure(data=go.Candlestick(
                x=pd.to_datetime(chart_frame.timestamp,unit='ms',utc=True),
                open=chart_frame.open, high=chart_frame.high,
                low=chart_frame.low, close=chart_frame.close,name='1m'))
        moment=pd.to_datetime(retest,unit='ms',utc=True)
        fig.add_vline(x=moment.timestamp()*1000,line_dash='dash',annotation_text='RETEST 1m')
        fig.add_trace(go.Scatter(x=[moment],y=[float(_num(touch.get('retest_price')) or price)],
                                 mode='markers',marker={'symbol':'diamond','size':14},
                                 name='REACTION persistida'))
        fig.add_hline(y=price,line_dash='dot',annotation_text='Swing 15m original')
        fig.update_layout(title=f'{symbol} · {side} · REACTION persistida',
                          height=640,xaxis_rangeslider_visible=False)
        st.plotly_chart(fig,use_container_width=True,key='rcms_candles',
                        config={'displaylogo':False,'scrollZoom':True})
    if not selected_tfs:
        st.info('Seleccioná al menos un timeframe.')
        return
    # The original scanner's 15m/3x3 pivot is authoritative. Do not silently
    # discard it if a short snapshot cannot reproduce its left/right neighbors.
    actionable=int(touch['actionable_timestamp'])
    if actionable > known:
        st.error('Identidad causal inválida: pivot original confirmado después de known_ts.')
        return
    other={tf:p for tf,p in points.items() if tf != '15m'}
    extra_matches=confluence_matches(other,pivot_ts=pivot_ts,pivot_price=price,
                                     side=target_side,known_ts=known,
                                     tolerance_pct=tolerance,time_bucket=buck)
    matches=[{'timeframe':'15m','side':target_side,'pivot_ts':pivot_ts,
              'price':price,'confirmed_available_ts':actionable,
              'distance_pct':0.0,'prominence_pct':touch.get('swing_prominence_pct'),
              'source':'SCANNER_ORIGINAL'}]
    matches.extend(dict(m,source='FROZEN_SNAPSHOT') for m in extra_matches)
    c1,c2,c3=st.columns(3)
    c1.metric('TF coincidentes (incluye 15m original)',len(matches))
    c2.metric('Doble o más', 'SÍ' if len(matches)>=2 else 'NO')
    c3.metric('Triple o más','SÍ' if len(matches)>=3 else 'NO')
    table=pd.DataFrame(matches)
    table['pivot_UTC']=pd.to_datetime(table.pivot_ts,unit='ms',utc=True)
    table['available_UTC']=pd.to_datetime(table.confirmed_available_ts,unit='ms',utc=True)
    st.dataframe(table[['timeframe','side','source','pivot_UTC','price','distance_pct',
                        'available_UTC','prominence_pct']],hide_index=True,use_container_width=True)
    with st.expander('🧬 Pivots originales simultáneos en el mismo retest 1m', expanded=True):
        simult=simultaneous_journal_pivots(project,symbol,retest)
        same_side=[x for x in simult if x['side']==side]
        a,b,c=st.columns(3)
        a.metric('Pivots de mismo lado, mismo 1m',len(same_side))
        b.metric('Pivots de ambos lados, mismo 1m',len(simult))
        c.metric('Identidades distintas',len({x['touch_key'] for x in simult}))
        if simult:
            observed=pd.DataFrame(simult)
            observed['pivot_UTC']=pd.to_datetime(observed.pivot_ts,unit='ms',utc=True)
            st.dataframe(observed[['touch_key','side','status','pivot_UTC',
                                   'actionable_ts','observation_type']],
                         hide_index=True,use_container_width=True)
        st.caption('Esto NO es lo mismo que confluencia 1m/5m/15m: '
                   'acá son identidades originales de primeros retests guardadas '
                   'por el journal en el mismo minuto. No sumar como operaciones '
                   'independientes. El Event ID Legacy puede ser uno solo.')
    st.caption('Conteo descriptivo de timeframes, NO cantidad de fuerzas independientes. '
               '1m/5m/15m pueden describir exactamente el mismo máximo/mínimo. '
               'Todas las confirmaciones deben estar disponibles cuando cerró la REACTION.')
    with st.expander('⏱️ Diagnóstico de rendimiento · Continuous',expanded=False):
        st.json({'snapshot_ms':round(snapshots_ms,1),
                 'swing_detectors_ms':round(detectors_ms,1),
                 'total_view_ms':round((_perf_time.perf_counter()-view_started)*1000,1),
                 'frozen_context_cache':str(_cached_original_context.cache_info()),
                 'post_path_cache':str(_cached_post_path.cache_info())})
        st.caption('Tiempos de esta vista, no del dashboard principal completo. '
                   'No se mide aquí el tiempo de transmisión o dibujo en el navegador.')
    with st.expander('Cobertura y procedencia',expanded=False):
        st.dataframe(pd.DataFrame(diag),use_container_width=True,hide_index=True)
        st.json({'touch_key':selected,'legacy_event_key':event_key,'state':state,
                 'known_ts':known,'retest_ts':retest,'pivot_ts':pivot_ts,
                 'pivot_price':price,'pre_1m':len(pre),'post_1m_consecutive':len(post)})
        st.caption('El resultado posterior no interviene en el contador; '
                   'la investigación estadística por cohorte vendrá en una fase posterior.')
