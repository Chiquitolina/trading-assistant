"""Causal, research-only V3 pressure events, episode grouping and outcomes.

Buy and sell are aggressor-side quantities (not passive absorption). All
return/MFE/MAE values are SIGNED TO SIGNAL DIRECTION: positive is favorable,
negative is adverse. Raw price excursions are named separately.

The first later observed bucket OPEN is a price proxy, NOT an executable fill.
Outcomes are observational, censored when the horizon is not covered, and must
not be interpreted as trading P&L. Never cross segment boundaries.
"""
from bisect import bisect_left
from collections import deque
from statistics import mean


def classify_observed_outcome(directional_return_pct, *, neutral_band_pct=0.01):
    """Ex-post description, not a prediction known at signal time."""
    if directional_return_pct is None:
        return 'CENSORED'
    band = max(0.0, float(neutral_band_pct))
    value = float(directional_return_pct)
    if value > band:
        return 'CONTINUATION'
    if value < -band:
        return 'REVERSAL'
    return 'NEUTRAL'


def cluster_pressure_events(events, *, window_seconds=180):
    """Non-overlapping fixed windows anchored at the first observed signal.

    Each event is assigned to exactly one episode within its causal segment.
    A later signal inside [anchor_ready, anchor_ready + window) belongs to the
    anchor episode, even if its direction conflicts. The next episode starts
    only at or after the fixed boundary. Does not alter signal detection.

    This is a de-duplication convention, NOT proof of statistical independence:
    entry proxy and longer outcomes may still overlap across episode windows.
    """
    seconds = int(window_seconds)
    if seconds <= 0:
        raise ValueError('episode window must be positive')
    window_ms = seconds * 1000
    annotated = []
    episodes = []
    for event in sorted(events, key=lambda e: (
            int(e['segment_id']), int(e['ready_ms']), str(e['event_id']))):
        item = dict(event)
        segment = int(item['segment_id'])
        ready = int(item['ready_ms'])
        if (not episodes or segment != episodes[-1]['segment_id']
                or ready >= episodes[-1]['end_ms_exclusive']):
            episode = {
                'episode_id': f"episode:{segment}:{item['event_id']}",
                'segment_id': segment,
                'anchor_event_id': item['event_id'],
                'anchor_ready_ms': ready,
                'end_ms_exclusive': ready + window_ms,
                'last_ready_ms': ready,
                'anchor_side': item['side'],
                'anchor_entry_ms': item.get('entry_ms'),
                'anchor_entry_proxy': item.get('entry_proxy'),
                'trigger_count': 0, 'buy_triggers': 0, 'sell_triggers': 0,
            }
            episodes.append(episode)
        else:
            episode = episodes[-1]
        episode['trigger_count'] += 1
        episode['last_ready_ms'] = ready
        if item['side'] == 'BUY_PRESSURE':
            episode['buy_triggers'] += 1
        else:
            episode['sell_triggers'] += 1
        item['episode_id'] = episode['episode_id']
        item['is_episode_anchor'] = item['event_id'] == episode['anchor_event_id']
        annotated.append(item)
    for episode in episodes:
        episode['mixed_sides'] = bool(episode['buy_triggers'] and episode['sell_triggers'])
        episode['episode_side'] = ('MIXED' if episode['mixed_sides']
                                   else episode['anchor_side'])
    return annotated, episodes


def outcome_comparison(events, *, horizons=(30, 60, 180), neutral_band_pct=0.01):
    """Summary of directional outcomes, split by pressure side and horizon.

    Caller MUST choose event population (e.g. anchor-only). Excludes censored
    outcomes from directional rates; shows them as their own counts.
    """
    result = []
    for side in ('BUY_PRESSURE', 'SELL_PRESSURE'):
        selected = [e for e in events if e['side'] == side]
        for horizon in horizons:
            key = f'return_{int(horizon)}s_pct'
            observed = [float(e[key]) for e in selected if e.get(key) is not None]
            continuation = sum(classify_observed_outcome(v,neutral_band_pct=neutral_band_pct)
                               == 'CONTINUATION' for v in observed)
            reversal = sum(classify_observed_outcome(v,neutral_band_pct=neutral_band_pct)
                           == 'REVERSAL' for v in observed)
            result.append({
                'side': side, 'horizon_s': int(horizon),
                'signals': len(selected), 'observed': len(observed),
                'censored': len(selected) - len(observed),
                'continuation': continuation, 'reversal': reversal,
                'neutral': len(observed) - continuation - reversal,
                'continuation_pct': (100 * continuation / len(observed)) if observed else None,
                'mean_directional_return_pct': mean(observed) if observed else None,
            })
    return result


def detect_pressure_events(buckets, *, lookback=30, relative_volume=2.0,
                           min_imbalance=0.70, min_trades=3,
                           cooldown_seconds=20, horizons=(30, 60, 180)):
    lookback = max(5, int(lookback))
    relative_volume = float(relative_volume)
    min_imbalance = float(min_imbalance)
    min_trades = max(1, int(min_trades))
    cooldown_ms = max(0, int(cooldown_seconds)) * 1000
    horizons = tuple(sorted(set(int(h) for h in horizons if h > 0)))
    data = sorted(buckets, key=lambda r: (int(r['segment_id']), int(r['second_start_ms'])))
    segments = {}
    for row in data:
        segments.setdefault(int(row['segment_id']), []).append(row)
    events = []
    for segment_id, segment in segments.items():
        last_signal_ready = -1
        history = deque(maxlen=lookback)
        starts = [int(r['second_start_ms']) for r in segment]
        for i, row in enumerate(segment):
            buy = float(row['buy_qty'])
            sell = float(row['sell_qty'])
            volume = buy + sell
            delta = buy - sell
            imbalance = delta / volume if volume > 0 else 0.0
            avg = mean(history) if len(history) == lookback else None
            rel = volume / avg if avg and avg > 0 else None
            ready_ms = int(row['available_ms'])
            eligible = (
                rel is not None and rel >= relative_volume
                and abs(imbalance) >= min_imbalance
                and int(row['trades']) >= min_trades
                and ready_ms > last_signal_ready + cooldown_ms
            )
            if eligible:
                direction = 1 if delta > 0 else -1
                # Bucket is only known at ready_ms: first later observed second
                # starting at/after that wall-clock timestamp is the proxy.
                entry_idx = bisect_left(starts, ready_ms, lo=i + 1)
                event = {
                    'event_id': f'{segment_id}:{int(row["second_start_ms"])}:{direction}',
                    'segment_id': segment_id, 'second_start_ms': int(row['second_start_ms']),
                    'ready_ms': ready_ms,
                    'side': 'BUY_PRESSURE' if direction > 0 else 'SELL_PRESSURE',
                    'direction': direction, 'signal_price': float(row['close']),
                    'relative_volume': rel, 'imbalance': imbalance,
                    'delta_qty': delta, 'volume_qty': volume,
                    'trades': int(row['trades']),
                    'entry_ms': None, 'entry_proxy': None,
                }
                for horizon in horizons:
                    suffix = f'{horizon}s'
                    for field in ('return', 'mfe', 'mae', 'directional_mfe',
                                  'directional_mae', 'price_high_excursion',
                                  'price_low_excursion', 'exit_proxy'):
                        event[f'{field}_{suffix}_pct' if field not in ('exit_proxy',)
                              else f'exit_proxy_{suffix}'] = None
                    event[f'exit_observed_ms_{suffix}'] = None
                    event[f'outcome_state_{suffix}'] = 'NO_ENTRY'
                if entry_idx < len(segment):
                    entry = segment[entry_idx]
                    entry_ms = int(entry['second_start_ms'])
                    px = float(entry['open'])
                    if px <= 0:
                        raise ValueError('V3 entry proxy must be positive')
                    event['entry_ms'] = entry_ms
                    event['entry_proxy'] = px
                    for horizon in horizons:
                        target = entry_ms + horizon * 1000
                        exit_idx = bisect_left(starts, target, lo=entry_idx)
                        suffix = f'{horizon}s'
                        if exit_idx >= len(segment):
                            event[f'outcome_state_{suffix}'] = 'HORIZON_UNAVAILABLE'
                            continue
                        # Only allow first observed trade at/after the horizon
                        # within 3s, otherwise censor rather than interpolate.
                        if starts[exit_idx] > target + 3000:
                            event[f'outcome_state_{suffix}'] = 'HORIZON_GAP'
                            continue
                        px_exit = float(segment[exit_idx]['open'])
                        signed_return = (px_exit / px - 1) * 100 * direction
                        # Only observed seconds strictly before target. Exclude
                        # the exit bucket's OHLC (would leak beyond target).
                        window = segment[entry_idx:exit_idx]
                        high = max(float(x['high']) for x in window)
                        low = min(float(x['low']) for x in window)
                        raw_high_pct = (high / px - 1) * 100
                        raw_low_pct = (low / px - 1) * 100
                        directional_mfe = raw_high_pct if direction > 0 else -raw_low_pct
                        directional_mae = raw_low_pct if direction > 0 else -raw_high_pct
                        event[f'return_{suffix}_pct'] = signed_return
                        # Backward-compatible fields were ALREADY direction-adjusted.
                        event[f'mfe_{suffix}_pct'] = directional_mfe
                        event[f'mae_{suffix}_pct'] = directional_mae
                        event[f'directional_mfe_{suffix}_pct'] = directional_mfe
                        event[f'directional_mae_{suffix}_pct'] = directional_mae
                        event[f'price_high_excursion_{suffix}_pct'] = raw_high_pct
                        event[f'price_low_excursion_{suffix}_pct'] = raw_low_pct
                        event[f'exit_proxy_{suffix}'] = px_exit
                        event[f'exit_observed_ms_{suffix}'] = starts[exit_idx]
                        event[f'outcome_state_{suffix}'] = 'OBSERVED'
                events.append(event)
                last_signal_ready = ready_ms
            history.append(volume)
    return events
