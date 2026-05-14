import ccxt
import pandas as pd
from config.timeframes import TIMEFRAME_CONFIGS

exchange = ccxt.binanceusdm({
    "enableRateLimit": True
})

def fetch_history(symbol: str, timeframe: str, days: int):
    limit = 1000

    tf_config = TIMEFRAME_CONFIGS[timeframe]
    ms_per_candle = tf_config["ms_per_candle"]

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
