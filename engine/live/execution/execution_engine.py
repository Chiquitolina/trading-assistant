from dataclasses import dataclass
from typing import Optional
from engine.live.journal.trade_journal import TradeJournal
from datetime import datetime
import random
import time
from engine.live.state_sync import ExchangeStateSync
from models.position import Position
from models.trade import Trade
from services.position_sizer import PositionSizer
from services.risk_manager import RiskManager
from binance.exceptions import BinanceAPIException
from engine.live.execution.order_executor import OrderExecutor
from engine.live.state.snapshot_manager import SnapshotManager
from config.strategies.v1 import SYMBOLS, MAX_GLOBAL_POSITIONS

class ExecutionEngine:

    def __init__(self, exchange, position_manager, strategy, symbol):
        self.exchange = exchange
        self.position_manager = position_manager
        self.symbol = symbol
        self.position: Optional[Position] = None
        self.trades: list[Trade] = []
        self.journal = TradeJournal()
        self.fees = self.exchange.get_futures_fees()
        self.position_sizer = PositionSizer()
        self.risk_manager = RiskManager()
        self.snapshot_manager = SnapshotManager()
        self.order_executor = OrderExecutor(exchange)
        self.strategy = strategy
        self.positions: dict[str, Position] = {}
        self.max_global_positions = MAX_GLOBAL_POSITIONS
        self.opening_position = False
        self.last_global_entry_ts = 0
        self.last_symbol_entry_ts = {}

        self.global_entry_cooldown = 180   # 3 min
        self.symbol_entry_cooldown = 900   # 15 min

    def _apply_slippage(self, price: float, side: str, is_entry: bool = True):
        slippage_pct = random.uniform(0.01, 0.05) / 100
        slippage = price * slippage_pct

        if side == "LONG":
            return price + slippage if is_entry else price - slippage

        if side == "SHORT":
            return price - slippage if is_entry else price + slippage
        
    def _get_last_close_fill(self, symbol, side, quantity, entry_ts, limit=50):
        fills = self.exchange.get_recent_fills(symbol, limit=limit)

        if not fills:
            return None

        exit_side = "SELL" if side == "LONG" else "BUY"

        candidates = [
            f for f in fills
            if f["side"] == exit_side
            and int(f["time"]) >= int(entry_ts)
        ]

        if not candidates:
            return None

        grouped = {}
        for f in candidates:
            oid = f["orderId"]
            grouped.setdefault(oid, []).append(f)

        valid_orders = []

        for oid, group in grouped.items():
            total_qty = sum(float(f["qty"]) for f in group)

            if abs(total_qty - float(quantity)) <= 0.000001:
                valid_orders.append((oid, group))

        if not valid_orders:
            return None

        latest_order_id, selected = max(
            valid_orders,
            key=lambda item: max(int(x["time"]) for x in item[1])
        )

        total_qty = sum(float(f["qty"]) for f in selected)
        total_quote = sum(float(f["qty"]) * float(f["price"]) for f in selected)
        total_commission = sum(float(f["commission"]) for f in selected)
        total_realized = sum(float(f["realizedPnl"]) for f in selected)
        last_time = max(int(f["time"]) for f in selected)

        avg_price = total_quote / total_qty if total_qty > 0 else None

        return {
            "orderId": latest_order_id,
            "price": avg_price,
            "qty": total_qty,
            "commission": total_commission,
            "realizedPnl": total_realized,
            "time": last_time,
            "fills": selected,
        }
        
    def _calc_candles_in_trade(self, current_candle_ts: int, tf_minutes: int = 1) -> int:
        if not self.position or self.position.entry_candle_ts is None:
            return 0

        tf_ms = tf_minutes * 60 * 1000
        diff_ms = current_candle_ts - self.position.entry_candle_ts

        return int(diff_ms // tf_ms) + 1
            
    def _infer_close_reason(self, pos, exit_price):
        tolerance = abs(pos.real_entry) * 0.001

        pnl_pct = (
            (exit_price - pos.real_entry) / pos.real_entry * 100
            if pos.side == "LONG"
            else (pos.real_entry - exit_price) / pos.real_entry * 100
        )

        if pos.side == "LONG":
            if exit_price >= pos.tp - tolerance:
                return "TP"

            if exit_price <= pos.sl + tolerance:
                if pnl_pct > 0:
                    return "TRAILING_SL"
                elif pnl_pct >= -0.05:
                    return "BE_SL"
                return "SL"

        else:
            if exit_price <= pos.tp + tolerance:
                return "TP"

            if exit_price >= pos.sl - tolerance:
                if pnl_pct > 0:
                    return "TRAILING_SL"
                elif pnl_pct >= -0.05:
                    return "BE_SL"
                return "SL"

        return "MANUAL_PROFIT" if pnl_pct > 0 else "MANUAL_LOSS"


    def _handle_external_close(self, price, timestamp):
        pos = self.position
        if not pos:
            return

        print("⚠️ Detectado cierre externo (TP/SL o manual)")

        # ==========================
        # BUSCAR FILL REAL DE CIERRE
        # ==========================
        close_fill = self._get_last_close_fill(
            symbol=pos.symbol,
            side=pos.side,
            quantity=pos.quantity,
            entry_ts=int(pos.entry_ts) - 60_000,
            limit=100
        )

        pnl_usd = 0.0
        fees = round(self.fees["taker"] * 2, 4)

        if close_fill:
            real_exit = float(close_fill["price"])
            exit_order_id = int(close_fill["orderId"])
            pnl_usd = float(close_fill.get("realizedPnl", 0) or 0)

            print(
                f"✅ Close fill encontrado | "
                f"orderId={exit_order_id} | real_exit={real_exit}"
            )

        else:
            exit_order_id = None
            real_exit = price

            print("⚠️ No se encontró close fill, usando fallback local")

        # ==========================
        # INFERIR REASON CON REAL EXIT
        # ==========================
        reason = self._infer_close_reason(pos, real_exit)

        print(f"\033[94m[SYNC]\033[0m 📊 External close reason: {reason}")

        pos.exit_order_id = exit_order_id

        # ==========================
        # PNL CALCULADO POR EL BOT
        # ==========================
        pnl_pct = (
            (real_exit - pos.real_entry) / pos.real_entry * 100
            if pos.side == "LONG"
            else (pos.real_entry - real_exit) / pos.real_entry * 100
        )

        pnl_pct = round(pnl_pct, 4)
        pnl_net = round(pnl_pct - fees, 4)

        print(f"❌ CLOSED (external {reason}) | PnL: {pnl_net}%")

        # ==========================
        # CONTEXTO DE SIGNAL
        # ==========================
        ctx = pos.signal_context or {}

        def _to_iso(ts):
            if not ts:
                return None
            return datetime.utcfromtimestamp(ts / 1000).isoformat(timespec="milliseconds")

        signal_iso = _to_iso(pos.signal_ts)
        entry_iso = _to_iso(pos.entry_ts)

        exit_ts = int(time.time() * 1000)
        exit_iso = _to_iso(exit_ts)

        self.journal.log_trade(
            symbol=pos.symbol,
            signal_ts=signal_iso,
            signal_price=pos.signal_price,

            entry_ts=entry_iso,
            exit_ts=exit_iso,

            side=pos.side,

            entry=pos.entry_price,
            real_entry=pos.real_entry,

            exit_price=price,
            real_exit=real_exit,

            tp=pos.tp,
            sl=pos.sl,

            pnl=pnl_net,
            pnl_gross=pnl_pct,
            pnl_usd=pnl_usd,
            fees=fees,

            exit_reason=reason,

            # ==========================
            # SIGNAL CONTEXT
            # ==========================
            signal_trend=ctx.get("trend"),
            signal_direction=ctx.get("direction"),
            signal_momentum=ctx.get("momentum"),

            signal_momentum_prev1=ctx.get("momentum_prev1"),
            signal_momentum_prev2=ctx.get("momentum_prev2"),
            signal_momentum_sequence=ctx.get("momentum_sequence"),

            signal_atr=ctx.get("signal_atr"),

            # ==========================
            # LIVE POSITION CONTEXT
            # ==========================
            current_trend=pos.current_trend,
            current_direction=pos.current_direction,
            current_momentum=pos.current_momentum,

            direction_t1=pos.direction_t1,
            momentum_t1=pos.momentum_t1,
            pnl_t1=pos.pnl_t1,

            micro_t1=pos.micro_t1,
            direction_5m_t1=pos.direction_5m_t1,

            reclaimed_ema20_1m=pos.reclaimed_ema20_1m,
            reclaimed_ema34_1m=pos.reclaimed_ema34_1m,
            reclaimed_ema50_1m=pos.reclaimed_ema50_1m,

            lost_ema20_1m=pos.lost_ema20_1m,
            lost_ema34_1m=pos.lost_ema34_1m,
            lost_ema50_1m=pos.lost_ema50_1m,

            dist_ema20_1m_pct=pos.dist_ema20_1m_pct,
            dist_ema34_1m_pct=pos.dist_ema34_1m_pct,
            dist_ema50_1m_pct=pos.dist_ema50_1m_pct,

            dist_ema50_15m_pct=ctx.get("dist_ema50_15m_pct"),
            dist_ema99_15m_pct=ctx.get("dist_ema99_15m_pct"),

            dist_ema50_1h_pct=ctx.get("dist_ema50_1h_pct"),
            dist_ema99_1h_pct=ctx.get("dist_ema99_1h_pct"),

            dist_ema50_4h_pct=ctx.get("dist_ema50_4h_pct"),
            dist_ema99_4h_pct=ctx.get("dist_ema99_4h_pct"),

            max_favorable_pct=pos.max_favorable_pct,
            max_adverse_pct=pos.max_adverse_pct,

            direction_5m_changed=pos.direction_5m_changed,
            direction_5m_after_entry=pos.direction_5m_after_entry,

            mae=pos.mae,
            mfe=pos.mfe,

            strategy_mode=ctx.get("strategy_name"),
            router_reason=ctx.get("router_reason")
        )

        try:
            self.order_executor.cancel_all(pos.symbol)
            print(f"🧹 Pending orders cancelled for {pos.symbol}")
        except Exception as e:
            print(f"⚠️ Failed cancelling pending orders for {pos.symbol}: {e}")

        self.positions.pop(pos.symbol, None)

        if self.position and self.position.symbol == pos.symbol:
            self.position = None

        self.snapshot_manager.clear(pos.symbol)
        
    def _place_tp_sl(self, symbol, quantity, side, tp_side, tp_price, sl_side, sl_price, real_entry):
        try:
            raw_tp = tp_price
            raw_sl = sl_price

            mark_price = float(self.exchange.get_mark_price(symbol))
            tick_size = float(self.exchange.get_price_tick_size(symbol))

            min_distance = tick_size * 10

            tp_float = float(tp_price)
            sl_float = float(sl_price)

            if side == "LONG":
                tp_float = max(tp_float, mark_price + min_distance)
                sl_float = min(sl_float, mark_price - min_distance)

            elif side == "SHORT":
                tp_float = min(tp_float, mark_price - min_distance)
                sl_float = max(sl_float, mark_price + min_distance)

            tp_price = self.exchange.normalize_price(symbol, tp_float)
            sl_price = self.exchange.normalize_price(symbol, sl_float)

            print(
                f"📌 TP/SL debug | "
                f"symbol={symbol} | qty={quantity} | side={side} | "
                f"mark={mark_price} | tick={tick_size} | "
                f"raw_tp={raw_tp} -> tp={tp_price} | "
                f"raw_sl={raw_sl} -> sl={sl_price}"
            )

            self.exchange.place_take_profit_limit(
                symbol=symbol,
                side=tp_side,
                quantity=quantity,
                price=tp_price
            )

            self.exchange.place_stop_loss(
                symbol=symbol,
                side=sl_side,
                quantity=quantity,
                stop_price=sl_price
            )

            print("✅ TP/SL colocados correctamente")
            return True

        except Exception as e:
            print(f"❌ Error colocando TP/SL: {e}")
            return False
        
    def exchange_open_positions_count(self, symbols):
        count = 0

        for symbol in symbols:
            pos = self.position_manager.sync(symbol)

            if pos:
                count += 1

        return count
        
    def get_position(self, symbol: str):
        return self.positions.get(symbol)


    def open_positions_count(self) -> int:
        return len(self.positions)


    def can_open_position(self, symbol: str) -> bool:
        if self.opening_position:
            return False

        if symbol in self.positions:
            return False

        if self.open_positions_count() >= self.max_global_positions:
            return False

        now = time.time()

        # ==========================
        # GLOBAL COOLDOWN
        # ==========================
        if now - self.last_global_entry_ts < self.global_entry_cooldown:
            remaining = self.global_entry_cooldown - (now - self.last_global_entry_ts)
            print(f"⏳ Global cooldown active | remaining={remaining:.0f}s")
            return False

        # ==========================
        # SYMBOL COOLDOWN
        # ==========================
        last_symbol_ts = self.last_symbol_entry_ts.get(symbol, 0)

        if now - last_symbol_ts < self.symbol_entry_cooldown:
            remaining = self.symbol_entry_cooldown - (now - last_symbol_ts)
            print(f"⏳ Symbol cooldown active | symbol={symbol} | remaining={remaining:.0f}s")
            return False

        return True

    def open_position(self, plan, leverage: int = 1):
        
        if self.opening_position:
            print("⛔ Opening already in progress. Plan ignored.")
            return False

        self.opening_position = True
        
        try:
            
            if self.exchange_open_positions_count(SYMBOLS) >= self.max_global_positions:
                print(
                    "\033[94m[EXECUTION ENGINE]\033[0m "
                    "⚠️ Max global exchange positions reached. Plan ignored.\n"
                )
                return False

            exchange_pos = self.position_manager.sync(plan.symbol)

            if exchange_pos:
                print("\033[94m[EXECUTION ENGINE]\033[0m ⚠️ Position already open (exchange). Plan ignored.\n")
                return False

            try:
                self.order_executor.cancel_all(plan.symbol)
            except Exception as e:
                print(f"⚠️ Failed to clean orders: {e}")

            if plan.side not in ("LONG", "SHORT"):
                return False

            side = "BUY" if plan.side == "LONG" else "SELL"

            balance = float(self.exchange.get_balance())

            if balance <= 0:
                print("❌ No balance")
                return False

            if self.order_executor.set_leverage(plan.symbol, leverage) is None:
                print("❌ Error setting leverage")
                return False

            print(f"✅ Leverage set to {leverage}x")

            price = float(self.exchange.get_price(plan.symbol))

            size_data = self.position_sizer.calculate(
                balance=balance,
                price=price,
                leverage=leverage
            )

            print(f"""
    [POSITION SIZER]
    Balance         : {balance}
    Price           : {price}
    Leverage        : {leverage}
    Quantity        : {size_data['quantity']}
    Notional        : {size_data['notional']:.2f}
    Required margin : {size_data['required_margin']:.2f}
    Usable balance  : {size_data['usable_balance']:.2f}
    """)

            is_valid, msg = self.position_sizer.validate(size_data)

            if not is_valid:
                print(msg)
                return False

            raw_quantity = size_data["quantity"]

            quantity = self.exchange.normalize_quantity(
                plan.symbol,
                raw_quantity
            )

            print(
                f"📏 Quantity normalized | "
                f"symbol={plan.symbol} | "
                f"raw={raw_quantity} -> qty={quantity}"
            )

            # colchón extra para evitar órdenes demasiado al límite
            required_margin = float(size_data["required_margin"])
            usable_balance = float(size_data["usable_balance"])

            if required_margin > usable_balance * 0.95:
                print(
                    f"⚠️ Margin too tight. "
                    f"required={required_margin:.2f} usable={usable_balance:.2f}. Trade skipped."
                )
                return False

            # ==========================
            # 🚀 ORDER
            # ==========================
            try:
                order = self.order_executor.market_order(
                    symbol=plan.symbol,
                    side=side,
                    quantity=quantity
                )
            except BinanceAPIException as e:
                if getattr(e, "code", None) == -2019 or "Margin is insufficient" in str(e):
                    print("❌ Margin insufficient. Trade skipped. Engine continues running.")
                    return False

                if getattr(e, "code", None) == -1007:
                    print("⚠️ Binance timeout / UNKNOWN status while placing order. Syncing position...")
                    time.sleep(2)

                    exchange_pos = self.position_manager.sync(plan.symbol)
                    if exchange_pos:
                        print("✅ Position detected after timeout sync")
                        order = {"status": "FILLED_AFTER_SYNC"}
                    else:
                        print("❌ No position found after timeout sync")
                        return False
                else:
                    print(f"❌ Binance API error placing market order: {e}")
                    return False

            except Exception as e:
                print(f"❌ Unexpected error placing market order: {e}")
                return False

            # 💣 CASO CRÍTICO: timeout Binance
            if order and order.get("status") == "UNKNOWN":

                print("🔎 Orden en estado desconocido, sincronizando...")

                time.sleep(2)

            elif not order:
                print("❌ Order failed")
                return False

            # 🔥 SIEMPRE sync después del order
            exchange_pos = self.position_manager.sync(plan.symbol)

            if not exchange_pos:
                print("❌ No hay posición después del order, abortando")
                return False

            time.sleep(1.0)

            pos = self.exchange.get_position(plan.symbol)
            real_entry = float(pos["entry_price"]) if pos else plan.entry

            # ==========================
            # 🎯 TP / SL
            # ==========================
            mark_price = float(self.exchange.get_mark_price(plan.symbol))

            tp_price, sl_price = self.risk_manager.calculate_tp_sl(
                plan=plan,
                real_entry=real_entry,
                mark_price=mark_price
            )

            tp_side = "SELL" if side == "BUY" else "BUY"

            tp_sl_ok = self._place_tp_sl(
                symbol=plan.symbol,
                quantity=quantity,
                side=plan.side,
                tp_side=tp_side,
                tp_price=tp_price,
                sl_side=tp_side,
                sl_price=sl_price,
                real_entry=real_entry
            )
            
            if not tp_sl_ok:
                print("🚨 TP/SL placement failed. Closing position for safety.")

                close_side = "SELL" if plan.side == "LONG" else "BUY"

                try:
                    self.exchange.close_position(
                        symbol=plan.symbol,
                        side=close_side,
                        quantity=quantity
                    )

                    print("✅ Position closed because TP/SL failed")

                except Exception as e:
                    print(f"❌ Failed to close unprotected position: {e}")

                return False
                
            TF_MS = 1 * 60 * 1000
            entry_candle_ts = int(plan.signal_ts + TF_MS)

            # ==========================
            # 📈 SAVE POSITION
            # ==========================
            position = Position(
                symbol=plan.symbol,
                side=plan.side,
                quantity=quantity,
                entry_price=self._apply_slippage(plan.entry, plan.side, True),
                real_entry=real_entry,
                tp=tp_price,
                sl=sl_price,
                entry_ts=int(time.time() * 1000),
                signal_price=float(plan.signal_price),
                signal_ts=int(plan.signal_ts),
                signal_context=plan.signal_context,
                
                current_momentum=plan.signal_context.get("momentum"),
                current_direction=plan.signal_context.get("direction"),
                
                plan_max_hold_candles=int(plan.max_hold_candles),
                entry_candle_ts=int(entry_candle_ts),
                candles_in_trade=1
            )
            
            self.positions[plan.symbol] = position

            # compatibilidad temporal
            self.position = position
            
            now = time.time()
            self.last_global_entry_ts = now
            self.last_symbol_entry_ts[plan.symbol] = now
            
            snapshot = {
                "position": {
                    "status": "OPEN",
                    "side": plan.side,
                    "entry_price": real_entry,
                    "qty": quantity,
                    "opened_ts": int(time.time() * 1000),

                    "signal_ts": plan.signal_ts,
                    "signal_price": plan.signal_price,

                    "entry_ts": int(time.time() * 1000),

                    "entry": plan.entry,
                    "real_entry": real_entry,

                    "tp": tp_price,
                    "sl": sl_price
                },

                "context": {
                    "signal_trend": plan.signal_context.get("trend"),
                    "signal_direction": plan.signal_context.get("direction"),
                    "signal_momentum": plan.signal_context.get("momentum"),

                    "signal_momentum_prev1": plan.signal_context.get("momentum_prev1"),
                    "signal_momentum_prev2": plan.signal_context.get("momentum_prev2"),
                    "signal_momentum_sequence": plan.signal_context.get("momentum_sequence"),

                    "signal_atr": plan.signal_context.get("signal_atr"),
                    "signal_atr_pct": plan.signal_context.get("signal_atr_pct"),
                    
                    "dist_ema50_15m_pct": plan.signal_context.get("dist_ema50_15m_pct"),
                    "dist_ema99_15m_pct": plan.signal_context.get("dist_ema99_15m_pct"),

                    "dist_ema50_1h_pct": plan.signal_context.get("dist_ema50_1h_pct"),
                    "dist_ema99_1h_pct": plan.signal_context.get("dist_ema99_1h_pct"),

                    "dist_ema50_4h_pct": plan.signal_context.get("dist_ema50_4h_pct"),
                    "dist_ema99_4h_pct": plan.signal_context.get("dist_ema99_4h_pct"),

                    "strategy_mode": plan.signal_context.get("strategy_name"),
                    "router_reason": plan.signal_context.get("router_reason")
                },

                "post_entry_analysis": {},
                "engine": {
                    "last_update_ts": None,
                    "last_candle_ts": None
                }
            }

            self.snapshot_manager.save(plan.symbol, snapshot)
            
            print(f"""
    \033[94m[EXECUTION ENGINE]\033[0m
    📈 POSITION OPENED
    Symbol       : {plan.symbol}
    Side         : {plan.side}
    Quantity     : {quantity}
    Signal Price : {plan.signal_price:.2f}
    Entry (bot)  : {self.position.entry_price:.2f}
    Entry (real) : {real_entry:.2f}
    TP           : {tp_price:.2f}
    SL           : {sl_price:.2f}
    """)

            return True

        except Exception as e:
            print(f"\033[94m[EXECUTION ENGINE]\033[0m ❌ execute_plan crashed safely: {e}")
            return False
        
        
        finally:
            self.opening_position = False
    # ==========================
    # 🔄 PRICE UPDATE
    # ==========================
    
    def update_position_context(
        self,
        trend,
        direction,
        momentum,
        micro_momentum,
        price,
        ema20_1m=None,
        ema34_1m=None,
        ema50_1m=None
    ):
        self.strategy.update_position_context(
            self,
            trend,
            direction,
            momentum,
            micro_momentum,
            price,
            ema20_1m,
            ema34_1m,
            ema50_1m
        )

        if not self.position:
            return

        self.snapshot_manager.update(
            self.position.symbol,
            "post_entry_analysis",
            {
                "current_trend": self.position.current_trend,
                "current_direction": self.position.current_direction,
                "current_momentum": self.position.current_momentum,

                "mae": self.position.mae,
                "mfe": self.position.mfe,

                "direction_t1": self.position.direction_t1,
                "momentum_t1": self.position.momentum_t1,
                "pnl_t1": self.position.pnl_t1,

                "micro_t1": self.position.micro_t1,
                "direction_5m_t1": self.position.direction_5m_t1,

                "reclaimed_ema20_1m": self.position.reclaimed_ema20_1m,
                "reclaimed_ema34_1m": self.position.reclaimed_ema34_1m,
                "reclaimed_ema50_1m": self.position.reclaimed_ema50_1m,

                "lost_ema20_1m": self.position.lost_ema20_1m,
                "lost_ema34_1m": self.position.lost_ema34_1m,
                "lost_ema50_1m": self.position.lost_ema50_1m,

                "dist_ema20_1m_pct": self.position.dist_ema20_1m_pct,
                "dist_ema34_1m_pct": self.position.dist_ema34_1m_pct,
                "dist_ema50_1m_pct": self.position.dist_ema50_1m_pct,

                "max_favorable_pct": self.position.max_favorable_pct,
                "max_adverse_pct": self.position.max_adverse_pct,

                "direction_5m_changed": self.position.direction_5m_changed,
                "direction_5m_after_entry": self.position.direction_5m_after_entry,
            }
        )
    
    def on_signal(self, trade_action, plan):
        return self.strategy.on_signal(self, trade_action, plan)
    
    def on_candle_close(self, ts, price):
        self.strategy.on_candle_close(self, ts, price)

    def on_price_update(self, symbol: str, price: float, timestamp: int):

        position = self.positions.get(symbol)

        if not position:
            return

        exchange_pos = self.position_manager.sync(symbol)

        if not exchange_pos:
            self.position = position
            self._handle_external_close(price, timestamp)
            return

        # compat temporal
        self.position = position

        self.strategy.on_price_update(self, price, timestamp)
        
        self.snapshot_manager.update(
            symbol,
            "engine",
            {
                "last_update_ts": timestamp
            }
        )

    # ==========================
    # ❌ CLOSE
    # ==========================

    def _close_position(self, price, timestamp, reason):

        pos = self.position
        if not pos:
            return

        side = "SELL" if pos.side == "LONG" else "BUY"

        order = self.exchange.close_position(
            symbol=pos.symbol,
            side=side,
            quantity=pos.quantity
        )

        if order and float(order.get("avgPrice", 0)) > 0:
            real_exit = float(order["avgPrice"])
        elif order and "fills" in order and len(order["fills"]) > 0:
            real_exit = float(order["fills"][0]["price"])
        else:
            print("⚠️ Close order without fills, using fallback price")
            real_exit = price
        
        qty = float(pos.quantity)

        if pos.side == "LONG":
            pnl_usd = (real_exit - pos.real_entry) * qty
        else:
            pnl_usd = (pos.real_entry - real_exit) * qty

        # 🔹 precio simulado (con slippage)
        price_with_slippage = self._apply_slippage(price, pos.side, is_entry=False)

        # 🔹 PnL
        pnl_pct = (
            (real_exit - pos.real_entry) / pos.real_entry * 100
            if pos.side == "LONG"
            else (pos.real_entry - real_exit) / pos.real_entry * 100
        )

        fees = self.fees["taker"] * 2
        pnl_gross = pnl_pct
        pnl_net = pnl_gross - fees

        pnl_pct = round(pnl_pct, 4)
        pnl_gross = round(pnl_gross, 4)
        pnl_net = round(pnl_net, 4)
        fees = round(fees, 2)

        print(f"\033[94m[EXECUTION ENGINE]\033[0m ❌ CLOSED {reason} | PnL: {round(pnl_net, 4)}%")
        
        ctx = pos.signal_context or {}
        
        signal_iso = datetime.utcfromtimestamp(pos.signal_ts / 1000).isoformat()
        entry_iso = datetime.utcfromtimestamp(pos.entry_ts / 1000).isoformat()
        exit_iso = datetime.utcfromtimestamp(timestamp / 1000).isoformat()

        self.journal.log_trade(
            symbol=pos.symbol,
            signal_ts=signal_iso,
            signal_price=pos.signal_price,

            entry_ts=entry_iso,
            exit_ts=exit_iso,

            side=pos.side,

            entry=pos.entry_price,
            real_entry=pos.real_entry,

            exit_price=price,
            real_exit=real_exit,

            tp=pos.tp,
            sl=pos.sl,

            pnl=pnl_net,
            pnl_gross=pnl_pct,
            pnl_usd=pnl_usd,
            fees=fees,

            exit_reason=reason,

            # ==========================
            # SIGNAL CONTEXT
            # ==========================
            signal_trend=ctx.get("trend"),
            signal_direction=ctx.get("direction"),
            signal_momentum=ctx.get("momentum"),

            # 🔥 NUEVO CONTEXTO
            signal_momentum_prev1=ctx.get("momentum_prev1"),
            signal_momentum_prev2=ctx.get("momentum_prev2"),
            signal_momentum_sequence=ctx.get("momentum_sequence"),

            signal_atr=ctx.get("signal_atr"),

            # ==========================
            # LIVE POSITION CONTEXT
            # ==========================
            current_trend=pos.current_trend,
            current_direction=pos.current_direction,
            current_momentum=pos.current_momentum,

            # ==========================
            # FIRST POST-ENTRY STATE
            # ==========================
            direction_t1=pos.direction_t1,
            momentum_t1=pos.momentum_t1,

            pnl_t1=pos.pnl_t1,
            
            # ==========================
            # AGGRESSIVE POST ANALYSIS
            # ==========================
            micro_t1=pos.micro_t1,
            direction_5m_t1=pos.direction_5m_t1,

            reclaimed_ema20_1m=pos.reclaimed_ema20_1m,
            reclaimed_ema34_1m=pos.reclaimed_ema34_1m,
            reclaimed_ema50_1m=pos.reclaimed_ema50_1m,

            lost_ema20_1m=pos.lost_ema20_1m,
            lost_ema34_1m=pos.lost_ema34_1m,
            lost_ema50_1m=pos.lost_ema50_1m,

            dist_ema20_1m_pct=pos.dist_ema20_1m_pct,
            dist_ema34_1m_pct=pos.dist_ema34_1m_pct,
            dist_ema50_1m_pct=pos.dist_ema50_1m_pct,

            dist_ema50_15m_pct=ctx.get("dist_ema50_15m_pct"),
            dist_ema99_15m_pct=ctx.get("dist_ema99_15m_pct"),

            dist_ema50_1h_pct=ctx.get("dist_ema50_1h_pct"),
            dist_ema99_1h_pct=ctx.get("dist_ema99_1h_pct"),

            dist_ema50_4h_pct=ctx.get("dist_ema50_4h_pct"),
            dist_ema99_4h_pct=ctx.get("dist_ema99_4h_pct"),

            max_favorable_pct=pos.max_favorable_pct,
            max_adverse_pct=pos.max_adverse_pct,

            direction_5m_changed=pos.direction_5m_changed,
            direction_5m_after_entry=pos.direction_5m_after_entry,

            # ==========================
            # TRADE EVOLUTION
            # ==========================
            mae=pos.mae,
            mfe=pos.mfe,
            
            strategy_mode=ctx.get("strategy_name"),
            router_reason=ctx.get("router_reason")
        )
        
        try:
            self.order_executor.cancel_all(pos.symbol)
            print(f"🧹 Pending orders cancelled for {pos.symbol}")
        except Exception as e:
            print(f"⚠️ Failed cancelling pending orders for {pos.symbol}: {e}")

        self.positions.pop(pos.symbol, None)

        if self.position and self.position.symbol == pos.symbol:
            self.position = None

        self.snapshot_manager.clear(pos.symbol)

    def get_state(self):
        return {
            "position": self.position,
            "total_trades": len(self.trades)
        }

    def restore_state(self, symbol):

        # ==========================
        # EXCHANGE SYNC
        # ==========================
        sync = ExchangeStateSync(self.exchange)
        exchange_state = sync.restore_position_state(symbol)

        if not exchange_state:
            print("\033[94m[SYNC]\033[0m ℹ️ No position to restore.")
            return

        # ==========================
        # LOAD SNAPSHOT
        # ==========================
        snapshot = self.snapshot_manager.load(symbol)

        if not snapshot:
            print("\033[94m[SYNC]\033[0m ⚠️ Snapshot not found.")
            return

        position_data = snapshot.get("position", {})
        context = snapshot.get("context", {})
                
        context = {
            **context,

            "trend": context.get("trend") or context.get("signal_trend"),
            "direction": context.get("direction") or context.get("signal_direction"),
            "momentum": context.get("momentum") or context.get("signal_momentum"),

            "momentum_prev1": context.get("momentum_prev1") or context.get("signal_momentum_prev1"),
            "momentum_prev2": context.get("momentum_prev2") or context.get("signal_momentum_prev2"),
            "momentum_sequence": context.get("momentum_sequence") or context.get("signal_momentum_sequence"),

            "signal_atr": context.get("signal_atr") or context.get("atr"),
            "signal_atr_pct": context.get("signal_atr_pct") or context.get("atr_pct"),

            "strategy_name": context.get("strategy_name") or context.get("strategy_mode"),
            "router_reason": context.get("router_reason"),

            "dist_ema50_15m_pct": context.get("dist_ema50_15m_pct"),
            "dist_ema99_15m_pct": context.get("dist_ema99_15m_pct"),

            "dist_ema50_1h_pct": context.get("dist_ema50_1h_pct"),
            "dist_ema99_1h_pct": context.get("dist_ema99_1h_pct"),

            "dist_ema50_4h_pct": context.get("dist_ema50_4h_pct"),
            "dist_ema99_4h_pct": context.get("dist_ema99_4h_pct"),
        }
        post_analysis = snapshot.get("post_entry_analysis", {})

        # ==========================
        # SNAPSHOT DATA
        # ==========================
        entry_price = float(
            position_data.get("real_entry")
            or position_data.get("entry_price")
            or position_data.get("entry")
            or exchange_state.get("entry_price")
        )
        quantity = float(
            position_data.get("qty")
            or position_data.get("quantity")
            or exchange_state.get("quantity")
        )
        side = (
            position_data.get("side")
            or exchange_state.get("side")
        )

        # ==========================
        # EXCHANGE VALIDATION
        # ==========================
        exchange_qty = float(exchange_state["quantity"])

        if abs(exchange_qty - quantity) > 0.0001:
            print(
                f"\033[93m[SYNC]\033[0m ⚠️ Quantity mismatch | "
                f"snapshot={quantity} exchange={exchange_qty}"
            )

        # ==========================
        # TP / SL
        # ==========================
        tp = exchange_state.get("tp")
        sl = exchange_state.get("sl")

        # ==========================
        # REBUILD TP / SL IF MISSING
        # ==========================
        if not tp or not sl:

            print("\033[94m[SYNC]\033[0m ⚠️ TP/SL missing → rebuilding")

            mark_price = float(
                self.exchange.get_mark_price(symbol)
            )

            tp, sl = self.risk_manager.calculate_tp_sl_from_position(
                side=side,
                entry_price=entry_price,
                mark_price=mark_price
            )

            tp_side = "SELL" if side == "LONG" else "BUY"

            tp_sl_ok = self._place_tp_sl(
                symbol=symbol,
                quantity=quantity,
                side=side,
                tp_side=tp_side,
                tp_price=tp,
                sl_side=tp_side,
                sl_price=sl,
                real_entry=entry_price,
            )

            if tp_sl_ok:
                print("\033[94m[SYNC]\033[0m ✅ TP/SL rebuilt")
            else:
                print("\033[94m[SYNC]\033[0m ❌ Failed rebuilding TP/SL")

        else:
            tp = float(tp)
            sl = float(sl)

        # ==========================
        # RESTORE POSITION
        # ==========================
        
        position = Position(
            symbol=symbol,
            side=side,
            quantity=quantity,

            entry_price=float(position_data.get("entry", entry_price)),
            real_entry=float(position_data.get("real_entry", entry_price)),

            tp=float(tp),
            sl=float(sl),

            entry_ts=int(position_data.get("entry_ts", time.time() * 1000)),

            signal_price=float(
                position_data.get("signal_price", entry_price)
            ),

            signal_ts=int(
                position_data.get("signal_ts", time.time() * 1000)
            ),

            signal_context=context,

            # ==========================
            # CURRENT CONTEXT
            # ==========================
            current_trend=post_analysis.get("current_trend"),
            current_direction=post_analysis.get("current_direction"),
            current_momentum=post_analysis.get("current_momentum"),

            # ==========================
            # FIRST POST ENTRY
            # ==========================
            direction_t1=post_analysis.get("direction_t1"),
            momentum_t1=post_analysis.get("momentum_t1"),
            pnl_t1=post_analysis.get("pnl_t1"),

            micro_t1=post_analysis.get("micro_t1"),
            direction_5m_t1=post_analysis.get("direction_5m_t1"),

            # ==========================
            # EMA RECLAIMS / LOSSES
            # ==========================
            reclaimed_ema20_1m=post_analysis.get("reclaimed_ema20_1m"),
            reclaimed_ema34_1m=post_analysis.get("reclaimed_ema34_1m"),
            reclaimed_ema50_1m=post_analysis.get("reclaimed_ema50_1m"),

            lost_ema20_1m=post_analysis.get("lost_ema20_1m"),
            lost_ema34_1m=post_analysis.get("lost_ema34_1m"),
            lost_ema50_1m=post_analysis.get("lost_ema50_1m"),

            # ==========================
            # EMA DISTANCES
            # ==========================
            dist_ema20_1m_pct=post_analysis.get("dist_ema20_1m_pct"),
            dist_ema34_1m_pct=post_analysis.get("dist_ema34_1m_pct"),
            dist_ema50_1m_pct=post_analysis.get("dist_ema50_1m_pct"),

            # ==========================
            # TRADE EXCURSIONS
            # ==========================
            max_favorable_pct=post_analysis.get("max_favorable_pct"),
            max_adverse_pct=post_analysis.get("max_adverse_pct"),

            direction_5m_changed=post_analysis.get("direction_5m_changed"),
            direction_5m_after_entry=post_analysis.get("direction_5m_after_entry"),

            mae=post_analysis.get("mae"),
            mfe=post_analysis.get("mfe"),
        )
        
        self.positions[symbol] = position
        self.position = position

        print(f"""
    \033[94m[SYNC]\033[0m 🔁 POSITION RESTORED

    Symbol           : {symbol}
    Side             : {side}
    Quantity         : {quantity}

    Entry            : {entry_price}
    TP               : {tp}
    SL               : {sl}

    Signal Direction : {context.get("direction")}
    Signal Momentum  : {context.get("momentum")}
    Strategy Mode    : {context.get("strategy_name")}
    Router Reason    : {context.get("router_reason")}
    """)

    def check_exchange_close(self, symbol):

        pos = self.exchange.get_position(symbol)

        if not pos or float(pos["amount"]) == 0:
            if self.position:
                print("🔄 Exchange cerró posición")
                self.position = None
                self.snapshot_manager.clear(symbol)