def momentum_5m(df):

    if len(df) < 2:
        return "no_data"

    last = df.iloc[-1]
    prev = df.iloc[-2]

    body = abs(last["close"] - last["open"])
    range_ = last["high"] - last["low"]

    if range_ == 0:
        return "flat_zero_range"

    body_ratio = body / range_

    upper_wick = last["high"] - max(last["open"], last["close"])
    lower_wick = min(last["open"], last["close"]) - last["low"]

    wick_ratio = max(upper_wick, lower_wick) / range_

    # ---------- TRUE INSIDE BAR ----------
    if last["high"] <= prev["high"] and last["low"] >= prev["low"]:
        return "inside_bar"

    # ---------- BREAKOUT UP ----------
    if last["close"] > prev["high"]:
        if body_ratio > 0.6:
            return "breakout_up_strong"
        else:
            return "breakout_up_weak"

    # ---------- BREAKOUT DOWN ----------
    if last["close"] < prev["low"]:
        if body_ratio > 0.6:
            return "breakout_down_strong"
        else:
            return "breakout_down_weak"

    # ---------- WICK EXHAUSTION ----------
    if wick_ratio > 0.6:
        if upper_wick > lower_wick:
            return "exhaustion_up"
        else:
            return "exhaustion_down"

    # ---------- CONTINUATION ----------
    if body_ratio > 0.5:
        if last["close"] > prev["close"]:
            return "trend_continuation_up"
        else:
            return "trend_continuation_down"

    # ---------- BULLISH PRESSURE ----------
    if last["close"] > last["open"] and body_ratio > 0.4:
        return "bullish_pressure"

    # ---------- BEARISH PRESSURE ----------
    if last["close"] < last["open"] and body_ratio > 0.4:
        return "bearish_pressure"

    # ---------- INDECISION ----------
    if body_ratio < 0.2:
        return "indecision"

    # ---------- WEAK MOVES ----------
    if last["close"] >= last["open"]:
        return "inside_bullish_weak"

    return "inside_bearish_weak"