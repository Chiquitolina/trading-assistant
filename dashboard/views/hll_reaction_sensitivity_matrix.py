"""Sensitivity comparison for HLL PRE Reaction overlay (read-only research).

Only the source SwingDetector window and its prominence threshold vary.
Original 15m swing confirmation and first 1m retest functions remain in use.
Does NOT compute post-PRE outcomes or infer trading edge from event counts.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from dashboard.views import hll_reaction_pre_overlay as original

SWING_SHAPES = original.SWING_SHAPES
PROMINENCE_GRID_PCT = original.PROMINENCE_GRID_PCT


def combine_first_retests(raw_events, prominence_pct: float):
    """Summarize eligible first retests after the prominence gate.

    Deduplicate REACTION events using the ORIGINAL scanner event identity.
    Failed/indecisive counts are pivot-level first touches (different identity).
    """
    gated = [r for r in raw_events if r['prominence_pct'] >= prominence_pct]
    confirmed = {}
    for r in gated:
        if r['status'] == 'REACTION':
            confirmed.setdefault(r['event_key'], r)
    long_keys = {k for k, r in confirmed.items() if r['side'] == 'LONG'}
    short_keys = {k for k, r in confirmed.items() if r['side'] == 'SHORT'}
    return {
        'events': sorted(confirmed.values(), key=lambda r: (r['known_ts'], r['event_key'])),
        'event_keys': set(confirmed),
        'long': len(long_keys),
        'short': len(short_keys),
        'reactions': len(confirmed),
        'failed_first_retests': sum(r['status'] == 'TOUCH_FAILED' for r in gated),
        'indecisive_first_retests': sum(r['status'] == 'TOUCH_INDECISIVE' for r in gated),
        'all_first_retests': len(gated),
    }


def summarize_grid(candidate_by_shape: dict):
    """Pure summary, easy to test without loading market data."""
    combinations = {}
    for left, right in SWING_SHAPES:
        shape = f'{left}x{right}'
        raw = candidate_by_shape[shape]
        for prom in PROMINENCE_GRID_PCT:
            combinations[(shape, prom)] = combine_first_retests(raw, prom)
    canonical = combinations[('3x3', 0.0)]['event_keys']
    rows = []
    for (shape, prom), r in combinations.items():
        common = len(r['event_keys'] & canonical)
        rows.append({
            'swing': shape, 'prominence_pct': prom,
            'long': r['long'], 'short': r['short'], 'reactions': r['reactions'],
            'failed_first_retests': r['failed_first_retests'],
            'indecisive_first_retests': r['indecisive_first_retests'],
            'all_first_retests': r['all_first_retests'],
            'shared_with_canonical': common,
            'new_vs_canonical': len(r['event_keys'] - canonical),
            'lost_from_canonical': len(canonical - r['event_keys']),
            'canonical_total': len(canonical),
        })
    return rows


def inspect_grid(root: Path, symbol: str, pre_ms: int, pre_candles: int):
    """Load and verify HLL 1m only once, replay 4 swing windows at 0 prominence.

    Higher prominence gates depend ONLY on the swing's original prominence,
    so filtering the complete pivot-level first-touch records is exactly
    equivalent to rerunning SwingDetector with that prominence, without 16
    repeated historical retest scans. Gating occurs before deduplicating IDs.
    """
    root = Path(root)
    if not symbol or '/' in symbol or '\\' in symbol or '..' in symbol:
        raise ValueError('Símbolo inválido')
    if pre_candles not in (48,96,144,192,288,384):
        raise ValueError('Ventana PRE no permitida')
    tape = original.load_verified_minute(
        root / original.MINUTE_DIR / (symbol + '_1m.csv'),
        root / original.MANIFEST_DIR / (symbol + '.json'))
    source = root / 'dashboard/app.py'
    env, swing_class, source_hash = original.canonical_functions(
        str(source), source.stat().st_mtime_ns)
    first_visible_ms = pre_ms - pre_candles*original.TF30
    by_shape = {}
    for left, right in SWING_SHAPES:
        by_shape[f'{left}x{right}'] = original.replay_canonical_pre(
            tape, pre_ms, first_visible_ms,
            env=env, swing_detector_class=swing_class, symbol=symbol,
            left_bars=left, right_bars=right, min_prominence_pct=0.0,
            include_first_retests=True)
    return {
        'rows': summarize_grid(by_shape),
        'source_hash': source_hash,
        'cutoff_ms': pre_ms,
        'first_visible_ms': first_visible_ms,
        'matrices': len(SWING_SHAPES)*len(PROMINENCE_GRID_PCT),
    }


def presentation_matrix(rows):
    """Columns prominence, rows swing, cell reports L/S first-touch REACTIONs."""
    data = []
    for left, right in SWING_SHAPES:
        shape = f'{left}x{right}'
        r = {'Swing 15m':shape}
        for prom in PROMINENCE_GRID_PCT:
            match = next(x for x in rows if x['swing']==shape and x['prominence_pct']==prom)
            r[f'{prom:.2f}%'] = f"{match['reactions']}  (L {match['long']} / S {match['short']})"
        data.append(r)
    return pd.DataFrame(data)
