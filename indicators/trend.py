import pandas as pd
from ta.trend import EMAIndicator, ADXIndicator

# ======================================================
# TREND BIAS – TIMEFRAMES GRANDES (1H, 4H)
# ======================================================
def trend_bias(
    df,
    fast_ema=20,
    slow_ema=50,
    adx_period=14,
    adx_min=15,
    slope_lookback=3
):
    if len(df) < slow_ema + slope_lookback:
        return "neutral"

    df = df.copy()

    df["ema_fast"] = EMAIndicator(df["close"], fast_ema).ema_indicator()
    df["ema_slow"] = EMAIndicator(df["close"], slow_ema).ema_indicator()
    df["adx"] = ADXIndicator(
        high=df["high"],
        low=df["low"],
        close=df["close"],
        window=adx_period
    ).adx()

    last = df.iloc[-1]
    prev = df.iloc[-1 - slope_lookback]

    ema_slope = last["ema_slow"] - prev["ema_slow"]

    if (
        last["ema_fast"] > last["ema_slow"]
        and ema_slope > 0
        and last["adx"] > adx_min
    ):
        return "bullish"

    if (
        last["ema_fast"] < last["ema_slow"]
        and ema_slope < 0
        and last["adx"] > adx_min
    ):
        return "bearish"

    return "neutral"

