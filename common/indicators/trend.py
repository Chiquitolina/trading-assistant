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
    df = df.copy()

    df["ema_fast"] = EMAIndicator(df["close"], fast_ema).ema_indicator()
    df["ema_slow"] = EMAIndicator(df["close"], slow_ema).ema_indicator()
    df["adx"] = ADXIndicator(
        high=df["high"],
        low=df["low"],
        close=df["close"],
        window=adx_period
    ).adx()

    df["ema_slope"] = df["ema_slow"].diff(slope_lookback)

    df["trend"] = "neutral"

    bullish = (
        (df["ema_fast"] > df["ema_slow"]) &
        (df["ema_slope"] > 0) &
        (df["adx"] > adx_min)
    )

    bearish = (
        (df["ema_fast"] < df["ema_slow"]) &
        (df["ema_slope"] < 0) &
        (df["adx"] > adx_min)
    )

    df.loc[bullish, "trend"] = "bullish"
    df.loc[bearish, "trend"] = "bearish"

    return df

