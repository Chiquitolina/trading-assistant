import pandas as pd

from ta.trend import EMAIndicator, ADXIndicator

from enums.trend import Trend


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

    # ⚠️ Validar que hay suficientes filas
    min_required = max(
        fast_ema,
        slow_ema,
        adx_period,
        slope_lookback
    )

    if len(df) < min_required:

        df["ema_fast"] = pd.NA
        df["ema_slow"] = pd.NA
        df["adx"] = pd.NA
        df["ema_slope"] = pd.NA

        df["trend"] = Trend.NEUTRAL

        return df

    # -------------------------
    # Indicadores
    # -------------------------

    df["ema_fast"] = EMAIndicator(
        df["close"],
        fast_ema
    ).ema_indicator()

    df["ema_slow"] = EMAIndicator(
        df["close"],
        slow_ema
    ).ema_indicator()

    df["adx"] = ADXIndicator(
        high=df["high"],
        low=df["low"],
        close=df["close"],
        window=adx_period
    ).adx()

    df["ema_slope"] = df["ema_slow"].diff(
        slope_lookback
    )

    # -------------------------
    # Trend
    # -------------------------

    df["trend"] = Trend.NEUTRAL

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

    df.loc[bullish, "trend"] = Trend.BULLISH
    df.loc[bearish, "trend"] = Trend.BEARISH

    return df