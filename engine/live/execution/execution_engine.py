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

    def __init__(self, exchange):
        self.exchange = exchange
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
        
         # 🔧 REDONDEO PARA BINANCE
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

        print("❌ No se pudo colocar TP/SL porque la posición nunca se registró")
        return False

    def execute_plan(self, plan, leverage: int = 1):

        if self.position is not None:
            print("⚠️ Position already open. Plan ignored.\n")
            return

        # limpiar órdenes viejas SOLO si vamos a abrir nueva posición
        try:
            self.exchange.cancel_all_orders(plan.symbol)
        except Exception as e:
            print(f"⚠️ Failed to clean orders before entry: {e}")

        if plan.side not in ("LONG", "SHORT"):
            return

        side = "BUY" if plan.side == "LONG" else "SELL"

        balance = float(self.exchange.get_balance())

        if balance <= 0:
            print("❌ No hay balance suficiente para abrir posición")
            return

        leverage_resp = self.exchange.set_leverage(plan.symbol, leverage)

        if leverage_resp is None:
            print(f"❌ Error setting leverage {leverage}x for {plan.symbol}")
            return

        print(f"✅ Leverage set to {leverage}x for {plan.symbol}")

        usable_balance = balance * 0.90
        notional = usable_balance * leverage

        quantity = notional / plan.entry
        quantity = round(quantity, 3)

        notional_check = quantity * plan.entry

        if notional_check < 10:
            print(f"❌ Notional too small: {notional_check:.2f} USDT")
            return

        if notional_check > balance * leverage:
            print(f"❌ Notional exceeds available margin")
            return

        print(f"✅ Quantity calculada: {quantity:.3f} ({notional_check:.2f} USDT notional)")

        order = self.exchange.place_market_order(
            symbol=plan.symbol,
            side=side,
            quantity=quantity
        )
                
        if not order:
            print("❌ Order failed. Position not opened.")
            return

        time.sleep(1)

        time.sleep(0.5)

        pos = self.exchange.get_position(plan.symbol)
        
        print(pos)

        if pos and float(pos["amount"]) != 0:
            real_entry = float(pos["entry_price"])
        else:
            real_entry = plan.entry

        print(f"""
    DEBUG ORDER
    Side        : {plan.side}
    Signal Entry: {plan.entry}
    Real Entry  : {real_entry}
    Signal TP   : {plan.tp}
    Signal SL   : {plan.sl}
    """)

        # ------------------------
        # Recalcular TP / SL
        # ------------------------
        risk = abs(plan.entry - plan.sl)
        reward = abs(plan.tp - plan.entry)

        if plan.side == "LONG":
            sl_price = real_entry - risk
            tp_price = real_entry + reward
        else:
            sl_price = real_entry + risk
            tp_price = real_entry - reward

        mark_price = float(self.exchange.get_mark_price(plan.symbol))

        # ------------------------
        # Validación contra precio actual
        # ------------------------

        if plan.side == "SHORT":

            if sl_price <= mark_price:
                sl_price = mark_price * 1.002

            if tp_price >= mark_price:
                tp_price = mark_price * 0.998

        elif plan.side == "LONG":

            if sl_price >= mark_price:
                sl_price = mark_price * 0.998

            if tp_price <= mark_price:
                tp_price = mark_price * 1.002

        print(f"""
    PRICE VALIDATION
    Mark Price : {mark_price}
    TP Final   : {tp_price}
    SL Final   : {sl_price}
    """)

        tp_side = "SELL" if side == "BUY" else "BUY"
        sl_side = tp_side

        self._place_tp_sl(
            symbol=plan.symbol,
            tp_side=tp_side,
            tp_price=tp_price,
            sl_side=sl_side,
            sl_price=sl_price
        )

        self.position = Position(
            symbol=plan.symbol,
            side=plan.side,
            quantity=quantity,
            entry_price=self._apply_slippage(plan.entry, plan.side, True),
            real_entry=real_entry,
            tp=float(tp_price),
            sl=float(sl_price),
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

    def on_price_update(self, price: float, timestamp: int):

        if self.position is None:
            return

        pos = self.position
        timestamp = int(timestamp)

        if pos.side == "LONG":

            if price >= pos.tp:
                self._close_position(price, timestamp, "TP")

            elif price <= pos.sl:
                self._close_position(price, timestamp, "SL")

        elif pos.side == "SHORT":

            if price <= pos.tp:
                self._close_position(price, timestamp, "TP")

            elif price >= pos.sl:
                self._close_position(price, timestamp, "SL")

    def _close_position(self, price: float, timestamp: int, reason: str):

        pos = self.position

        if pos is None:
            return
        
        pos_exchange = self.exchange.get_position(pos.symbol)

        if not pos_exchange or float(pos_exchange["amount"]) == 0:
            print("⚠️ Position already closed on exchange")
            self.position = None
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
            real_exit = price

        price_with_slippage = self._apply_slippage(price, pos.side, is_entry=False)

        pnl_pct = ((real_exit - pos.real_entry) / pos.real_entry * 100
                if pos.side == "LONG"
                else (pos.real_entry - real_exit) / pos.real_entry * 100)

        fees = self.fees["taker"] * 2
        pnl_gross = pnl_pct
        pnl_net = pnl_gross - fees

        pnl_pct = round(pnl_pct, 4)
        pnl_gross = round(pnl_gross, 4)
        pnl_net = round(pnl_net, 4)
        fees = round(fees, 2)

        trade = Trade(
            side=pos.side,
            entry_price=pos.entry_price,
            real_entry=pos.real_entry,
            exit_price=price_with_slippage,
            real_exit=real_exit,
            pnl_pct=pnl_pct,
            entry_ts=pos.entry_ts,
            exit_ts=timestamp,
            exit_reason=reason
        )

        self.trades.append(trade)

        print(f"""
❌ POSITION CLOSED
Side         : {trade.side}
Entry (bot)  : {pos.entry_price:.2f}
Entry (real) : {pos.real_entry:.2f}
Exit  (bot)  : {price_with_slippage:.2f}
Exit  (real) : {real_exit:.2f}
PnL Net      : {pnl_net:.4f}%
Reason       : {reason}
""")

        signal_iso = datetime.utcfromtimestamp(pos.signal_ts / 1000).isoformat()
        entry_iso = datetime.utcfromtimestamp(pos.entry_ts / 1000).isoformat()
        exit_iso = datetime.utcfromtimestamp(timestamp / 1000).isoformat()

        try:

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
            )

        finally:

            try:

                # verificar si todavía estamos cerrando la misma posición
                if self.position and self.position.entry_ts == pos.entry_ts:

                    self.exchange.cancel_all_orders(pos.symbol)
                    self.position = None

            except Exception as e:
                print(f"⚠️ Failed to cancel orders: {e}")

    def get_state(self):

        return {
            "position": self.position,
            "total_trades": len(self.trades)
        }
        
    def restore_state(self, symbol):

        sync = ExchangeStateSync(self.exchange)

        state = sync.restore_position_state(symbol)

        if not state:
            print("ℹ️ No position to restore")
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

        print("🔁 Position restored from exchange")
        
        # Si no hay TP/SL restaurados, recalcular
        
        if not self.position.tp or not self.position.sl:

            print("⚠️ Restored position without TP/SL. Recalculating...")

            entry = self.position.real_entry
            side = self.position.side

            risk_pct = 0.005   # 0.5%
            reward_pct = 0.01  # 1%

            if side == "LONG":
                sl_price = entry * (1 - risk_pct)
                tp_price = entry * (1 + reward_pct)

            else:
                sl_price = entry * (1 + risk_pct)
                tp_price = entry * (1 - reward_pct)

            tp_side = "SELL" if side == "LONG" else "BUY"

            self._place_tp_sl(
                symbol=self.position.symbol,
                tp_side=tp_side,
                tp_price=tp_price,
                sl_side=tp_side,
                sl_price=sl_price
            )

            self.position.tp = tp_price
            self.position.sl = sl_price

            print(f"""
        🔧 TP/SL recalculated
        Entry : {entry}
        TP    : {tp_price}
        SL    : {sl_price}
        """)