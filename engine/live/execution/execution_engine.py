from dataclasses import dataclass
from typing import Optional
from engine.live.journal.trade_journal import TradeJournal
from datetime import datetime
import random
import time
from engine.live.state_sync import ExchangeStateSync


@dataclass
class Position:
    symbol: str
    side: str
    quantity: float
    entry_price: float
    real_entry: float
    tp: float
    sl: float
    entry_ts: int
    signal_price: float
    signal_ts: int


@dataclass
class Trade:
    side: str
    entry_price: float
    real_entry: float
    exit_price: float
    real_exit: float
    pnl_pct: float
    entry_ts: int
    exit_ts: int
    exit_reason: str


class ExecutionEngine:

    def __init__(self, exchange, position_manager, symbol="BTCUSDT"):
        self.exchange = exchange
        self.position_manager = position_manager
        self.symbol = symbol
        self.position: Optional[Position] = None
        self.trades: list[Trade] = []
        self.journal = TradeJournal()
        self.fees = self.exchange.get_futures_fees()

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

        # ==========================
        # 💰 POSITION SIZING (FIXED)
        # ==========================

        price = float(self.exchange.get_price(plan.symbol))

        usable_balance = balance * 0.90
        buffer = 0.97

        raw_notional = usable_balance * leverage * buffer

        quantity = raw_notional / price
        quantity = round(quantity, 3)

        notional_check = quantity * price
        required_margin = notional_check / leverage

        if required_margin > usable_balance:
            quantity *= 0.98
            quantity = round(quantity, 3)

            notional_check = quantity * price
            required_margin = notional_check / leverage

        if notional_check < 10:
            print(f"❌ Notional too small: {notional_check:.2f}")
            return

        print(f"""
💰 MARGIN DEBUG
Balance           : {balance}
Usable            : {usable_balance}
Price             : {price}
Qty               : {quantity}
Notional real     : {notional_check}
Leverage          : {leverage}
Required margin   : {required_margin}
Check             : {usable_balance >= required_margin}
""")

        if required_margin > usable_balance:
            print("❌ Margin insuficiente")
            return

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

        risk = abs(plan.entry - plan.sl)
        reward = abs(plan.tp - plan.entry)

        if plan.side == "LONG":
            sl_price = real_entry - risk
            tp_price = real_entry + reward
        else:
            sl_price = real_entry + risk
            tp_price = real_entry - reward

        mark_price = float(self.exchange.get_mark_price(plan.symbol))

        if plan.side == "LONG":
            sl_price = min(sl_price, mark_price * 0.998)
            tp_price = max(tp_price, mark_price * 1.002)
        else:
            sl_price = max(sl_price, mark_price * 1.002)
            tp_price = min(tp_price, mark_price * 0.998)

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
            print("ℹ️ No position")
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