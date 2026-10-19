"""Shared causal V3 AggTrades effort features for independent research modules.

All volumes/price references come exclusively from previously published or
current *fully available* observed 1s Replay buckets, per causal segment.
Not an order-book absorption estimate. No backfill, no future labels.
"""
from bisect import bisect_left


def effort_snapshot(rows, starts, index, *, window_seconds=5, baseline_buckets=30,
                    minimum_window_observations=3, max_observation_gap_seconds=3):
    """Return sweep/impulse-time features or None if observation is insufficient.

    Baseline is strictly earlier than the measurement window. The sample unit
    for relative volume is an *observed second*, NOT a calendar second. Coverage
    is explicit. The caller must pass validated, same-segment ordered buckets.
    """
    if not (1 <= window_seconds <= 60 and baseline_buckets >= 10
            and minimum_window_observations >= 1 and max_observation_gap_seconds >= 1):
        raise ValueError('Invalid effort feature configuration')
    now = int(starts[index])
    left = bisect_left(starts, now - (window_seconds-1)*1000)
    window = rows[left:index+1]
    if len(window) < minimum_window_observations or left < baseline_buckets:
        return None
    if any(int(starts[k])-int(starts[k-1]) > max_observation_gap_seconds*1000
           for k in range(left+1,index+1)):
        return None
    previous = rows[left-baseline_buckets:left]
    historical_mean = sum(float(r['buy_qty'])+float(r['sell_qty']) for r in previous) / len(previous)
    if historical_mean <= 0:
        return None
    buy = sum(float(r['buy_qty']) for r in window)
    sell = sum(float(r['sell_qty']) for r in window)
    qty = buy + sell
    if qty <= 0:
        return None
    direction = 1 if buy > sell else -1 if sell > buy else 0
    first_price = float(window[0]['open'])
    last_price = float(window[-1]['close'])
    directed_move = direction*(last_price/first_price-1)*100 if direction else 0.0
    net = abs(buy-sell)
    return {
        'window_start_ms':int(starts[left]),
        'window_end_observed_second_ms':now,
        'available_wall_ns':int(window[-1]['available_wall_ns']),
        'first_seq':window[0]['first_seq'],
        'last_seq':window[-1]['last_seq'],
        'observed_seconds':len(window),
        'window_seconds':window_seconds,
        'coverage_fraction':len(window)/window_seconds,
        'baseline_buckets':len(previous),
        'baseline_first_observed_second_ms':int(previous[0]['second_start_ms']),
        'baseline_last_observed_second_ms':int(previous[-1]['second_start_ms']),
        'baseline_timespan_seconds':(int(previous[-1]['second_start_ms'])-int(previous[0]['second_start_ms']))/1000,
        'baseline_mean_qty_per_observed_second':historical_mean,
        'buy_qty':buy, 'sell_qty':sell, 'delta_qty':buy-sell,
        'volume_qty':qty, 'imbalance':(buy-sell)/qty,
        'relative_volume':qty/len(window)/historical_mean,
        'trades':sum(int(r['trades']) for r in window),
        'pressure_side':'BUY' if direction>0 else 'SELL' if direction<0 else 'BALANCED',
        'window_open_price':first_price,
        'window_close_price':last_price,
        'window_high':max(float(r['high']) for r in window),
        'window_low':min(float(r['low']) for r in window),
        'pressure_signed_displacement_pct':directed_move,
        'signed_impact_pct_per_100eth_net_delta':directed_move*100/net if net else None,
    }
