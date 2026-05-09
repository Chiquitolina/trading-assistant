import ccxt
import pandas as pd

exchange = ccxt.binanceusdm({
    "enableRateLimit": True
})

def fetch_history(symbol: str, timeframe: str, days: int):
    limit = 1000
    ms_per_candle = {
        "5m": 5 * 60 * 1000,
        "15m": 15 * 60 * 1000,
        "1h": 60 * 60 * 1000,
        "4h": 4 * 60 * 60 * 1000,
        "1d": 24 * 60 * 60 * 1000,
    }[timeframe]

    since = int(
        (pd.Timestamp.utcnow() - pd.Timedelta(days=days)).timestamp() * 1000
    )

    all_ohlcv = []

    while True:
        ohlcv = exchange.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            since=since,
            limit=limit
        )

        if not ohlcv:
            break

        all_ohlcv.extend(ohlcv)

        last_ts = ohlcv[-1][0]
        since = last_ts + ms_per_candle

        if len(ohlcv) < limit:
            break

    df = pd.DataFrame(
        all_ohlcv,
        columns=["timestamp", "open", "high", "low", "close", "volume"]
    )

    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")

    return df
