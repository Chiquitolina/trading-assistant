import json
import time
from pathlib import Path

import requests
import pandas as pd

from config.strategies.v1 import SYMBOLS


# ==========================
# CONFIG
# ==========================

BASE_URL = "https://fapi.binance.com"

INTERVAL = "1h"
LOOKBACK_DAYS = 30
LIMIT = 24 * LOOKBACK_DAYS + 5

OUTPUT_DIR = Path("snapshots/btc_context")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ==========================
# SYMBOLS
# ==========================

SYMBOLS = sorted(set(SYMBOLS))

if "BTCUSDT" in SYMBOLS:
    SYMBOLS.remove("BTCUSDT")

SYMBOLS.insert(0, "BTCUSDT")


# ==========================
# FETCH
# ==========================

def fetch_klines(
    symbol: str,
    interval: str = INTERVAL,
    limit: int = LIMIT,
) -> pd.DataFrame:
    url = f"{BASE_URL}/fapi/v1/klines"

    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
    }

    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()

    data = r.json()

    df = pd.DataFrame(
        data,
        columns=[
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_volume",
            "trades",
            "taker_buy_base",
            "taker_buy_quote",
            "ignore",
        ],
    )

    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")

    df = df.dropna(subset=["close"]).reset_index(drop=True)

    return df[["open_time", "close", "volume"]]


# ==========================
# METRICS
# ==========================

def calc_return(df: pd.DataFrame, bars: int) -> float | None:
    if len(df) < bars + 1:
        return None

    old = df["close"].iloc[-bars]
    now = df["close"].iloc[-1]

    if old <= 0:
        return None

    return (now / old - 1) * 100


def safe_round(value, decimals: int = 4):
    if value is None or pd.isna(value):
        return None

    return round(float(value), decimals)


def calc_relative_metrics(
    symbol_df: pd.DataFrame,
    btc_df: pd.DataFrame,
) -> dict:
    merged = pd.merge(
        symbol_df,
        btc_df,
        on="open_time",
        suffixes=("_coin", "_btc"),
    )

    if len(merged) < 50:
        return {
            "error": "not_enough_merged_data",
            "rows": len(merged),
        }

    merged["coin_ret"] = merged["close_coin"].pct_change()
    merged["btc_ret"] = merged["close_btc"].pct_change()

    merged = merged.dropna(subset=["coin_ret", "btc_ret"])

    if len(merged) < 50:
        return {
            "error": "not_enough_return_data",
            "rows": len(merged),
        }

    last_7d = merged.tail(24 * 7)
    last_30d = merged.tail(24 * 30)

    def metrics_for_window(window: pd.DataFrame, suffix: str) -> dict:
        coin_ret = window["coin_ret"]
        btc_ret = window["btc_ret"]

        corr = coin_ret.corr(btc_ret)

        btc_var = btc_ret.var()
        beta = (
            coin_ret.cov(btc_ret) / btc_var
            if btc_var is not None and btc_var > 0
            else None
        )

        r2 = corr ** 2 if corr is not None and not pd.isna(corr) else None

        btc_vol = btc_ret.std()
        coin_vol = coin_ret.std()

        vol_ratio = (
            coin_vol / btc_vol
            if btc_vol is not None and btc_vol > 0
            else None
        )

        return {
            f"btc_corr_{suffix}": safe_round(corr),
            f"beta_vs_btc_{suffix}": safe_round(beta),
            f"r2_vs_btc_{suffix}": safe_round(r2),
            f"vol_ratio_vs_btc_{suffix}": safe_round(vol_ratio),
        }

    coin_return_7d = calc_return(symbol_df, 24 * 7)
    btc_return_7d = calc_return(btc_df, 24 * 7)

    coin_return_30d = calc_return(symbol_df, 24 * 30)
    btc_return_30d = calc_return(btc_df, 24 * 30)

    result = {}

    result.update(metrics_for_window(last_7d, "7d"))
    result.update(metrics_for_window(last_30d, "30d"))

    result["coin_return_7d"] = safe_round(coin_return_7d)
    result["btc_return_7d"] = safe_round(btc_return_7d)
    result["outperformance_7d"] = (
        safe_round(coin_return_7d - btc_return_7d)
        if coin_return_7d is not None and btc_return_7d is not None
        else None
    )

    result["coin_return_30d"] = safe_round(coin_return_30d)
    result["btc_return_30d"] = safe_round(btc_return_30d)
    result["outperformance_30d"] = (
        safe_round(coin_return_30d - btc_return_30d)
        if coin_return_30d is not None and btc_return_30d is not None
        else None
    )

    result["rows"] = len(merged)
    result["timeframe"] = INTERVAL
    result["lookback_days"] = LOOKBACK_DAYS
    result["updated_at"] = pd.Timestamp.utcnow().isoformat()

    return result


# ==========================
# SAVE
# ==========================

def save_symbol_context(symbol: str, data: dict):
    path = OUTPUT_DIR / f"{symbol}.json"

    payload = {
        "symbol": symbol,
        **data,
    }

    tmp_path = path.with_suffix(".json.tmp")

    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=4)

    tmp_path.replace(path)


# ==========================
# MAIN
# ==========================

def main():
    print("[BTC RELATIVE CONTEXT] fetching BTCUSDT...")

    btc_df = fetch_klines("BTCUSDT")

    rows = []

    for symbol in SYMBOLS:
        if symbol == "BTCUSDT":
            continue

        try:
            print(f"[BTC RELATIVE CONTEXT] processing {symbol}...")

            symbol_df = fetch_klines(symbol)

            metrics = calc_relative_metrics(
                symbol_df=symbol_df,
                btc_df=btc_df,
            )

            save_symbol_context(symbol, metrics)

            rows.append(
                {
                    "symbol": symbol,
                    **metrics,
                }
            )

            time.sleep(0.15)

        except Exception as e:
            print(f"[ERROR] {symbol}: {e}")

            save_symbol_context(
                symbol,
                {
                    "error": str(e),
                    "updated_at": pd.Timestamp.utcnow().isoformat(),
                },
            )

    df = pd.DataFrame(rows)

    if not df.empty and "outperformance_7d" in df.columns:
        print("\n===== TOP OUTPERFORMANCE 7D =====")

        cols = [
            "symbol",
            "outperformance_7d",
            "coin_return_7d",
            "btc_return_7d",
            "vol_ratio_vs_btc_7d",
            "beta_vs_btc_7d",
            "btc_corr_7d",
            "r2_vs_btc_7d",
        ]

        existing_cols = [
            c for c in cols
            if c in df.columns
        ]

        print(
            df[existing_cols]
            .dropna(subset=["outperformance_7d"])
            .sort_values("outperformance_7d", ascending=False)
            .head(30)
            .to_string(index=False)
        )

    print(f"\n[DONE] saved JSON files in {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()