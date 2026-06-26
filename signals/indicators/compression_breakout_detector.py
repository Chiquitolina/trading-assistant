import pandas as pd

from signals.indicators.trend_detector import detect_trend_up
from signals.indicators.compression_detector import detect_compression


def detect_compression_breakout(
    df: pd.DataFrame,
    trend_lookback: int = 20,
    compression_lookback: int = 10,
    compression_base_lookback: int = 40,
    min_trend_score: int = 4,
    min_compression_score: int = 3,
    volume_lookback: int = 20,
    min_volume_expansion: float = 1.5,
):
    if df is None or len(df) < compression_base_lookback + compression_lookback + 5:
        return {
            "breakout": False,
            "reason": "not_enough_data",
        }

    d = df.copy()

    required_cols = ["open", "high", "low", "close", "volume"]
    missing = [c for c in required_cols if c not in d.columns]

    if missing:
        return {
            "breakout": False,
            "reason": f"missing_cols:{missing}",
        }

    for col in required_cols:
        d[col] = pd.to_numeric(d[col], errors="coerce")

    d = d.dropna(subset=required_cols).reset_index(drop=True)

    if len(d) < compression_base_lookback + compression_lookback + 5:
        return {
            "breakout": False,
            "reason": "not_enough_clean_data",
        }

    # La última vela es el posible breakout
    prev = d.iloc[:-1].copy()
    last = d.iloc[-1]

    trend = detect_trend_up(
        prev,
        lookback=trend_lookback,
        ema_fast=20,
        ema_slow=50,
        min_score=min_trend_score,
    )

    if not trend.get("trend_up", False):
        return {
            "breakout": False,
            "reason": "no_trend_up",
            "trend": trend,
        }

    compression = detect_compression(
        prev,
        lookback=compression_lookback,
        base_lookback=compression_base_lookback,
        max_range_ratio=0.65,
        max_atr_ratio=0.75,
        max_volume_ratio=0.95,
        max_body_pct=0.50,
        min_score=min_compression_score,
    )

    if not compression.get("is_compression", False):
        return {
            "breakout": False,
            "reason": "no_compression",
            "trend": trend,
            "compression": compression,
        }

    compression_high = float(compression["compression_high"])

    volume_base = prev["volume"].tail(volume_lookback).mean()
    volume_ratio = float(last["volume"] / volume_base) if volume_base > 0 else 0

    close_breaks_high = float(last["close"]) > compression_high
    high_breaks_high = float(last["high"]) > compression_high
    bullish_candle = float(last["close"]) > float(last["open"])
    volume_expansion = volume_ratio >= min_volume_expansion

    breakout = (
        close_breaks_high
        and bullish_candle
        and volume_expansion
    )

    passed_reasons = []
    failed_reasons = []

    if close_breaks_high:
        passed_reasons.append("close_breaks_compression_high")
    else:
        failed_reasons.append(
            f"close_not_above_compression_high "
            f"close={float(last['close']):.8f} "
            f"compression_high={compression_high:.8f}"
        )

    if high_breaks_high:
        passed_reasons.append("high_breaks_compression_high")
    else:
        failed_reasons.append(
            f"high_not_above_compression_high "
            f"high={float(last['high']):.8f} "
            f"compression_high={compression_high:.8f}"
        )

    if bullish_candle:
        passed_reasons.append("bullish_candle")
    else:
        failed_reasons.append(
            f"not_bullish_candle "
            f"open={float(last['open']):.8f} "
            f"close={float(last['close']):.8f}"
        )

    if volume_expansion:
        passed_reasons.append("volume_expansion")
    else:
        failed_reasons.append(
            f"volume_not_expanded "
            f"volume_ratio={volume_ratio:.4f} "
            f"min_volume_expansion={min_volume_expansion:.4f}"
        )

    return {
        "breakout": breakout,
        "side": "LONG" if breakout else None,
        "reason": "compression_breakout" if breakout else "breakout_conditions_not_met",
        "reasons": passed_reasons,
        "failed_reasons": failed_reasons,

        "trend": trend,
        "compression": compression,

        "last_close": float(last["close"]),
        "last_open": float(last["open"]),
        "last_high": float(last["high"]),
        "last_low": float(last["low"]),

        "compression_high": compression_high,
        "compression_low": float(compression["compression_low"]),
        "volume_ratio": round(volume_ratio, 4),
        "volume_base": float(volume_base),
        "last_volume": float(last["volume"]),

        "close_breaks_high": close_breaks_high,
        "high_breaks_high": high_breaks_high,
        "bullish_candle": bullish_candle,
        "volume_expansion": volume_expansion,
    }