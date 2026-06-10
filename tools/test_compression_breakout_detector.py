from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT))

from dotenv import load_dotenv
import pandas as pd

from config.strategies.v1 import SYMBOLS
from data.market_data import fetch_history
from signals.indicators.compression_breakout_detector import detect_compression_breakout


load_dotenv()

TF = "15m"
DAYS = 7
OUTPUT_PATH = "compression_breakout_test.csv"

rows = []

print()
print("===== COMPRESSION BREAKOUT TEST =====")
print(f"TF={TF} DAYS={DAYS}")
print()

for symbol in SYMBOLS:
    try:
        df = fetch_history(symbol, TF, DAYS)

        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.dropna(subset=["open", "high", "low", "close", "volume"]).reset_index(drop=True)

        breakout_count = 0
        last_result = None

        for i in range(80, len(df)):
            window = df.iloc[: i + 1].copy()
            result = detect_compression_breakout(window)

            last = window.iloc[-1]
            timestamp = last["timestamp"] if "timestamp" in last else i

            if result.get("breakout", False):
                breakout_count += 1

            rows.append({
                "symbol": symbol,
                "tf": TF,
                "timestamp": timestamp,
                "close": float(last["close"]),
                "breakout": result.get("breakout", False),
                "reason": result.get("reason"),
                "volume_ratio": result.get("volume_ratio"),
                "compression_high": result.get("compression_high"),
                "compression_low": result.get("compression_low"),
                "close_breaks_high": result.get("close_breaks_high"),
                "bullish_candle": result.get("bullish_candle"),
                "volume_expansion": result.get("volume_expansion"),
            })

            last_result = result

        print(
            f"{symbol:<14} "
            f"breakouts={breakout_count:<3} "
            f"last_breakout={last_result.get('breakout', False)} "
            f"last_reason={last_result.get('reason')}"
        )

    except Exception as e:
        print(f"❌ {symbol} error: {e}")

out = pd.DataFrame(rows)
out.to_csv(OUTPUT_PATH, index=False)

print()
print("===== DONE =====")
print(f"Saved: {OUTPUT_PATH}")