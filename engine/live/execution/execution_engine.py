from dataclasses import dataclass
from typing import Optional
from engine.live.journal.trade_journal import TradeJournal
from datetime import datetime


# =========================
# MODELOS
# =========================

@dataclass
class Position:
    side: str
    entry_price: float
    tp: float
    sl: float
    entry_ts: int


@dataclass
class Trade:
    side: str
    entry_price: float
    exit_price: float
    pnl_pct: float
    entry_ts: int
    exit_ts: int
    exit_reason: str


# =========================
# EXECUTION ENGINE
# =========================

class ExecutionEngine:

    def __init__(self):
        self.position: Optional[Position] = None
        self.trades: list[Trade] = []
        self.journal = TradeJournal()

    # ----------------------------------
    # EXECUTE PLAN
    # ----------------------------------
    def execute_plan(self, plan):

        if self.position is not None:
            print("⚠️ Position already open. Plan ignored.\n")
            return

        if plan.side not in ("LONG", "SHORT"):
            return

        self.position = Position(
            side=plan.side,
            entry_price=float(plan.entry),
            tp=float(plan.tp),
            sl=float(plan.sl),
            entry_ts=int(plan.timestamp)
        )

        print(f"""
📈 POSITION OPENED
Side : {plan.side}
Entry: {plan.entry:.2f}
TP   : {plan.tp:.2f}
SL   : {plan.sl:.2f}
""")

    # ----------------------------------
    # CANDLE UPDATE
    # ----------------------------------
    def on_candle_update(self, high: float, low: float, timestamp: int):

        if self.position is None:
            return

        pos = self.position

        if pos.side == "LONG":
            if low <= pos.sl:
                self._close_position(pos.sl, timestamp, "SL")
            elif high >= pos.tp:
                self._close_position(pos.tp, timestamp, "TP")

        elif pos.side == "SHORT":
            if high >= pos.sl:
                self._close_position(pos.sl, timestamp, "SL")
            elif low <= pos.tp:
                self._close_position(pos.tp, timestamp, "TP")

    # ----------------------------------
    # PRICE UPDATE (tick)
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
    # CLOSE POSITION
    # ----------------------------------
    def _close_position(self, price: float, timestamp: int, reason: str):

        pos = self.position
        if pos is None:
            return

        # --- PnL % (PORCENTAJE REAL)
        if pos.side == "LONG":
            pnl_pct = ((price - pos.entry_price) / pos.entry_price) * 100
        else:
            pnl_pct = ((pos.entry_price - price) / pos.entry_price) * 100

        trade = Trade(
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=price,
            pnl_pct=pnl_pct,
            entry_ts=pos.entry_ts,
            exit_ts=timestamp,
            exit_reason=reason
        )

        self.trades.append(trade)

        print(f"""
❌ POSITION CLOSED
Side   : {trade.side}
Exit   : {price:.2f}
PnL    : {pnl_pct:.2f}%
Reason : {reason}
""")

        # --- timestamps ISO
        entry_iso = datetime.utcfromtimestamp(pos.entry_ts / 1000).isoformat()
        exit_iso  = datetime.utcfromtimestamp(timestamp / 1000).isoformat()

        # --- FEES (%)
        fees = 0.08   # 0.08%

        # --- PNL
        pnl_gross = pnl_pct
        pnl_net = pnl_gross - fees

        # --- ROUND (solo presentación / CSV)
        pnl_pct   = round(pnl_pct, 4)
        pnl_gross = round(pnl_gross, 4)
        pnl_net   = round(pnl_net, 4)
        fees      = round(fees, 2)

        # --- JOURNAL
        try:
            self.journal.log_trade(
                entry_ts=entry_iso,
                exit_ts=exit_iso,
                side=pos.side,
                entry=pos.entry_price,
                exit_price=price,
                tp=pos.tp,
                sl=pos.sl,
                pnl_pct=pnl_pct,
                pnl=pnl_net,
                pnl_gross=pnl_gross,
                fees=fees,
                exit_reason=reason
            )
        finally:
            # 🔒 LIMPIAR ESTADO SIEMPRE
            self.position = None

    # ----------------------------------
    # STATE
    # ----------------------------------
    def get_state(self):
        return {
            "position": self.position,
            "total_trades": len(self.trades)
        }