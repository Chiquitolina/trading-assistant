"""Visual entry anchors for the HLL research dashboard (LONG scenarios only).

- V2.6 defended flip: OPEN of 1m bar at known flip confirmation + one minute,
  matching the frozen V2.9.2 entry clock (not the flip signal candle).
- V2.9 breakout continuation / post-failure reclaim: OPEN of the 30m bar
  starting at the signal confirmation's close (not the signal candle close).
- Original breakout next OPEN is a comparison reference, NOT a confirmed entry.

All entries are observed data, never filled with a previous close or zone price.
No order execution, no reclassification, and no future-based signal selection.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import math

import pandas as pd

from dashboard.views.hll_v292_percent_guides import (
    MIN, _ms, _valid_price, available_reference_entries,
)

ENTRY_INFO = {
    'FLIP_1M': ('ENTRADA FLIP', '#21c98d', 'diamond', 'Flip V2.6 · siguiente OPEN 1m (+1 min)'),
    'V29_CONTINUATION_30M': ('ENTRADA CONT.', '#4cbfee', 'circle', 'Continuación V2.9 · siguiente OPEN 30m'),
    'V29_RECLAIM_30M': ('ENTRADA RECLAIM', '#c191f5', 'star', 'Reclaim V2.9 · siguiente OPEN 30m'),
    'BREAKOUT_30M': ('REF. BREAKOUT', '#b6bac4', 'circle-open', 'Referencia desde CROSS_UP · siguiente OPEN 30m'),
}


@lru_cache(maxsize=512)
def _read_observed_1m_open(filename: str, entry_ts: int, size_bytes: int, mtime_ns: int):
    """Find an *existing* minute's OPEN, even if a later path has a GAP.

    The file fingerprint is part of the cache key: updated archives get re-read.
    Only the matching candle can support an entry; no interpolation. This lookup
    is for plotting an entry, NEVER evidence for a complete future trajectory.
    """
    found = None
    previous_timestamp = None
    with pd.read_csv(filename, usecols=['timestamp', 'open', 'high', 'low', 'close'],
                     chunksize=200_000) as reader:
        for chunk in reader:
            ts = pd.to_numeric(chunk['timestamp'], errors='raise').astype('int64')
            if ts.duplicated().any() or (len(ts) > 1 and not ts.is_monotonic_increasing):
                raise ValueError('Histórico 1m con timestamps duplicados o desordenados')
            if previous_timestamp is not None and len(ts) and int(ts.iloc[0]) <= previous_timestamp:
                raise ValueError('Histórico 1m no monótono entre bloques')
            if len(ts):
                previous_timestamp = int(ts.iloc[-1])
            matches = chunk.loc[ts.eq(int(entry_ts))]
            if len(matches) > 1 or (found is not None and not matches.empty):
                raise ValueError('Timestamp 1m de entrada duplicado')
            if not matches.empty:
                candle = matches.iloc[0]
                prices = [float(candle[x]) for x in ('open', 'high', 'low', 'close')]
                if not all(math.isfinite(x) and x > 0 for x in prices):
                    raise ValueError('OHLC 1m de entrada no finito/positivo')
                op, hi, lo, cl = prices
                if hi < max(op, cl, lo) or lo > min(op, cl, hi):
                    raise ValueError('OHLC 1m de entrada inconsistente')
                found = op
            # Historical 1m is ascending and was validated for V2.9.2.
            # If we passed the timestamp without finding it, it is truly absent.
            if len(ts) and int(ts.iloc[-1]) > int(entry_ts):
                break
    return found


def _verified_v29_reference(event, bars):
    """Anchor a CONFIRMED V2.9 event to its actual next 30m OPEN.

    The frozen V2.9 CSV already stores next_open_ts/next_open. Use those
    fields to detect schema / time mismatches rather than silently discarding
    a genuinely confirmed continuation. The observed 30m candle must agree.
    """
    closed_ts = _ms(event.get('confirmed_ts'))
    if closed_ts is None:
        raise ValueError('V2.9 confirmado sin confirmed_ts')
    stored_ts = _ms(event.get('next_open_ts'))
    if stored_ts is not None and stored_ts != closed_ts:
        raise ValueError('V2.9: next_open_ts no coincide con confirmed_ts')
    if not {'timestamp', 'open'}.issubset(bars.columns):
        raise ValueError('Histórico 30m sin timestamp/open')
    matches = bars.loc[bars.timestamp.eq(closed_ts)]
    if len(matches) != 1:
        raise ValueError(f'V2.9 confirmado pero OPEN 30m no verificable en {closed_ts}: {len(matches)} velas')
    observed = matches.iloc[0].open
    if not _valid_price(observed):
        raise ValueError('V2.9 confirmado con OPEN 30m inválido')
    stored = event.get('next_open')
    if _valid_price(stored):
        if abs(float(observed)-float(stored)) > max(1e-10, abs(float(stored))*1e-7):
            raise ValueError(f'V2.9: next_open del CSV {stored} != OPEN OHLC {observed}')
    elif stored is not None and not pd.isna(stored):
        raise ValueError('V2.9: next_open registrado no es un precio válido')
    branch = str(event.get('branch','')).strip()
    return dict(label=('Continuación V2.9 · OPEN siguiente 30m'
                       if branch == 'BREAKOUT_CONTINUATION'
                       else 'Reclaim V2.9 · OPEN siguiente 30m'),
                price=float(observed), entry_ts=closed_ts)


def get_entries(row, bars, survival_rows=None, continuation_events=None,
                minute_dir=None, include_breakout_reference=False):
    """All available entry anchors, keyed by path, independent of guide selection.

    `entry_ts` is the OPEN candle timestamp. The signal must have been known
    strictly earlier (or at the 30m next-open boundary). Missing means no
    marker, never an invented price. No mock executed fills are implied.
    """
    refs = available_reference_entries(row, bars, survival_rows, continuation_events)
    # Resolve confirmed paths against both the frozen V2.9 event and actual
    # next OPEN. A valid CONFIRMED signal should never disappear silently.
    expected_branches = {
        'BREAKOUT_CONTINUATION': 'V29_CONTINUATION_30M',
        'RECLAIM_CONTINUATION': 'V29_RECLAIM_30M',
    }
    confirmed_events = []
    for ev in continuation_events or []:
        branch = str(ev.get('branch','')).strip()
        status = str(ev.get('status','')).strip()
        if branch not in expected_branches or status != 'CONFIRMED':
            continue
        confirmed_events.append(ev)
        key = expected_branches[branch]
        evidence = _verified_v29_reference(ev, bars)
        if key in refs and (refs[key]['entry_ts'] != evidence['entry_ts'] or
                            abs(float(refs[key]['price'])-evidence['price']) >
                            max(1e-10,evidence['price']*1e-7)):
            raise ValueError(f'Entrada {key} no coincide entre helpers visuales')
        refs[key] = evidence
    if len(set(str(ev.get('branch')).strip() for ev in confirmed_events)) != len(confirmed_events):
        raise ValueError('V2.9: múltiples confirmaciones para una misma ruta')
    flip = _ms(row.get('flip_confirmed_closed_ts'))
    # If the 120m path is censored, a real entry candle can still be present.
    # A direct archive lookup is allowed, but it does NOT make path COMPLETE_1M.
    if flip is not None and 'FLIP_1M' not in refs and minute_dir is not None:
        path = Path(minute_dir) / f"{row['symbol']}_1m.csv"
        if path.is_file():
            stamp = path.stat()
            start = flip + MIN
            price = _read_observed_1m_open(str(path), start, stamp.st_size, stamp.st_mtime_ns)
            if price is not None:
                refs['FLIP_1M'] = dict(
                    label='Flip confirmado · OPEN 1m observado (+1 min)',
                    price=price, entry_ts=start,
                )
    allowed = ['FLIP_1M', 'V29_CONTINUATION_30M', 'V29_RECLAIM_30M']
    if include_breakout_reference:
        allowed += ['BREAKOUT_30M']
    entries = []
    for key in allowed:
        if key not in refs:
            continue
        item = refs[key]
        clock = _ms(item.get('entry_ts'))
        price = item.get('price')
        if clock is None or not _valid_price(price):
            continue
        if key == 'FLIP_1M':
            known_ts = flip
            if known_ts is None or clock != known_ts + MIN:
                raise ValueError('Entrada flip con reloj temporal inconsistente')
        elif key == 'BREAKOUT_30M':
            known_ts = _ms(row.get('breakout_closed_ts'))
            if known_ts is None or clock != known_ts:
                raise ValueError('Entrada breakout con reloj temporal inconsistente')
        else:
            branch = 'BREAKOUT_CONTINUATION' if key == 'V29_CONTINUATION_30M' else 'RECLAIM_CONTINUATION'
            matches = [e for e in continuation_events or []
                       if e.get('branch') == branch and e.get('status') == 'CONFIRMED']
            if len(matches) != 1 or clock != _ms(matches[0].get('confirmed_ts')):
                raise ValueError('Entrada V2.9 con reloj temporal inconsistente')
            signal = matches[0]
            signal_open_ts = _ms(signal.get('signal_open_ts'))
            if signal_open_ts is None or signal_open_ts + 1_800_000 != clock:
                raise ValueError('Entrada V2.9: cierre de la vela de señal inconsistente')
            candle = bars.loc[bars.timestamp.eq(signal_open_ts)]
            if len(candle) != 1 or not _valid_price(signal.get('signal_close')):
                raise ValueError('Entrada V2.9: falta vela de confirmación OHLC')
            recorded = float(signal['signal_close'])
            if abs(float(candle.close.iloc[0]) - recorded) > max(1e-10,abs(recorded)*1e-7):
                raise ValueError('Entrada V2.9: cierre de confirmación distinto de OHLC')
            known_ts = clock
        short, color, marker, description = ENTRY_INFO[key]
        entries.append(dict(key=key, short=short, color=color, marker=marker,
                            description=description, price=float(price),
                            entry_ts=int(clock), known_ts=int(known_ts),
                            label=item.get('label',description)))
    return entries


def overlay_entry_markers(fig, entries, selected_reference=None):
    """Plot one point per actually observed OPEN; no full-height entry lines."""
    import plotly.graph_objects as go
    selected_key = selected_reference
    for entry in entries:
        ts = pd.Timestamp(entry['entry_ts'], unit='ms', tz='UTC')
        time_string = ts.strftime('%Y-%m-%d %H:%M UTC')
        selected = entry['key'] == selected_key
        fig.add_trace(go.Scatter(
            x=[ts], y=[entry['price']], mode='markers+text',
            text=[entry['short']], textposition='top center',
            textfont=dict(color=entry['color'],size=10),
            marker=dict(symbol=entry['marker'],size=17 if selected else 14,
                        color=entry['color'],line=dict(color='#152231',width=2)),
            name=entry['description'] + (' (referencia seleccionada)' if selected else ''),
            hovertemplate=(f"<b>{entry['short']}</b><br>{entry['description']}"
                           f"<br>OPEN: {time_string}"
                           '<br>Precio observado: %{y:.8g}'
                           '<br>Marcador informativo, no orden ejecutada<extra></extra>'),
        ))
    return fig
