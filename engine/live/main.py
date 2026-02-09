from engine.live.ws.ws_client import WSClient
from engine.live.data.data_buffer import DataBuffer
from signals.signals_engine import SignalEngine
from engine.live.strategy.entry_engine import EntryEngine
from engine.live.execution.execution_engine import ExecutionEngine
from engine.live.trade.trade_manager import TradeManager
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
execution = ExecutionEngine()
trade_manager = TradeManager(buffer, debug=True)

# ---------- CONNECT WS ----------
ws = WSClient(buffer.on_ws_message)
ws.start()

print("🚀 Live Engine started! Ctrl+C to stop.\n")

# ---------- EVENT LOOP ----------
try:
    while True:

        price = buffer.last_price()
        timestamp = buffer.last_timestamp()

        # 1️⃣ Execution maneja TP/SL
        execution.on_price_update(price, timestamp)

        # opcional: sync estado
        # trade_manager.sync(execution.get_state())

        # 🔔 SOLO reaccionamos al cierre del TF gatillo
        if buffer.new_closed_tf == TRIGGER_TF:
            buffer.new_closed_tf = None

            signal = signals.generate_signal()
            if not signal:
                continue

            print("💡 SIGNAL:", signal)

            plan = entry_engine.generate_entry(signal)
            if not plan:
                print("❌ PLAN DESCARTADO\n")
                continue

            # 👇 AHORA ejecuta ExecutionEngine
            execution.execute_plan(plan)

except KeyboardInterrupt:
    ws.stop()
    print("🛑 Stopped.")
