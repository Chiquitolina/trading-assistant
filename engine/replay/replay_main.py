from dotenv import load_dotenv
import os
import time
import pandas as pd

from engine.live.position.position_manager import PositionManager
from engine.live.data.data_buffer import DataBuffer
from signals.signals_engine import SignalEngine
from engine.live.strategy.entry_engine import EntryEngine
from engine.live.execution.execution_engine import ExecutionEngine

from exchange.binance_exchange import BinanceExchange
from data.market_data import fetch_history
from ui.banners import print_live_banner

# ---------- LOAD ENV ----------
load_dotenv()
api_key = os.getenv("API_KEY")
secret = os.getenv("SECRET_KEY")

# ---------- CONFIG ----------
SYMBOL = "BTCUSDT"
TIMEFRAMES = ["5m", "15m", "1h"]
TRIGGER_TF = "15m"
DAYS = 3
SLEEP = 0  # 0.05 para slow-motion opcional

# ---------- INIT ----------
buffer = DataBuffer()
print_live_banner()

print("\033[95m[REPLAY]\033[0m 🎬 Replay mode started\n")

# ---------- 🔧 NORMALIZE DF ----------
def normalize_df(df):
    if not isinstance(df.iloc[0]["timestamp"], (int, float)):
        df["timestamp"] = df["timestamp"].apply(lambda x: int(x.timestamp() * 1000))
    return df

# ---------- 🔥 LOAD DATA ----------
df_5m = normalize_df(fetch_history(SYMBOL, "5m", DAYS))
df_15m = normalize_df(fetch_history(SYMBOL, "15m", DAYS))
df_1h = normalize_df(fetch_history(SYMBOL, "1h", DAYS))

print(f"[REPLAY] Loaded {len(df_5m)} candles (5m)")
print(f"[REPLAY] Loaded {len(df_15m)} candles (15m)")
print(f"[REPLAY] Loaded {len(df_1h)} candles (1h)")

# ---------- 🔥 MERGE STREAM ----------
all_candles = []
for _, row in df_5m.iterrows():
    all_candles.append((row["timestamp"], "5m", row))
for _, row in df_15m.iterrows():
    all_candles.append((row["timestamp"], "15m", row))
for _, row in df_1h.iterrows():
    all_candles.append((row["timestamp"], "1h", row))

# ordenar cronológicamente (simula WS real)
all_candles.sort(key=lambda x: x[0])

# ---------- EXCHANGE ----------
exchange = BinanceExchange(api_key=api_key, api_secret=secret, testnet=True)

# ---------- ENGINES ----------
signals = SignalEngine(buffer)
entry_engine = EntryEngine(buffer, debug=True)
position_manager = PositionManager(exchange)
execution = ExecutionEngine(exchange, position_manager)
execution.restore_state(SYMBOL)

# ---------- STORAGE ----------
all_signals = []
timestamps_log = []

# ---------- REPLAY LOOP ----------
try:
    for ts, tf, candle in all_candles:

        # 🔹 simular WS multi-timeframe
        buffer.on_replay_candle(candle, tf)
        price = candle["close"]

        # 🔹 ejecución normal
        execution.on_price_update(price, ts)

        # 🔴 SOLO trigger TF
        if tf == TRIGGER_TF and buffer.consume_closed_tf(TRIGGER_TF):

            signal_ts = buffer.last_close_time[TRIGGER_TF]
            print(f"\033[95m[SYNC]\033[0m 🕒 {TRIGGER_TF} CLOSED @ {signal_ts}")

            # -------------------------
            # SIGNAL
            # -------------------------
            signal = signals.generate_signal()

            plan = None
            plan_executed = False
            reason_ignored = None

            if not signal:
                print("\033[93m[SIGNAL]\033[0m ❌ No signal")
                reason_ignored = "no signal generated"
            else:
                print(f"\033[92m[SIGNAL]\033[0m ✅ {signal['side']}")
                # -------------------------
                # ENTRY
                # -------------------------
                plan = entry_engine.generate_entry(signal)

                if not plan:
                    print("\033[94m[ENTRY]\033[0m ❌ PLAN DESCARTADO")
                    reason_ignored = "plan discarded"
                else:
                    print(
                        f"\033[96m[ENTRY]\033[0m 📍 Entry={plan.entry} | SL={plan.sl} | TP={plan.tp}"
                    )
                    # -------------------------
                    # EXECUTION
                    # -------------------------
                    if execution.position is not None:
                        # ya hay posición abierta en el bot
                        plan_executed = False
                        reason_ignored = "position already open"
                        print("[EXECUTION ENGINE] ⚠️ Position already open. Plan ignored.")
                    else:
                        execution.execute_plan(plan)
                        plan_executed = True

            # -------------------------
            # LOG ALL SIGNALS
            # -------------------------
            all_signals.append({
                "timestamp": signal_ts,
                "tf": TRIGGER_TF,
                "side": signal["side"] if signal else None,
                "signal_price": signal["price"] if signal and "price" in signal else candle["close"],
                "dir": signal.get("direction", None) if signal else None,
                "trend": signal.get("trend", None) if signal else None,
                "momentum": signal.get("momentum", None) if signal else None,
                "entry": plan.entry if plan else None,
                "sl": plan.sl if plan else None,
                "tp": plan.tp if plan else None,
                "atr": plan.atr if plan else None,
                "reason": plan.reason if plan else reason_ignored,
                "plan_executed": plan_executed,
                "ignored_reason": reason_ignored
            })

            # -------------------------
            # DEBUG TIMESTAMPS CORREGIDO
            # -------------------------
            last_5m_ts = (
                df_5m["timestamp"].iloc[df_5m["timestamp"].searchsorted(signal_ts, side="right") - 1]
                if len(df_5m) > 0 else None
            )
            last_15m_ts = (
                df_15m["timestamp"].iloc[df_15m["timestamp"].searchsorted(signal_ts, side="right") - 1]
                if len(df_15m) > 0 else None
            )
            timestamps_log.append({
                "tf": TRIGGER_TF,
                "signal_ts": signal_ts,
                "last_ts_5m": last_5m_ts,
                "last_ts_15m": last_15m_ts
            })
            print(f"[DEBUG] ⏱ TIMESTAMPS")
            print(f"15m signal_ts : {signal_ts}")
            print(f"5m last_ts    : {last_5m_ts}")
            print(f"15m last_ts   : {last_15m_ts}\n")

        # 🔹 slow-motion opcional
        if SLEEP > 0:
            time.sleep(SLEEP)

    print("\033[95m[REPLAY]\033[0m ✅ Replay finished")

except KeyboardInterrupt:
    print("\033[95m[REPLAY]\033[0m 🛑 Replay stopped")

# ---------- SAVE CSV ----------
if all_signals:
    df_signals = pd.DataFrame(all_signals)
    csv_path = f"replay_signals_{SYMBOL}.csv"
    df_signals.to_csv(csv_path, index=False)
    print(f"\033[95m[REPLAY]\033[0m 💾 Saved signals to {csv_path}")