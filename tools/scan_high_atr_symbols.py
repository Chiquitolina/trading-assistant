import requests
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

from signals.indicators.direction import trade_direction

BASE_URL = "https://fapi.binance.com"

INTERVAL = "15m"
LIMIT = 120
ATR_EXPANSION_PERIOD = 50
MIN_ATR_EXPANSION = 1.00
ATR_PERIOD = 14
TOP_N = 30
MAX_WORKERS = 12


def get_futures_symbols():
    url = f"{BASE_URL}/fapi/v1/exchangeInfo"
    data = requests.get(url, timeout=10).json()

    symbols = []

    for s in data["symbols"]:
        if (
            s["contractType"] == "PERPETUAL"
            and s["quoteAsset"] == "USDT"
            and s["status"] == "TRADING"
        ):
            symbols.append(s["symbol"])

    return symbols


def get_klines(symbol: str):
    url = f"{BASE_URL}/fapi/v1/klines"
    params = {
        "symbol": symbol,
        "interval": INTERVAL,
        "limit": LIMIT,
    }

    data = requests.get(url, params=params, timeout=10).json()

    df = pd.DataFrame(data, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])

    for col in ["open", "high", "low", "close", "quote_volume"]:
        df[col] = df[col].astype(float)

    return df


def calculate_atr(df: pd.DataFrame, period: int = 14):
    df = df.copy()

    df["prev_close"] = df["close"].shift(1)

    df["tr1"] = df["high"] - df["low"]
    df["tr2"] = (df["high"] - df["prev_close"]).abs()
    df["tr3"] = (df["low"] - df["prev_close"]).abs()

    df["tr"] = df[["tr1", "tr2", "tr3"]].max(axis=1)
    df["atr"] = df["tr"].rolling(period).mean()

    df["atr_mean_50"] = df["atr"].rolling(ATR_EXPANSION_PERIOD).mean()

    last = df.iloc[-1]

    atr = float(last["atr"])
    close = float(last["close"])
    atr_mean_50 = float(last["atr_mean_50"])

    atr_pct = (atr / close) * 100 if close else 0

    atr_expansion = (
        atr / atr_mean_50
        if atr_mean_50 > 0
        else 0
    )

    quote_volume = float(df["quote_volume"].tail(20).mean())

    return atr, atr_pct, atr_expansion, close, quote_volume


def analyze_symbol(symbol: str):
    try:
        df = get_klines(symbol)

        if len(df) < ATR_PERIOD + ATR_EXPANSION_PERIOD + 2:
            return None

        atr, atr_pct, atr_expansion, close, quote_volume = calculate_atr(df, ATR_PERIOD)

        direction = trade_direction(df)

        if hasattr(direction, "value"):
            direction = direction.value

        return {
            "symbol": symbol,
            "close": close,
            "atr": atr,
            "atr_pct": atr_pct,
            "atr_expansion": atr_expansion,
            "avg_quote_volume_20": quote_volume,
            "direction": direction,
        }

    except Exception as e:
        print(f"[ERROR] {symbol}: {e}")
        return None


def scan_high_atr_symbols():
    symbols = get_futures_symbols()
    results = []

    print(f"Scanning {len(symbols)} futures symbols...")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(analyze_symbol, symbol): symbol
            for symbol in symbols
        }

        for future in as_completed(futures):
            result = future.result()
            if result:
                results.append(result)

    all_df = pd.DataFrame(results)

    # =========================
    # RANGE SYMBOLS
    # =========================

    range_symbols = all_df[
        all_df["direction"].astype(str).str.lower().eq("range")
    ]["symbol"].sort_values().tolist()

    df = all_df.copy()

    # =========================
    # SCANNER FILTERS
    # =========================

    MIN_ATR_PCT = 0.30
    MAX_ATR_PCT = 1.50

    MIN_AVG_QUOTE_VOLUME_20 = 400_000

    MIN_PRICE = 0.001

    BLACKLIST_KEYWORDS = [
        "1000",
        "UP",
        "DOWN",
        "BULL",
        "BEAR",
    ]

    df = df[
        (df["atr_pct"] >= MIN_ATR_PCT) &
        (df["atr_pct"] <= MAX_ATR_PCT) &
        (df["atr_expansion"] >= MIN_ATR_EXPANSION) &
        (df["avg_quote_volume_20"] >= MIN_AVG_QUOTE_VOLUME_20) &
        (df["close"] >= MIN_PRICE)
    ]

    for keyword in BLACKLIST_KEYWORDS:
        df = df[~df["symbol"].str.contains(keyword, case=False)]

    df["score"] = df["atr_pct"] * df["atr_expansion"]

    df = df.sort_values(
        by="score",
        ascending=False
    )

    return df, range_symbols


if __name__ == "__main__":
    df, range_symbols = scan_high_atr_symbols()

    print("\n🔥 TOP VOLATILITY SCORE SYMBOLS")
    print(
        df.head(TOP_N).to_string(
            index=False,
            formatters={
                "close": "{:.6f}".format,
                "atr": "{:.6f}".format,
                "atr_pct": "{:.3f}%".format,
                "avg_quote_volume_20": "{:,.0f}".format,
                "atr_expansion": "{:.2f}x".format,
                "score": "{:.3f}".format,
            }
        )
    )

    print("\n📦 SYMBOLS EN RANGE")
    print(range_symbols)