#!/usr/bin/env python3
"""Isolated experimental Continuous V1 historical replay acceleration.

This module does NOT modify the canonical scanner, V1 replay, LIVE processes,
or portfolio rules. It transparently reuses expensive, read-only candle preparation, 15m swing
confirmation and SwingDetector results on unchanged inputs *within a symbol*.
The original V1 scanner still processes every 1m checkpoint and retest.

Exact event/causal/timestamp parity with V1 MUST be checked separately before
relying on this performance optimization. No speedup is promised a priori.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time

import replay_continuous_v1_historical as v1

ENGINE_VERSION = 'experimental-causal-helper-cache-v2'
_ORIGINAL_RUN_SYMBOL = v1.run_symbol
_ORIGINAL_PROGRESS_SIGNATURE = v1.progress_signature


def _v2_signature(**kwargs):
    """Reject accidental --resume mixing between V1 and V2 outputs."""
    v1_signature = _ORIGINAL_PROGRESS_SIGNATURE(**kwargs)
    return hashlib.sha256(f'{ENGINE_VERSION}:{v1_signature}'.encode()).hexdigest()


def _cache_detector_class(original_cls, stats):
    """Memoize ONLY repeated SwingDetector inputs from one immutable symbol path.

    The key is valid specifically because V1 run_symbol snapshots are slices
    from one already loaded, immutable 15m OHLC dataset. With that invariant,
    (first_timestamp, last_timestamp, count) uniquely identifies the full
    underlying slice. Cache never persists across symbols or separate runs.
    """
    memo = {}

    class CachedSwingDetector(original_cls):
        def detect_all(self, candles):
            stats['calls'] += 1
            # Unexpected input shapes always fall back to canonical detector.
            if not isinstance(candles, list) or not candles or not isinstance(candles[0], dict):
                stats['bypassed'] += 1
                return super().detect_all(candles)
            first = candles[0].get('timestamp')
            last = candles[-1].get('timestamp')
            if first is None or last is None:
                stats['bypassed'] += 1
                return super().detect_all(candles)
            key = (
                self.left_bars, self.right_bars, self.min_prominence_pct,
                len(candles), int(first), int(last),
            )
            if key in memo:
                stats['hits'] += 1
                # Independent list, same immutable frozen SwingPoint objects.
                return list(memo[key])
            t0 = time.perf_counter()
            result = super().detect_all(candles)
            stats['compute_seconds'] += time.perf_counter() - t0
            memo[key] = tuple(result)
            return result

    return CachedSwingDetector


def run_symbol_with_swing_cache(sym, scan, normalize_reactions, ticks, candles_1m,
                                candles_15m, start_ms, end_ms, step_min):
    """Optimize redundant preparation/confirmation/swing computations only.

    All 1m checkpoints still call V1's unmodified run_symbol and canonical
    _find_confirmed_swing_retest. No retest/departure computation is skipped.
    """
    from collections import OrderedDict

    globals_ = scan.__globals__
    original_cls = globals_.get('SwingDetector')
    original_prepare = globals_.get('_prepare_confirmed_swing_retest_candles')
    original_info = globals_.get('get_volume_exhaustion_swing_confirmation_info')
    if any(x is None for x in (original_cls, original_prepare, original_info)):
        raise RuntimeError('Canonical scanner missing required timing-safe helpers')
    stats = {'calls': 0, 'hits': 0, 'bypassed': 0, 'compute_seconds': 0.0,
             'prepare_hits': 0, 'prepare_misses': 0,
             'info_hits': 0, 'info_misses': 0}

    # DataFrame identity cache is bounded; object references avoid id reuse.
    # Canonical consumers use the result read-only (their event slices .copy()).
    prepared_memo = OrderedDict()

    def prepare_once(one_minute):
        if one_minute is None:
            return original_prepare(one_minute)
        key = id(one_minute)
        pair = prepared_memo.get(key)
        if pair is not None and pair[0] is one_minute:
            prepared_memo.move_to_end(key)
            stats['prepare_hits'] += 1
            return pair[1]
        result = original_prepare(one_minute)
        stats['prepare_misses'] += 1
        prepared_memo[key] = (one_minute, result)
        prepared_memo.move_to_end(key)
        if len(prepared_memo) > 12:
            prepared_memo.popitem(last=False)
        return result

    # At most 3 immutable 15m closed-candle snapshots per symbol, preventing
    # repeating get_volume_exhaustion_swing_confirmation_info's DataFrame
    # copy, normalization and sort for every old swing on each 1m checkpoint.
    info_windows = OrderedDict()

    def info_once(point, timeframe_candles, swing_timeframe):
        if (timeframe_candles is None or timeframe_candles.empty or
                'timestamp' not in timeframe_candles):
            return original_info(point, timeframe_candles, swing_timeframe)
        timestamps = timeframe_candles['timestamp']
        window = (str(swing_timeframe), len(timestamps),
                  int(timestamps.iloc[0]), int(timestamps.iloc[-1]))
        points = info_windows.get(window)
        if points is None:
            points = {}
            info_windows[window] = points
            if len(info_windows) > 3:
                info_windows.popitem(last=False)
        else:
            info_windows.move_to_end(window)
        if point is None:
            return original_info(point, timeframe_candles, swing_timeframe)
        key = (point.side, point.price, point.pivot_timestamp,
               point.confirmed_timestamp, point.left_bars, point.right_bars,
               point.prominence_pct)
        if key in points:
            stats['info_hits'] += 1
            result = points[key]
            return dict(result) if result is not None else None
        stats['info_misses'] += 1
        result = original_info(point, timeframe_candles, swing_timeframe)
        points[key] = dict(result) if result is not None else None
        return result

    globals_['SwingDetector'] = _cache_detector_class(original_cls, stats)
    globals_['_prepare_confirmed_swing_retest_candles'] = prepare_once
    globals_['get_volume_exhaustion_swing_confirmation_info'] = info_once
    began = time.perf_counter()
    try:
        return _ORIGINAL_RUN_SYMBOL(sym, scan, normalize_reactions, ticks,
                                    candles_1m, candles_15m,
                                    start_ms, end_ms, step_min)
    finally:
        globals_['SwingDetector'] = original_cls
        globals_['_prepare_confirmed_swing_retest_candles'] = original_prepare
        globals_['get_volume_exhaustion_swing_confirmation_info'] = original_info
        duration = time.perf_counter() - began
        hit_pct = (100 * stats['hits'] / stats['calls']) if stats['calls'] else 0.0
        print(f'V2_CACHE {sym} calls={stats["calls"]} '
              f'hits={stats["hits"]} hit_pct={hit_pct:.2f} '
              f'prep_hits={stats["prepare_hits"]} '
              f'prep_misses={stats["prepare_misses"]} '
              f'info_hits={stats["info_hits"]} '
              f'info_misses={stats["info_misses"]} '
              f'total_seconds={duration:.3f}', flush=True)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    # Never let V1 manifests be silently treated as compatible with V2.
    v1.progress_signature = _v2_signature
    v1.run_symbol = run_symbol_with_swing_cache
    print(f'ENGINE={ENGINE_VERSION} | isolated V2 | NOT PARITY CERTIFIED', flush=True)
    result = v1.main(args)
    # Tag the output summary without altering any event row, timestamp, or
    # canonical fingerprint. This is provenance only.
    try:
        if '--output' in args:
            output = Path(args[args.index('--output') + 1])
            summary_path = output / 'summary.json'
            if summary_path.is_file():
                obj = json.loads(summary_path.read_text(encoding='utf-8'))
                obj['historical_replay_engine'] = ENGINE_VERSION
                obj['v2_canonical_event_parity_independently_verified'] = False
                tmp = summary_path.with_suffix('.json.tmp')
                tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding='utf-8')
                os.replace(tmp, summary_path)
    except (OSError, ValueError, IndexError, json.JSONDecodeError) as exc:
        print(f'V2 warning: could not tag summary: {exc}', file=sys.stderr)
    return result


if __name__ == '__main__':
    sys.exit(main())
