from ta.trend import EMAIndicator, ADXIndicator

# ======================================================
# TRADE DIRECTION – TIMEFRAMES MEDIOS (15m)
# ======================================================
def trade_direction(
    df,
    fast_ema=20,
    slow_ema=50
):
    if len(df) < slow_ema:
        return "range"

    df = df.copy()

    df["ema_fast"] = EMAIndicator(df["close"], fast_ema).ema_indicator()
    df["ema_slow"] = EMAIndicator(df["close"], slow_ema).ema_indicator()

    last = df.iloc[-1]

    if last["ema_fast"] > last["ema_slow"]:
        return "up"

    if last["ema_fast"] < last["ema_slow"]:
        return "down"

    return "range"
