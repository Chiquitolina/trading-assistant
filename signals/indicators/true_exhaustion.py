def true_exhaustion(df, atr, side: str):
    last = df.iloc[-1]

    body = abs(last["close"] - last["open"])
    range_ = last["high"] - last["low"]

    upper_wick = last["high"] - max(last["close"], last["open"])
    lower_wick = min(last["close"], last["open"]) - last["low"]

    # =========================
    # ATR SPIKE (CORREGIDO)
    # =========================

    # usamos volatilidad del propio precio (no df["atr"])
    recent_ranges = (df["high"] - df["low"]).tail(20)
    avg_range = recent_ranges.mean()

    atr_spike = range_ > avg_range * 1.3

    # =========================
    # WICK REJECTION
    # =========================

    wick_ratio = (
        upper_wick / range_ if side == "LONG"
        else lower_wick / range_
    ) if range_ != 0 else 0

    wick_rejection = wick_ratio > 0.55

    # =========================
    # CLOSE BACK INSIDE RANGE
    # =========================

    close_back = (
        last["close"] < last["high"] - (range_ * 0.3)
        if side == "LONG"
        else last["close"] > last["low"] + (range_ * 0.3)
    )

    return atr_spike and wick_rejection and close_back