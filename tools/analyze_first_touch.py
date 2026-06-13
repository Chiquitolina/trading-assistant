import time
import requests
import pandas as pd
from datetime import timedelta

TRADES_CSV = "trades.csv"
BASE_URL = "https://fapi.binance.com"

THRESHOLDS = [0.10, 0.15, 0.20, 0.25, 0.30, 0.40]
INTERVAL = "1m"
LOOKAHEAD_MINUTES = 240


def to_ms(ts):
    return int(pd.to_datetime(ts, utc=True).timestamp() * 1000)


def get_klines(symbol, start_ms, end_ms):
    url = f"{BASE_URL}/fapi/v1/klines"
    params = {
        "symbol": symbol,
        "interval": INTERVAL,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": 1000,
    }

    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()

    rows = r.json()

    return [
        {
            "open_time": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
        }
        for k in rows
    ]


def first_touch_from_candles(trade, candles, threshold):
    side = trade["side"]
    entry = float(trade["real_entry"])

    for c in candles:
        high = c["high"]
        low = c["low"]

        if side == "LONG":
            tp_hit = ((high - entry) / entry) * 100 >= threshold
            sl_hit = ((low - entry) / entry) * 100 <= -threshold
        else:
            tp_hit = ((entry - low) / entry) * 100 >= threshold
            sl_hit = ((entry - high) / entry) * 100 <= -threshold

        if tp_hit and sl_hit:
            return "BOTH_SAME_CANDLE"
        if tp_hit:
            return "TP_FIRST"
        if sl_hit:
            return "SL_FIRST"

    return "NONE"


def main():
    trades = pd.read_csv(
        TRADES_CSV,
        engine="python",
        on_bad_lines="skip"
    )

    trades = trades.dropna(subset=["symbol", "side", "entry_ts", "real_entry"])

    print(f"📊 Trades loaded: {len(trades)}")

    counts_by_threshold = {
        threshold: {
            "TP_FIRST": 0,
            "SL_FIRST": 0,
            "BOTH_SAME_CANDLE": 0,
            "NONE": 0,
        }
        for threshold in THRESHOLDS
    }

    for i, trade in trades.iterrows():
        try:
            symbol = trade["symbol"]

            entry_ms = to_ms(trade["entry_ts"])
            end_ms = entry_ms + LOOKAHEAD_MINUTES * 60 * 1000

            candles = get_klines(symbol, entry_ms, end_ms)

            print(f"🔎 {i + 1}/{len(trades)} {symbol} candles={len(candles)}")

            for threshold in THRESHOLDS:
                result = first_touch_from_candles(trade, candles, threshold)
                counts_by_threshold[threshold][result] += 1

            time.sleep(0.08)

        except Exception as e:
            print(f"⚠️ Error {trade['symbol']} {trade['entry_ts']}: {e}")

            for threshold in THRESHOLDS:
                counts_by_threshold[threshold]["NONE"] += 1

    results = []

    for threshold, counts in counts_by_threshold.items():
        total = sum(counts.values())
        decided = counts["TP_FIRST"] + counts["SL_FIRST"]

        winrate_all = counts["TP_FIRST"] / total * 100 if total else 0
        winrate_decided = counts["TP_FIRST"] / decided * 100 if decided else 0

        results.append({
            "threshold": threshold,
            "total": total,
            "tp_first": counts["TP_FIRST"],
            "sl_first": counts["SL_FIRST"],
            "both_same_candle": counts["BOTH_SAME_CANDLE"],
            "none": counts["NONE"],
            "winrate_all_pct": round(winrate_all, 2),
            "winrate_decided_pct": round(winrate_decided, 2),
        })

    df = pd.DataFrame(results)

    print("\n==============================")
    print("FIRST TOUCH RESULTS")
    print("==============================")
    print(df.to_string(index=False))

    df.to_csv("first_touch_results.csv", index=False)
    print("\n✅ Guardado en first_touch_results.csv")


if __name__ == "__main__":
    main()