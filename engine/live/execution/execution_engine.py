from dataclasses import dataclass
from typing import Optional


# =========================
# MODELOS
# =========================

@dataclass
class Position:
    side: str
    entry_price: float
    tp: float
    sl: float
    entry_time: int


@dataclass
class Trade:
    side: str
    entry_price: float
    exit_price: float
    pnl: float
    entry_time: int
    exit_time: int
    reason: str


# =========================
# EXECUTION ENGINE (PLAN DRIVEN)
# =========================

class ExecutionEngine:

    def __init__(self):
        self.position: Optional[Position] = None
        self.trades: list[Trade] = []

    # ----------------------------------
    # 👇 NUEVO: ejecuta TradePlan
    # ----------------------------------
    def execute_plan(self, plan):

        if self.position is not None:
            print("⚠️ Position already open. Plan ignored.")
            print('\n')
            return

        if plan.side not in ("LONG", "SHORT"):
            return

        self.position = Position(
            side=plan.side,
            entry_price=float(plan.entry),
            tp=float(plan.tp),
            sl=float(plan.sl),
            entry_time=int(plan.timestamp)
        )

        print(f"""
📈 POSITION OPENED
Side : {plan.side}
Entry: {plan.entry:.2f}
TP   : {plan.tp:.2f}
SL   : {plan.sl:.2f}
""")

    # ----------------------------------
    # Updates de precio (gestión)
    # ----------------------------------
    def on_price_update(self, price: float, timestamp: int):

        if self.position is None:
            return

        pos = self.position

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

    # ----------------------------------
    # CLOSE
    # ----------------------------------
    def _close_position(self, price: float, timestamp: int, reason: str):

        pos = self.position

        if pos.side == "LONG":
            pnl = (price - pos.entry_price) / pos.entry_price
        else:
            pnl = (pos.entry_price - price) / pos.entry_price

        trade = Trade(
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=price,
            pnl=pnl,
            entry_time=pos.entry_time,
            exit_time=timestamp,
            reason=reason
        )

        self.trades.append(trade)
        self.position = None

        print(f"""
❌ POSITION CLOSED
Side : {trade.side}
Exit : {price:.2f}
PnL  : {pnl*100:.2f}%
Reason: {reason}
""")

    # ----------------------------------
    # STATE
    # ----------------------------------
    def get_state(self):

        return {
            "position": self.position,
            "total_trades": len(self.trades)
        }
