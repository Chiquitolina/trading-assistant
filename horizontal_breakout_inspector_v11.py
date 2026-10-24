"""Read-only interpretation helpers for Horizontal Breakout Lab.

Historical V1 CSV values remain unmodified. A FIRST close over the frozen
threshold is not equivalent to a sustained breakout. The confirmation diagnostic
requires additional POST data and must never be used as a PRE-only signal.
"""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd

M = 60_000
TF = 30 * M


def display_state(raw: str) -> str:
    """Present legacy run labels accurately, without changing the CSV."""
    return 'CLOSE_ABOVE_LEVEL' if raw == 'BREAKOUT' else str(raw)


def _number(record, key, default=None):
    value = record.get(key, default)
    if value is None or pd.isna(value):
        return default
    return float(value)


def _moment(ms):
    return pd.Timestamp(int(ms), unit='ms', tz='UTC').isoformat()


def ceiling_origin(record, thirty):
    """Return PRE-only window/touch details, with independently recorded visits.

    Level comes from median of ALL highs within tolerance in the frozen window;
    touch timestamps are representative visits, NOT necessarily its only inputs.
    """
    freeze = int(record['formation_closed_ts'])
    window = int(record['window'])
    start = freeze - window * TF
    f = thirty[(thirty.timestamp >= start) & (thirty.timestamp < freeze)].copy()
    try:
        times = json.loads(record.get('touch_open_timestamps', '[]'))
        times = [int(t) for t in times]
    except (ValueError, TypeError, OverflowError):
        times = []
    observed = {int(r.timestamp):r for r in f.itertuples(index=False)}
    touches = [dict(number=i + 1, open_ts=t, utc=_moment(t),
                    high=float(observed[t].high) if t in observed else None)
               for i,t in enumerate(times)]
    return {'start_ts':start, 'start_utc':_moment(start), 'freeze_ts':freeze,
            'freeze_utc':_moment(freeze), 'window':window, 'visible_bars_in_window':len(f),
            'complete_window':(len(f)==window and (len(f)==1 or
                               bool(np.all(np.diff(f.timestamp.to_numpy(dtype='int64'))==TF)))),
            'touches':touches}


def trigger_inspection(thirty, minute, record, trigger='30m', margin_pct=.1):
    """Inspect ONLY the first observed crossing candle, without later bars."""
    level = _number(record, 'level')
    b = _number(record, 'breakout_ts')
    if b is None or level is None:
        return {'status':'NO_CROSS', 'threshold':None}
    ts = int(b)
    threshold = level * (1 + margin_pct/100)
    tf = TF if trigger == '30m' else M
    source = thirty if trigger == '30m' else minute
    start = ts - tf
    bar = (source[source.timestamp == start] if 'timestamp' in source.columns
           else pd.DataFrame())
    result = {'status':'TRIGGER_BAR_MISSING', 'threshold':threshold,
              'trigger_closed_ts':ts, 'trigger_open_ts':start, 'trigger_close':None,
              'margin_pct':margin_pct, 'trigger':trigger}
    if bar.empty:
        return result
    close = float(bar.iloc[0]['close'])
    result['trigger_close'] = close
    result['cross_above_threshold_pct'] = (close/level - 1)*100
    result['status'] = 'CLOSE_ABOVE_LEVEL' if close > threshold else 'TRIGGER_INCONSISTENT_WITH_ARCHIVE'
    return result


def confirmation_post(thirty, record, observation_bars=4):
    """Post-only diagnostic: two consecutive *future* closed 30m candles above zone.

    For a 1m trigger, the unfinished containing 30m candle is never used.
    The test is deliberately descriptive, never used to relabel original trades.
    """
    b = _number(record, 'breakout_ts')
    level = _number(record, 'level')
    zhi = _number(record, 'zone_high')
    if b is None or level is None or zhi is None:
        return {'status':'NO_CROSS', 'evaluated_bars':0}
    b = int(b)
    # Earliest HTF candle opening at or AFTER the triggering observation.
    first_open = ((b + TF - 1) // TF) * TF
    threshold = max(level, zhi)
    after = thirty.set_index('timestamp', drop=False)
    consecutive = 0
    for j in range(observation_bars):
        open_ts = first_open + j * TF
        if open_ts not in after.index:
            return {'status':'INSUFFICIENT_POST_COVERAGE', 'evaluated_bars':j,
                    'threshold':threshold, 'first_unavailable_ts':open_ts}
        close = float(after.loc[open_ts, 'close'])
        consecutive = consecutive + 1 if close > threshold else 0
        if consecutive >= 2:
            return {'status':'BREAKOUT_CONFIRMED_POST', 'evaluated_bars':j+1,
                    'confirmed_at_ts':open_ts+TF, 'threshold':threshold}
    return {'status':'NOT_CONFIRMED_IN_4_BARS', 'evaluated_bars':observation_bars,
            'threshold':threshold}
