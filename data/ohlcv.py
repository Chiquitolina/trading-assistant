
import ccxt
import pandas as pd

# --------------------
# EXCHANGE
# --------------------
exchange = ccxt.binanceusdm({
    "enableRateLimit": True
})

# --------------------
# DATA
# --------------------
def fetch_ohlcv(symbol, timeframe, candles):
    ohlcv = exchange.fetch_ohlcv(
        symbol,
        timeframe=timeframe,
        limit=candles
    )

    return pd.DataFrame(
        ohlcv,
        columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
