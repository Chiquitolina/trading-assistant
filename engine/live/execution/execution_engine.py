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

    def _place_tp_sl(self, symbol, tp_side, tp_price, sl_side, sl_price, retries=10, delay=0.5):

        tp_price = round(tp_price, 2)
        sl_price = round(sl_price, 2)

        for attempt in range(1, retries + 1):
            try:
                pos = self.exchange.get_position(symbol)

                if pos and float(pos["amount"]) != 0:
                    qty = abs(float(pos["amount"]))

                    self.exchange.place_take_profit(
                        symbol=symbol,
                        side=tp_side,
                        quantity=qty,
                        stop_price=tp_price
                    )

                    self.exchange.place_stop_loss(
                        symbol=symbol,
                        side=sl_side,
                        quantity=qty,
                        stop_price=sl_price
                    )

                    print("✅ TP/SL colocados correctamente")
                    return True

                else:
                    print(f"⚠️ Posición aún no detectada ({attempt}/{retries})...")
                    time.sleep(delay)

            except Exception as e:
                print(f"⚠️ Error al verificar la posición: {e}")
                time.sleep(delay)

        print("❌ No se pudo colocar TP/SL")
        return False

    def execute_plan(self, plan, leverage: int = 1):

        exchange_pos = self.position_manager.sync(plan.symbol)

        if exchange_pos:
            print("⚠️ Position already open (exchange). Plan ignored.\n")
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

        print(f"""
        💰 MARGIN DEBUG
        Balance           : {balance}
        Usable            : {size_data["usable_balance"]}
        Price             : {price}
        Qty               : {quantity}
        Notional real     : {size_data["notional"]}
        Leverage          : {leverage}
        Required margin   : {size_data["required_margin"]}
        Check             : {size_data["usable_balance"] >= size_data["required_margin"]}
        """)

        print(f"✅ Quantity: {quantity}")

        # ==========================
        # 🚀 ORDER
        # ==========================

        order = self.exchange.place_market_order(
            symbol=plan.symbol,
            side=side,
            quantity=quantity
        )

        self.position_manager.sync(plan.symbol)

        if not order:
            print("❌ Order failed")
            return

        time.sleep(1.5)

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
            entry_ts=int(plan.timestamp),
            signal_price=float(plan.signal_price),
            signal_ts=int(plan.signal_ts)
        )

        
        print(f"""
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

        if not exchange_pos and self.position:
            print("🔄 Sync: closed externally")
            self.position = None

        if not self.position:
            return

        pos = self.position

        if pos.side == "LONG":
            if price >= pos.tp:
                self._close_position(price, timestamp, "TP")
            elif price <= pos.sl:
                self._close_position(price, timestamp, "SL")

        else:
            if price <= pos.tp:
                self._close_position(price, timestamp, "TP")
            elif price >= pos.sl:
                self._close_position(price, timestamp, "SL")

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

        real_exit = float(order.get("avgPrice", price)) if order else price

        pnl = ((real_exit - pos.real_entry) / pos.real_entry * 100
               if pos.side == "LONG"
               else (pos.real_entry - real_exit) / pos.real_entry * 100)

        pnl -= self.fees["taker"] * 2

        print(f"❌ CLOSED {reason} | PnL: {round(pnl, 4)}%")

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
            signal_ts=int(time.time() * 1000)
        )

        print("🔁 Position restored")

    def check_exchange_close(self, symbol):

        pos = self.exchange.get_position(symbol)

        if not pos or float(pos["amount"]) == 0:
            if self.position:
                print("🔄 Exchange cerró posición")
                self.position = None