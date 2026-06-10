import numpy as np
import pandas as pd


def detect_trend_up(
    df: pd.DataFrame,
    lookback: int = 20,
    ema_fast: int = 20,
    ema_slow: int = 50,
    min_score: int = 4,
):
    if df is None or len(df) < max(lookback, ema_slow) + 5:
        return {
            "trend_up": False,
            "score": 0,
            "reason": "not_enough_data",
        }

    d = df.copy()

    d["ema_fast"] = d["close"].ewm(span=ema_fast, adjust=False).mean()
    d["ema_slow"] = d["close"].ewm(span=ema_slow, adjust=False).mean()

    recent = d.tail(lookback)

    close = float(d["close"].iloc[-1])
    ema_f = float(d["ema_fast"].iloc[-1])
    ema_s = float(d["ema_slow"].iloc[-1])

    ema_f_prev = float(d["ema_fast"].iloc[-5])
    ema_s_prev = float(d["ema_slow"].iloc[-5])

    recent_high_now = recent["high"].tail(5).max()
    recent_high_prev = recent["high"].head(5).max()

    recent_low_now = recent["low"].tail(5).min()
    recent_low_prev = recent["low"].head(5).min()

    score = 0
    reasons = []

    # 1) Precio arriba de EMA rápida
    if close > ema_f:
        score += 1
        reasons.append("close_above_ema_fast")

    # 2) EMA rápida arriba de EMA lenta
    if ema_f > ema_s:
        score += 1
        reasons.append("ema_fast_above_slow")

    # 3) EMA rápida con pendiente positiva
    if ema_f > ema_f_prev:
        score += 1
        reasons.append("ema_fast_slope_up")

    # 4) EMA lenta con pendiente positiva
    if ema_s > ema_s_prev:
        score += 1
        reasons.append("ema_slow_slope_up")

    # 5) Higher high
    if recent_high_now > recent_high_prev:
        score += 1
        reasons.append("higher_high")

    # 6) Higher low
    if recent_low_now > recent_low_prev:
        score += 1
        reasons.append("higher_low")

    trend_up = score >= min_score

    return {
        "trend_up": trend_up,
        "score": score,
        "close": close,
        "ema_fast": ema_f,
        "ema_slow": ema_s,
        "higher_high": bool(recent_high_now > recent_high_prev),
        "higher_low": bool(recent_low_now > recent_low_prev),
        "reasons": reasons,
    }