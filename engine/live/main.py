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
from engine.live.strategy.router import StrategyRouter

from engine.live.execution.execution_engine import ExecutionEngine
from engine.live.execution.strategies.default_execution_strategy import (
    DefaultExecutionStrategy
)
from engine.live.execution.strategies.direction_execution_strategy import DirectionExecutionStrategy

from engine.live.trade.trade_manager import TradeManager

from exchange.binance_exchange import BinanceExchange

from data.market_data import fetch_history

from ui.banners import print_live_banner

from engine.live.status_writer import StatusWriter

from enums.actions import Action

from engine.live.status_helper import update_status

from config.timeframes import (
    MODE_CONFIG,
    TIMEFRAME_CONFIGS
)

from config.strategies.v1 import SYMBOLS

load_dotenv()

# =========================================================
# CLI
# =========================================================

parser = argparse.ArgumentParser()

parser.add_argument(
    "--strategy",
    type=str,
    default="default",
    choices=[
        "default",
        "direction",
        "aggressive"
    ],
)

args = parser.parse_args()

STRATEGY_MODE = args.strategy

mode_config = MODE_CONFIG[STRATEGY_MODE]

TIMEFRAMES = mode_config["timeframes"]
TRIGGER_TF = mode_config["trigger_tf"]

# =========================================================
# ENV
# =========================================================

api_key = os.getenv("API_KEY")
secret = os.getenv("SECRET_KEY")

# =========================================================
# CONFIG
# =========================================================

SYMBOL = "DOGEUSDT"

#TIMEFRAMES = [
#    "5m",
#    "15m",
#    "1h"
#]

#TRIGGER_TF = "15m"

DAYS = 3

DAYS_BY_TF = {
    "1m": 1,
    "5m": 2,
    "15m": 3,
    "1h": 7,
    "4h": 25,
}

STATUS_INTERVAL = 3

# =========================================================
# INIT BUFFER
# =========================================================

buffer = DataBuffer(
    TIMEFRAMES,
    symbols=SYMBOLS
)

signal_journal = SignalJournal(
    "live_signals_multi_asset.csv"
)

print_live_banner()

print(
    f"\033[95m[STRATEGY]\033[0m "
    f"mode={STRATEGY_MODE}\n"
)

# =========================================================
# LOAD HISTORICAL
# =========================================================

for symbol in SYMBOLS:
    for tf in TIMEFRAMES:

        print(f"[HISTORY] loading symbol={symbol} tf={tf}")

        days = DAYS_BY_TF.get(tf, 3)

        df_hist = fetch_history(
            symbol,
            tf,
            days
        )

        print(f"[HISTORY] {symbol} {tf} fetched rows={len(df_hist)}")

        buffer.load_historical(
            symbol,
            tf,
            df_hist
        )

        print(f"[HISTORY] {symbol} {tf} loaded into buffer\n")

# =========================================================
# EXCHANGE
# =========================================================

exchange = BinanceExchange(
    api_key=api_key,
    api_secret=secret,
    testnet=False
)

# =========================================================
# TEST API CONNECTION
# =========================================================

try:

    print(
        f"\033[94m[EXCHANGE]\033[0m "
        f"🔐 Testing Binance API connection."
    )

    balance = exchange.get_balance()

    print(
        f"\033[94m[EXCHANGE]\033[0m "
        f"✅ Binance account connected!"
    )

    print(
        f"\033[94m[EXCHANGE]\033[0m "
        f"💰 USDT Balance: {balance}\n"
    )

except Exception as e:

    print(
        f"\033[94m[EXCHANGE]\033[0m "
        f"❌ Binance API connection failed"
    )

    print(e)

    raise SystemExit(1)

# =========================================================
# INIT ENGINES
# =========================================================

signals = SignalEngine(
    buffer,
    mode=STRATEGY_MODE
)

entry_engine = EntryEngine(
    buffer,
    debug=True,
    config=mode_config,
    symbol=SYMBOL,
)

trade_manager = TradeManager(
    buffer,
    debug=True
)

position_manager = PositionManager(exchange)

if STRATEGY_MODE == "direction":
    execution_strategy = DirectionExecutionStrategy()
else:
    execution_strategy = DefaultExecutionStrategy()

execution = ExecutionEngine(
    exchange,
    position_manager,
    execution_strategy,
    symbol=SYMBOL
)

status_writer = StatusWriter()

status_data = status_writer._read_current()

last_signal_direction = status_data.get("signal_direction")

strategy_router = StrategyRouter(
    mode=STRATEGY_MODE
)

# =========================================================
# STATUS DEFAULT
# =========================================================

last_status_ts = 0

last_signal_side = "N/A"

last_signal_trend = None
last_signal_momentum = None
opening_position = False

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
    
    # 🔥 NUEVO
    "strategy_mode": STRATEGY_MODE,
    "last_executed_strategy": None,
    "last_router_action": None,
    "last_router_reason": None,
})

# =========================================================
# RESTORE STATE
# =========================================================

for symbol in SYMBOLS:
    pos = exchange.get_position(symbol)

    if pos:
        execution.restore_state(symbol)

        if len(execution.positions) >= execution.max_global_positions:
            break
else:
    print("\033[94m[SYNC]\033[0m ℹ️ No position to restore.")

# =========================================================
# CONNECT WS
# =========================================================

ws = WSClient(
    buffer.on_ws_message,
    timeframes=TIMEFRAMES,
    symbols=SYMBOLS
)

ws.start()

ws_thread = threading.Thread(
    target=ws.run,
    daemon=True
)

ws_thread.start()

print(
    "\033[94m[LIVE ENGINE]\033[0m "
    "🚀 Live Engine started! Ctrl+C to stop.\n"
)

# =========================================================
# HEARTBEAT
# =========================================================

def write_heartbeat():

    try:
        balance = exchange.get_balance()
    except Exception:
        balance = 0.0

    try:
        open_positions = exchange.get_open_positions()
    except Exception:
        open_positions = []

    first_position = open_positions[0] if open_positions else None

    if first_position:
        position_side = first_position["side"]
        position_qty = first_position["quantity"]
        entry_price = first_position["entry_price"]
        unpnl = first_position["unrealized_pnl"]
        symbol_status = first_position["symbol"]

    else:
        position_side = "NONE"
        position_qty = 0.0
        entry_price = 0.0
        unpnl = 0.0
        symbol_status = SYMBOL

    current_status = status_writer._read_current()

    status_writer.write({
        "engine_online": True,
        "ws_online": ws.is_connected,
        "symbol": symbol_status,
        "balance": balance,

        # compat dashboard viejo
        "position_side": position_side,
        "position_qty": position_qty,
        "entry_price": entry_price,
        "unpnl": unpnl,

        # nuevo multi-position
        "open_positions": open_positions,

        "last_signal": last_signal_side,
        "signal_trend": last_signal_trend,
        "signal_direction": last_signal_direction,
        "signal_momentum": last_signal_momentum,

        "strategy_mode": current_status.get("strategy_mode"),
        "last_executed_strategy": current_status.get("last_executed_strategy"),
        "last_router_action": current_status.get("last_router_action"),
        "last_router_reason": current_status.get("last_router_reason"),
    })
    
# =========================================================
# EVENT LOOP
# =========================================================

try:

    while True:

        now = time.time()

        # =================================================
        # HEARTBEAT
        # =================================================

        if now - last_status_ts >= STATUS_INTERVAL:

            write_heartbeat()

            last_status_ts = now

        # =================================================
        # PRICE UPDATE
        # =================================================

        for pos_symbol in list(execution.positions.keys()):

            price = buffer.last_price(pos_symbol)
            timestamp = buffer.last_timestamp(pos_symbol)

            if price is not None and timestamp is not None:
                execution.on_price_update(
                    pos_symbol,
                    price,
                    timestamp
                )

        # =================================================
        # 15m CLOSED
        # =================================================

        symbol = buffer.consume_any_closed_tf(TRIGGER_TF)

        if symbol:

            print(
                f"\033[93m[LIVE MAIN]\033[0m "
                f"✅ {TRIGGER_TF} close event consumed"
            )

            close_price = buffer.last_price(symbol)
            closed_candle_ts = buffer.last_close_time[symbol][TRIGGER_TF]

            # =================================================
            # 1. SNAPSHOT + SIGNAL GENERATION (PRIMERO SIEMPRE)
            # =================================================

            signal = signals.generate_signal(symbol)

            if not signal:
                continue
            
            # ================================
            # DEBUG ROUTER INPUT
            # ================================

            current_status = status_writer._read_current()

            previous_direction = current_status.get("signal_direction")

            print("\n[ROUTER DEBUG]")
            print(f"prev_direction (from disk) : {previous_direction}")
            print(f"signal direction           : {signal.direction.value}")
            print(f"signal trend              : {signal.trend.value}")
            print(f"signal momentum           : {signal.momentum.value}")
            print("===========================\n")

            trade_action = strategy_router.evaluate(
                signal,
                previous_direction=previous_direction,    
                current_position=execution.get_position(symbol)
            )

            update_status(
                status_writer,
                last_router_action=trade_action.action.value,
                last_router_reason=trade_action.reason
            )
            
            print(
                f"\033[93m[STRATEGY ROUTER]\033[0m "
                f"action={trade_action.action.value} "
                f"reason={trade_action.reason}"
            )

            print(
                f"\033[93m[LIVE MAIN]\033[0m "
                f"signal returned: {signal}"
            )

            # =================================================
            # 2. UPDATE CONTEXT (SI YA HAY POSICIÓN)
            # =================================================

            if execution.get_position(symbol):

                execution.update_position_context(
                    trend=signal.trend.value,
                    direction=signal.direction.value,
                    momentum=signal.momentum.value,
                    micro_momentum=signal.momentum.value if signal.momentum else None,
                    price=close_price,
                    ema20_1m=getattr(signal, "ema20_1m", None),
                    ema34_1m=getattr(signal, "ema34_1m", None),
                    ema50_1m=getattr(signal, "ema50_1m", None),
                )

            # =================================================
            # 3. STATUS UPDATE
            # =================================================

            last_signal_side = trade_action.action.value
            last_signal_trend = signal.trend.value
            last_signal_direction = signal.direction.value
            last_signal_momentum = signal.momentum.value

            # =================================================
            # 4. SIGNAL JOURNAL
            # =================================================

            signal_journal.log_signal(
                timestamp=signal.signal_ts,
                tf=TRIGGER_TF,
                side=trade_action.action.value,
                signal_price=signal.signal_price,
                direction=signal.direction.value,
                trend=signal.trend.value,
                momentum=signal.momentum.value,
            )

            # =================================================
            # 5. HOLD CHECK
            # =================================================

            if trade_action.action == Action.HOLD:
                continue
            
            # =================================================
            # GLOBAL POSITION LOCK
            # =================================================

            if not execution.can_open_position(symbol) or opening_position:
                print(
                    f"\033[93m[LIVE MAIN]\033[0m "
                    f"⛔ global lock active"
                )
                continue

            # =================================================
            # 6. ENTRY PLAN
            # =================================================

            plan = entry_engine.generate_entry(trade_action)

            if not plan:
                print("\033[94m[ENTRY PLANNER]\033[0m ❌ PLAN DESCARTADO\n")
                continue

            print(
                f"[DEBUG PLAN] "
                f"signal_price={signal.signal_price} "
                f"plan_symbol={plan.symbol} "
                f"entry={plan.entry}"
            )

            # =================================================
            # 7. EXECUTION STRATEGY (ULTIMO PASO)
            # =================================================

            try:
                opening_position = True

                executed = execution_strategy.on_signal(
                    execution_engine=execution,
                    trade_action=trade_action,
                    plan=plan
                )

                update_status(
                    status_writer,
                    last_executed_strategy=execution_strategy.__class__.__name__
                )

                print(f"[LIVE MAIN] execution result: {executed}")

            except Exception as e:
                print(f"[LIVE MAIN] ❌ execution error but loop continues: {e}")

            finally:
                opening_position = False

# =========================================================
# STOP
# =========================================================

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

    print(
        "\033[94m[LIVE ENGINE]\033[0m "
        "🛑 Live Engine stopped."
    )