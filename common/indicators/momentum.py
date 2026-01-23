def momentum_5m(df):
    if len(df) < 2:
        return "none"

    last = df.iloc[-1]
    prev = df.iloc[-2]

    body = abs(last["close"] - last["open"])
    range_ = last["high"] - last["low"]

    if range_ == 0:
        return "none"

    body_ratio = body / range_

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
        
    # ---------- BULLISH PRESSURE ----------
    if last["close"] > last["open"] and body_ratio > 0.4:
        return "bullish_pressure"

    # ---------- BEARISH PRESSURE ----------
    if last["close"] < last["open"] and body_ratio > 0.4:
        return "bearish_pressure"

