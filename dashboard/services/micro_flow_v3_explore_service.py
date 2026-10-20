"""Read-only orchestration for the Micro Flow V3 Explore workspace.

Do not modify source tapes, replay artifacts, frozen detectors or result labels.
The default Explore catalogue exposes ONLY impulse-time features; later labels
remain in their original engine outputs and are never used to select a case.
"""
from __future__ import annotations

from research.micro_flow_v3.aggression_exhaustion_discovery import discover_exhaustion
from research.micro_flow_v3.microstructure_exhaustion_features import (
    build_exhaustion_feature_catalog,
)
from research.micro_flow_v3.swing_sweep_discovery import detect_causal_swings
from research.micro_flow_v3.volume_aggression_confluence import (
    observed_volume_candles, discover_volume_exhaustion,
)

FEATURE_LABELS = {
    'AGGRESSIVE_FLOW_PRICE_DISAGREEMENT': 'Divergencia flujo/precio',
    'AGGRESSIVE_FLOW_LOW_PRICE_PROGRESS': 'Poco desplazamiento',
    'AGGRESSIVE_FLOW_PRICE_ALIGNED': 'Flujo y precio alineados',
    'FLOW_NOT_ALIGNED_WITH_IMPULSE_SIDE': 'Delta contrario al impulso',
}
FOCUS_OPTIONS = (
    'Divergencia flujo/precio', 'Rechazo observable', 'Poco desplazamiento',
    'Todas las agresiones', 'Volume Exhaustion 1m/5m',
)


def build_explore_catalog(buckets, *, timeframe_minutes=1, swing_bars=3,
                          feature_window_seconds=15, min_coverage=.60):
    """One causal source cohort. Every published event is retained.

    VE and AE detectors have their own independent publication timestamps;
    joining them visually does NOT itself create an executable confluence entry.
    """
    if timeframe_minutes not in (1, 5) or swing_bars not in (2, 3, 5):
        raise ValueError('Timeframe or swing configuration unsupported')
    if feature_window_seconds not in (5, 15, 30, 60):
        raise ValueError('Unsupported feature window')
    candles = observed_volume_candles(buckets, timeframe_minutes)
    swings = detect_causal_swings(candles, swing_bars)
    aggression = discover_exhaustion(buckets)
    features = build_exhaustion_feature_catalog(
        buckets, aggression, min_coverage=min_coverage)
    ve = discover_volume_exhaustion(candles, timeframe_minutes=timeframe_minutes)
    by_id = {r['event_id']: r for r in features['features']}
    events = []
    feature_key = f'{feature_window_seconds}s'
    for ev in aggression['events']:
        f = by_id[ev['event_id']]['windows'][feature_key]
        if f['state'] == 'OBSERVED':
            tag = FEATURE_LABELS.get(f['flow_price_alignment'], 'Sin clasificar')
            rejected = f.get('rejection_after_prior_extreme_observed') is True
        else:
            tag = 'Sin cobertura suficiente'
            rejected = False
        events.append({
            'event_id': ev['event_id'], 'kind': 'AE',
            'segment_id': ev['segment_id'], 'available_ms': int(ev['impulse']['available_ms']),
            'pressure_side': ev['pressure_side'], 'hypothesis_direction': ev['hypothesis_direction'],
            'label': tag, 'rejection': rejected, 'coverage': f['state'],
            'relative_volume': ev['impulse_features']['relative_volume'],
            'imbalance': ev['impulse_features']['imbalance'],
            'signed_move_pct': f.get('pressure_signed_move_pct'),
            'impact_bps_per_100base': f.get('signed_impact_bps_per_100base_gross_qty'),
            'feature': f,
        })
    for ev in ve['events']:
        events.append({
            'event_id': ev['event_id'], 'kind': 'VE',
            'segment_id': ev['segment_id'], 'available_ms': int(ev['available_ms']),
            'pressure_side': ev['pressure_side'], 'hypothesis_direction': ev['hypothesis_direction'],
            'label': 'Volume Exhaustion (RSI + volumen, experimental)',
            'rejection': False, 'coverage': 'OBSERVED',
            'relative_volume': ev['volume_ratio_prior_candles'],
            'imbalance': (ev['delta_qty'] / ev['volume_qty'] if ev['volume_qty'] else None),
            'signed_move_pct': ev['move_pct'], 'impact_bps_per_100base': None,
            'feature': None,
        })
    events.sort(key=lambda e: (e['available_ms'], e['event_id']), reverse=True)
    return {
        'events': events, 'candles': candles, 'swings': swings,
        'aggression': aggression, 'features': features, 've': ve,
        'parameters': {
            'timeframe_minutes': timeframe_minutes,
            'swing_bars': swing_bars,
            'feature_window_seconds': feature_window_seconds,
            'min_coverage': min_coverage,
        },
    }


def filter_explore_events(events, *, focus='Divergencia flujo/precio',
                          pressure='Ambas'):
    """Filter only by known-as-of attributes; never by outcomes or failure."""
    if focus not in FOCUS_OPTIONS:
        raise ValueError('Unknown focus')
    if pressure not in ('Ambas', 'BUY', 'SELL'):
        raise ValueError('Unknown pressure filter')
    out = []
    for e in events:
        if pressure != 'Ambas' and e['pressure_side'] != pressure:
            continue
        if focus == 'Volume Exhaustion 1m/5m':
            accept = e['kind'] == 'VE'
        elif e['kind'] != 'AE':
            accept = False
        elif focus == 'Todas las agresiones':
            accept = True
        elif focus == 'Rechazo observable':
            accept = e['rejection']
        else:
            accept = e['label'] == focus
        if accept:
            out.append(e)
    return out


def observed_view(buckets, candles, *, segment_id, anchor_ms,
                  before_minutes=20, after_minutes=15, reveal=False):
    """Slice by real availability, preserving publication boundaries and segments.

    No candle whose real publication is later than the cutoff can be shown.
    In reveal mode, the returned future is for visual retrospective inspection.
    """
    start = anchor_ms - before_minutes * 60_000
    cutoff = anchor_ms + after_minutes * 60_000 if reveal else anchor_ms
    candle_rows = [c for c in candles if c['segment_id'] == segment_id
                   and c['timestamp'] >= start and c['timestamp'] <= cutoff
                   and c['available_ms'] <= cutoff]
    bucket_rows = [b for b in buckets if b['segment_id'] == segment_id
                   and start <= b['second_start_ms'] <= cutoff
                   and b['available_wall_ns'] <= cutoff * 1_000_000]
    return {'candles': candle_rows, 'buckets': bucket_rows,
            'start_ms': start, 'cutoff_ms': cutoff}
