from dotenv import load_dotenv
import os
from engine.live.position.position_manager import PositionManager
from engine.live.ws.ws_client import WSClient
from engine.live.data.data_buffer import DataBuffer
from signals.signals_engine import SignalEngine
from engine.live.strategy.entry_engine import EntryEngine
from engine.live.execution.execution_engine import ExecutionEngine
from engine.live.trade.trade_manager import TradeManager
from exchange.binance_exchange import BinanceExchange
from data.market_data import fetch_history
from ui.banners import print_live_banner

load_dotenv()

api_key = os.getenv("API_KEY")
secret = os.getenv("SECRET_KEY")

# ---------- CONFIG ----------
SYMBOL = "BTCUSDT"
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

# ---------- EXCHANGE ----------
exchange = BinanceExchange(
    api_key=api_key,
    api_secret=secret,
    testnet=True
)

# ---------- TEST API CONNECTION ----------
try:
    print("\n🔐 Testing Binance API connection...")
    balance = exchange.get_balance()
    print("✅ Binance account connected!")
    print(f"💰 USDT Balance: {balance}\n")
except Exception as e:
    print("❌ Binance API connection failed")
    print(e)
    exit()

# ---------- INIT ENGINES ----------
signals = SignalEngine(buffer)
entry_engine = EntryEngine(buffer, debug=True)
trade_manager = TradeManager(buffer, debug=True)
position_manager = PositionManager(exchange)
execution = ExecutionEngine(exchange, position_manager)

execution.restore_state(SYMBOL)

# ---------- CONNECT WS ----------
ws = WSClient(buffer.on_ws_message)
ws.start()

print("🚀 Live Engine started! Ctrl+C to stop.\n")

# ---------- EVENT LOOP ----------
try:
    while True:
        price = buffer.last_price()
        timestamp = buffer.last_timestamp()

        if price is not None and timestamp is not None:
            execution.on_price_update(price, timestamp)

        if buffer.consume_closed_tf(TRIGGER_TF):
            signal = signals.generate_signal()
            if not signal:
                continue

            plan = entry_engine.generate_entry(signal)
            if not plan:
                print("❌ PLAN DESCARTADO\n")
                continue

            execution.execute_plan(plan)

except KeyboardInterrupt:
    ws.stop()
    print("\n🛑 Live Engine stopped.")