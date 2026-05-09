from ta.trend import EMAIndicator


def trade_direction(
    df,
    fast_ema=20,
    slow_ema=50,
    buffer_pct=0.0008,  # 0.08%
):
    if len(df) < slow_ema:
        return "range"

    df = df.copy()

    df["ema_fast"] = EMAIndicator(
        df["close"],
        fast_ema
    ).ema_indicator()

    df["ema_slow"] = EMAIndicator(
        df["close"],
        slow_ema
    ).ema_indicator()

    last = df.iloc[-1]

    fast = last["ema_fast"]
    slow = last["ema_slow"]

    diff_pct = (fast - slow) / slow

    if diff_pct > buffer_pct:
        return "up"

    if diff_pct < -buffer_pct:
        return "down"

    return "range"