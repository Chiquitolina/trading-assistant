"""Causal, observational Failed Aggression study for Micro Flow V3.

This is NOT a strategy, fill model, or order generator.  At a fixed clock
moment after an already published aggressive-pressure signal, only buckets
whose full contents had been published by that time are eligible as evidence.
The first subsequent observed bucket OPEN is an exploratory entry proxy.
Outcomes are ex-post, directional to the hypothetical post-confirmation side,
and always censored at causal segment boundaries.
"""
from bisect import bisect_left, bisect_right
from collections import defaultdict
from statistics import mean


DEFAULT_HORIZONS = (30, 60, 180, 300)


def analyze_failed_aggression(
    buckets, anchor_events, *, confirmation_seconds=30,
    min_opposite_pct=0.03, max_favorable_pct=0.10,
    min_followthrough_pct=0.03, freshness_seconds=3,
    entry_tolerance_seconds=3, horizons=DEFAULT_HORIZONS,
):
    """Evaluate anchor signals at one *later*, fixed causal decision time.

    FAILED: opposing net movement >= min_opposite_pct and the most favorable
    excursion in the signal's original direction <= max_favorable_pct.
    CONTINUED: net movement in original direction >= min_followthrough_pct.
    Other signals are UNRESOLVED. Missing or stale evidence is CENSORED.

    Observation clock: bucket.available_ms, NOT its nominal second close.
    Episodes should be clustered upstream; pass anchor events to avoid counting
    multiple triggers of one move as separate opportunities.
    """
    confirmation_seconds = int(confirmation_seconds)
    if confirmation_seconds <= 0:
        raise ValueError('confirmation_seconds must be positive')
    for value, name in ((min_opposite_pct, 'min_opposite_pct'),
                        (max_favorable_pct, 'max_favorable_pct'),
                        (min_followthrough_pct, 'min_followthrough_pct')):
        if float(value) < 0:
            raise ValueError(f'{name} must be non-negative')
    if freshness_seconds <= 0 or entry_tolerance_seconds <= 0:
        raise ValueError('freshness and entry tolerance must be positive')
    horizons = tuple(sorted({int(h) for h in horizons if int(h) > 0}))
    if not horizons:
        raise ValueError('at least one positive horizon is required')

    by_segment = defaultdict(list)
    for row in buckets:
        by_segment[int(row['segment_id'])].append(row)
    indexed = {}
    for segment, rows in by_segment.items():
        rows.sort(key=lambda r: int(r['second_start_ms']))
        starts = [int(r['second_start_ms']) for r in rows]
        available = [int(r['available_ms']) for r in rows]
        if any(b < a for a, b in zip(available, available[1:])):
            raise ValueError(f'nonmonotonic bucket availability in segment {segment}')
        if len(starts) != len(set(starts)):
            raise ValueError(f'duplicate seconds in segment {segment}')
        indexed[segment] = rows, starts, available

    findings = []
    for event in sorted(anchor_events, key=lambda e: (
            int(e['segment_id']), int(e['ready_ms']), str(e['event_id']))):
        if event.get('is_episode_anchor') is False:
            continue
        seg = int(event['segment_id'])
        decision_ms = int(event['ready_ms']) + 1000 * confirmation_seconds
        original_direction = int(event['direction'])
        reference = float(event['signal_price'])
        if original_direction not in (-1, 1) or reference <= 0:
            raise ValueError('invalid anchor direction or signal price')
        result = {
            'event_id': str(event['event_id']),
            'episode_id': event.get('episode_id'),
            'segment_id': seg,
            'original_side': event['side'],
            'original_direction': original_direction,
            'signal_ready_ms': int(event['ready_ms']),
            'decision_ms': decision_ms,
            'confirmation_seconds': confirmation_seconds,
            'reference_price': reference,
            'state': 'CENSORED_NO_DATA',
            'observed_asof_ms': None,
            'observed_price': None,
            'directional_progress_pct': None,
            'peak_original_favorable_pct': None,
            'opposite_progress_pct': None,
            'confirmation_net_delta_qty': None,
            'confirmation_volume_qty': None,
            'confirmation_trades': None,
            'candidate_side': None,
            'candidate_direction': None,
            'entry_ms': None,
            'entry_proxy': None,
            'entry_state': 'NOT_APPLICABLE',
        }
        for horizon in horizons:
            suffix = f'{horizon}s'
            result[f'outcome_state_{suffix}'] = 'NOT_APPLICABLE'
            result[f'return_{suffix}_pct'] = None
            result[f'directional_mfe_{suffix}_pct'] = None
            result[f'directional_mae_{suffix}_pct'] = None
            result[f'exit_proxy_{suffix}'] = None
            result[f'exit_observed_ms_{suffix}'] = None

        if seg not in indexed:
            findings.append(result)
            continue
        rows, starts, available = indexed[seg]
        left = bisect_right(available, result['signal_ready_ms'])
        # available_ms truncates sub-ms precision: exclude equality rather than
        # accidentally treating a later publication in that millisecond as known.
        right = bisect_left(available, decision_ms)
        visible = rows[left:right]  # strictly BEFORE decision, no same-ms ambiguity
        if not visible:
            findings.append(result)
            continue
        last_observed = available[right - 1]
        if decision_ms - last_observed > freshness_seconds * 1000:
            result['state'] = 'CENSORED_STALE'
            result['observed_asof_ms'] = last_observed
            findings.append(result)
            continue

        last_price = float(visible[-1]['close'])
        highest = max(float(r['high']) for r in visible)
        lowest = min(float(r['low']) for r in visible)
        signed_progress = (last_price / reference - 1) * 100 * original_direction
        peak_original = max(0.0, ((highest / reference - 1) * 100 if
                                   original_direction > 0 else
                                   (1 - lowest / reference) * 100))
        result.update({
            'observed_asof_ms': last_observed,
            'observed_price': last_price,
            'directional_progress_pct': signed_progress,
            'peak_original_favorable_pct': peak_original,
            'opposite_progress_pct': -signed_progress,
            'confirmation_net_delta_qty': sum(float(r['delta_qty']) for r in visible),
            'confirmation_volume_qty': sum(
                float(r['buy_qty']) + float(r['sell_qty']) for r in visible),
            'confirmation_trades': sum(int(r['trades']) for r in visible),
        })
        if (-signed_progress >= float(min_opposite_pct)
                and peak_original <= float(max_favorable_pct)):
            result['state'] = 'FAILED_AGGRESSION'
            candidate_direction = -original_direction
        elif signed_progress >= float(min_followthrough_pct):
            result['state'] = 'PRESSURE_CONTINUED'
            candidate_direction = original_direction
        else:
            result['state'] = 'UNRESOLVED'
            findings.append(result)
            continue

        result['candidate_direction'] = candidate_direction
        result['candidate_side'] = 'LONG' if candidate_direction == 1 else 'SHORT'
        # The decision is at decision_ms, after all evidence above was published.
        # A later bucket open is a price proxy only, not an executable fill.
        entry_idx = bisect_left(starts, decision_ms)
        if (entry_idx >= len(rows) or
                starts[entry_idx] > decision_ms + entry_tolerance_seconds * 1000):
            result['entry_state'] = 'CENSORED_NO_NEARBY_BUCKET'
            findings.append(result)
            continue
        entry = rows[entry_idx]
        entry_px = float(entry['open'])
        if entry_px <= 0:
            raise ValueError('entry proxy price must be positive')
        entry_ms = starts[entry_idx]
        result['entry_state'] = 'PROXY_AVAILABLE'
        result['entry_ms'] = entry_ms
        result['entry_proxy'] = entry_px

        for horizon in horizons:
            suffix = f'{horizon}s'
            target = entry_ms + horizon * 1000
            exit_idx = bisect_left(starts, target, lo=entry_idx + 1)
            if exit_idx == len(rows):
                result[f'outcome_state_{suffix}'] = 'CENSORED_END_OF_SEGMENT'
                continue
            if starts[exit_idx] > target + entry_tolerance_seconds * 1000:
                result[f'outcome_state_{suffix}'] = 'CENSORED_HORIZON_GAP'
                continue
            exit_px = float(rows[exit_idx]['open'])
            window = rows[entry_idx:exit_idx]
            high = max(float(r['high']) for r in window)
            low = min(float(r['low']) for r in window)
            raw_high_pct = (high / entry_px - 1) * 100
            raw_low_pct = (low / entry_px - 1) * 100
            result[f'outcome_state_{suffix}'] = 'OBSERVED'
            result[f'return_{suffix}_pct'] = (
                (exit_px / entry_px - 1) * 100 * candidate_direction)
            result[f'directional_mfe_{suffix}_pct'] = (
                max(0.0, raw_high_pct) if candidate_direction > 0
                else max(0.0, -raw_low_pct))
            result[f'directional_mae_{suffix}_pct'] = (
                min(0.0, raw_low_pct) if candidate_direction > 0
                else min(0.0, -raw_high_pct))
            result[f'exit_proxy_{suffix}'] = exit_px
            result[f'exit_observed_ms_{suffix}'] = starts[exit_idx]
        findings.append(result)
    return findings


def summarize_failed_aggression(findings, *, horizons=DEFAULT_HORIZONS):
    """Descriptive comparison with censoring; never aggregate mixed directions."""
    summary = []
    for state in ('FAILED_AGGRESSION', 'PRESSURE_CONTINUED'):
        for original_side in ('BUY_PRESSURE', 'SELL_PRESSURE'):
            subset = [r for r in findings if r['state'] == state and
                      r['original_side'] == original_side]
            for horizon in horizons:
                name = f'{int(horizon)}s'
                observed = [r[f'return_{name}_pct'] for r in subset
                            if r.get(f'outcome_state_{name}') == 'OBSERVED']
                summary.append({
                    'state': state,
                    'original_side': original_side,
                    'horizon_s': int(horizon),
                    'episodes': len(subset),
                    'observed': len(observed),
                    'censored': len(subset) - len(observed),
                    'mean_return_pct': mean(observed) if observed else None,
                    'positive_pct': (100 * sum(v > 0 for v in observed) / len(observed)
                                     if observed else None),
                })
    return summary
