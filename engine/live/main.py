from dotenv import load_dotenv
import os
import time
import threading
import argparse

import pandas as pd
from signals.indicators.direction import trade_direction

from signals.utils.logger import BotLogger

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

from concurrent.futures import ThreadPoolExecutor, as_completed

from services.market_context.btc_velocity_context import BTCVelocityContextService

from config.timeframes import (
    MODE_CONFIG,
    TIMEFRAME_CONFIGS
)

from config.strategies.v1 import SYMBOLS

load_dotenv()

def chunk_list(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]

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

DEBUG_LOGS = False
logger = BotLogger(debug=DEBUG_LOGS)

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
    "1d": 180,
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
    print(symbol)
    for tf in TIMEFRAMES:

        #print(f"[HISTORY] loading symbol={symbol} tf={tf}")

        days = DAYS_BY_TF.get(tf, 3)

        df_hist = fetch_history(
            symbol,
            tf,
            days
        )

        #print(f"[HISTORY] {symbol} {tf} fetched rows={len(df_hist)}")

        buffer.load_historical(
            symbol,
            tf,
            df_hist
        )

        #print(f"[HISTORY] {symbol} {tf} loaded into buffer\n")
        
# =========================================================
# INIT PREVIOUS DIRECTION FROM HISTORY
# =========================================================

last_direction_by_symbol = {}

for symbol in SYMBOLS:
    candles_15m = buffer.get_candles(symbol, "15m")

    if not candles_15m:
        continue

    df_15m = pd.DataFrame(candles_15m)

    direction = trade_direction(df_15m)

    last_direction_by_symbol[symbol] = direction

    print(
        f"\033[96m[DIRECTION INIT]\033[0m "
        f"{symbol} previous_direction={direction}"
    )

# =========================================================
# EXCHANGE
# =========================================================

exchange = BinanceExchange(
    api_key=api_key,
    api_secret=secret,
    testnet=True
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
    config=mode_config,
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
    mode=STRATEGY_MODE,
    entry_rules=mode_config.get(
        "entry_rules",
        "standard"
    )
)

btc_context_service = BTCVelocityContextService()

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

restored_count = 0

for symbol in SYMBOLS:
    try:
        pos = exchange.get_position(symbol)

    except Exception as e:
        print(
            f"\033[94m[SYNC]\033[0m "
            f"⚠️ Error checking position | symbol={symbol} | error={e}"
        )
        continue

    if not pos:
        continue

    try:
        execution.restore_state(symbol)
        restored_count += 1

    except Exception as e:
        print(
            f"\033[94m[SYNC]\033[0m "
            f"⚠️ Restore failed | symbol={symbol} | error={e}"
        )
        continue

    if len(execution.positions) >= execution.max_global_positions:
        break

if restored_count == 0:
    print("\033[94m[SYNC]\033[0m ℹ️ No position to restore.")
else:
    print(
        f"\033[94m[SYNC]\033[0m "
        f"✅ Restored {restored_count} positions: {list(execution.positions.keys())}"
    )

# =========================================================
# CONNECT WS
# =========================================================

ws = WSClient(
    buffer.on_ws_message,
    timeframes=TIMEFRAMES,
    symbols=SYMBOLS,
    chunk_size=15,
    stale_after=90
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
    
    max_15m_queue = 0

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
        # 1m CONTEXT UPDATE
        # =================================================

        context_symbol = buffer.consume_any_closed_tf("1m")

        if context_symbol and execution.get_position(context_symbol):

            context_signal = signals.generate_direction_context(
                context_symbol
            )

            if context_signal:

                context_price = buffer.last_price(context_symbol)

                micro = getattr(
                    context_signal,
                    "micro",
                    None
                )

                execution.update_position_context(
                    symbol=context_symbol,
                    trend=context_signal.trend.value,
                    direction=context_signal.direction.value,
                    momentum=context_signal.momentum.value,
                    micro_momentum=(
                        micro.value
                        if micro
                        else context_signal.momentum.value
                    ),
                    current_price=context_price,
                    ema20_1m=getattr(context_signal, "ema20_1m", None),
                    ema34_1m=getattr(context_signal, "ema34_1m", None),
                    ema50_1m=getattr(context_signal, "ema50_1m", None),
                )

        # =================================================
        # 15m CLOSED
        # =================================================
        
        closed_events_snapshot = buffer.closed_events_snapshot()

        pending_15m = sum(
            1
            for _, tf in closed_events_snapshot
            if tf == TRIGGER_TF
        )

        if pending_15m > 0:
            max_15m_queue = max(max_15m_queue, pending_15m)

            print(
                f"\033[93m[15M QUEUE]\033[0m "
                f"pending={pending_15m} max={max_15m_queue}"
            )

        symbols_to_process = []

        while True:
            symbol = buffer.consume_any_closed_tf(TRIGGER_TF)

            if not symbol:
                break

            symbols_to_process.append(symbol)

        if symbols_to_process:

            print(
                f"\033[93m[15M BATCH]\033[0m "
                f"symbols={len(symbols_to_process)}"
            )

            batch_started = time.perf_counter()
            max_delay_seen = 0

            for symbol in symbols_to_process:

                logger.debug(
                    f"\033[93m[LIVE MAIN]\033[0m "
                    f"✅ {TRIGGER_TF} close event consumed"
                )

                # =================================================
                # 1. SNAPSHOT + SIGNAL GENERATION (PRIMERO SIEMPRE)
                # =================================================
                close_price = buffer.last_price(symbol)
                closed_candle_ts = buffer.last_ws_close_time[symbol][TRIGGER_TF]

                if not closed_candle_ts:
                    continue

                closed_ts = closed_candle_ts

                if closed_ts > 10_000_000_000:
                    closed_ts = closed_ts / 1000

                event_delay_sec = time.time() - closed_ts

                logger.debug(
                    f"\033[91m[EVENT DELAY]\033[0m "
                    f"{symbol} tf={TRIGGER_TF} "
                    f"delay={event_delay_sec:.2f}s"
                )
                
                max_delay_seen = max(
                    max_delay_seen,
                    event_delay_sec
                )

                signal_started = time.perf_counter()

                signal = signals.generate_signal(symbol)

                elapsed_ms = (time.perf_counter() - signal_started) * 1000

                logger.debug(
                    f"\033[96m[ANALYZE TIME]\033[0m "
                    f"{symbol} {elapsed_ms:.2f}ms"
                )

                if not signal:
                    continue
                

                signal_delay_sec = (
                    int(time.time() * 1000) - int(signal.signal_ts)
                ) / 1000

                logger.debug(
                    f"\033[91m[SIGNAL DELAY]\033[0m "
                    f"{symbol} delay={signal_delay_sec:.2f}s"
                )
                
                btc_context = btc_context_service.evaluate(buffer)

                logger.debug(
                    f"[BTC CONTEXT] "
                    f"state={btc_context.state} | "
                    f"reason={btc_context.reason} | "
                    f"v15={btc_context.velocity_15m} | "
                    f"v1h={btc_context.velocity_1h} | "
                    f"d15={btc_context.direction_15m} | "
                    f"d1h={btc_context.direction_1h}"
                )
                
                # ================================
                # DEBUG ROUTER INPUT
                # ================================

                previous_direction = last_direction_by_symbol.get(symbol)

                logger.debug("\n[ROUTER DEBUG]")
                logger.debug(f"prev_direction (from memory) : {previous_direction}")
                logger.debug(f"signal direction           : {signal.direction.value}")
                logger.debug(f"signal trend              : {signal.trend.value}")
                logger.debug(f"signal momentum           : {signal.momentum.value}")

                logger.debug(
                    f"[DIRECTION TRANSITION] "
                    f"{symbol} "
                    f"{previous_direction} -> {signal.direction}"
                )

                logger.debug("===========================\n")

                trade_action = strategy_router.evaluate(
                    signal,
                    previous_direction=previous_direction,    
                    current_position=execution.get_position(symbol)
                )
                
                last_direction_by_symbol[symbol] = signal.direction

                update_status(
                    status_writer,
                    last_router_action=trade_action.action.value,
                    last_router_reason=trade_action.reason
                )
                
                if trade_action.action != Action.HOLD:
                    print(
                        f"\033[93m[STRATEGY ROUTER]\033[0m "
                        f"symbol={symbol} "
                        f"action={trade_action.action.value} "
                        f"reason={trade_action.reason}"
                    )

                logger.debug(
                    f"\033[93m[LIVE MAIN]\033[0m "
                    f"signal returned: {signal}"
                )

                # =================================================
                # 2. UPDATE CONTEXT (SI YA HAY POSICIÓN)
                # =================================================

                if execution.get_position(symbol):

                    execution.update_position_context(
                        symbol=symbol,
                        trend=signal.trend.value,
                        direction=signal.direction.value,
                        momentum=signal.momentum.value,
                        micro_momentum=signal.momentum.value if signal.momentum else None,
                        current_price=close_price,                    
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
                
                plan.signal_context = {
                    **(plan.signal_context or {}),

                    "btc_velocity_15m": btc_context.velocity_15m,
                    "btc_velocity_1h": btc_context.velocity_1h,
                    "btc_direction_15m": btc_context.direction_15m,
                    "btc_direction_1h": btc_context.direction_1h,
                    "btc_context_state": btc_context.state,
                    "btc_context_reason": btc_context.reason,
                }

                logger.debug(
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

                    exec_started = time.perf_counter()

                    executed = execution_strategy.on_signal(
                        execution_engine=execution,
                        trade_action=trade_action,
                        plan=plan
                    )

                    exec_elapsed = time.perf_counter() - exec_started

                    print(
                        f"\033[91m[EXECUTION TIME]\033[0m "
                        f"symbol={symbol} elapsed={exec_elapsed:.2f}s"
                    )

                    update_status(
                        status_writer,
                        last_executed_strategy=execution_strategy.__class__.__name__
                    )

                    logger.debug(f"[LIVE MAIN] execution result: {executed}")

                except Exception as e:
                    print(f"[LIVE MAIN] ❌ execution error but loop continues: {e}")

                finally:
                    opening_position = False
                    
            batch_elapsed = time.perf_counter() - batch_started

            print(
                f"\033[95m[15M BATCH SUMMARY]\033[0m "
                f"symbols={len(symbols_to_process)} "
                f"elapsed={batch_elapsed:.2f}s "
                f"max_delay={max_delay_seen:.2f}s"
            )

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