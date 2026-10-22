"""Read-only, shared structural-chart outcome overlay.

PRE structures remain untouched. POST candles and First-Touch labels are
added only as DISPLAY layers. They must never enter feature selection.
"""
from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from contextlib import closing

ROOT = Path('reports/reaction_lab')
MINUTE = 60_000


def _finite_number(x):
    try:
        f = float(x)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def get_post_bars(project, event_key, retest_ts):
    """Exact, contiguous 360m Auto Paths. No network, no writes."""
    from reaction_lab_auto_paths import decode_path
    file = Path(project) / ROOT / 'auto_paths_v1.sqlite3'
    if not file.is_file():
        return [], 'MISSING_DATABASE'
    with closing(sqlite3.connect(file.resolve().as_uri() + '?mode=ro', uri=True)) as con:
        row = con.execute(
            'SELECT entry_ts, state, count, path_blob FROM events WHERE event_key=?',
            (str(event_key),),
        ).fetchone()
    if row is None:
        return [], 'MISSING_PATH'
    entry, state, n, blob = row
    if str(state) != 'COMPLETE':
        return [], str(state)
    if int(n) != 360 or int(entry) != int(retest_ts) + MINUTE:
        return [], 'INVALID_PATH_HEADER'
    try:
        bars = decode_path(blob)
        if len(bars) != 360:
            return [], 'INVALID_LENGTH'
        for i, bar in enumerate(bars):
            if len(bar) < 5 or int(bar[0]) != int(entry) + i * MINUTE:
                return [], 'INVALID_CONTINUITY'
            nums = [_finite_number(v) for v in bar[1:5]]
            if any(v is None or v <= 0 for v in nums):
                return [], 'INVALID_OHLC'
            o, h, l, c = nums
            if h < max(o, c, l) or l > min(o, c, h):
                return [], 'INVALID_OHLC'
    except (TypeError, ValueError, OverflowError, IndexError):
        return [], 'INVALID_PATH_DATA'
    return bars, 'COMPLETE'


def overlay_future(fig, bars, *, retest_ts, side, entry_ts=None,
                   entry_price=None, outcome=None, first_touch_minute=None,
                   tp_pct=1.0, sl_pct=0.5, display_minutes=180,
                   outcome_horizon=180, pre_hours=12):
    """Adds 1m POST candles and labelled FIRST TOUCH to a PRE structural chart.

    Outcome is the frozen First-Touch result *from the selected global horizon*.
    A custom displayed window never recalculates that outcome. For NO_HIT,
    AMBIGUOUS, missing paths or an unentered observation, no hit is invented.
    """
    import pandas as pd
    import plotly.graph_objects as go

    horizon = int(display_minutes)
    if horizon not in (15, 30, 60, 180, 360):
        raise ValueError('Invalid POST display horizon')
    if str(side).upper() not in ('LONG', 'SHORT'):
        raise ValueError('Invalid side')
    if not bars:
        return fig
    part = bars[:horizon]
    dates = pd.to_datetime([int(v[0]) for v in part], unit='ms', utc=True)
    boundary=pd.to_datetime(int(retest_ts)+MINUTE,unit='ms',utc=True)
    ending=pd.to_datetime(int(retest_ts)+horizon*MINUTE+MINUTE,unit='ms',utc=True)
    fig.add_vrect(x0=boundary,x1=ending,fillcolor='#14b8a6',opacity=.045,
                  line_width=0,layer='below')
    fig.add_trace(go.Candlestick(
        x=dates,
        open=[v[1] for v in part], high=[v[2] for v in part],
        low=[v[3] for v in part], close=[v[4] for v in part],
        increasing_line_color='#22c55e', decreasing_line_color='#fb7185',
        name=f'POST · 1m ({len(part)} velas)', showlegend=True,
        opacity=0.92,
    ))
    ep = _finite_number(entry_price)
    ts = _finite_number(entry_ts)
    tp = _finite_number(tp_pct)
    sl = _finite_number(sl_pct)
    if ep is not None and ep > 0 and ts is not None and tp is not None and sl is not None and tp > 0 and sl > 0:
        sign = 1 if side == 'LONG' else -1
        tp_price = ep * (1 + sign * tp / 100)
        sl_price = ep * (1 - sign * sl / 100)
        fig.add_hline(y=ep, line_color='#94a3b8', line_dash='dot',
                      annotation_text='Entrada observable / replay',annotation_position='bottom right')
        fig.add_hline(y=tp_price, line_color='#16a085', line_dash='dash',
                      annotation_text=f'TP +{tp:g}%',annotation_position='top right')
        fig.add_hline(y=sl_price, line_color='#e87985', line_dash='dash',
                      annotation_text=f'SL −{sl:g}%',annotation_position='bottom right')
        entry_dt = pd.to_datetime(int(ts), unit='ms', utc=True)
        fig.add_trace(go.Scatter(
            x=[entry_dt], y=[ep], mode='markers', name='Entrada del replay',
            marker=dict(size=11, symbol='diamond', color='#93c5fd'),
            hovertemplate='Entrada del replay: %{x}<br>Precio: %{y:.8g}<extra></extra>',
        ))
        first = _finite_number(first_touch_minute)
        if str(outcome) in ('TP_FIRST', 'SL_FIRST') and first is not None:
            minute = int(first)
            # First-Touch minute is an absolute minute index after the retest.
            # Never take it from the entry time; delayed entries differ.
            if 1 <= minute <= len(part) and minute <= int(outcome_horizon):
                hit_is_tp = str(outcome) == 'TP_FIRST'
                fig.add_trace(go.Scatter(
                    x=[dates[minute-1]], y=[tp_price if hit_is_tp else sl_price],
                    mode='markers+text',
                    text=['TP PRIMERO' if hit_is_tp else 'SL PRIMERO'],
                    textposition='top center',
                    name='Primer desenlace',
                    marker=dict(size=14, symbol='star', color='#16a085' if hit_is_tp else '#e87985',
                                line=dict(color='#ffffff',width=1)),
                    hovertemplate=f"{str(outcome)} · minuto {minute} desde retest<extra></extra>",
                ))
    fig.update_layout(
        showlegend=True,
        legend=dict(orientation='h', y=-.16, x=0),
        height=490,
        xaxis_rangeslider_visible=False,
    )
    fig.update_xaxes(range=[
        pd.to_datetime(int(retest_ts)-int(pre_hours)*3600_000,unit='ms',utc=True),
        pd.to_datetime(int(retest_ts)+horizon*MINUTE+MINUTE,unit='ms',utc=True),
    ])
    return fig


def outcome_explanation(outcome, first_touch_minute, *, tp_pct, sl_pct, outcome_horizon,
                        display_minutes, path_state='COMPLETE', entry_mode='OBSERVED_OPEN'):
    if path_state != 'COMPLETE':
        return f'POST sin trayectoria completa ({path_state}). Sin resultado extrapolado.'
    label = str(outcome) if outcome is not None else 'SIN_PATH'
    first = _finite_number(first_touch_minute)
    tail = (f' · primer toque: minuto {int(first)} desde el retest'
            if label in ('TP_FIRST','SL_FIRST') and first is not None else '')
    return (f'Replay seleccionado: TP +{float(tp_pct):g}% / SL −{float(sl_pct):g}% · '
            f'horizonte {int(outcome_horizon)}m · **{label}**{tail}. '
            f'Gráfico POST: {int(display_minutes)}m; si se amplía más allá del horizonte, '
            'las velas adicionales son visuales, no un recálculo del resultado. '
            'REACTION = cierre de su vela 1m; entrada = ' +
            ('apertura observable del replay.' if entry_mode == 'OBSERVED_OPEN'
             else 'apertura histórica teórica (no necesariamente ejecutable).'))
