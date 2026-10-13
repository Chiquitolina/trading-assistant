"""Read-only adapter for the canonical Legacy REACTION inspector.

Input is the immutable Continuous V1 scanner/context cohort. Reuse the
canonical build_confirmed_swing_retest_detail_chart() in dashboard/app.py,
without running live Redis scans or fabricating candles/forward metrics.
"""
from __future__ import annotations
import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path

FIFTEEN_MS = 900_000
ONE_MS = 60_000


def _num(value):
    try:
        x=float(value)
        return x if math.isfinite(x) else None
    except (TypeError,ValueError):
        return None


def inspector_rows(frame, tz="UTC"):
    """Preserve Event ID and normalize the canonical shared inspector fields."""
    import pandas as pd
    if frame is None or frame.empty:
        return pd.DataFrame()
    df=frame.copy()
    df['timeframe']='15m'
    df['detector']='3x3'
    df['status']='REACTION'
    df['reaction']=True
    df['retest_time']=pd.to_datetime(pd.to_numeric(df['retest_timestamp'],errors='coerce'),unit='ms',utc=True,errors='coerce').dt.tz_convert(tz)
    # Canonical inspector uses this only for its optional label, not detection.
    # Never alias later 180m MFE to a different historical metric.
    if 'reaction_mfe_pct' not in df:df['reaction_mfe_pct']=float('nan')
    required=['pivot_timestamp','actionable_timestamp','departure_timestamp',
              'swing_price','departure_price','retest_price','retest_timestamp']
    for field in required:
        if field not in df: df[field]=float('nan')
        df[field]=pd.to_numeric(df[field],errors='coerce')
    # Malformed/incomplete scanner payloads are not chartable, keep in the table.
    return df


def frozen_room(row):
    """Return exactly the dict consumed by canonical Legacy inspector/chart."""
    get=row.get
    known=_num(get('known_ts'))
    confirmed=_num(get('nearest_opposing_swing_actionable_timestamp'))
    room=_num(get('nearest_opposing_room_pct'))
    price=_num(get('nearest_opposing_swing_price'))
    if confirmed is not None and known is not None and confirmed>known:
        room=None;price=None
    ready=all(_num(get('rsi14_'+tf)) is not None for tf in ('1m','5m','15m','1h'))
    count=_num(get('aligned_rsi_extreme_count')) if ready else None
    return {
        'reaction_price': _num(get('retest_close')) or _num(get('retest_price')),
        'nearest_opposing_room_pct':room,
        'nearest_opposing_swing_tf':get('nearest_opposing_swing_tf') if room is not None else None,
        'nearest_opposing_swing_price':price,
        'nearest_opposing_swing_pivot_timestamp':_num(get('nearest_opposing_swing_pivot_timestamp')),
        'nearest_opposing_swing_confirmed_timestamp':_num(get('nearest_opposing_swing_confirmed_timestamp')),
        'nearest_opposing_swing_actionable_timestamp':confirmed,
        'aligned_rsi_extreme_count':count,
    }


def _normalized_ohlc(records):
    import pandas as pd
    if not records:return pd.DataFrame(columns=['timestamp','open','high','low','close','volume'])
    df=pd.DataFrame(records)
    cols=['timestamp','open','high','low','close']
    if not set(cols).issubset(df.columns):
        return pd.DataFrame(columns=cols+['volume'])
    for col in cols:df[col]=pd.to_numeric(df[col],errors='coerce')
    df=df.dropna(subset=cols).sort_values('timestamp').drop_duplicates('timestamp',keep='last')
    if 'volume' not in df:df['volume']=float('nan')
    return df[cols+['volume']].reset_index(drop=True)


def post_15m_from_1m(path, known_ts):
    """Resample ONLY full contiguous 15x1m blocks starting after known_ts.

    A path starting midway inside a 15m candle cannot synthesize that candle.
    We never merge partial 1m bars with the pre-signal closed 15m snapshot.
    """
    import pandas as pd
    empty=_normalized_ohlc([])
    known=_num(known_ts)
    if path is None or path.empty or known is None:return empty
    work=_normalized_ohlc(path.to_dict('records'))
    if work.empty:return empty
    work=work.loc[work['timestamp']>=math.ceil(known/FIFTEEN_MS)*FIFTEEN_MS].copy()
    if work.empty:return empty
    work['bucket']=(work['timestamp'].astype('int64')//FIFTEEN_MS)*FIFTEEN_MS
    output=[]
    for ts,group in work.groupby('bucket',sort=True):
        group=group.sort_values('timestamp')
        stamps=group['timestamp'].astype('int64').tolist()
        if stamps!=list(range(int(ts),int(ts)+FIFTEEN_MS,ONE_MS)):
            continue
        output.append({'timestamp':int(ts),'open':float(group['open'].iloc[0]),
            'high':float(group['high'].max()),'low':float(group['low'].min()),
            'close':float(group['close'].iloc[-1]),
            'volume':float(group['volume'].sum(min_count=1))})
    return _normalized_ohlc(output)


def combined_archived_15m(archived, path, known_ts, limit=400):
    import pandas as pd
    known=_num(known_ts)
    if known is None:return _normalized_ohlc([])
    asof=_normalized_ohlc(archived)
    if not asof.empty:
        asof=asof.loc[asof['timestamp']+FIFTEEN_MS<=known]
    future=post_15m_from_1m(path,known)
    if asof.empty:combined=future
    elif future.empty:combined=asof
    else:combined=pd.concat([asof,future],ignore_index=True)
    if combined.empty:return combined
    return combined.sort_values('timestamp').drop_duplicates('timestamp',keep='first').tail(int(limit)).reset_index(drop=True)


def observed_reaction_run(bars, *, retest_timestamp, retest_close, signal):
    """Legacy reaction_mfe_pct semantics on the archived post-retest 1m prefix.

    The baseline is the REACTION candle close (NOT next-bar entry open).
    The REACTION candle is excluded; the first usable bar is retest+60s.
    Missing first minute, gaps, or invalid OHLC stop the observation.  A
    missing observation is None, never zero.  This is descriptive hindsight,
    never a causal feature for candidate eligibility.
    """
    baseline = _num(retest_close)
    stamp = _num(retest_timestamp)
    side = str(signal).upper()
    if baseline is None or baseline <= 0 or stamp is None or side not in ('LONG', 'SHORT'):
        return None
    if not bars:
        return None
    next_ts = int(stamp) + ONE_MS
    expected = next_ts
    favorable = 0.0
    observed = 0
    # The collector contract is at most 360 consecutive, fully closed bars.
    for item in bars[:360]:
        try:
            ts, _open, high, low, _close = item[:5]
            ts = int(ts)
            high = _num(high)
            low = _num(low)
        except (TypeError, ValueError, IndexError):
            break
        if ts != expected or high is None or low is None or low > high or low <= 0:
            break
        move = ((high / baseline - 1.0) * 100.0 if side == 'LONG'
                else (1.0 - low / baseline) * 100.0)
        favorable = max(favorable, move)
        observed += 1
        expected += ONE_MS
    if observed == 0:
        return None
    return {'reaction_mfe_pct': float(max(0.0, favorable)),
            'reaction_observed_min': float(observed)}


def attach_observed_reaction_runs(project, frame):
    """Enrich Continuous rows from persisted paths; never scan live Redis.

    This deliberately does NOT use followups.mfe_180m_pct: that field has a
    different price baseline (the next-bar OPEN) from Legacy reaction run.
    """
    import pandas as pd
    import zlib
    from reaction_lab_auto_paths import decode_path

    if frame is None or frame.empty:
        return pd.DataFrame() if frame is None else frame.copy()
    out = frame.copy()
    out['reaction_mfe_pct'] = float('nan')
    out['reaction_observed_min'] = float('nan')
    db = Path(project) / 'reports/reaction_lab/auto_paths_v1.sqlite3'
    if not db.is_file():
        return out
    index_by_key = {str(k): idx for idx, k in out['event_key'].items()}
    keys = list(index_by_key)
    # In-memory keyed lookups, no writes or global paths loading.
    with closing(sqlite3.connect(f'file:{db.resolve()}?mode=ro', uri=True, timeout=10)) as con:
        for offset in range(0, len(keys), 400):
            batch = keys[offset:offset+400]
            if not batch:
                continue
            placeholders = ','.join('?' for _ in batch)
            query = ('SELECT event_key, path_blob FROM events WHERE count>0 '
                     f'AND event_key IN ({placeholders})')
            for key, blob in con.execute(query, batch):
                if not blob:
                    continue
                idx = index_by_key.get(str(key))
                if idx is None:
                    continue
                try:
                    bars = decode_path(blob)
                except (TypeError, ValueError, zlib.error):
                    # Treat corrupted/missing paths as unavailable; do not invent a run.
                    # The inspector continues to show the Event ID and status.
                    continue
                row = out.loc[idx]
                measure = observed_reaction_run(
                    bars,
                    retest_timestamp=row.get('retest_timestamp'),
                    retest_close=row.get('retest_close'),
                    signal=row.get('signal'),
                )
                if measure is not None:
                    for field, value in measure.items():
                        out.at[idx, field] = value
    return out


def sort_retests_by_run(frame):
    """Highest observed post-REACTION MFE first; no-path events last."""
    if frame is None or frame.empty:
        return frame
    return frame.sort_values(
        ['reaction_mfe_pct', 'retest_timestamp', 'event_key'],
        ascending=[False, False, True], na_position='last', kind='stable',
    ).copy()


def archived_event_candles(project, key):
    path=Path(project)/'reports/reaction_lab/causal_context_v1.sqlite3'
    if not path.is_file():return {}
    with sqlite3.connect(f'file:{path.resolve()}?mode=ro',uri=True,timeout=10) as con:
        result=con.execute('SELECT candles_json FROM contexts WHERE event_key=?',(str(key),)).fetchone()
    if not result:return {}
    try:return json.loads(result[0])
    except (TypeError,ValueError):return {}


def load_selected_path(project, key):
    """One Event ID only; avoids decompressing the full multi-symbol archive."""
    import pandas as pd
    from reaction_lab_auto_paths import decode_path
    db=Path(project)/'reports/reaction_lab/auto_paths_v1.sqlite3'
    columns=['timestamp','open','high','low','close']
    if not db.is_file():return pd.DataFrame(columns=columns)
    with sqlite3.connect(f'file:{db.resolve()}?mode=ro',uri=True,timeout=10) as con:
        row=con.execute('SELECT path_blob FROM events WHERE event_key=?',(str(key),)).fetchone()
    if not row or row[0] is None:return pd.DataFrame(columns=columns)
    return pd.DataFrame(decode_path(row[0]),columns=columns)
