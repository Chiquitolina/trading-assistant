from ta.volatility import AverageTrueRange

# --------------------
# FILTERS
# --------------------
def atr_is_expanding(df, period, expansion_factor):
    atr = AverageTrueRange(
        high=df["high"],
        low=df["low"],
        close=df["close"],
        window=period
    ).average_true_range()

    current_atr = atr.iloc[-1]
    mean_atr = atr.iloc[-period:].mean()

    return current_atr > mean_atr * expansion_factor