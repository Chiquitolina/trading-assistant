from dotenv import load_dotenv
import os
import time
import threading
import argparse

from engine.live.journal.compression_watch_journal import CompressionWatchJournal

import json
from pathlib import Path

import pandas as pd
from signals.indicators.direction import trade_direction

from signals.utils.logger import BotLogger

from services.market_context.btc_correlation_analyzer import (
    BTCCorrelationAnalyzer,
)

from signals.snapshots.signal_compression_snapshot_builder import SignalCompressionSnapshotBuilder
from engine.live.snapshots.compression_snapshot_manager import CompressionSnapshotManager

from engine.live.strategy.modes.compression_strategy import CompressionStrategy
from engine.live.execution.strategies.compression_execution_strategy import CompressionExecutionStrategy

from engine.live.position.position_manager import PositionManager
from engine.live.ws.ws_client import WSClient
from engine.live.data.data_buffer import DataBuffer
from engine.live.journal.signal_journal import SignalJournal

from signals.signals_engine import SignalEngine

from engine.live.selection.candidate_scorer import (
    NoOpCandidateScorer,
)

from engine.live.selection.plan_selection_manager import (
    PlanSelectionManager,
)

from engine.live.strategy.entry_engine import EntryEngine
from engine.live.strategy.router import StrategyRouter

from engine.live.execution.execution_engine import ExecutionEngine
from engine.live.execution.pending_entries.bucket_touch_entry_manager import (
    BucketTouchEntryManager,
)

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
        "aggressive",
        "compression"
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
    "30m": 5,
    "1h": 7,
    "4h": 25,
    "1d": 180,
}

STATUS_INTERVAL = 3
MAX_COMPRESSION_ENTRIES_PER_BATCH = 2
MAX_SELECTED_PLANS_PER_WINDOW = len(SYMBOLS)

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
    candles_trigger = buffer.get_candles(symbol, TRIGGER_TF)

    if not candles_trigger:
        continue

    df_trigger = pd.DataFrame(candles_trigger)

    direction = trade_direction(df_trigger)

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
    mode="direction" if STRATEGY_MODE == "compression" else STRATEGY_MODE
)

entry_engine = EntryEngine(
    buffer,
    debug=True,
    config=mode_config,
    symbol=SYMBOL,
)

candidate_scorer = NoOpCandidateScorer()

plan_selection_manager = PlanSelectionManager(
    scorer=candidate_scorer,
    max_selected_per_window=MAX_SELECTED_PLANS_PER_WINDOW,
    minimum_score=None,
)

trade_manager = TradeManager(
    buffer,
    debug=True
)

btc_correlation_analyzer = BTCCorrelationAnalyzer(
    buffer=buffer,
    btc_symbol="BTCUSDT",
)
position_manager = PositionManager(exchange)

if STRATEGY_MODE == "compression":
    execution_strategy = CompressionExecutionStrategy()
elif STRATEGY_MODE == "direction":
    execution_strategy = DirectionExecutionStrategy()
else:
    execution_strategy = DefaultExecutionStrategy()
    
execution = ExecutionEngine(
    exchange,
    position_manager,
    execution_strategy,
    symbol=SYMBOL
)

bucket_touch_manager = BucketTouchEntryManager(
    breakout_min_pct=0.50,
    breakout_max_pct=0.75,
    entry_band_min_pct=-0.25,
    entry_band_max_pct=0.00,
    expiry_minutes=150,
)

status_writer = StatusWriter()

status_data = status_writer._read_current()

last_signal_direction = status_data.get("signal_direction")

strategy_router = StrategyRouter(
    mode="direction" if STRATEGY_MODE == "compression" else STRATEGY_MODE,
    entry_rules=mode_config.get(
        "entry_rules",
        "standard"
    )
)

btc_context_service = BTCVelocityContextService()

compression_snapshot_builder = SignalCompressionSnapshotBuilder(
    buffer,
    trigger_tf=TRIGGER_TF,
)
compression_snapshot_manager = CompressionSnapshotManager()

compression_watch_journal = CompressionWatchJournal()

compression_strategy = CompressionStrategy(
    buffer=buffer,
    journal=compression_watch_journal,
    max_watch_candles=8,
    max_pullback_candles=5,
    pullback_max_pct=1.2,
    pullback_min_hold_high=True,
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
    "trigger_tf": TRIGGER_TF,
    
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
        before = len(execution.positions)

        execution.restore_state(symbol)

        after = len(execution.positions)

        if after > before:
            restored_count += 1

    except Exception as e:
        print(
            f"\033[94m[SYNC]\033[0m "
            f"⚠️ Restore failed | symbol={symbol} | error={e}"
        )
        continue

if restored_count == 0:
    print("\033[94m[SYNC]\033[0m ℹ️ No position restored.")
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

        "strategy_mode": STRATEGY_MODE,
        "trigger_tf": TRIGGER_TF,
        
        "last_executed_strategy": current_status.get("last_executed_strategy"),
        "last_router_action": current_status.get("last_router_action"),
        "last_router_reason": current_status.get("last_router_reason"),
    })
    
# =========================================================
# EVENT LOOP
# =========================================================

try:
    
    max_trigger_queue = 0

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
        
        # =================================================
        # BUCKET V2 PENDING ENTRY UPDATE
        # =================================================

        if STRATEGY_MODE == "compression":

            for pending_symbol in bucket_touch_manager.symbols():

                pending_price = buffer.last_price(
                    pending_symbol
                )

                pending_timestamp = buffer.last_timestamp(
                    pending_symbol
                )

                if (
                    pending_price is None
                    or pending_timestamp is None
                ):
                    continue

                triggered_entry = (
                    bucket_touch_manager.evaluate_price(
                        symbol=pending_symbol,
                        price=pending_price,
                        timestamp=pending_timestamp,
                    )
                )

                if triggered_entry is None:
                    continue

                if not execution.can_open_position(
                    pending_symbol
                ):
                    bucket_touch_manager.mark_failed(
                        pending_symbol,
                        reason="cannot_open_position_at_touch",
                    )
                    continue

                try:
                    opening_position = True

                    executed = execution_strategy.on_signal(
                        execution_engine=execution,
                        trade_action=(
                            triggered_entry.trade_action
                        ),
                        plan=triggered_entry.plan,
                    )

                    if executed:
                        bucket_touch_manager.mark_executed(
                            pending_symbol
                        )

                        update_status(
                            status_writer,
                            last_executed_strategy=(
                                "BucketTouchEntryV2"
                            ),
                        )

                    else:
                        bucket_touch_manager.mark_failed(
                            pending_symbol,
                            reason=(
                                "execution_strategy_rejected"
                            ),
                        )

                except Exception as exc:
                    bucket_touch_manager.mark_failed(
                        pending_symbol,
                        reason=f"execution_error:{exc}",
                    )

                    print(
                        f"[BUCKET V2 EXECUTION ERROR] "
                        f"symbol={pending_symbol} "
                        f"error={exc}"
                    )

                finally:
                    opening_position = False

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
        # TRIGGER TF CLOSED
        # =================================================
        
        closed_events_snapshot = buffer.closed_events_snapshot()

        pending_trigger = sum(
            1
            for _, tf in closed_events_snapshot
            if tf == TRIGGER_TF
        )

        if pending_trigger > 0:
            max_trigger_queue = max(max_trigger_queue, pending_trigger)

            print(
                f"\033[93m[{TRIGGER_TF} QUEUE]\033[0m "
                f"pending={pending_trigger} max={max_trigger_queue}"
            )

        symbols_to_process = []

        while True:
            symbol = buffer.consume_any_closed_tf(TRIGGER_TF)

            if not symbol:
                break

            symbols_to_process.append(symbol)

        if symbols_to_process:

            print(
                f"\033[93m[{TRIGGER_TF} BATCH]\033[0m "
                f"symbols={len(symbols_to_process)}"
            )

            batch_started = time.perf_counter()
            max_delay_seen = 0
            compression_entries_opened_in_batch = 0
            
            if STRATEGY_MODE == "compression":
                compression_strategy.reset_stats()

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
                
                # =================================================
                # COMPRESSION SNAPSHOT LIVE
                # =================================================

                compression_snapshot = compression_snapshot_builder.build(symbol)

                if compression_snapshot:
                    compression_snapshot_manager.save(compression_snapshot)

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
                    f"move15={btc_context.signed_move_15m_pct} | "
                    f"move1h={btc_context.signed_move_1h_pct} | "
                    f"d15={btc_context.direction_15m} | "
                    f"d1h={btc_context.direction_1h} | "
                    f"alignment={btc_context.direction_alignment}"
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
                
                compression_signal_context = {}
                
                if STRATEGY_MODE == "compression":

                    compression_result = compression_strategy.evaluate(
                        symbol=symbol,
                        signal=signal,
                        tf=TRIGGER_TF,
                        btc_context=btc_context,
                        current_position=execution.get_position(symbol),
                    )

                    trade_action = compression_result.trade_action
                    compression_signal_context = compression_result.signal_context

                else:
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

                #if not execution.can_open_position(symbol) or opening_position:
                #    print(
                #        f"\033[93m[LIVE MAIN]\033[0m "
                #        f"⛔ global lock active"
                #    )
                #    continue

                # =================================================
                # 6. ENTRY PLAN
                # =================================================

                plan = entry_engine.generate_entry(trade_action)

                if not plan:
                    print(
                        "\033[94m[ENTRY PLANNER]\033[0m "
                        "❌ PLAN DESCARTADO\n"
                    )
                    continue

                if STRATEGY_MODE == "compression":
                    plan.signal_context = {
                        **(plan.signal_context or {}),
                        **compression_signal_context,
                    }
                    
                try:
                    btc_correlation_context = (
                        btc_correlation_analyzer.analyze(
                            symbol=plan.symbol
                        )
                    )

                except Exception as exc:
                    print(
                        f"[BTC CORRELATION] "
                        f"symbol={plan.symbol} "
                        f"error={exc}"
                    )

                    btc_correlation_context = {
                        "btc_correlation_error": str(exc),
                    }

                btc_velocity_context = {
                    "btc_velocity_15m": (
                        btc_context.velocity_15m
                    ),
                    "btc_velocity_1h": (
                        btc_context.velocity_1h
                    ),
                    "btc_signed_move_15m_pct": (
                        btc_context.signed_move_15m_pct
                    ),
                    "btc_signed_move_1h_pct": (
                        btc_context.signed_move_1h_pct
                    ),
                    "btc_direction_15m": (
                        btc_context.direction_15m
                    ),
                    "btc_direction_1h": (
                        btc_context.direction_1h
                    ),
                    "btc_direction_alignment": (
                        btc_context.direction_alignment
                    ),
                    "btc_context_state": (
                        btc_context.state
                    ),
                    "btc_context_reason": (
                        btc_context.reason
                    ),
                }

                plan.signal_context = {
                    **(plan.signal_context or {}),
                    **btc_velocity_context,
                    **btc_correlation_context,
                }
                
                btc_trade_relationship = (
                    btc_context_service
                    .evaluate_trade_relationship(
                        context=btc_context,
                        side=plan.side,
                        correlation=(
                            btc_correlation_context.get(
                                "btc_corr_5m_1h"
                            )
                        ),
                        beta=(
                            btc_correlation_context.get(
                                "btc_beta_5m_1h"
                            )
                        ),
                    )
                )
                
                plan.signal_context = {
                    **(plan.signal_context or {}),
                    **btc_trade_relationship,
                }
                
                logger.debug(
                    f"[BTC TRADE RELATIONSHIP] "
                    f"symbol={plan.symbol} | "
                    f"side={plan.side} | "
                    f"btc_state={btc_context.state} | "
                    f"btc_direction_alignment="
                    f"{btc_context.direction_alignment} | "
                    f"corr_1h="
                    f"{btc_correlation_context.get('btc_corr_5m_1h')} | "
                    f"beta_1h="
                    f"{btc_correlation_context.get('btc_beta_5m_1h')} | "
                    f"trade_alignment="
                    f"{btc_trade_relationship.get('btc_trade_alignment')} | "
                    f"risk_state="
                    f"{btc_trade_relationship.get('btc_trade_risk_state')} | "
                    f"relationship="
                    f"{btc_trade_relationship.get('btc_relationship_label')}"
                )

                logger.debug(
                    f"[DEBUG PLAN] "
                    f"signal_price={signal.signal_price} "
                    f"plan_symbol={plan.symbol} "
                    f"entry={plan.entry}"
                )

                # =================================================
                # 7. SEND PLAN TO SELECTION LAYER
                # =================================================

                window_id = int(closed_candle_ts)

                if window_id < 10_000_000_000:
                    window_id *= 1000

                plan_selection_manager.add_plan(
                    plan=plan,
                    trade_action=trade_action,
                    window_id=window_id,
                )
                
            # =========================================================
            # RESOLVE SELECTION WINDOWS
            # =========================================================

            pending_window_ids = (
                plan_selection_manager.pending_window_ids()
            )

            for window_id in sorted(pending_window_ids):

                selected_candidates, rejected_candidates = (
                    plan_selection_manager.resolve_window(window_id)
                )

                for candidate in selected_candidates:

                    plan = candidate.plan
                    trade_action = candidate.trade_action
                    symbol = candidate.symbol

                    # =================================================
                    # FINAL EXECUTION LOCK
                    # =================================================

                    if opening_position:
                        candidate.reject("opening_position_lock")

                        print(
                            f"[SELECTION BLOCKED] "
                            f"symbol={symbol} "
                            f"reason=opening_position_lock"
                        )
                        continue

                    if not execution.can_open_position(symbol):
                        candidate.reject("cannot_open_position")

                        print(
                            f"[SELECTION BLOCKED] "
                            f"symbol={symbol} "
                            f"reason=cannot_open_position"
                        )
                        continue

                    # =================================================
                    # BUCKET V2: ARM PENDING ENTRY
                    # =================================================

                    armed_timestamp = buffer.last_timestamp(
                        symbol
                    )

                    if armed_timestamp is None:
                        armed_timestamp = int(
                            time.time() * 1000
                        )

                    armed = bucket_touch_manager.arm(
                        plan=plan,
                        trade_action=trade_action,
                        timestamp=armed_timestamp,
                    )

                    if not armed:
                        candidate.reject(
                            "bucket_v2_not_armed"
                        )

                        print(
                            f"[BUCKET V2 SELECTION REJECTED] "
                            f"symbol={symbol} "
                            f"rank={candidate.rank} "
                            f"score={candidate.score:.4f}"
                        )

                        continue

                    print(
                        f"[BUCKET V2 PLAN ARMED] "
                        f"symbol={symbol} "
                        f"rank={candidate.rank} "
                        f"score={candidate.score:.4f}"
                    )
                                        
            batch_elapsed = time.perf_counter() - batch_started

            if STRATEGY_MODE == "compression":

                alive_watches = compression_strategy.alive_watches_count()

                print(
                    f"[COMPRESSION SUMMARY] "
                    f"alive_watches={alive_watches} "
                    f"{compression_strategy.get_stats()}"
                )
                
                pipeline_path = Path("compression_pipeline.json")
                tmp_path = pipeline_path.with_suffix(".json.tmp")

                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(
                        compression_strategy.active_watches(),
                        f,
                        indent=4,
                        default=str
                    )

                tmp_path.replace(pipeline_path)
                
                print(f"[PIPELINE] Saved -> {pipeline_path.resolve()}")

            print(
                f"\033[95m[{TRIGGER_TF} BATCH SUMMARY]\033[0m "
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
        "strategy_mode": STRATEGY_MODE,
        "trigger_tf": TRIGGER_TF,
    })

    print(
        "\033[94m[LIVE ENGINE]\033[0m "
        "🛑 Live Engine stopped."
    )