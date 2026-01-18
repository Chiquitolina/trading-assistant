from ta.volatility import AverageTrueRange

def add_atr(df, period=14):
    atr = AverageTrueRange(
        high=df["high"],
        low=df["low"],
        close=df["close"],
        window=period
    ).average_true_range()

    df = df.copy()
    df["atr"] = atr
    return df
