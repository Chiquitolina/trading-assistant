"""Read-only, 30m pre-breakout compression scanner.

No market connections, state-machine transitions, watches, or orders are created here.
The scanner is a pure transformation of CLOSED OHLCV candles into structural
features; UI thresholds filter those features without re-reading Market Data.
"""

from __future__ import annotations

import math
import time

import numpy as np
import pandas as pd

from signals.indicators.compression_detector import detect_compression


TIMEFRAME_MS = 30 * 60 * 1000
REQUIRED_COLUMNS = {"timestamp", "open", "high", "low", "close", "volume"}


def _between(value: float, left: float = 0.0, right: float = 1.0) -> float:
    return max(left, min(right, float(value)))


def _score(row: dict) -> float:
    """Descriptive 0-100 ranking, NOT a probability of a breakout."""
    range_component = _between((1.20 - row["range_ratio"]) / 0.65)
    atr_component = _between((1.15 - row["atr_ratio"]) / 0.60)
    volume_component = _between((1.20 - row["volume_ratio"]) / 0.85)
    compact_component = _between((6.0 - row["compression_height_pct"]) / 5.0)
    touches_component = _between(min(row["touches_high"], row["touches_low"]) / 3.0)
    proximity_component = _between((50.0 - row["wall_distance_pct"]) / 40.0)
    last_candles_component = _between((1.15 - row["micro_range_ratio"]) / 0.70)

    return round(
        20 * range_component
        + 15 * atr_component
        + 10 * volume_component
        + 15 * compact_component
        + 10 * touches_component
        + 20 * proximity_component
        + 10 * last_candles_component,
        1,
    )


def scan_compression_market(
    histories: dict,
    lookback: int = 30,
    base_lookback: int = 60,
    now_ms: int | None = None,
    timeframe_ms: int = TIMEFRAME_MS,
    max_age_candles: int = 2,
) -> tuple[pd.DataFrame, dict]:
    """Scan ONE currently-forming structure per symbol, at the last closed bar.

    Base is non-overlapping: previous 60 bars versus most recent 30 bars by
    default. Conform to the production detector's feature calculations, but
    do NOT apply its score thresholds here. No future candles are accessed.
    """
    lookback = int(lookback)
    base_lookback = int(base_lookback)
    if lookback < 14 or base_lookback < 20:
        raise ValueError("lookback >= 14 and base_lookback >= 20 are required")
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    counts = {
        "symbols_with_history": len(histories),
        "scanned": 0,
        "insufficient": 0,
        "invalid": 0,
        "gapped": 0,
        "stale": 0,
        "evaluation_errors": 0,
    }
    rows = []
    required = base_lookback + lookback

    for symbol, candles in sorted(histories.items()):
        if candles is None or len(candles) < required:
            counts["insufficient"] += 1
            continue
        if not REQUIRED_COLUMNS.issubset(candles.columns):
            counts["invalid"] += 1
            continue

        # Data service provides verified, closed candles; guard independently
        # against gaps, duplicates, staleness, and non-finite data.
        recent_history = (
            candles.sort_values("timestamp")
            .drop_duplicates(subset=["timestamp"], keep="last")
            .tail(required)
            .reset_index(drop=True)
        )
        if len(recent_history) < required:
            counts["insufficient"] += 1
            continue

        timestamps = pd.to_numeric(recent_history["timestamp"], errors="coerce")
        if timestamps.isna().any():
            counts["invalid"] += 1
            continue
        if not (timestamps.diff().iloc[1:] == timeframe_ms).all():
            counts["gapped"] += 1
            continue

        last_close_ts = int(timestamps.iloc[-1]) + timeframe_ms - 1
        if "close_timestamp" in recent_history.columns:
            try:
                last_close_ts = int(recent_history["close_timestamp"].iloc[-1])
            except (TypeError, ValueError):
                pass
        if last_close_ts > now_ms or now_ms - last_close_ts > (max_age_candles * timeframe_ms):
            counts["stale"] += 1
            continue

        numeric = recent_history[["open", "high", "low", "close", "volume"]].apply(
            pd.to_numeric, errors="coerce"
        )
        if (
            not np.isfinite(numeric.to_numpy()).all()
            or (numeric[["open", "high", "low", "close"]] <= 0).any().any()
            or (numeric["volume"] < 0).any()
            or (numeric["high"] < numeric[["open", "close", "low"]].max(axis=1)).any()
            or (numeric["low"] > numeric[["open", "close", "high"]].min(axis=1)).any()
        ):
            counts["invalid"] += 1
            continue

        try:
            # A deliberately permissive call, used strictly to extract RAW
            # metrics. Thresholds are applied later by the dashboard.
            features = detect_compression(
                recent_history,
                lookback=lookback,
                base_lookback=base_lookback,
                max_range_ratio=1e9,
                max_atr_ratio=1e9,
                max_volume_ratio=1e9,
                max_body_pct=1e9,
                min_score=0,
                base_window_mode="separate",
            )
            if "compression_high" not in features:
                counts["evaluation_errors"] += 1
                continue

            window = recent_history.tail(lookback)
            high = float(features["compression_high"])
            low = float(features["compression_low"])
            last_close = float(window["close"].iloc[-1])
            height = high - low
            if height <= 0 or low <= 0:
                counts["invalid"] += 1
                continue

            position = _between((last_close - low) / height)
            wall_distance = 100.0 * min(position, 1.0 - position)
            n = max(4, lookback // 3)
            first_ranges = (window["high"].iloc[:n] - window["low"].iloc[:n])
            last_ranges = (window["high"].iloc[-n:] - window["low"].iloc[-n:])
            avg_first_range = float(first_ranges.mean())
            micro_range_ratio = (
                float(last_ranges.mean()) / avg_first_range
                if avg_first_range > 0 else float("inf")
            )
            quote_volume = window.get("quoteVolume")
            if quote_volume is None:
                quote_volume = window["volume"] * window["close"]
            median_quote_volume = float(pd.to_numeric(quote_volume, errors="coerce").median())

            row = {
                "symbol": str(symbol).upper(),
                "timeframe": "30m",
                "window": lookback,
                "start_ts": int(window["timestamp"].iloc[0]),
                "end_ts": last_close_ts,
                "high": high,
                "low": low,
                "last_close": last_close,
                "range_ratio": float(features["range_ratio"]),
                "atr_ratio": float(features["atr_ratio"]),
                "volume_ratio": float(features["volume_ratio"]),
                "avg_body_pct": float(features["avg_body_pct"]),
                "compression_height_pct": float(features["compression_height_pct"]),
                "touches_high": int(features["touches_high"]),
                "touches_low": int(features["touches_low"]),
                "compression_shape": features["compression_shape"],
                "quality_label": features["compression_quality_label"],
                "price_position_pct": round(100 * position, 1),
                "wall_distance_pct": round(wall_distance, 1),
                "closest_wall": "TECHO" if position >= .5 else "PISO",
                "micro_range_ratio": round(micro_range_ratio, 4),
                "median_quote_volume": median_quote_volume,
            }
            if not all(math.isfinite(float(row[field])) for field in (
                "range_ratio", "atr_ratio", "volume_ratio", "avg_body_pct",
                "compression_height_pct", "micro_range_ratio", "median_quote_volume"
            )):
                counts["invalid"] += 1
                continue
            row["readiness_score"] = _score(row)
            rows.append(row)
            counts["scanned"] += 1
        except (ValueError, TypeError, IndexError, KeyError, ArithmeticError, np.linalg.LinAlgError):
            counts["evaluation_errors"] += 1

    result = pd.DataFrame(rows)
    if not result.empty:
        result = result.sort_values(
            ["readiness_score", "symbol"], ascending=[False, True]
        ).reset_index(drop=True)
    return result, counts


def filter_compression_market(
    raw: pd.DataFrame,
    *,
    max_range_ratio: float = 0.80,
    max_atr_ratio: float = 0.90,
    max_volume_ratio: float = 1.10,
    max_body_pct: float = 0.60,
    min_conditions: int = 3,
    max_height_pct: float = 4.0,
    min_touches: int = 2,
    min_quote_volume: float = 25_000.0,
    only_near_wall: bool = True,
    max_wall_distance_pct: float = 30.0,
    require_micro_contraction: bool = False,
    max_micro_range_ratio: float = 1.0,
) -> pd.DataFrame:
    """Cheap filters over fixed snapshots; changing sliders never re-fetches."""
    if raw is None or raw.empty:
        return pd.DataFrame(columns=list(raw.columns) if raw is not None else [])
    filtered = raw.copy()
    filtered["core_score"] = (
        (filtered["range_ratio"] <= max_range_ratio).astype(int)
        + (filtered["atr_ratio"] <= max_atr_ratio).astype(int)
        + (filtered["volume_ratio"] <= max_volume_ratio).astype(int)
        + (filtered["avg_body_pct"] <= max_body_pct).astype(int)
    )
    qualified = (
        # A tight, low-volatility box is mandatory. Core score lets the user
        # decide how strictly to require volume/body confirmation as well.
        (filtered["range_ratio"] <= max_range_ratio)
        & (filtered["atr_ratio"] <= max_atr_ratio)
        & (filtered["core_score"] >= min_conditions)
        & (filtered["compression_height_pct"] <= max_height_pct)
        & (filtered["touches_high"] >= min_touches)
        & (filtered["touches_low"] >= min_touches)
        & (filtered["median_quote_volume"] >= min_quote_volume)
    )
    if only_near_wall:
        qualified &= filtered["wall_distance_pct"] <= max_wall_distance_pct
    if require_micro_contraction:
        qualified &= filtered["micro_range_ratio"] <= max_micro_range_ratio

    filtered = filtered.loc[qualified].copy()
    if filtered.empty:
        return filtered
    filtered["status"] = np.where(
        (filtered["readiness_score"] >= 70)
        & (filtered["wall_distance_pct"] <= max_wall_distance_pct),
        "CERCA_DE_ROMPER", "COMPRIMIENDO",
    )
    return filtered.sort_values(
        ["readiness_score", "symbol"], ascending=[False, True]
    ).reset_index(drop=True)
