from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT))

from dotenv import load_dotenv
import pandas as pd

from config.strategies.v1 import SYMBOLS
from data.market_data import fetch_history
from signals.indicators.trend_detector import detect_trend_up
from signals.indicators.compression_detector import detect_compression


load_dotenv()

TF = "15m"
DAYS = 7

OUTPUT_PATH = "compression_detector_test.csv"

rows = []

print()
print("===== COMPRESSION DETECTOR TEST =====")
print(f"TF={TF} DAYS={DAYS}")
print()

for symbol in SYMBOLS:
    try:
        df = fetch_history(symbol, TF, DAYS)

        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.dropna(subset=["open", "high", "low", "close", "volume"]).reset_index(drop=True)

        if len(df) < 80:
            print(f"⚠️ {symbol} not enough rows={len(df)}")
            continue

        last_trend = None
        last_compression = None

        compression_count = 0
        trend_and_compression_count = 0

        for i in range(len(df)):
            window = df.iloc[: i + 1].copy()

            trend = detect_trend_up(
                window,
                lookback=20,
                ema_fast=20,
                ema_slow=50,
                min_score=4,
            )

            compression = detect_compression(
                window,
                lookback=10,
                base_lookback=40,
                max_range_ratio=0.65,
                max_atr_ratio=0.75,
                max_volume_ratio=0.95,
                max_body_pct=0.50,
                min_score=3,
            )

            last = window.iloc[-1]
            timestamp = last["timestamp"] if "timestamp" in last else i

            trend_up = bool(trend.get("trend_up", False))
            is_compression = bool(compression.get("is_compression", False))

            if is_compression:
                compression_count += 1

            if trend_up and is_compression:
                trend_and_compression_count += 1

            rows.append({
                "symbol": symbol,
                "tf": TF,
                "timestamp": timestamp,
                "close": float(last["close"]),

                "trend_up": trend_up,
                "trend_score": trend.get("score", 0),

                "is_compression": is_compression,
                "compression_score": compression.get("score", 0),
                "compression_reasons": ",".join(compression.get("reasons", [])),

                "range_ratio": compression.get("range_ratio"),
                "atr_ratio": compression.get("atr_ratio"),
                "volume_ratio": compression.get("volume_ratio"),
                "avg_body_pct": compression.get("avg_body_pct"),
                "compression_high": compression.get("compression_high"),
                "compression_low": compression.get("compression_low"),
                "compression_range_pct": compression.get("compression_range_pct"),
            })

            last_trend = trend
            last_compression = compression

        compression_pct = round((compression_count / len(df)) * 100, 2)
        combo_pct = round((trend_and_compression_count / len(df)) * 100, 2)

        print(
            f"{symbol:<14} "
            f"rows={len(df):<4} "
            f"comp%={compression_pct:<6} "
            f"trend+comp%={combo_pct:<6} "
            f"last_trend={last_trend.get('trend_up', False)} "
            f"last_comp={last_compression.get('is_compression', False)} "
            f"last_score={last_compression.get('score', 0)}"
        )

    except Exception as e:
        print(f"❌ {symbol} error: {e}")


out = pd.DataFrame(rows)
out.to_csv(OUTPUT_PATH, index=False)

print()
print("===== DONE =====")
print(f"Saved: {OUTPUT_PATH}")