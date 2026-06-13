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

        self.global_entry_cooldown = 0   # 0 min
        self.symbol_entry_cooldown = 900   # 15 min

    def _apply_slippage(self, price: float, side: str, is_entry: bool = True):
        slippage_pct = random.uniform(0.01, 0.05) / 100
        slippage = price * slippage_pct

        if side == "LONG":
            return price + slippage if is_entry else price - slippage

        if side == "SHORT":
            return price - slippage if is_entry else price + slippage
        
    def _resolve_leverage(self, plan, default_leverage: int = 1) -> int:
        side = plan.side
        direction = plan.signal_context.get("direction")
        momentum = plan.signal_context.get("momentum")

        if (
            side == "LONG"
            and direction == "up"
            and momentum == "inside_bar"
        ):
            print("[LEVERAGE ROUTER] LONG up inside_bar -> 3x")
            return 3

        return default_leverage
            
    def _get_last_close_fill(self, symbol, side, quantity, entry_ts, limit=1000):
        fills = self.exchange.get_recent_fills(symbol, limit=limit)

        if not fills:
            return None

        exit_side = "SELL" if side == "LONG" else "BUY"
        expected_qty = abs(float(quantity))

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
            total_qty = sum(abs(float(f["qty"])) for f in group)

            qty_diff = abs(total_qty - expected_qty)
            qty_diff_pct = qty_diff / expected_qty if expected_qty > 0 else 999

            # tolerancia flexible: 0.2% o 1e-6, lo que sea mayor
            if qty_diff_pct <= 0.002 or qty_diff <= 0.000001:
                valid_orders.append((oid, group))

        if not valid_orders:
            print(
                f"⚠️ Close fill candidates found but qty mismatch | "
                f"symbol={symbol} expected_qty={expected_qty} "
                f"candidates={[(oid, sum(abs(float(x['qty'])) for x in g)) for oid, g in grouped.items()]}"
            )
            return None

        latest_order_id, selected = max(
            valid_orders,
            key=lambda item: max(int(x["time"]) for x in item[1])
        )

        total_qty = sum(abs(float(f["qty"])) for f in selected)
        total_quote = sum(abs(float(f["qty"])) * float(f["price"]) for f in selected)
        total_commission = sum(float(f.get("commission", 0) or 0) for f in selected)
        total_realized = sum(float(f.get("realizedPnl", 0) or 0) for f in selected)
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
        pnl_pct = (
            (exit_price - pos.real_entry) / pos.real_entry * 100
            if pos.side == "LONG"
            else (pos.real_entry - exit_price) / pos.real_entry * 100
        )

        tp_distance = abs(exit_price - float(pos.tp))
        sl_distance = abs(exit_price - float(pos.sl))

        # Si el cierre quedó más cerca del TP que del SL, clasificamos TP
        if tp_distance < sl_distance:
            return "TP"

        # Si quedó más cerca del SL, clasificamos según PnL
        if pnl_pct > 0.05:
            return "TRAILING_SL"

        if pnl_pct >= -0.10:
            return "BE_SL"

        return "SL"


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
            entry_ts=int(pos.entry_ts) - 5 * 60_000,
            limit=500
        )

        pnl_usd = 0.0
        fees = round(self.fees["taker"] * 2, 4)

        if close_fill:
            real_exit = float(close_fill["price"])
            exit_order_id = int(close_fill["orderId"])

            realized_pnl = float(close_fill.get("realizedPnl", 0) or 0)
            close_commission = float(close_fill.get("commission", 0) or 0)

            pnl_usd = realized_pnl - close_commission

            print(
                f"✅ Close fill encontrado | "
                f"orderId={exit_order_id} | "
                f"real_exit={real_exit} | "
                f"realized_pnl={realized_pnl:.6f} | "
                f"close_commission={close_commission:.6f} | "
                f"net_pnl_usd={pnl_usd:.6f}"
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
            # ==========================
            # TRADE ID / TIME
            # ==========================
            symbol=pos.symbol,
            side=pos.side,
            signal_ts=signal_iso,
            entry_ts=entry_iso,
            exit_ts=exit_iso,

            # ==========================
            # PRICES / LEVELS
            # ==========================
            signal_price=pos.signal_price,
            entry=pos.entry_price,
            real_entry=pos.real_entry,
            exit_price=price,
            real_exit=real_exit,
            tp=pos.tp,
            sl=pos.sl,

            # ==========================
            # RESULT
            # ==========================
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
            # BTC CONTEXT
            # ==========================
            btc_velocity_15m=ctx.get("btc_velocity_15m"),
            btc_velocity_1h=ctx.get("btc_velocity_1h"),
            btc_direction_15m=ctx.get("btc_direction_15m"),
            btc_direction_1h=ctx.get("btc_direction_1h"),
            btc_context_state=ctx.get("btc_context_state"),
            btc_context_reason=ctx.get("btc_context_reason"),
            
            # ==========================
            # BTC SWING CONTEXT
            # ==========================
            btc_dist_swing_low_1h_pct=ctx.get("btc_dist_swing_low_1h_pct"),
            btc_dist_swing_high_1h_pct=ctx.get("btc_dist_swing_high_1h_pct"),
            btc_near_swing_low_1h=ctx.get("btc_near_swing_low_1h"),
            btc_near_swing_high_1h=ctx.get("btc_near_swing_high_1h"),

            btc_dist_swing_low_4h_pct=ctx.get("btc_dist_swing_low_4h_pct"),
            btc_dist_swing_high_4h_pct=ctx.get("btc_dist_swing_high_4h_pct"),
            btc_near_swing_low_4h=ctx.get("btc_near_swing_low_4h"),
            btc_near_swing_high_4h=ctx.get("btc_near_swing_high_4h"),

            btc_dist_swing_low_1d_pct=ctx.get("btc_dist_swing_low_1d_pct"),
            btc_dist_swing_high_1d_pct=ctx.get("btc_dist_swing_high_1d_pct"),
            btc_near_swing_low_1d=ctx.get("btc_near_swing_low_1d"),
            btc_near_swing_high_1d=ctx.get("btc_near_swing_high_1d"),

            # ==========================
            # EMA CONTEXT
            # ==========================
            dist_ema20_1m_pct=pos.dist_ema20_1m_pct,
            dist_ema34_1m_pct=pos.dist_ema34_1m_pct,
            dist_ema50_1m_pct=pos.dist_ema50_1m_pct,

            dist_ema50_15m_pct=ctx.get("dist_ema50_15m_pct"),
            dist_ema99_15m_pct=ctx.get("dist_ema99_15m_pct"),
            dist_ema20_15m_pct=ctx.get("dist_ema20_15m_pct"),
            dist_ema50_1h_pct=ctx.get("dist_ema50_1h_pct"),
            dist_ema99_1h_pct=ctx.get("dist_ema99_1h_pct"),
            dist_ema20_1h_pct=ctx.get("dist_ema20_1h_pct"),
            dist_ema50_4h_pct=ctx.get("dist_ema50_4h_pct"),
            dist_ema99_4h_pct=ctx.get("dist_ema99_4h_pct"),
            dist_ema20_4h_pct=ctx.get("dist_ema20_4h_pct"),

            reclaimed_ema20_1m=pos.reclaimed_ema20_1m,
            reclaimed_ema34_1m=pos.reclaimed_ema34_1m,
            reclaimed_ema50_1m=pos.reclaimed_ema50_1m,
            lost_ema20_1m=pos.lost_ema20_1m,
            lost_ema34_1m=pos.lost_ema34_1m,
            lost_ema50_1m=pos.lost_ema50_1m,

            # ==========================
            # SWING CONTEXT
            # ==========================
            swing_low_15m=ctx.get("swing_low_15m"),
            swing_high_15m=ctx.get("swing_high_15m"),
            dist_swing_low_15m_pct=ctx.get("dist_swing_low_15m_pct"),
            dist_swing_high_15m_pct=ctx.get("dist_swing_high_15m_pct"),
            near_swing_low_15m=ctx.get("near_swing_low_15m"),
            near_swing_high_15m=ctx.get("near_swing_high_15m"),

            swing_low_1h=ctx.get("swing_low_1h"),
            swing_high_1h=ctx.get("swing_high_1h"),
            dist_swing_low_1h_pct=ctx.get("dist_swing_low_1h_pct"),
            dist_swing_high_1h_pct=ctx.get("dist_swing_high_1h_pct"),
            near_swing_low_1h=ctx.get("near_swing_low_1h"),
            near_swing_high_1h=ctx.get("near_swing_high_1h"),

            swing_low_4h=ctx.get("swing_low_4h"),
            swing_high_4h=ctx.get("swing_high_4h"),
            dist_swing_low_4h_pct=ctx.get("dist_swing_low_4h_pct"),
            dist_swing_high_4h_pct=ctx.get("dist_swing_high_4h_pct"),
            near_swing_low_4h=ctx.get("near_swing_low_4h"),
            near_swing_high_4h=ctx.get("near_swing_high_4h"),

            # ==========================
            # RECENT MOVE CONTEXT
            # ==========================
            move_5_bars_pct=ctx.get("move_5_bars_pct"),
            move_10_bars_pct=ctx.get("move_10_bars_pct"),

            green_candles_last_10=ctx.get("green_candles_last_10"),
            red_candles_last_10=ctx.get("red_candles_last_10"),

            # ==========================
            # LIVE / POST ENTRY CONTEXT
            # ==========================
            current_trend=pos.current_trend,
            current_direction=pos.current_direction,
            current_momentum=pos.current_momentum,

            direction_t1=pos.direction_t1,
            momentum_t1=pos.momentum_t1,
            pnl_t1=pos.pnl_t1,
            micro_t1=pos.micro_t1,
            direction_5m_t1=pos.direction_5m_t1,

            direction_5m_changed=pos.direction_5m_changed,
            direction_5m_after_entry=pos.direction_5m_after_entry,

            # ==========================
            # TRADE EXCURSION
            # ==========================
            max_favorable_pct=pos.max_favorable_pct,
            max_adverse_pct=pos.max_adverse_pct,
            mae=pos.mae,
            mfe=pos.mfe,
            
            # ==========================
            # LIQUIDITY CONTEXT
            # ==========================
            quote_volume_24h=ctx.get("quote_volume_24h"),
            avg_quote_volume_15m=ctx.get("avg_quote_volume_15m"),
            avg_quote_volume_1h=ctx.get("avg_quote_volume_1h"),
            avg_quote_volume_4h=ctx.get("avg_quote_volume_4h"),
            relative_volume_15m=ctx.get("relative_volume_15m"),
            relative_volume_1h=ctx.get("relative_volume_1h"),
            relative_volume_4h=ctx.get("relative_volume_4h"),
            volume_tier=ctx.get("volume_tier"),
            rvol_tier_15m=ctx.get("rvol_tier_15m"),
            rvol_tier_1h=ctx.get("rvol_tier_1h"),
            rvol_tier_4h=ctx.get("rvol_tier_4h"),

            # ==========================
            # STRATEGY / ROUTER
            # ==========================
            strategy_mode=ctx.get("strategy_name"),
            router_reason=ctx.get("router_reason"),
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

            #print(
            #    f"📌 TP/SL debug | "
            #    f"symbol={symbol} | qty={quantity} | side={side} | "
            #   f"mark={mark_price} | tick={tick_size} | "
            #    f"raw_tp={raw_tp} -> tp={tp_price} | "
            #    f"raw_sl={raw_sl} -> sl={sl_price}"
            #)

            # 1) Primero SL. La posición nunca debe quedar desnuda.
            self.exchange.place_stop_loss(
                symbol=symbol,
                side=sl_side,
                quantity=quantity,
                stop_price=sl_price
            )

            # 2) Después TP.
            self.exchange.place_take_profit_limit(
                symbol=symbol,
                side=tp_side,
                quantity=quantity,
                price=tp_price
            )

            print("✅ TP/SL colocados correctamente")
            return True

        except Exception as e:
            print(f"❌ Error colocando TP/SL: {e}")
            return False
            
    def replace_stop_loss_only(self, pos, new_sl_price):
        symbol = pos.symbol
        side = "SELL" if pos.side == "LONG" else "BUY"

        try:
            print(
                f"[SL] Replacing SL only | "
                f"symbol={symbol} | old_sl={pos.sl} | new_sl={new_sl_price}"
            )

            self.order_executor.cancel_stop_orders(symbol)

            self.exchange.place_stop_loss(
                symbol=symbol,
                side=side,
                quantity=pos.quantity,
                stop_price=new_sl_price
            )

            print(f"[SL] ✅ SL replaced | symbol={symbol} | stop={new_sl_price}")
            return True

        except Exception as e:
            print(f"[SL] ❌ Failed replacing SL | symbol={symbol} | error={e}")
            return False
            
    def move_sl_to_be(self, pos):
        be_price = self.exchange.normalize_price(
            pos.symbol,
            pos.real_entry
        )

        current_sl = self.exchange.normalize_price(
            pos.symbol,
            pos.sl
        )

        if float(current_sl) == float(be_price):
            print(f"[BE] Already at BE | {pos.symbol} | sl={current_sl}")
            return True

        print(
            f"[BE] Moving SL to BE | "
            f"symbol={pos.symbol} | "
            f"old_sl={pos.sl} | "
            f"new_sl={be_price}"
        )

        try:
            ok = self.replace_stop_loss_only(pos, be_price)

            if not ok:
                print("[BE] Failed moving SL to BE")
                return False

            pos.sl = float(be_price)
            pos.be_moved = True

            self.snapshot_manager.update(
                pos.symbol,
                "position",
                {
                    "sl": float(be_price),
                    "tp": float(pos.tp),
                    "be_moved": True,
                }
            )

            print(f"[BE] SL moved to BE successfully | {pos.symbol} | sl={be_price}")
            return True

        except Exception as e:
            print(f"[BE] Error moving SL to BE: {e}")
            return False
        
    def exchange_open_positions_count(self, symbols):
        count = 0

        for symbol in symbols:
            pos = self.position_manager.sync(symbol)

            if pos == "INVALID_SYMBOL":
                continue

            if pos is not None:
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
    
    def _timer(self, label, t):
        elapsed = time.perf_counter() - t
        print(
            f"\033[91m[OPEN_POSITION TIMER]\033[0m "
            f"{label} elapsed={elapsed:.2f}s"
        )
        return time.perf_counter()

    def open_position(self, plan, leverage: int = 1):
        
        started = time.perf_counter()
        t = started
        
        if self.opening_position:
            print("⛔ Opening already in progress. Plan ignored.")
            return False

        self.opening_position = True
        
        try:
            
            if len(self.positions) >= self.max_global_positions:
                print(
                    "\033[94m[EXECUTION ENGINE]\033[0m "
                    "⚠️ Max global local positions reached. Plan ignored.\n"
                )
                return False
            t = self._timer("local_positions_count", t)
            
            exchange_pos = self.position_manager.sync(plan.symbol)
            t = self._timer("position_manager.sync before order", t)

            if exchange_pos == "INVALID_SYMBOL":
                print(f"⚠️ Invalid symbol skipped | {plan.symbol}")
                return False

            if exchange_pos is not None:
                print("\033[94m[EXECUTION ENGINE]\033[0m ⚠️ Position already open (exchange). Plan ignored.\n")
                return False

            if plan.side not in ("LONG", "SHORT"):
                return False

            side = "BUY" if plan.side == "LONG" else "SELL"

            balance = float(self.exchange.get_balance())
            t = self._timer("get_balance", t)
            
            if balance <= 0:
                print("❌ No balance")
                return False
            
            leverage = self._resolve_leverage(plan, default_leverage=leverage)

            if self.order_executor.set_leverage(plan.symbol, leverage) is None:
                print("❌ Error setting leverage")
                return False
            t = self._timer("set_leverage", t)
            
            print(f"✅ Leverage set to {leverage}x")

            try:
                price = float(self.exchange.get_price(plan.symbol))
                t = self._timer("get_price", t)
            except Exception as e:
                print(f"❌ Error getting price | symbol={plan.symbol} | error={e}")
                return False

            size_data = self.position_sizer.calculate(
                balance=balance,
                price=price,
                leverage=leverage
            )

    #        print(f"""
    #[POSITION SIZER]
    #Balance         : {balance}
    #Price           : {price}
    #Leverage        : {leverage}
    #Quantity        : {size_data['quantity']}
    #Notional        : {size_data['notional']:.2f}
    #Required margin : {size_data['required_margin']:.2f}
    #Usable balance  : {size_data['usable_balance']:.2f}
    #""")

            is_valid, msg = self.position_sizer.validate(size_data)

            if not is_valid:
                print(msg)
                return False

            raw_quantity = size_data["quantity"]

            quantity = self.exchange.normalize_quantity(
                plan.symbol,
                raw_quantity
            )
            
            if float(quantity) <= 0:
                print(f"❌ Invalid quantity after normalization | symbol={plan.symbol} qty={quantity}")
                return False

            #print(
            #    f"📏 Quantity normalized | "
            #    f"symbol={plan.symbol} | "
            #    f"raw={raw_quantity} -> qty={quantity}"
            #)

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

                    if exchange_pos == "INVALID_SYMBOL":
                        print(f"⚠️ Invalid symbol after timeout sync | {plan.symbol}")
                        return False

                    if exchange_pos is not None:
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
            t = self._timer("market_order", t)

            # 💣 CASO CRÍTICO: timeout Binance
            if order and order.get("status") == "UNKNOWN":

                print("🔎 Orden en estado desconocido, sincronizando...")

                time.sleep(2)

            elif not order:
                print("❌ Order failed")
                return False

            exchange_pos = self.position_manager.sync(plan.symbol)
            t = self._timer("position_manager.sync after order", t)

            if exchange_pos == "INVALID_SYMBOL":
                print(f"⚠️ Invalid symbol skipped after order | {plan.symbol}")
                return False

            if exchange_pos is None:
                print("❌ No hay posición después del order, abortando")
                return False

            time.sleep(1.0)
            t = self._timer("sleep_1s", t)

            pos = self.exchange.get_position(plan.symbol)
            t = self._timer("get_position", t)
            real_entry = float(pos["entry_price"]) if pos else plan.entry
            entry_ts = int(time.time() * 1000)

            opening_snapshot = {
                "position": {
                    "status": "OPENING",
                    "side": plan.side,
                    "entry_price": real_entry,
                    "qty": quantity,
                    "leverage": leverage,
                    "opened_ts": entry_ts,

                    "signal_ts": plan.signal_ts,
                    "signal_price": plan.signal_price,

                    "entry_ts": entry_ts,
                    "entry": plan.entry,
                    "real_entry": real_entry,

                    "tp": None,
                    "sl": None,
                    "be_moved": False
                },

                "context": {
                    **plan.signal_context,
                    "strategy_mode": plan.signal_context.get("strategy_name"),
                    "router_reason": plan.signal_context.get("router_reason"),
                },

                "post_entry_analysis": {},

                "engine": {
                    "last_update_ts": None,
                    "last_candle_ts": None
                }
            }

            self.snapshot_manager.save(plan.symbol, opening_snapshot)

            print(f"📝 OPENING snapshot saved | symbol={plan.symbol}")

            # ==========================
            # 🎯 TP / SL
            # ==========================
            mark_price = float(self.exchange.get_mark_price(plan.symbol))
            t = self._timer("get_mark_price", t)
            
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
            t = self._timer("_place_tp_sl", t)
                        
            if not tp_sl_ok:
                print("🚨 TP/SL placement failed. Closing position for safety.")

                close_side = "SELL" if plan.side == "LONG" else "BUY"

                try:
                    self.exchange.close_position(
                        symbol=plan.symbol,
                        side=close_side,
                        quantity=quantity
                    )

                    self.order_executor.cancel_all(plan.symbol)
                    self.snapshot_manager.clear(plan.symbol)

                    print("✅ Position closed because TP/SL failed")

                except Exception as e:
                    print(f"❌ Failed to close unprotected position: {e}")

                return False
            
                
            TF_MS = 1 * 60 * 1000
            entry_candle_ts = int(plan.signal_ts + TF_MS)
            entry_ts = int(time.time() * 1000)

            delay_sec = (entry_ts - int(plan.signal_ts)) / 1000

            print(
                f"\033[91m[ENTRY DELAY]\033[0m "
                f"symbol={plan.symbol} "
                f"side={plan.side} "
                f"delay={delay_sec:.2f}s "
                f"signal_ts={plan.signal_ts} "
                f"entry_ts={entry_ts}"
            )

            # ==========================
            # 📈 SAVE POSITION
            # ==========================
            position = Position(
                symbol=plan.symbol,
                side=plan.side,
                quantity=quantity,
                entry_price=float(plan.entry),
                real_entry=float(real_entry),
                tp=tp_price,
                sl=sl_price,
                entry_ts=entry_ts,
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
                    "leverage": leverage,
                    "opened_ts": entry_ts,

                    "signal_ts": plan.signal_ts,
                    "signal_price": plan.signal_price,

                    "entry_ts": entry_ts,

                    "entry": plan.entry,
                    "real_entry": real_entry,

                    "tp": tp_price,
                    "sl": sl_price,
                    "be_moved": False
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
                    
                    "quote_volume_24h": plan.signal_context.get("quote_volume_24h"),
                    "avg_quote_volume_15m": plan.signal_context.get("avg_quote_volume_15m"),
                    "avg_quote_volume_1h": plan.signal_context.get("avg_quote_volume_1h"),
                    "avg_quote_volume_4h": plan.signal_context.get("avg_quote_volume_4h"),

                    "relative_volume_15m": plan.signal_context.get("relative_volume_15m"),
                    "relative_volume_1h": plan.signal_context.get("relative_volume_1h"),
                    "relative_volume_4h": plan.signal_context.get("relative_volume_4h"),

                    "volume_tier": plan.signal_context.get("volume_tier"),
                    "rvol_tier_15m": plan.signal_context.get("rvol_tier_15m"),
                    "rvol_tier_1h": plan.signal_context.get("rvol_tier_1h"),
                    "rvol_tier_4h": plan.signal_context.get("rvol_tier_4h"),
                    
                    "btc_velocity_15m": plan.signal_context.get("btc_velocity_15m"),
                    "btc_velocity_1h": plan.signal_context.get("btc_velocity_1h"),

                    "btc_direction_15m": plan.signal_context.get("btc_direction_15m"),
                    "btc_direction_1h": plan.signal_context.get("btc_direction_1h"),

                    "btc_context_state": plan.signal_context.get("btc_context_state"),
                    "btc_context_reason": plan.signal_context.get("btc_context_reason"),
                    
                    "btc_dist_swing_low_1h_pct": plan.signal_context.get("btc_dist_swing_low_1h_pct"),
                    "btc_dist_swing_high_1h_pct": plan.signal_context.get("btc_dist_swing_high_1h_pct"),
                    "btc_near_swing_low_1h": plan.signal_context.get("btc_near_swing_low_1h"),
                    "btc_near_swing_high_1h": plan.signal_context.get("btc_near_swing_high_1h"),

                    "btc_dist_swing_low_4h_pct": plan.signal_context.get("btc_dist_swing_low_4h_pct"),
                    "btc_dist_swing_high_4h_pct": plan.signal_context.get("btc_dist_swing_high_4h_pct"),
                    "btc_near_swing_low_4h": plan.signal_context.get("btc_near_swing_low_4h"),
                    "btc_near_swing_high_4h": plan.signal_context.get("btc_near_swing_high_4h"),

                    "btc_dist_swing_low_1d_pct": plan.signal_context.get("btc_dist_swing_low_1d_pct"),
                    "btc_dist_swing_high_1d_pct": plan.signal_context.get("btc_dist_swing_high_1d_pct"),
                    "btc_near_swing_low_1d": plan.signal_context.get("btc_near_swing_low_1d"),
                    "btc_near_swing_high_1d": plan.signal_context.get("btc_near_swing_high_1d"),
                    
                    "dist_ema50_15m_pct": plan.signal_context.get("dist_ema50_15m_pct"),
                    "dist_ema99_15m_pct": plan.signal_context.get("dist_ema99_15m_pct"),
                    "dist_ema20_15m_pct": plan.signal_context.get("dist_ema20_15m_pct"),

                    "dist_ema50_1h_pct": plan.signal_context.get("dist_ema50_1h_pct"),
                    "dist_ema99_1h_pct": plan.signal_context.get("dist_ema99_1h_pct"),
                    "dist_ema20_1h_pct": plan.signal_context.get("dist_ema20_1h_pct"),

                    "dist_ema50_4h_pct": plan.signal_context.get("dist_ema50_4h_pct"),
                    "dist_ema99_4h_pct": plan.signal_context.get("dist_ema99_4h_pct"),
                    "dist_ema20_4h_pct": plan.signal_context.get("dist_ema20_4h_pct"),
                    
                    "swing_low_15m": plan.signal_context.get("swing_low_15m"),
                    "swing_high_15m": plan.signal_context.get("swing_high_15m"),
                    "dist_swing_low_15m_pct": plan.signal_context.get("dist_swing_low_15m_pct"),
                    "dist_swing_high_15m_pct": plan.signal_context.get("dist_swing_high_15m_pct"),
                    "near_swing_low_15m": plan.signal_context.get("near_swing_low_15m"),
                    "near_swing_high_15m": plan.signal_context.get("near_swing_high_15m"),

                    "swing_low_1h": plan.signal_context.get("swing_low_1h"),
                    "swing_high_1h": plan.signal_context.get("swing_high_1h"),
                    "dist_swing_low_1h_pct": plan.signal_context.get("dist_swing_low_1h_pct"),
                    "dist_swing_high_1h_pct": plan.signal_context.get("dist_swing_high_1h_pct"),
                    "near_swing_low_1h": plan.signal_context.get("near_swing_low_1h"),
                    "near_swing_high_1h": plan.signal_context.get("near_swing_high_1h"),

                    "swing_low_4h": plan.signal_context.get("swing_low_4h"),
                    "swing_high_4h": plan.signal_context.get("swing_high_4h"),
                    "dist_swing_low_4h_pct": plan.signal_context.get("dist_swing_low_4h_pct"),
                    "dist_swing_high_4h_pct": plan.signal_context.get("dist_swing_high_4h_pct"),
                    "near_swing_low_4h": plan.signal_context.get("near_swing_low_4h"),
                    "near_swing_high_4h": plan.signal_context.get("near_swing_high_4h"),
                    
                    "move_5_bars_pct": plan.signal_context.get("move_5_bars_pct"),
                    "move_10_bars_pct": plan.signal_context.get("move_10_bars_pct"),

                    "green_candles_last_10": plan.signal_context.get("green_candles_last_10"),
                    "red_candles_last_10": plan.signal_context.get("red_candles_last_10"),

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
            t = self._timer("snapshot_manager.save FULL OPEN", t)

            
            print(f"""
    \033[94m[EXECUTION ENGINE]\033[0m
    📈 POSITION OPENED
    Symbol       : {plan.symbol}
    Side         : {plan.side}
    Quantity     : {quantity}
    Signal Price : {plan.signal_price:.8f}
    Entry (bot)  : {self.position.entry_price:.8f}
    Entry (real) : {real_entry:.8f}
    TP           : {tp_price:.8f}
    SL           : {sl_price:.8f}
    """)
            
            print(
            f"\033[91m[OPEN_POSITION TOTAL]\033[0m "
            f"symbol={plan.symbol} "
            f"elapsed={time.perf_counter() - started:.2f}s"
            )

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
        symbol,
        trend,
        direction,
        momentum,
        micro_momentum=None,
        current_price=None,
        ema20_1m=None,
        ema34_1m=None,
        ema50_1m=None
    ):
        self.strategy.update_position_context(
            execution_engine=self,
            symbol=symbol,
            trend=trend,
            direction=direction,
            momentum=momentum,
            micro_momentum=micro_momentum,
            current_price=current_price,
            ema20_1m=ema20_1m,
            ema34_1m=ema34_1m,
            ema50_1m=ema50_1m
        )

        pos = self.get_position(symbol)

        if not pos:
            return

        self.snapshot_manager.update(
            symbol,
            "post_entry_analysis",
            {
                "current_trend": pos.current_trend,
                "current_direction": pos.current_direction,
                "current_momentum": pos.current_momentum,

                "mae": pos.mae,
                "mfe": pos.mfe,

                "direction_t1": pos.direction_t1,
                "momentum_t1": pos.momentum_t1,
                "pnl_t1": pos.pnl_t1,

                "micro_t1": pos.micro_t1,
                "direction_5m_t1": pos.direction_5m_t1,

                "reclaimed_ema20_1m": pos.reclaimed_ema20_1m,
                "reclaimed_ema34_1m": pos.reclaimed_ema34_1m,
                "reclaimed_ema50_1m": pos.reclaimed_ema50_1m,

                "lost_ema20_1m": pos.lost_ema20_1m,
                "lost_ema34_1m": pos.lost_ema34_1m,
                "lost_ema50_1m": pos.lost_ema50_1m,

                "dist_ema20_1m_pct": pos.dist_ema20_1m_pct,
                "dist_ema34_1m_pct": pos.dist_ema34_1m_pct,
                "dist_ema50_1m_pct": pos.dist_ema50_1m_pct,

                "max_favorable_pct": pos.max_favorable_pct,
                "max_adverse_pct": pos.max_adverse_pct,

                "direction_5m_changed": pos.direction_5m_changed,
                "direction_5m_after_entry": pos.direction_5m_after_entry,
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

        if exchange_pos == "INVALID_SYMBOL":
            return

        if exchange_pos is None:
            self.position = position
            self._handle_external_close(price, timestamp)
            return

        # compat temporal
        self.position = position

        self.strategy.on_price_update(
            self,
            symbol,
            price,
            timestamp
        )
                
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
            # ==========================
            # TRADE ID / TIME
            # ==========================
            symbol=pos.symbol,
            side=pos.side,
            signal_ts=signal_iso,
            entry_ts=entry_iso,
            exit_ts=exit_iso,

            # ==========================
            # PRICES / LEVELS
            # ==========================
            signal_price=pos.signal_price,
            entry=pos.entry_price,
            real_entry=pos.real_entry,
            exit_price=price,
            real_exit=real_exit,
            tp=pos.tp,
            sl=pos.sl,

            # ==========================
            # RESULT
            # ==========================
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
            # BTC CONTEXT
            # ==========================
            btc_velocity_15m=ctx.get("btc_velocity_15m"),
            btc_velocity_1h=ctx.get("btc_velocity_1h"),
            btc_direction_15m=ctx.get("btc_direction_15m"),
            btc_direction_1h=ctx.get("btc_direction_1h"),
            btc_context_state=ctx.get("btc_context_state"),
            btc_context_reason=ctx.get("btc_context_reason"),
            
            # ==========================
            # BTC SWING CONTEXT
            # ==========================
            btc_dist_swing_low_1h_pct=ctx.get("btc_dist_swing_low_1h_pct"),
            btc_dist_swing_high_1h_pct=ctx.get("btc_dist_swing_high_1h_pct"),
            btc_near_swing_low_1h=ctx.get("btc_near_swing_low_1h"),
            btc_near_swing_high_1h=ctx.get("btc_near_swing_high_1h"),

            btc_dist_swing_low_4h_pct=ctx.get("btc_dist_swing_low_4h_pct"),
            btc_dist_swing_high_4h_pct=ctx.get("btc_dist_swing_high_4h_pct"),
            btc_near_swing_low_4h=ctx.get("btc_near_swing_low_4h"),
            btc_near_swing_high_4h=ctx.get("btc_near_swing_high_4h"),

            btc_dist_swing_low_1d_pct=ctx.get("btc_dist_swing_low_1d_pct"),
            btc_dist_swing_high_1d_pct=ctx.get("btc_dist_swing_high_1d_pct"),
            btc_near_swing_low_1d=ctx.get("btc_near_swing_low_1d"),
            btc_near_swing_high_1d=ctx.get("btc_near_swing_high_1d"),

            # ==========================
            # EMA CONTEXT
            # ==========================
            dist_ema20_1m_pct=pos.dist_ema20_1m_pct,
            dist_ema34_1m_pct=pos.dist_ema34_1m_pct,
            dist_ema50_1m_pct=pos.dist_ema50_1m_pct,

            dist_ema50_15m_pct=ctx.get("dist_ema50_15m_pct"),
            dist_ema99_15m_pct=ctx.get("dist_ema99_15m_pct"),
            dist_ema20_15m_pct=ctx.get("dist_ema20_15m_pct"),
            dist_ema50_1h_pct=ctx.get("dist_ema50_1h_pct"),
            dist_ema99_1h_pct=ctx.get("dist_ema99_1h_pct"),
            dist_ema20_1h_pct=ctx.get("dist_ema20_1h_pct"),
            dist_ema50_4h_pct=ctx.get("dist_ema50_4h_pct"),
            dist_ema99_4h_pct=ctx.get("dist_ema99_4h_pct"),
            dist_ema20_4h_pct=ctx.get("dist_ema20_4h_pct"),

            reclaimed_ema20_1m=pos.reclaimed_ema20_1m,
            reclaimed_ema34_1m=pos.reclaimed_ema34_1m,
            reclaimed_ema50_1m=pos.reclaimed_ema50_1m,
            lost_ema20_1m=pos.lost_ema20_1m,
            lost_ema34_1m=pos.lost_ema34_1m,
            lost_ema50_1m=pos.lost_ema50_1m,

            # ==========================
            # SWING CONTEXT
            # ==========================
            swing_low_15m=ctx.get("swing_low_15m"),
            swing_high_15m=ctx.get("swing_high_15m"),
            dist_swing_low_15m_pct=ctx.get("dist_swing_low_15m_pct"),
            dist_swing_high_15m_pct=ctx.get("dist_swing_high_15m_pct"),
            near_swing_low_15m=ctx.get("near_swing_low_15m"),
            near_swing_high_15m=ctx.get("near_swing_high_15m"),

            swing_low_1h=ctx.get("swing_low_1h"),
            swing_high_1h=ctx.get("swing_high_1h"),
            dist_swing_low_1h_pct=ctx.get("dist_swing_low_1h_pct"),
            dist_swing_high_1h_pct=ctx.get("dist_swing_high_1h_pct"),
            near_swing_low_1h=ctx.get("near_swing_low_1h"),
            near_swing_high_1h=ctx.get("near_swing_high_1h"),

            swing_low_4h=ctx.get("swing_low_4h"),
            swing_high_4h=ctx.get("swing_high_4h"),
            dist_swing_low_4h_pct=ctx.get("dist_swing_low_4h_pct"),
            dist_swing_high_4h_pct=ctx.get("dist_swing_high_4h_pct"),
            near_swing_low_4h=ctx.get("near_swing_low_4h"),
            near_swing_high_4h=ctx.get("near_swing_high_4h"),

            # ==========================
            # RECENT MOVE CONTEXT
            # ==========================
            move_5_bars_pct=ctx.get("move_5_bars_pct"),
            move_10_bars_pct=ctx.get("move_10_bars_pct"),

            green_candles_last_10=ctx.get("green_candles_last_10"),
            red_candles_last_10=ctx.get("red_candles_last_10"),

            # ==========================
            # LIVE / POST ENTRY CONTEXT
            # ==========================
            current_trend=pos.current_trend,
            current_direction=pos.current_direction,
            current_momentum=pos.current_momentum,

            direction_t1=pos.direction_t1,
            momentum_t1=pos.momentum_t1,
            pnl_t1=pos.pnl_t1,
            micro_t1=pos.micro_t1,
            direction_5m_t1=pos.direction_5m_t1,

            direction_5m_changed=pos.direction_5m_changed,
            direction_5m_after_entry=pos.direction_5m_after_entry,

            # ==========================
            # TRADE EXCURSION
            # ==========================
            max_favorable_pct=pos.max_favorable_pct,
            max_adverse_pct=pos.max_adverse_pct,
            mae=pos.mae,
            mfe=pos.mfe,
            
            # ==========================
            # LIQUIDITY CONTEXT
            # ==========================
            quote_volume_24h=ctx.get("quote_volume_24h"),
            avg_quote_volume_15m=ctx.get("avg_quote_volume_15m"),
            avg_quote_volume_1h=ctx.get("avg_quote_volume_1h"),
            avg_quote_volume_4h=ctx.get("avg_quote_volume_4h"),
            relative_volume_15m=ctx.get("relative_volume_15m"),
            relative_volume_1h=ctx.get("relative_volume_1h"),
            relative_volume_4h=ctx.get("relative_volume_4h"),
            volume_tier=ctx.get("volume_tier"),
            rvol_tier_15m=ctx.get("rvol_tier_15m"),
            rvol_tier_1h=ctx.get("rvol_tier_1h"),
            rvol_tier_4h=ctx.get("rvol_tier_4h"),

            # ==========================
            # STRATEGY / ROUTER
            # ==========================
            strategy_mode=ctx.get("strategy_name"),
            router_reason=ctx.get("router_reason"),
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
            print(f"\033[91m[SYNC]\033[0m 🚨 ORPHAN POSITION WITHOUT SNAPSHOT | {symbol}")

            side = exchange_state.get("side")
            quantity = abs(float(exchange_state.get("quantity")))

            close_side = "SELL" if side == "LONG" else "BUY"

            try:
                print(
                    f"\033[91m[SYNC]\033[0m "
                    f"Closing orphan position | symbol={symbol} side={side} qty={quantity}"
                )

                self.exchange.close_position(
                    symbol=symbol,
                    side=close_side,
                    quantity=quantity
                )

                self.order_executor.cancel_all(symbol)
                self.snapshot_manager.clear(symbol)

                print(f"\033[91m[SYNC]\033[0m ✅ Orphan closed | {symbol}")

            except Exception as e:
                print(f"\033[91m[SYNC]\033[0m ❌ Failed closing orphan | {symbol} | error={e}")

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
            
            "quote_volume_24h": context.get("quote_volume_24h"),

            "avg_quote_volume_15m": context.get("avg_quote_volume_15m"),
            "avg_quote_volume_1h": context.get("avg_quote_volume_1h"),
            "avg_quote_volume_4h": context.get("avg_quote_volume_4h"),

            "relative_volume_15m": context.get("relative_volume_15m"),
            "relative_volume_1h": context.get("relative_volume_1h"),
            "relative_volume_4h": context.get("relative_volume_4h"),

            "volume_tier": context.get("volume_tier"),

            "rvol_tier_15m": context.get("rvol_tier_15m"),
            "rvol_tier_1h": context.get("rvol_tier_1h"),
            "rvol_tier_4h": context.get("rvol_tier_4h"),
            
            "btc_velocity_15m": context.get("btc_velocity_15m"),
            "btc_velocity_1h": context.get("btc_velocity_1h"),

            "btc_direction_15m": context.get("btc_direction_15m"),
            "btc_direction_1h": context.get("btc_direction_1h"),

            "btc_context_state": context.get("btc_context_state"),
            "btc_context_reason": context.get("btc_context_reason"),
            
            "btc_dist_swing_low_1h_pct": context.get("btc_dist_swing_low_1h_pct"),
            "btc_dist_swing_high_1h_pct": context.get("btc_dist_swing_high_1h_pct"),
            "btc_near_swing_low_1h": context.get("btc_near_swing_low_1h"),
            "btc_near_swing_high_1h": context.get("btc_near_swing_high_1h"),

            "btc_dist_swing_low_4h_pct": context.get("btc_dist_swing_low_4h_pct"),
            "btc_dist_swing_high_4h_pct": context.get("btc_dist_swing_high_4h_pct"),
            "btc_near_swing_low_4h": context.get("btc_near_swing_low_4h"),
            "btc_near_swing_high_4h": context.get("btc_near_swing_high_4h"),

            "btc_dist_swing_low_1d_pct": context.get("btc_dist_swing_low_1d_pct"),
            "btc_dist_swing_high_1d_pct": context.get("btc_dist_swing_high_1d_pct"),
            "btc_near_swing_low_1d": context.get("btc_near_swing_low_1d"),
            "btc_near_swing_high_1d": context.get("btc_near_swing_high_1d"),

            "strategy_name": context.get("strategy_name") or context.get("strategy_mode"),
            "router_reason": context.get("router_reason"),

            "dist_ema50_15m_pct": context.get("dist_ema50_15m_pct"),
            "dist_ema99_15m_pct": context.get("dist_ema99_15m_pct"),
            "dist_ema20_15m_pct": context.get("dist_ema20_15m_pct"),

            "dist_ema50_1h_pct": context.get("dist_ema50_1h_pct"),
            "dist_ema99_1h_pct": context.get("dist_ema99_1h_pct"),
            "dist_ema20_1h_pct": context.get("dist_ema20_1h_pct"),

            "dist_ema50_4h_pct": context.get("dist_ema50_4h_pct"),
            "dist_ema99_4h_pct": context.get("dist_ema99_4h_pct"),
            "dist_ema20_4h_pct": context.get("dist_ema20_4h_pct"),
            
            "swing_low_15m": context.get("swing_low_15m"),
            "swing_high_15m": context.get("swing_high_15m"),
            "dist_swing_low_15m_pct": context.get("dist_swing_low_15m_pct"),
            "dist_swing_high_15m_pct": context.get("dist_swing_high_15m_pct"),
            "near_swing_low_15m": context.get("near_swing_low_15m"),
            "near_swing_high_15m": context.get("near_swing_high_15m"),

            "swing_low_1h": context.get("swing_low_1h"),
            "swing_high_1h": context.get("swing_high_1h"),
            "dist_swing_low_1h_pct": context.get("dist_swing_low_1h_pct"),
            "dist_swing_high_1h_pct": context.get("dist_swing_high_1h_pct"),
            "near_swing_low_1h": context.get("near_swing_low_1h"),
            "near_swing_high_1h": context.get("near_swing_high_1h"),

            "swing_low_4h": context.get("swing_low_4h"),
            "swing_high_4h": context.get("swing_high_4h"),
            "dist_swing_low_4h_pct": context.get("dist_swing_low_4h_pct"),
            "dist_swing_high_4h_pct": context.get("dist_swing_high_4h_pct"),
            "near_swing_low_4h": context.get("near_swing_low_4h"),
            "near_swing_high_4h": context.get("near_swing_high_4h"),
            
            "move_5_bars_pct": context.get("move_5_bars_pct"),
            "move_10_bars_pct": context.get("move_10_bars_pct"),

            "green_candles_last_10": context.get("green_candles_last_10"),
            "red_candles_last_10": context.get("red_candles_last_10"),
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
        exchange_qty = abs(float(exchange_state["quantity"]))

        if abs(exchange_qty - quantity) > 0.0001:

            print(
                f"\033[93m[SYNC]\033[0m ⚠️ Quantity mismatch | "
                f"snapshot={quantity} exchange={exchange_qty} -> using exchange"
            )

            print(
                f"[SYNC DEBUG] "
                f"symbol={symbol} "
                f"snapshot_qty={quantity} "
                f"exchange_qty={exchange_qty}"
            )

            print(exchange_state)

            quantity = exchange_qty

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
                print("\033[94m[SYNC]\033[0m ❌ Failed rebuilding TP/SL. Position NOT restored.")
                return

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
            
            be_moved=bool(position_data.get("be_moved", False)),
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

        if pos == "INVALID_SYMBOL":
            return

        if not pos or float(pos["amount"]) == 0:
            if symbol in self.positions:
                print(f"🔄 Exchange cerró posición | symbol={symbol}")

                self.positions.pop(symbol, None)

                if self.position and self.position.symbol == symbol:
                    self.position = None

                self.snapshot_manager.clear(symbol)