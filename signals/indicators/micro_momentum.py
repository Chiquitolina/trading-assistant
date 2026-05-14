from ta.trend import EMAIndicator

from enums.momentum import Momentum


# ======================================================
# MICRO MOMENTUM – AGGRESSIVE 1M SCALPING
# ======================================================

def micro_momentum_1m(
    df,
    ema_period=20,

    # =========================
    # OVEREXTENSION
    # =========================
    overextension_pct=0.0012,   # 0.12%
    extreme_pct=0.0022,         # 0.22%

    # =========================
    # WICKS
    # =========================
    wick_ratio_threshold=0.35,

    # =========================
    # ATR EXPANSION
    # =========================
    atr=None,
    atr_expansion_mult=1.4,

    # =========================
    # VELOCITY
    # =========================
    velocity_mult=1.8,
):

    # ==================================================
    # MIN DATA
    # ==================================================

    if len(df) < ema_period + 3:
        return Momentum.NO_DATA

    df = df.copy()

    # ==================================================
    # EMA
    # ==================================================

    df["ema"] = EMAIndicator(
        close=df["close"],
        window=ema_period
    ).ema_indicator()

    last = df.iloc[-1]
    prev = df.iloc[-2]

    close = float(last["close"])
    open_ = float(last["open"])
    high = float(last["high"])
    low = float(last["low"])

    ema = float(last["ema"])

    # ==================================================
    # CANDLE STRUCTURE
    # ==================================================

    candle_range = high - low

    if candle_range == 0:
        return Momentum.FLAT_ZERO_RANGE

    body = abs(close - open_)

    upper_wick = high - max(open_, close)
    lower_wick = min(open_, close) - low

    upper_wick_ratio = upper_wick / candle_range
    lower_wick_ratio = lower_wick / candle_range

    body_ratio = body / candle_range

    # ==================================================
    # PREVIOUS CANDLE
    # ==================================================

    prev_body = abs(prev["close"] - prev["open"])

    # ==================================================
    # DISTANCE FROM EMA
    # ==================================================

    distance_pct = (close - ema) / ema
    abs_distance = abs(distance_pct)

    # ==================================================
    # ATR EXPANSION
    # ==================================================

    is_expanded = False

    if atr is not None:
        is_expanded = candle_range > (atr * atr_expansion_mult)

    # ==================================================
    # VELOCITY EXPANSION
    # ==================================================

    velocity_expansion = False

    if prev_body > 0:
        velocity_expansion = body > (prev_body * velocity_mult)

    # ==================================================
    # EXTREME OVEREXTENSION UP
    # ==================================================

    if distance_pct >= extreme_pct:

        # rejection wick
        if upper_wick_ratio >= wick_ratio_threshold:
            return Momentum.EXHAUSTION_UP

        # strong expansion
        if is_expanded or velocity_expansion:
            return Momentum.BREAKOUT_UP_STRONG

        return Momentum.BULLISH_PRESSURE

    # ==================================================
    # EXTREME OVEREXTENSION DOWN
    # ==================================================

    if distance_pct <= -extreme_pct:

        if lower_wick_ratio >= wick_ratio_threshold:
            return Momentum.EXHAUSTION_DOWN

        if is_expanded or velocity_expansion:
            return Momentum.BREAKOUT_DOWN_STRONG

        return Momentum.BEARISH_PRESSURE

    # ==================================================
    # NORMAL OVEREXTENSION UP
    # ==================================================

    if distance_pct >= overextension_pct:

        if upper_wick_ratio >= wick_ratio_threshold:
            return Momentum.EXHAUSTION_UP

        if velocity_expansion:
            return Momentum.BREAKOUT_UP_WEAK

        return Momentum.TREND_CONTINUATION_UP

    # ==================================================
    # NORMAL OVEREXTENSION DOWN
    # ==================================================

    if distance_pct <= -overextension_pct:

        if lower_wick_ratio >= wick_ratio_threshold:
            return Momentum.EXHAUSTION_DOWN

        if velocity_expansion:
            return Momentum.BREAKOUT_DOWN_WEAK

        return Momentum.TREND_CONTINUATION_DOWN

    # ==================================================
    # PRESSURE CANDLES
    # ==================================================

    if body_ratio >= 0.45:

        if close > open_:
            return Momentum.BULLISH_PRESSURE

        if close < open_:
            return Momentum.BEARISH_PRESSURE

    # ==================================================
    # INSIDE BAR
    # ==================================================

    if (
        high <= prev["high"]
        and low >= prev["low"]
    ):
        return Momentum.INSIDE_BAR

    # ==================================================
    # INDECISION
    # ==================================================

    if body_ratio < 0.20:
        return Momentum.INDECISION

    # ==================================================
    # WEAK STRUCTURE
    # ==================================================

    if close >= open_:
        return Momentum.INSIDE_BULLISH_WEAK

    return Momentum.INSIDE_BEARISH_WEAK