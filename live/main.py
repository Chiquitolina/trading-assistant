from live.ws.ws_client import WSClient
from live.data.data_buffer import DataBuffer
from live.signals.signals_engine import SignalEngine
from live.strategy.entry_engine import EntryEngine
from live.trade.trade_manager import TradeManager
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
trade_manager = TradeManager(buffer, debug=True)

# ---------- CONNECT WS ----------
ws = WSClient(buffer.on_ws_message)
ws.start()

print("🚀 Live Signal Engine started! Ctrl+C to stop.\n")

# ---------- EVENT LOOP ----------
try:
    while True:

        # 1️⃣ chequeo continuo de salida (tick o vela)
        trade_manager.on_price(buffer.last_price())

        # 🔔 SOLO reaccionamos al cierre del TF gatillo
        if buffer.new_closed_tf == TRIGGER_TF:
            buffer.new_closed_tf = None

            # 2️⃣ generar señal
            signal = signals.generate_signal()
            if not signal:
                continue

            print("💡 SIGNAL:", signal)

            # 3️⃣ generar trade plan
            plan = entry_engine.generate_entry(signal)

            if not plan:
                print("❌ PLAN DESCARTADO\n")
                continue

            print("📥 TRADE PLAN")
            print(plan)

            # 4️⃣ enviar plan al TradeManager
            trade_manager.on_plan(plan)

except KeyboardInterrupt:
    ws.stop()
    print("🛑 Stopped.")
