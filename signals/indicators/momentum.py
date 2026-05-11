from enums.momentum import Momentum

def momentum_5m(df):

    if len(df) < 2:
        return Momentum.NO_DATA

    last = df.iloc[-1]
    prev = df.iloc[-2]

    body = abs(last["close"] - last["open"])
    range_ = last["high"] - last["low"]

    if range_ == 0:
        return Momentum.FLAT_ZERO_RANGE

    body_ratio = body / range_

    upper_wick = last["high"] - max(last["open"], last["close"])
    lower_wick = min(last["open"], last["close"]) - last["low"]

    wick_ratio = max(upper_wick, lower_wick) / range_

    # ---------- TRUE INSIDE BAR ----------
    if last["high"] <= prev["high"] and last["low"] >= prev["low"]:
        return Momentum.INSIDE_BAR

    # ---------- BREAKOUT UP ----------
    if last["close"] > prev["high"]:
        if body_ratio > 0.6:
            return Momentum.BREAKOUT_UP_STRONG
        else:
            return Momentum.BREAKOUT_UP_WEAK

    # ---------- BREAKOUT DOWN ----------
    if last["close"] < prev["low"]:
        if body_ratio > 0.6:
            return Momentum.BREAKOUT_DOWN_STRONG
        else:
            return Momentum.BREAKOUT_DOWN_WEAK

    # ---------- WICK EXHAUSTION ----------
    if wick_ratio > 0.6:
        if upper_wick > lower_wick:
            return Momentum.EXHAUSTION_UP
        else:
            return Momentum.EXHAUSTION_DOWN

    # ---------- CONTINUATION ----------
    if body_ratio > 0.5:
        if last["close"] > prev["close"]:
            return Momentum.TREND_CONTINUATION_UP
        else:
            return Momentum.TREND_CONTINUATION_DOWN

    # ---------- BULLISH PRESSURE ----------
    if last["close"] > last["open"] and body_ratio > 0.4:
        return Momentum.BULLISH_PRESSURE

    # ---------- BEARISH PRESSURE ----------
    if last["close"] < last["open"] and body_ratio > 0.4:
        return Momentum.BEARISH_PRESSURE

    # ---------- INDECISION ----------
    if body_ratio < 0.2:
        return Momentum.INDECISION

    # ---------- WEAK MOVES ----------
    if last["close"] >= last["open"]:
        return Momentum.INSIDE_BULLISH_WEAK

    return Momentum.INSIDE_BEARISH_WEAK