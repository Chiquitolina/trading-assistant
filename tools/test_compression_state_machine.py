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
from signals.indicators.compression_breakout_detector import detect_compression_breakout
from signals.indicators.compression_state_machine import CompressionStateMachine


load_dotenv()

TF = "15m"
DAYS = 7
OUTPUT_PATH = "compression_state_machine_test.csv"

rows = []

print()
print("===== COMPRESSION STATE MACHINE TEST =====")
print(f"TF={TF} DAYS={DAYS}")
print()

for symbol in SYMBOLS:
    try:
        df = fetch_history(symbol, TF, DAYS)

        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.dropna(
            subset=["open", "high", "low", "close", "volume"]
        ).reset_index(drop=True)

        machine = CompressionStateMachine(
            max_watch_candles=8,
            max_pullback_candles=5,
            pullback_max_pct=1.2,
            pullback_min_hold_high=True,
        )

        watch_count = 0
        breakout_count = 0
        entry_ready_count = 0
        last_state = None

        for i in range(80, len(df)):
            window = df.iloc[: i + 1].copy()
            prev_window = window.iloc[:-1].copy()
            last = window.iloc[-1]

            trend = detect_trend_up(
                prev_window,
                lookback=20,
                ema_fast=20,
                ema_slow=50,
                min_score=4,
            )

            compression = detect_compression(
                prev_window,
                lookback=10,
                base_lookback=40,
                max_range_ratio=0.65,
                max_atr_ratio=0.75,
                max_volume_ratio=0.95,
                max_body_pct=0.50,
                min_score=3,
            )

            breakout = detect_compression_breakout(window)

            result = machine.update(
                symbol=symbol,
                candle=last.to_dict(),
                trend=trend,
                compression=compression,
                breakout=breakout,
            )

            state = result.get("state")
            reason = result.get("reason")

            if state == "WATCHING_COMPRESSION":
                watch_count += 1

            if state == "BREAKOUT_DETECTED":
                breakout_count += 1

            if state == "ENTRY_READY":
                entry_ready_count += 1

            timestamp = last["timestamp"] if "timestamp" in last else i

            rows.append({
                "symbol": symbol,
                "tf": TF,
                "timestamp": timestamp,
                "close": float(last["close"]),
                "state": state,
                "reason": reason,

                "trend_up": trend.get("trend_up", False),
                "trend_score": trend.get("score", 0),

                "is_compression": compression.get("is_compression", False),
                "compression_score": compression.get("score", 0),
                "compression_high": compression.get("compression_high"),
                "compression_low": compression.get("compression_low"),

                "breakout": breakout.get("breakout", False),
                "volume_ratio": breakout.get("volume_ratio"),

                "entry_price": result.get("entry_price"),
                "breakout_price": result.get("breakout_price"),
                "breakout_volume_ratio": result.get("breakout_volume_ratio"),
            })

            last_state = state

        print(
            f"{symbol:<14} "
            f"watch={watch_count:<4} "
            f"breakout={breakout_count:<3} "
            f"entry_ready={entry_ready_count:<3} "
            f"last_state={last_state}"
        )

    except Exception as e:
        print(f"❌ {symbol} error: {e}")


out = pd.DataFrame(rows)
out.to_csv(OUTPUT_PATH, index=False)

print()
print("===== DONE =====")
print(f"Saved: {OUTPUT_PATH}")