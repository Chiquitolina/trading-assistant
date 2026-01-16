def momentum_5m(df):
    if len(df) < 2:
        return "none"

    last = df.iloc[-1]
    prev = df.iloc[-2]

    if last["close"] > prev["high"]:
        return "breakout_up"

    if last["close"] < prev["low"]:
        return "breakout_down"

    return "none"