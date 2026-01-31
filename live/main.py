from live.ws.ws_client import WSClient
from live.data.data_buffer import DataBuffer
from live.signals.signals_engine import SignalEngine
from live.strategy.entry_engine import EntryEngine
from data.market_data import fetch_history
from ui.banners import print_live_banner

# ---------- CONFIG ----------
SYMBOL = "BTC/USDT:USDT"
TIMEFRAMES = ["5m", "15m", "1h"]
TRIGGER_TF = "15m"
DAYS = 3

# ---------- INIT BUFFER ----------
buffer = DataBuffer()

print_live_banner()

# ---------- LOAD HISTORICAL ----------
for tf in TIMEFRAMES:
    df_hist = fetch_history(SYMBOL, tf, DAYS)
    buffer.load_historical(tf, df_hist)

# ---------- INIT ENGINES ----------
signals = SignalEngine(buffer)
entry_engine = EntryEngine(buffer, debug=True)

# ---------- CONNECT WS ----------
ws = WSClient(buffer.on_ws_message)
ws.start()

print("🚀 Live Signal Engine started! Ctrl+C to stop.\n")

# ---------- EVENT LOOP ----------
try:
    while True:

        # 🔔 SOLO reaccionamos al cierre del TF gatillo
        if buffer.new_closed_tf == TRIGGER_TF:
            buffer.new_closed_tf = None

            # 1️⃣ generar señal (LONG / SHORT / None)
            signal = signals.generate_signal()

            if not signal:
                continue

            print("💡 SIGNAL:", signal)

            # 2️⃣ generar trade plan (idéntico al backtest)
            plan = entry_engine.generate_entry(signal)

            if plan:
                print("✅ PLAN CONFIRMADO\n")
            else:
                print("❌ PLAN DESCARTADO\n")

except KeyboardInterrupt:
    ws.stop()
    print("🛑 Stopped.")
