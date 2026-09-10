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

def fetch_closed_futures_candle(
    symbol: str,
    timeframe: str,
    candle_timestamp: int,
):
    if timeframe not in TIMEFRAME_CONFIGS:
        raise ValueError(
            f"Unsupported timeframe: {timeframe}"
        )

    symbol = str(symbol).upper()
    candle_timestamp = int(
        candle_timestamp
    )

    ms_per_candle = int(
        TIMEFRAME_CONFIGS[
            timeframe
        ]["ms_per_candle"]
    )

    expected_close_timestamp = (
        candle_timestamp
        + ms_per_candle
        - 1
    )

    now_ms = int(
        pd.Timestamp.utcnow().timestamp()
        * 1000
    )

    if expected_close_timestamp > now_ms:
        raise ValueError(
            "Requested candle is not closed: "
            f"symbol={symbol} "
            f"timeframe={timeframe} "
            f"timestamp={candle_timestamp}"
        )

    rows = exchange.fapiPublicGetKlines({
        "symbol": symbol,
        "interval": timeframe,
        "startTime": candle_timestamp,
        "endTime": expected_close_timestamp,
        "limit": 1,
    })

    if not isinstance(rows, list) or not rows:
        return None

    row = rows[0]

    if not isinstance(row, list) or len(row) < 8:
        raise ValueError(
            "Invalid Binance kline response: "
            f"symbol={symbol} "
            f"timeframe={timeframe}"
        )

    returned_timestamp = int(
        row[0]
    )

    returned_close_timestamp = int(
        row[6]
    )

    if returned_timestamp != candle_timestamp:
        return None

    if returned_close_timestamp > now_ms:
        raise ValueError(
            "Binance returned an open candle: "
            f"symbol={symbol} "
            f"timeframe={timeframe} "
            f"timestamp={returned_timestamp}"
        )

    return {
        "timestamp": returned_timestamp,
        "close_timestamp": (
            returned_close_timestamp
        ),
        "open": float(row[1]),
        "high": float(row[2]),
        "low": float(row[3]),
        "close": float(row[4]),
        "volume": float(row[5]),
        "quoteVolume": float(row[7]),
    }