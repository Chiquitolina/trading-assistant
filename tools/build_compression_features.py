import argparse
import os
import time
import numpy as np
import pandas as pd
import ccxt

from config.timeframes import TIMEFRAME_CONFIGS


exchange = ccxt.binanceusdm({
    "enableRateLimit": True
})


def normalize_symbol(symbol: str) -> str:
    # GMXUSDT -> GMX/USDT:USDT para Binance Futures en CCXT
    symbol = str(symbol).strip()

    if "/" in symbol:
        return symbol

    if symbol.endswith("USDT"):
        base = symbol.replace("USDT", "")
        return f"{base}/USDT:USDT"

    return symbol


def normalize_ts_ms(value):
    if pd.isna(value):
        return None

    if isinstance(value, pd.Timestamp):
        return int(value.timestamp() * 1000)

    value_str = str(value)

    if "-" in value_str or "T" in value_str:
        return int(pd.to_datetime(value_str, utc=True).timestamp() * 1000)

    return int(float(value))


def fetch_history(symbol: str, timeframe: str, days: int):
    limit = 1000

    tf_config = TIMEFRAME_CONFIGS[timeframe]
    ms_per_candle = tf_config["ms_per_candle"]

    ccxt_symbol = normalize_symbol(symbol)

    since = int(
        (pd.Timestamp.utcnow() - pd.Timedelta(days=days)).timestamp() * 1000
    )

    all_ohlcv = []

    while True:
        ohlcv = exchange.fetch_ohlcv(
            ccxt_symbol,
            timeframe=timeframe,
            since=since,
            limit=limit,
        )

        if not ohlcv:
            break

        all_ohlcv.extend(ohlcv)

        last_ts = ohlcv[-1][0]
        since = last_ts + ms_per_candle

        if len(ohlcv) < limit:
            break

        time.sleep(exchange.rateLimit / 1000)

    df = pd.DataFrame(
        all_ohlcv,
        columns=["timestamp", "open", "high", "low", "close", "volume"]
    )

    if df.empty:
        return df

    df = df.drop_duplicates("timestamp")
    df = df.sort_values("timestamp").reset_index(drop=True)

    return df


def reconstruct_compression_window(
    candles,
    breakout_ts,
    compression_high,
    compression_low,
    max_lookback=30,
    min_window=3,
    tolerance_pct=0.10,
):
    """
    Reconstruye la compresión mirando hacia atrás desde el breakout.
    Permite una pequeña tolerancia para no cortar por mechas mínimas.
    """

    prev = candles[candles["timestamp"] < breakout_ts].copy()

    if prev.empty:
        return pd.DataFrame()

    prev = prev.tail(max_lookback)

    height = compression_high - compression_low
    tolerance = height * tolerance_pct

    rows = []

    for _, candle in prev.iloc[::-1].iterrows():
        high_ok = candle["high"] <= compression_high + tolerance
        low_ok = candle["low"] >= compression_low - tolerance

        if not high_ok or not low_ok:
            if len(rows) >= min_window:
                break
            else:
                continue

        rows.append(candle)

    if not rows:
        return pd.DataFrame()

    window = pd.DataFrame(rows).iloc[::-1].reset_index(drop=True)

    if len(window) < min_window:
        return pd.DataFrame()

    return window


def classify_shape(upper_slope, lower_slope, flat_threshold=0.03):
    upper_flat = abs(upper_slope) <= flat_threshold
    lower_flat = abs(lower_slope) <= flat_threshold

    if upper_flat and lower_flat:
        return "horizontal_range"

    if upper_slope < -flat_threshold and lower_slope > flat_threshold:
        return "symmetrical_triangle"

    if upper_flat and lower_slope > flat_threshold:
        return "ascending_triangle"

    if upper_slope < -flat_threshold and lower_flat:
        return "descending_triangle"

    if upper_slope > flat_threshold and lower_slope > flat_threshold:
        return "ascending_channel"

    if upper_slope < -flat_threshold and lower_slope < -flat_threshold:
        return "descending_channel"

    return "irregular"


def compute_compression_features(window, compression_high, compression_low):
    duration = len(window)

    if duration < 3:
        return None

    compression_height = compression_high - compression_low
    compression_height_pct = compression_height / compression_low * 100

    x = np.arange(duration)

    upper_raw_slope = np.polyfit(x, window["high"].values, 1)[0]
    lower_raw_slope = np.polyfit(x, window["low"].values, 1)[0]

    avg_price = window["close"].mean()

    upper_slope_pct = upper_raw_slope / avg_price * 100
    lower_slope_pct = lower_raw_slope / avg_price * 100

    slope_difference = abs(upper_slope_pct - lower_slope_pct)

    tolerance_price = compression_height * 0.15

    touches_high = (
        (window["high"] >= compression_high - tolerance_price)
        & (window["high"] <= compression_high + tolerance_price)
    ).sum()

    touches_low = (
        (window["low"] <= compression_low + tolerance_price)
        & (window["low"] >= compression_low - tolerance_price)
    ).sum()

    inside_ratio = (
        (window["high"] <= compression_high)
        & (window["low"] >= compression_low)
    ).mean()

    avg_body_pct_recalc = (
        (window["close"] - window["open"]).abs() / window["open"] * 100
    ).mean()

    range_usage_pct = (
        (window["high"].max() - window["low"].min())
        / compression_height
        * 100
    )

    shape = classify_shape(
        upper_slope=upper_slope_pct,
        lower_slope=lower_slope_pct,
    )

    return {
        "compression_height_pct_recalc": compression_height_pct,
        "compression_duration_recalc": duration,
        "upper_slope": upper_slope_pct,
        "lower_slope": lower_slope_pct,
        "slope_difference": slope_difference,
        "touches_high": int(touches_high),
        "touches_low": int(touches_low),
        "inside_ratio": inside_ratio,
        "avg_body_pct_recalc": avg_body_pct_recalc,
        "range_usage_pct": range_usage_pct,
        "compression_shape": shape,
    }


def estimate_days_needed(trades, buffer_days=5, min_days=10):
    timestamps = []

    for col in ["breakout_ts", "signal_ts", "entry_ts"]:
        if col in trades.columns:
            parsed = pd.to_datetime(trades[col], errors="coerce", utc=True)
            timestamps.append(parsed)

    all_ts = pd.concat(timestamps).dropna()

    if all_ts.empty:
        return min_days

    oldest = all_ts.min()
    now = pd.Timestamp.utcnow()

    days = (now - oldest).days + buffer_days

    return max(days, min_days)


def build_compression_features(
    trades_path,
    timeframe,
    output_path,
    days,
    max_lookback,
    min_window,
):
    trades = pd.read_csv(trades_path)

    trades = trades[
        (trades["strategy_mode"] == "compression")
        & trades["compression_high"].notna()
        & trades["compression_low"].notna()
    ].copy()

    if trades.empty:
        print("No hay trades de compresión para procesar.")
        return

    if days is None:
        days = estimate_days_needed(trades)

    print(f"Days to fetch: {days}")
    print(f"Trades compression: {len(trades)}")

    symbols = sorted(trades["symbol"].dropna().unique())

    history_cache = {}

    for symbol in symbols:
        try:
            print(f"Fetching {symbol} {timeframe}...")
            history_cache[symbol] = fetch_history(symbol, timeframe, days)
            print(f"  candles: {len(history_cache[symbol])}")
        except Exception as e:
            print(f"  ERROR fetching {symbol}: {e}")
            history_cache[symbol] = pd.DataFrame()

    rows = []

    for _, trade in trades.iterrows():
        base = trade.to_dict()

        symbol = trade["symbol"]

        candles = history_cache.get(symbol, pd.DataFrame())

        if candles.empty:
            base["feature_error"] = "missing_history"
            rows.append(base)
            continue

        compression_high = float(trade["compression_high"])
        compression_low = float(trade["compression_low"])

        breakout_ts = None

        if "breakout_ts" in trade and not pd.isna(trade["breakout_ts"]):
            breakout_ts = normalize_ts_ms(trade["breakout_ts"])

        if breakout_ts is None and "signal_ts" in trade:
            breakout_ts = normalize_ts_ms(trade["signal_ts"])

        if breakout_ts is None:
            base["feature_error"] = "missing_breakout_ts"
            rows.append(base)
            continue

        window = reconstruct_compression_window(
            candles=candles,
            breakout_ts=breakout_ts,
            compression_high=compression_high,
            compression_low=compression_low,
            max_lookback=max_lookback,
            min_window=min_window,
        )

        if window.empty:
            base["feature_error"] = "empty_window"
            rows.append(base)
            continue

        features = compute_compression_features(
            window=window,
            compression_high=compression_high,
            compression_low=compression_low,
        )

        if features is None:
            base["feature_error"] = "not_enough_window"
            rows.append(base)
            continue

        base.update(features)

        base["feature_error"] = ""
        base["window_start_ts"] = window["timestamp"].iloc[0]
        base["window_end_ts"] = window["timestamp"].iloc[-1]

        rows.append(base)

    out = pd.DataFrame(rows)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    out.to_csv(output_path, index=False)

    print()
    print(f"✅ Saved: {output_path}")
    print(f"Rows: {len(out)}")

    if "feature_error" in out.columns:
        print()
        print("Feature errors:")
        print(out["feature_error"].value_counts(dropna=False))

    if "compression_shape" in out.columns:
        print()
        print("Shapes:")
        print(out["compression_shape"].value_counts(dropna=False))


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--trades", default="trades.csv")
    parser.add_argument("--timeframe", default="30m")
    parser.add_argument("--output", default="reports/compression_features_by_trade.csv")
    parser.add_argument("--days", type=int, default=None)

    parser.add_argument("--max-lookback", type=int, default=30)
    parser.add_argument("--min-window", type=int, default=3)

    args = parser.parse_args()

    build_compression_features(
        trades_path=args.trades,
        timeframe=args.timeframe,
        output_path=args.output,
        days=args.days,
        max_lookback=args.max_lookback,
        min_window=args.min_window,
    )


if __name__ == "__main__":
    main()