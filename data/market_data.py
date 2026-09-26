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

def fetch_futures_klines_range(
    symbol: str,
    timeframe: str,
    start_ms: int,
    end_ms: int,
):
    if timeframe not in TIMEFRAME_CONFIGS:
        raise ValueError(
            f"Unsupported timeframe: {timeframe}"
        )

    symbol = str(symbol).upper()
    start_ms = int(start_ms)
    end_ms = int(end_ms)

    if end_ms < start_ms:
        raise ValueError(
            "end_ms must be >= start_ms"
        )

    ms_per_candle = int(
        TIMEFRAME_CONFIGS[
            timeframe
        ]["ms_per_candle"]
    )

    limit = 1000
    since = start_ms
    candles = []

    while since <= end_ms:
        rows = exchange.fapiPublicGetKlines({
            "symbol": symbol,
            "interval": timeframe,
            "startTime": since,
            "endTime": end_ms,
            "limit": limit,
        })

        if not rows:
            break

        for row in rows:
            if not isinstance(row, list) or len(row) < 8:
                raise ValueError(
                    "Invalid Binance kline response | "
                    f"symbol={symbol} | "
                    f"timeframe={timeframe}"
                )

            open_timestamp = int(row[0])
            close_timestamp = int(row[6])

            if open_timestamp < start_ms:
                continue

            if open_timestamp > end_ms:
                continue

            candles.append({
                "timestamp": open_timestamp,
                "close_timestamp": close_timestamp,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "quoteVolume": float(row[7]),
            })

        last_timestamp = int(rows[-1][0])
        next_since = last_timestamp + ms_per_candle

        if next_since <= since:
            raise RuntimeError(
                "Historical pagination did not advance | "
                f"symbol={symbol} | "
                f"timeframe={timeframe} | "
                f"since={since}"
            )

        since = next_since

        if len(rows) < limit:
            break

    return candles

HISTORY_TARGET_CANDLES = {
    "1m": 400,
    "5m": 400,
    "15m": 288,
    "30m": 240,
    "1h": 168,
    "4h": 150,
    "1d": 180,
}


def fetch_closed_history_before(
    symbol: str,
    timeframe: str,
    cutoff_ms: int,
    target_candles: int | None = None,
):
    """
    Return a deterministic historical seed.

    Rules:
    - only fully closed candles before cutoff_ms
    - sorted by open timestamp
    - deduplicated by open timestamp
    - exactly the last target_candles when available
    """

    if timeframe not in TIMEFRAME_CONFIGS:
        raise ValueError(
            f"Unsupported timeframe: {timeframe}"
        )

    symbol = str(symbol).upper()
    cutoff_ms = int(cutoff_ms)

    if target_candles is None:
        target_candles = (
            HISTORY_TARGET_CANDLES[
                timeframe
            ]
        )

    target_candles = int(
        target_candles
    )

    if target_candles <= 0:
        raise ValueError(
            "target_candles must be > 0"
        )

    ms_per_candle = int(
        TIMEFRAME_CONFIGS[
            timeframe
        ]["ms_per_candle"]
    )

    # Fetch substantially more than required.
    # This gives room for alignment and any
    # occasional missing interval.
    lookback_candles = (
        target_candles * 2
        + 10
    )

    start_ms = max(
        0,
        cutoff_ms
        - (
            lookback_candles
            * ms_per_candle
        ),
    )

    candles = fetch_futures_klines_range(
        symbol=symbol,
        timeframe=timeframe,
        start_ms=start_ms,
        end_ms=cutoff_ms - 1,
    )

    # Only candles that were already completely
    # closed at the requested cutoff.
    candles = [
        candle
        for candle in candles
        if int(
            candle["close_timestamp"]
        ) < cutoff_ms
    ]

    # Deterministic deduplication by candle open.
    by_timestamp = {}

    for candle in candles:
        timestamp = int(
            candle["timestamp"]
        )

        by_timestamp[
            timestamp
        ] = candle

    candles = [
        by_timestamp[timestamp]
        for timestamp
        in sorted(by_timestamp)
    ]

    if len(candles) > target_candles:
        candles = candles[
            -target_candles:
        ]

    return candles

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