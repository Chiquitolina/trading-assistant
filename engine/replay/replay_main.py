from dotenv import load_dotenv
import time
import pandas as pd

from engine.live.data.data_buffer import DataBuffer
from signals.signals_engine import SignalEngine

from data.market_data import fetch_history
from ui.banners import print_live_banner


# ---------- LOAD ENV ----------
load_dotenv()

# ---------- CONFIG ----------
SYMBOL = "BTCUSDT"
TIMEFRAMES = ["5m", "15m", "1h"]
TRIGGER_TF = "15m"
DAYS = 30
SLEEP = 0
ARG_TZ = "America/Argentina/Cordoba"


# ---------- INIT ----------
buffer = DataBuffer()
signals = SignalEngine(buffer)

print_live_banner()
print("\033[95m[REPLAY]\033[0m 🎬 Replay mode (SIGNALS ONLY)\n")


# ---------- NORMALIZE ----------
def normalize_df(df):
    if not isinstance(df.iloc[0]["timestamp"], (int, float)):
        df["timestamp"] = df["timestamp"].apply(lambda x: int(x.timestamp() * 1000))
    return df


# ---------- LOAD DATA ----------
df_5m = normalize_df(fetch_history(SYMBOL, "5m", DAYS))
df_15m = normalize_df(fetch_history(SYMBOL, "15m", DAYS))
df_1h = normalize_df(fetch_history(SYMBOL, "1h", DAYS))


# ---------- MERGE STREAM ----------
all_candles = []

for _, row in df_5m.iterrows():
    all_candles.append((row["timestamp"], "5m", row))

for _, row in df_15m.iterrows():
    all_candles.append((row["timestamp"], "15m", row))

for _, row in df_1h.iterrows():
    all_candles.append((row["timestamp"], "1h", row))

all_candles.sort(key=lambda x: x[0])


# ---------- STORAGE ----------
all_signals = []


# ---------- REPLAY LOOP ----------
try:
    for ts, tf, candle in all_candles:

        buffer.on_replay_candle(candle, tf)

        if tf == TRIGGER_TF and buffer.consume_closed_tf(TRIGGER_TF):

            signal_ts = buffer.last_close_time[TRIGGER_TF]
            price = candle["close"]

            print(f"\033[95m[SYNC]\033[0m 🕒 {TRIGGER_TF} CLOSED @ {signal_ts}")

            signal = signals.generate_signal()

            if not signal:
                print("\033[93m[SIGNAL]\033[0m ❌ No signal\n")
                continue

            print(f"\033[92m[SIGNAL]\033[0m ✅ {signal['side']}")

            # ---------- SAVE CLEAN SIGNAL ----------
            all_signals.append({
                "timestamp": signal_ts,
                "tf": TRIGGER_TF,
                "side": signal["side"],
                "signal_price": signal.get("price", price),
                "dir": signal.get("dir"),
                "trend": signal.get("trend"),
                "momentum": signal.get("momentum"),
            })

            print()

        if SLEEP > 0:
            time.sleep(SLEEP)

    print("\033[95m[REPLAY]\033[0m ✅ Replay finished")

except KeyboardInterrupt:
    print("\033[95m[REPLAY]\033[0m 🛑 Replay stopped")


# ---------- SAVE CSV ----------
if all_signals:
    df_signals = pd.DataFrame(all_signals)

    df_signals["timestamp"] = (
        pd.to_datetime(df_signals["timestamp"], unit="ms", utc=True)
        .dt.tz_convert(ARG_TZ)
        .dt.tz_localize(None)
    )

    df_signals.sort_values("timestamp", inplace=True)

    csv_path = f"replay_signals_{SYMBOL}.csv"
    df_signals.to_csv(csv_path, index=False)

    print(f"\033[95m[REPLAY]\033[0m 💾 Saved signals to {csv_path}")