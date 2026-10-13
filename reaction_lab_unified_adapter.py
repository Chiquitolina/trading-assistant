"""Read-only adapter for the canonical Legacy REACTION inspector.

Input is the immutable Continuous V1 scanner/context cohort. Reuse the
canonical build_confirmed_swing_retest_detail_chart() in dashboard/app.py,
without running live Redis scans or fabricating candles/forward metrics.
"""
from __future__ import annotations
import json
import math
import sqlite3
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
