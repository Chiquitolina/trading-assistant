import ccxt
import pandas as pd

exchange = ccxt.binanceusdm({
    "enableRateLimit": True
})

def fetch_history(symbol: str, timeframe: str, limit: int = 1000) -> pd.DataFrame:
    ohlcv = exchange.fetch_ohlcv(
        symbol,
        timeframe=timeframe,
        limit=limit
    )

    return pd.DataFrame(
        ohlcv,
        columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    