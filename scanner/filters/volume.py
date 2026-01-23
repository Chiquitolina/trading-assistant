def volume_above_average(df, lookback):
    current_volume = df["volume"].iloc[-1]
    avg_volume = df["volume"].iloc[-lookback:].mean()

    return current_volume > avg_volume