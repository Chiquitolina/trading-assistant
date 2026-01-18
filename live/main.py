from live.ws.ws_client import WSClient
from live.data.data_buffer import DataBuffer
from live.signals.signals_engine import SignalEngine
from data.market_data import fetch_history

# ---------- CONFIG ----------
# 
SYMBOL = "BTC/USDT:USDT"
TIMEFRAMES = ["1m", "5m", "15m", "1h"]
DAYS = 3

# ---------- INIT BUFFER ----------
buffer = DataBuffer()

# Cargar histórico
limits = {
    "1m": 1440,
    "5m": 288,
    "15m": 96,
    "1h": 24
}

for tf in TIMEFRAMES:
    df_hist = fetch_history(SYMBOL, tf, limit=DAYS * limits[tf])
    buffer.load_historical(tf, df_hist)

# ---------- INIT SIGNAL ENGINE ----------
signals = SignalEngine(buffer)

# ---------- CONNECT WS ----------
ws = WSClient(buffer.on_ws_message)
ws.start()

print("🚀 Live Signal Engine started! Ctrl+C to stop.")

# ---------- EVENT LOOP ----------
try:
    while True:
        # 🔔 SOLO reaccionamos al cierre de 5m
        if buffer.new_closed_tf == "5m":
            buffer.new_closed_tf = None

            signal = signals.generate_signal()
            if signal:
                print("💡 SIGNAL:", signal)

except KeyboardInterrupt:
    ws.stop()
    print("🛑 Stopped.")
