from dotenv import load_dotenv
import os
import time
import threading
import argparse

from engine.live.position.position_manager import PositionManager
from engine.live.ws.ws_client import WSClient
from engine.live.data.data_buffer import DataBuffer
from engine.live.journal.signal_journal import SignalJournal
from signals.signals_engine import SignalEngine
from engine.live.strategy.entry_engine import EntryEngine
from engine.live.execution.execution_engine import ExecutionEngine
from engine.live.trade.trade_manager import TradeManager
from exchange.binance_exchange import BinanceExchange
from data.market_data import fetch_history
from ui.banners import print_live_banner
from engine.live.status_writer import StatusWriter

load_dotenv()

parser = argparse.ArgumentParser()

parser.add_argument(
    "--strategy",
    type=str,
    default="default",
    choices=[
        "default",
        "direction",
    ],
)

args = parser.parse_args()

STRATEGY_MODE = args.strategy

api_key = os.getenv("API_KEY")
secret = os.getenv("SECRET_KEY")

# ---------- CONFIG ----------
SYMBOL = "BTCUSDT"
TIMEFRAMES = ["5m", "15m", "1h"]
TRIGGER_TF = "15m"
DAYS = 3
STATUS_INTERVAL = 3

# ---------- INIT BUFFER ----------
buffer = DataBuffer()
signal_journal = SignalJournal(f"live_signals_{SYMBOL}.csv")
print_live_banner()

print(
    f"\033[95m[STRATEGY]\033[0m "
    f"mode={STRATEGY_MODE}\n"
)

# ---------- LOAD HISTORICAL ----------
for tf in TIMEFRAMES:
    df_hist = fetch_history(SYMBOL, tf, DAYS)
    buffer.load_historical(tf, df_hist)

# ---------- EXCHANGE ----------
exchange = BinanceExchange(
    api_key=api_key,
    api_secret=secret,
    testnet=False
)

# ---------- TEST API CONNECTION ----------
try:
    print(f"\033[94m[EXCHANGE]\033[0m 🔐 Testing Binance API connection.")
    balance = exchange.get_balance()
    print(f"\033[94m[EXCHANGE]\033[0m ✅ Binance account connected!")
    print(f"\033[94m[EXCHANGE]\033[0m 💰 USDT Balance: {balance}\n")
except Exception as e:
    print(f"\033[94m[EXCHANGE]\033[0m ❌ Binance API connection failed")
    print(e)
    raise SystemExit(1)

# ---------- INIT ENGINES ----------
signals = SignalEngine(buffer)
entry_engine = EntryEngine(buffer, debug=True)
trade_manager = TradeManager(buffer, debug=True)
position_manager = PositionManager(exchange)
execution = ExecutionEngine(exchange, position_manager)
status_writer = StatusWriter()

# ---------- STATUS DEFAULT ----------
last_status_ts = 0
last_signal_side = "N/A"
last_signal_trend = None
last_signal_direction = None
last_signal_momentum = None

status_writer.write({
    "engine_online": True,
    "ws_online": False,
    "symbol": SYMBOL,
    "balance": 0.0,
    "position_side": "NONE",
    "position_qty": 0.0,
    "entry_price": 0.0,
    "unpnl": 0.0,
    "last_signal": "N/A",
    "signal_trend": None,
    "signal_direction": None,
    "signal_momentum": None,
})

# ---------- RESTORE STATE ----------
execution.restore_state(SYMBOL)

# ---------- CONNECT WS ----------
ws = WSClient(buffer.on_ws_message)
ws.start()

# correr heartbeat/reconnect loop del WS en background
ws_thread = threading.Thread(target=ws.run, daemon=True)
ws_thread.start()

print("\033[94m[LIVE ENGINE]\033[0m 🚀 Live Engine started! Ctrl+C to stop.\n")


def write_heartbeat():
    try:
        balance = exchange.get_balance()
    except Exception:
        balance = 0.0

    try:
        position = exchange.get_position(SYMBOL)
    except Exception:
        position = None

    if position:
        amount = float(position["amount"])
        position_side = "LONG" if amount > 0 else "SHORT"
        position_qty = abs(amount)
        entry_price = float(position["entry_price"])
        unpnl = float(position["unrealized_pnl"])
    else:
        position_side = "NONE"
        position_qty = 0.0
        entry_price = 0.0
        unpnl = 0.0

    status_writer.write({
        "engine_online": True,
        "ws_online": ws.is_connected,
        "symbol": SYMBOL,
        "balance": balance,
        "position_side": position_side,
        "position_qty": position_qty,
        "entry_price": entry_price,
        "unpnl": unpnl,
        "last_signal": last_signal_side,
        "signal_trend": last_signal_trend,
        "signal_direction": last_signal_direction,
        "signal_momentum": last_signal_momentum,
    })


# ---------- EVENT LOOP ----------
try:
    while True:
        now = time.time()

        if now - last_status_ts >= STATUS_INTERVAL:
            write_heartbeat()
            last_status_ts = now

        price = buffer.last_price()
        timestamp = buffer.last_timestamp()

        if price is not None and timestamp is not None:
            execution.on_price_update(price, timestamp)

        if buffer.consume_closed_tf(TRIGGER_TF):
            print("\033[93m[LIVE MAIN]\033[0m ✅ 15m close event consumed")

            closed_candle_ts = buffer.last_close_time[TRIGGER_TF]
            close_price = buffer.last_price()

            # ==========================
            # 🕒 TIME STOP CHECK
            # ==========================
            execution._on_15m_candle_closed(
                closed_candle_ts=closed_candle_ts,
                close_price=close_price
            )

            signal = signals.generate_signal()
            print(f"\033[93m[LIVE MAIN]\033[0m signal returned: {signal}")
            
            if signal and execution.position:

                execution.update_position_context(
                    trend=signal.get("trend"),
                    direction=signal.get("direction"),
                    momentum=signal.get("momentum"),
                    current_price=close_price
                )

            if not signal:
                continue

            last_signal_side = signal["side"]
            last_signal_trend = signal.get("trend")
            last_signal_direction = signal.get("direction")
            last_signal_momentum = signal.get("momentum")

            signal_journal.log_signal(
                timestamp=signal["signal_ts"],
                tf=TRIGGER_TF,
                side=signal["side"],
                signal_price=signal["signal_price"],
                direction=signal.get("direction"),
                trend=signal.get("trend"),
                momentum=signal.get("momentum"),
            )

            if signal["side"] == "NONE":
                continue

            plan = entry_engine.generate_entry(signal)
            if not plan:
                print("\033[94m[ENTRY PLANNER]\033[0m ❌ PLAN DESCARTADO\n")
                continue

            try:
                executed = execution.execute_plan(plan)
                print(f"[LIVE MAIN] execution result: {executed}")
            except Exception as e:
                print(f"[LIVE MAIN] ❌ execution error but loop continues: {e}")

        time.sleep(0.2)

except KeyboardInterrupt:
    ws.stop()

    status_writer.write({
        "engine_online": False,
        "ws_online": False,
        "symbol": SYMBOL,
        "balance": 0.0,
        "position_side": "NONE",
        "position_qty": 0.0,
        "entry_price": 0.0,
        "unpnl": 0.0,
        "last_signal": "STOPPED",
        "signal_trend": None,
        "signal_direction": None,
        "signal_momentum": None,
    })

    print("\033[94m[LIVE ENGINE]\033[0m 🛑 Live Engine stopped.")