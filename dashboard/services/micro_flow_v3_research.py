"""Offline experimental buy/sell-pressure research on causal V3 1s buckets.

No strategy executions or fabricated fills: entry/exit prices are *observed*
price proxies from later buckets, and outcomes are censored if unavailable.
"""
from bisect import bisect_left
from collections import deque
from statistics import mean


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
                # The selected bucket is only known at ready_ms. Use the OPEN
                # of the FIRST subsequent observed second whose start is >= ready.
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
                if entry_idx < len(segment):
                    entry = segment[entry_idx]
                    entry_ms = int(entry['second_start_ms'])
                    px = float(entry['open'])
                    event['entry_ms'] = entry_ms
                    event['entry_proxy'] = px
                    for horizon in horizons:
                        target = entry_ms + horizon * 1000
                        exit_idx = bisect_left(starts, target, lo=entry_idx)
                        suffix = f'{horizon}s'
                        if exit_idx >= len(segment):
                            event[f'return_{suffix}_pct'] = None
                            event[f'mfe_{suffix}_pct'] = None
                            event[f'mae_{suffix}_pct'] = None
                            continue
                        # The first observed trade at or after horizon is only
                        # acceptable within 3 seconds of its target timestamp.
                        # Otherwise mark censored rather than pretending it was exact.
                        if starts[exit_idx] > target + 3000:
                            event[f'return_{suffix}_pct'] = None
                            event[f'mfe_{suffix}_pct'] = None
                            event[f'mae_{suffix}_pct'] = None
                            continue
                        px_exit = float(segment[exit_idx]['open'])
                        event[f'return_{suffix}_pct'] = (px_exit / px - 1) * 100 * direction
                        slice_ = segment[entry_idx:exit_idx]  # Do not include prices after the horizon
                        high = max(float(x['high']) for x in slice_)
                        low = min(float(x['low']) for x in slice_)
                        if direction > 0:
                            mfe = (high / px - 1) * 100
                            mae = (low / px - 1) * 100
                        else:
                            mfe = (1 - low / px) * 100
                            mae = (1 - high / px) * 100
                        event[f'mfe_{suffix}_pct'] = mfe
                        event[f'mae_{suffix}_pct'] = mae
                events.append(event)
                last_signal_ready = ready_ms
            history.append(volume)
    return events
