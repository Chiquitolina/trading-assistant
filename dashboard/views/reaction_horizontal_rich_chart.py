"""Pure visualization helpers for the Horizontal Level Rich Card.

Do not touch the Atlas V2 levels, PRE/KNOWN classifications, or POST outcomes.
The historic HTF candle widths remain proportional to their 15m/30m duration
when overlaid with 1m candles on one Plotly time axis.
"""
from __future__ import annotations

import math
from typing import Any

TF_MINUTES = {'15m': 15, '30m': 30}
UP = '#20c997'
DOWN = '#fb7185'


def _candles_from_trace(trace):
    """Extract only the already loaded PRE closed HTF bars."""
    import pandas as pd

    candles = []
    for stamp, o, h, lo, c in zip(
        trace.x, trace.open, trace.high, trace.low, trace.close
    ):
        vals = (float(o), float(h), float(lo), float(c))
        if not all(math.isfinite(v) and v > 0 for v in vals):
            raise ValueError('PRE HTF contains invalid OHLC')
        if vals[1] < max(vals[0], vals[3], vals[2]) or vals[2] > min(vals[0], vals[3]):
            raise ValueError('PRE HTF inconsistent OHLC')
        candles.append((pd.Timestamp(stamp), *vals))
    return candles


def confirmed_swing_lows(candles, *, left=3, right=3, latest=None):
    """3x3 local lows; require the 3 following bars to be already PRE closed.

    This is a visual swing progression marker, NOT a recomputation of Atlas V2
    levels or its ATR prominence tests. The event/retest candle is never used.
    """
    n = len(candles)
    out = []
    for i in range(left, n-right):
        lo = float(candles[i][3])
        surrounding = (
            [float(candles[j][3]) for j in range(i-left, i)]
            + [float(candles[j][3]) for j in range(i+1, i+right+1)]
        )
        if all(lo < other for other in surrounding):
            if latest is None or candles[i][0] >= latest:
                out.append((candles[i][0], lo, candles[i+right][0]))
    return out


def enhance_pre_candles(fig, timeframe, *, show_higher_lows=True, visible_hours=12):
    """Return fig and PRE-only diagnostic, preserving all Atlas/POST information.

    Replace only the original HTF candlestick with filled bars and wicks.
    Bar width is 0.76 * actual timeframe (milliseconds on a date axis), never
    inferred from the 1m POST spacing. Doji bodies are represented by ticks.
    """
    import pandas as pd
    import plotly.graph_objects as go

    if timeframe not in TF_MINUTES:
        raise ValueError(f'Unsupported timeframe: {timeframe}')
    source = next((tr for tr in fig.data if getattr(tr, 'type', None) == 'candlestick'
                   and getattr(tr, 'name', None) == timeframe), None)
    if source is None:
        return fig, {'status': 'NO_PRE_CANDLES', 'count': 0}

    candles = _candles_from_trace(source)
    # Plotly permits removing traces, then adding new ones; swapping to a
    # newly created fig.data tuple directly is not permitted by Figure.
    fig.data = tuple(t for t in fig.data if t is not source)

    if not candles:
        return fig, {'status': 'NO_PRE_CANDLES', 'count': 0}
    width_ms = int(TF_MINUTES[timeframe] * 60_000 * .76)
    body_x, body_y, body_base, body_color, custom = [], [], [], [], []
    up_x, up_y, down_x, down_y = [], [], [], []
    doji_x, doji_y, doji_color = [], [], []
    for stamp, o, h, lo, c in candles:
        rising = c >= o
        body_x.append(stamp)
        body_y.append(abs(c-o))
        body_base.append(min(o,c))
        body_color.append(UP if rising else DOWN)
        custom.append([o,h,lo,c])
        xx,yy=(up_x,up_y) if rising else (down_x,down_y)
        xx.extend([stamp,stamp,None]);yy.extend([lo,h,None])
        if c == o:
            doji_x.append(stamp);doji_y.append(o);doji_color.append(UP)

    for xx, yy, color in [(up_x,up_y,UP),(down_x,down_y,DOWN)]:
        if xx:
            fig.add_trace(go.Scatter(
                x=xx,y=yy,mode='lines',line=dict(color=color,width=1.4),
                hoverinfo='skip',showlegend=False,name=f'Mechas PRE {timeframe}',
            ))
    fig.add_trace(go.Bar(
        x=body_x,y=body_y,base=body_base,width=width_ms,
        marker=dict(color=body_color,line=dict(color=body_color,width=.8)),
        customdata=custom,opacity=.98,showlegend=False,
        name=f'Cuerpos PRE · {timeframe}',
        hovertemplate=(f'<b>PRE {timeframe}</b><br>%{{x|%d %b %H:%M}} UTC'
                       '<br>O: %{customdata[0]:.8g}'
                       '<br>H: %{customdata[1]:.8g}'
                       '<br>L: %{customdata[2]:.8g}'
                       '<br>C: %{customdata[3]:.8g}<extra></extra>'),
    ))
    if doji_x:
        fig.add_trace(go.Scatter(
            x=doji_x,y=doji_y,mode='markers',
            marker=dict(symbol='line-ew',size=8,color=doji_color,line=dict(width=2)),
            hoverinfo='skip',showlegend=False,name='Doji PRE',
        ))
    fig.update_layout(barmode='overlay')

    if not show_higher_lows:
        return fig, {'status': 'DISABLED', 'count': 0}

    last_stamp=candles[-1][0]
    lower_bound=last_stamp-pd.Timedelta(hours=visible_hours)
    lows=confirmed_swing_lows(candles,latest=lower_bound)
    status='INSUFFICIENT_PIVOTS'
    if len(lows)>=2:
        a,b=lows[-2:]
        if b[1]>a[1]: status='HIGHER_LOWS'
        elif b[1]<a[1]: status='LOWER_LOWS'
        else: status='EQUAL_LOWS'
        # Always mark the local lows; dashed connecting segment conveys their
        # progression without asserting that a triangle/breakout exists.
        fig.add_trace(go.Scatter(
            x=[a[0],b[0]],y=[a[1],b[1]],mode='lines+markers',
            line=dict(color='#fbbf24',width=2,dash='dot'),
            marker=dict(color='#fbbf24',size=8,symbol='circle-open',line=dict(width=2)),
            name='Mínimos locales confirmados PRE 3×3',showlegend=True,
            hovertemplate='Mínimo local 3×3 confirmado PRE<br>%{x|%d %b %H:%M} UTC · %{y:.8g}<extra></extra>',
        ))
    return fig, {'status':status,'count':len(lows),
                 'last_two': tuple((l[0],l[1]) for l in lows[-2:])}


def diagnostic_text(diag,tf):
    status=diag.get('status')
    if status=='HIGHER_LOWS':
        return f'**{tf} · mínimos ascendentes (HL)** entre los últimos dos pivotes 3×3 confirmados PRE. No confirma un triángulo ni predice ruptura.'
    if status=='LOWER_LOWS':
        return f'**{tf} · mínimos descendentes (LL)** en los últimos dos pivotes 3×3 confirmados PRE.'
    if status=='EQUAL_LOWS':
        return f'**{tf} · dos mínimos locales de precio igual** (PRE).'
    if status=='INSUFFICIENT_PIVOTS':
        return f'**{tf} · no hay dos mínimos 3×3 confirmados en las últimas 12h PRE.** Eso no descarta HL de menor timeframe.'
    if status=='DISABLED':
        return 'Marcas HL ocultas; velas HTF con cuerpos rellenos.'
    return 'No hay histórico HTF suficiente para inspeccionar mínimos.'
