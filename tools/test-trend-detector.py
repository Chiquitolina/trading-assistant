from dotenv import load_dotenv
import pandas as pd

from config.strategies.v1 import SYMBOLS
from data.market_data import fetch_history
from signals.indicators.trend_score import detect_trend_up


load_dotenv()

# ======================================================
# CONFIG
# ======================================================

TF = "15m"
DAYS = 7

LOOKBACK = 20
EMA_FAST = 20
EMA_SLOW = 50
MIN_SCORE = 4

OUTPUT_PATH = "trend_detector_test.csv"


# ======================================================
# HELPERS
# ======================================================

def normalize_df(df):
    df = df.copy()

    required_cols = ["open", "high", "low", "close", "volume"]

    missing = [c for c in required_cols if c not in df.columns]

    if missing:
        raise ValueError(f"Missing columns: {missing}")

    for col in required_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=required_cols).reset_index(drop=True)

    return df


# ======================================================
# RUN
# ======================================================

rows = []

print()
print("===== TREND DETECTOR TEST =====")
print(f"TF={TF} DAYS={DAYS}")
print()

for symbol in SYMBOLS:
    try:
        df = fetch_history(symbol, TF, DAYS)
        df = normalize_df(df)

        if len(df) < EMA_SLOW + LOOKBACK + 5:
            print(f"⚠️ {symbol} not enough rows: {len(df)}")
            continue

        trend_up_count = 0
        last_trend = None

        for i in range(len(df)):
            window = df.iloc[: i + 1].copy()

            trend = detect_trend_up(
                window,
                lookback=LOOKBACK,
                ema_fast=EMA_FAST,
                ema_slow=EMA_SLOW,
                min_score=MIN_SCORE,
            )

            last = window.iloc[-1]

            timestamp = (
                last["timestamp"]
                if "timestamp" in last
                else i
            )

            trend_up = bool(trend.get("trend_up", False))
            score = int(trend.get("score", 0))

            if trend_up:
                trend_up_count += 1

            last_trend = trend

            rows.append({
                "symbol": symbol,
                "tf": TF,
                "timestamp": timestamp,
                "close": float(last["close"]),
                "trend_up": trend_up,
                "trend_score": score,
                "higher_high": trend.get("higher_high", False),
                "higher_low": trend.get("higher_low", False),
                "ema_fast": trend.get("ema_fast"),
                "ema_slow": trend.get("ema_slow"),
                "reasons": ",".join(trend.get("reasons", [])),
            })

        trend_pct = round((trend_up_count / len(df)) * 100, 2)

        print(
            f"{symbol:<12} "
            f"rows={len(df):<4} "
            f"trend_up%={trend_pct:<6} "
            f"last_score={last_trend.get('score', 0)} "
            f"last_trend={last_trend.get('trend_up', False)}"
        )

    except Exception as e:
        print(f"❌ {symbol} error: {e}")


# ======================================================
# SAVE
# ======================================================

out = pd.DataFrame(rows)
out.to_csv(OUTPUT_PATH, index=False)

print()
print("===== DONE =====")
print(f"Saved: {OUTPUT_PATH}")