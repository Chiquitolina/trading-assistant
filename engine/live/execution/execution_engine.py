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
class ExecutionEngine:

    def __init__(self, exchange, position_manager, symbol="BTCUSDT"):
        self.exchange = exchange
        self.position_manager = position_manager
        self.symbol = symbol
        self.position: Optional[Position] = None
        self.trades: list[Trade] = []
        self.journal = TradeJournal()
        self.fees = self.exchange.get_futures_fees()
        self.position_sizer = PositionSizer()
        self.risk_manager = RiskManager()

    def _apply_slippage(self, price: float, side: str, is_entry: bool = True):
        slippage_pct = random.uniform(0.01, 0.05) / 100
        slippage = price * slippage_pct

        if side == "LONG":
            return price + slippage if is_entry else price - slippage

        if side == "SHORT":
            return price - slippage if is_entry else price + slippage
        
    def _get_last_close_fill(self, symbol, side, quantity, limit=20):
        fills = self.exchange.get_recent_fills(symbol, limit=limit)

        if not fills:
            return None

        exit_side = "SELL" if side == "LONG" else "BUY"

        candidates = [f for f in fills if f["side"] == exit_side]

        if not candidates:
            return None

        grouped = {}
        for f in candidates:
            oid = f["orderId"]
            grouped.setdefault(oid, []).append(f)

        # elegir la orden más reciente según time
        latest_order_id = max(
            grouped.keys(),
            key=lambda oid: max(int(x["time"]) for x in grouped[oid])
        )

        selected = grouped[latest_order_id]

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
        
    def _handle_external_close(self, price, timestamp):
        pos = self.position
        if not pos:
            return

        print("⚠️ Detectado cierre externo (TP/SL o manual)")

        # ==========================
        # Detectar reason
        # ==========================
        if pos.side == "LONG":
            if price >= pos.tp:
                reason = "TP"
            elif price <= pos.sl:
                reason = "SL"
            else:
                reason = "UNKNOWN"
        else:
            if price <= pos.tp:
                reason = "TP"
            elif price >= pos.sl:
                reason = "SL"
            else:
                reason = "UNKNOWN"

        print(f"📊 External close reason: {reason}")

        # ==========================
        # Buscar fill real de cierre
        # ==========================
        close_fill = self._get_last_close_fill(
            symbol=pos.symbol,
            side=pos.side,
            quantity=pos.quantity,
            limit=20
        )

        if close_fill:
            real_exit = float(close_fill["price"])
            exit_order_id = int(close_fill["orderId"])
            fees = round(self.fees["taker"] * 2, 4)
            exchange_realized_pnl = round(float(close_fill["realizedPnl"]), 4)

            print(f"✅ Close fill encontrado | orderId={exit_order_id} | real_exit={real_exit}")
        else:
            exit_order_id = None
            exchange_realized_pnl = None

            # fallback
            if reason == "TP":
                real_exit = pos.tp
            elif reason == "SL":
                real_exit = pos.sl
            else:
                real_exit = price

            fees = round(self.fees["taker"] * 2, 4)

            print("⚠️ No se encontró close fill, usando fallback local")

        # guardar exit order id en la posición si querés rastrearlo
        pos.exit_order_id = exit_order_id

        # ==========================
        # PnL calculado por el bot
        # ==========================
        pnl_pct = (
            (real_exit - pos.real_entry) / pos.real_entry * 100
            if pos.side == "LONG"
            else (pos.real_entry - real_exit) / pos.real_entry * 100
        )

        pnl_pct = round(pnl_pct, 4)
        pnl_net = round(pnl_pct - fees, 4)

        # ==========================
        # DEBUG
        # ==========================
        print(f"""
    📊 EXTERNAL CLOSE DEBUG
    Entry              : {pos.real_entry}
    Exit market price  : {price}
    Real exit fill     : {real_exit}
    TP                 : {pos.tp}
    SL                 : {pos.sl}
    Reason             : {reason}
    Exit order id      : {exit_order_id}
    Exchange realized  : {exchange_realized_pnl}
    Fees               : {fees}
    PnL gross          : {pnl_pct}%
    PnL net            : {pnl_net}%
    """)

        print(f"❌ CLOSED (external {reason}) | PnL: {pnl_net}%")

        # ==========================
        # Contexto de signal
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
            fees=fees,
            exit_reason=reason,
            signal_trend=ctx.get("trend"),
            signal_direction=ctx.get("direction"),
            signal_momentum=ctx.get("momentum"),
            signal_atr=ctx.get("atr"),
        )

        self.position = None
        
    def _place_tp_sl(self, symbol, quantity, tp_side, tp_price, sl_side, sl_price):
        try:
            raw_tp = tp_price
            raw_sl = sl_price

            tp_price = self.exchange.normalize_price(symbol, tp_price)
            sl_price = self.exchange.normalize_price(symbol, sl_price)

            print(
                f"📌 TP/SL debug | "
                f"symbol={symbol} | qty={quantity} | "
                f"raw_tp={raw_tp} -> tp={tp_price} ({type(tp_price)}) | "
                f"raw_sl={raw_sl} -> sl={sl_price} ({type(sl_price)})"
            )

            self.exchange.place_take_profit(
                symbol=symbol,
                side=tp_side,
                quantity=quantity,
                stop_price=tp_price
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

    def execute_plan(self, plan, leverage: int = 1):

        exchange_pos = self.position_manager.sync(plan.symbol)

        if exchange_pos:
            print("\033[94m[EXECUTION ENGINE]\033[0m ⚠️ Position already open (exchange). Plan ignored.\n")
            return

        try:
            self.exchange.cancel_all_orders(plan.symbol)
        except Exception as e:
            print(f"⚠️ Failed to clean orders: {e}")

        if plan.side not in ("LONG", "SHORT"):
            return

        side = "BUY" if plan.side == "LONG" else "SELL"

        balance = float(self.exchange.get_balance())

        if balance <= 0:
            print("❌ No balance")
            return

        if self.exchange.set_leverage(plan.symbol, leverage) is None:
            print("❌ Error setting leverage")
            return

        print(f"✅ Leverage set to {leverage}x")

        price = float(self.exchange.get_price(plan.symbol))

        size_data = self.position_sizer.calculate(
            balance=balance,
            price=price,
            leverage=leverage
        )

        is_valid, msg = self.position_sizer.validate(size_data)

        if not is_valid:
            print(msg)
            return

        quantity = size_data["quantity"]

#        print(f"""
#        💰 MARGIN DEBUG
#        Balance           : {balance}
#        Usable            : {size_data["usable_balance"]}
#        Price             : {price}
#        Qty               : {quantity}
#        Notional real     : {size_data["notional"]}
#        Leverage          : {leverage}
#        Required margin   : {size_data["required_margin"]}
#        Check             : {size_data["usable_balance"] >= size_data["required_margin"]}
#        """)

#       print(f"✅ Quantity: {quantity}")

        # ==========================
        # 🚀 ORDER
        # ==========================

        order = self.exchange.place_market_order(
            symbol=plan.symbol,
            side=side,
            quantity=quantity
        )

        # 💣 CASO CRÍTICO: timeout Binance
        if order and order.get("status") == "UNKNOWN":

            print("🔎 Orden en estado desconocido, sincronizando...")

            time.sleep(2)

            exchange_pos = self.position_manager.sync(plan.symbol)

            if exchange_pos:
                print("✅ Orden SI se ejecutó (detectado por sync)")
            else:
                print("❌ Orden NO ejecutada")
                return

        # ❌ fallo real (no UNKNOWN)
        elif not order:
            print("❌ Order failed")
            return

        # 🔥 SIEMPRE sync después del order (CLAVE)
        exchange_pos = self.position_manager.sync(plan.symbol)

        if not exchange_pos:
            print("❌ No hay posición después del order, abortando")
            return

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

        self._place_tp_sl(
            symbol=plan.symbol,
            quantity=quantity,  # 🔥 ESTE ES EL CAMBIO
            tp_side=tp_side,
            tp_price=tp_price,
            sl_side=tp_side,
            sl_price=sl_price
        )

        # ==========================
        # 📈 SAVE POSITION
        # ==========================

        self.position = Position(
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
            
            signal_context=plan.signal_context
            
        )

        
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

    # ==========================
    # 🔄 PRICE UPDATE
    # ==========================

    def on_price_update(self, price: float, timestamp: int):

        exchange_pos = self.position_manager.sync(self.symbol)

        # 🔥 cierre externo (TP/SL)
        if not exchange_pos and self.position:
            self._handle_external_close(price, timestamp)
            return

        if not self.position:
            return

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
            signal_ts=signal_iso,
            signal_price=pos.signal_price,
            entry_ts=entry_iso,
            exit_ts=exit_iso,
            side=pos.side,
            entry=pos.entry_price,
            real_entry=pos.real_entry,
            exit_price=price_with_slippage,
            real_exit=real_exit,
            tp=pos.tp,
            sl=pos.sl,
            pnl=pnl_net,
            pnl_gross=pnl_gross,
            fees=fees,
            exit_reason=reason,
            
             # 🧠 CONTEXTO
            signal_trend=ctx.get("trend"),
            signal_direction=ctx.get("direction"),
            signal_momentum=ctx.get("momentum"),
            signal_atr=ctx.get("atr")
            )

        self.position = None

    def get_state(self):
        return {
            "position": self.position,
            "total_trades": len(self.trades)
        }

    def restore_state(self, symbol):

        sync = ExchangeStateSync(self.exchange)
        state = sync.restore_position_state(symbol)

        if not state:
            print("ℹ️ No position to restore.")
            return

        self.position = Position(
            symbol=state["symbol"],
            side=state["side"],
            quantity=state["quantity"],
            entry_price=state["entry_price"],
            real_entry=state["entry_price"],
            tp=state["tp"],
            sl=state["sl"],
            entry_ts=int(time.time() * 1000),
            signal_price=state["entry_price"],
            signal_ts=int(time.time() * 1000),
            signal_context=None
        )

        print("\033[94m[EXCHANGE]\033[0m 🔁 Position restored")

    def check_exchange_close(self, symbol):

        pos = self.exchange.get_position(symbol)

        if not pos or float(pos["amount"]) == 0:
            if self.position:
                print("🔄 Exchange cerró posición")
                self.position = None