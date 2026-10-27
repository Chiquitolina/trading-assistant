"""HLL V2.9.3 visual-only percentage guides. No order execution or outcome calculation.

For LONG breakout research: percentage offsets are always measured from the
specified *available next-open reference*, never the resistance or the signal close.
"""
from __future__ import annotations

import math
from collections import OrderedDict

import pandas as pd

MIN = 60_000


def _ms(value):
    if value is None or pd.isna(value):
        return None
    try:
        n = int(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return n if n > 0 else None


def _valid_price(price):
    try:
        number = float(price)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def _next_open_30m(bars, event_closed_ts):
    """At a 30m candle close T, its successor OPEN has timestamp T."""
    clock = _ms(event_closed_ts)
    if clock is None:
        return None
    if not {'timestamp', 'open'}.issubset(bars.columns):
        raise ValueError('OHLC 30m sin columnas timestamp/open')
    a = bars.loc[bars.timestamp.eq(clock), 'open']
    if len(a) != 1:
        return None  # missing next candle; no inferred entry
    price = a.iloc[0]
    return float(price) if _valid_price(price) else None


def available_reference_entries(row, bars, survival_rows=None, continuation_events=None):
    """Return only directly observed entry reference prices, keyed by signal clock.

    These are REFERENCE prices for a visual LONG scenario, not executed fills.
    Flip uses the frozen V2.9.2 entry convention and only valid complete 1m
    coverage. V2.9 continuation/reclaim uses NEXT 30m OPEN (not 1m).
    """
    result = OrderedDict()
    breakout = _ms(row.get('breakout_closed_ts'))
    price = _next_open_30m(bars, breakout)
    if price is not None:
        result['BREAKOUT_30M'] = dict(
            label='Breakout · OPEN siguiente 30m', price=price, entry_ts=breakout)

    flip = _ms(row.get('flip_confirmed_closed_ts'))
    if flip is not None and survival_rows is not None and not survival_rows.empty:
        valid = survival_rows.loc[(survival_rows.horizon_min.eq(120)) &
                                  (survival_rows.path_state.eq('COMPLETE_1M'))]
        if len(valid) > 1:
            raise ValueError('Referencia flip: más de un horizonte 120m para este intento')
        if len(valid) == 1:
            item = valid.iloc[0]
            start = flip + MIN
            if _ms(item.get('entry_ts')) != start:
                raise ValueError('Referencia flip: timestamp 1m no coincide con el cierre confirmado')
            if _valid_price(item.get('entry_open')):
                result['FLIP_1M'] = dict(
                    label='Flip confirmado · OPEN siguiente 1m',
                    price=float(item.entry_open), entry_ts=start)

    for ev in continuation_events or []:
        if ev.get('status') != 'CONFIRMED':
            continue
        branch = ev.get('branch')
        if branch not in ('BREAKOUT_CONTINUATION', 'RECLAIM_CONTINUATION'):
            continue
        event_ts = _ms(ev.get('confirmed_ts'))
        entry_price = _next_open_30m(bars, event_ts)
        if entry_price is None:
            continue
        key = ('V29_CONTINUATION_30M' if branch == 'BREAKOUT_CONTINUATION'
               else 'V29_RECLAIM_30M')
        if key in result:
            raise ValueError(f'Referencia V2.9 duplicada: {branch}')
        result[key] = dict(label=(
            'Continuación V2.9 · OPEN siguiente 30m' if branch == 'BREAKOUT_CONTINUATION'
            else 'Reclaim V2.9 · OPEN siguiente 30m'),
            price=entry_price, entry_ts=event_ts)
    return result


def price_guides(price, max_pct=5):
    """Price ladder for a hypothetical LONG position, with exact %-from-entry."""
    if not _valid_price(price):
        raise ValueError('Se necesita un precio base positivo, real y finito')
    if isinstance(max_pct, bool) or not isinstance(max_pct, int) or not 1 <= max_pct <= 5:
        raise ValueError('Rango de niveles: 1 a 5 por ciento')
    p = float(price)
    return [(i, p * (1 + i / 100), p * (1 - i / 100)) for i in range(1, max_pct+1)]


def overlay_visual_levels(fig, reference, max_pct=5, include_tp=True, include_sl=True):
    """Overlay ten dotted guide lines and center without calculating PnL.

    Visual LONG scenario only. User must separately simulate 1m order-of-hit,
    fees, spreads, funding and position sizing for any TP/SL conclusions.
    """
    p = float(reference['price'])
    if not include_tp and not include_sl:
        return fig
    levels = price_guides(p, max_pct)
    fmt = lambda v: f'{v:.8g}'

    def line(price, color, text, width=1, dash='dot'):
        fig.add_shape(type='line', xref='x domain', yref='y',
                      x0=0, x1=1, y0=price, y1=price,
                      line=dict(color=color, width=width, dash=dash), layer='above')
        fig.add_annotation(xref='x domain', yref='y', x=.995, y=price,
                           text=text, showarrow=False, xanchor='right', yanchor='bottom',
                           font=dict(color=color, size=10),
                           bgcolor='rgba(15,23,42,0.75)', borderpad=1)

    line(p, '#e2e8f0', f'REF {fmt(p)}', width=1.8, dash='solid')
    for i, tp, sl in levels:
        if include_tp:
            line(tp, 'rgba(29,185,135,.65)', f'TP +{i}% · {fmt(tp)}')
        if include_sl:
            line(sl, 'rgba(242,100,114,.65)', f'SL −{i}% · {fmt(sl)}')
    # Deliberately preserve plotly autoscale: historical candle spikes and 1..5%
    # price guides remain visible; users can zoom into the candles afterward.
    fig.update_layout(margin=dict(l=12, r=14, t=22, b=15))
    return fig
