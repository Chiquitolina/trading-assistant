from dataclasses import dataclass
from typing import Optional
from engine.live.journal.trade_journal import TradeJournal
from datetime import datetime
import random


# =========================
# MODELOS
# =========================

@dataclass
class Position:
    side: str
    entry_price: float
    tp: float
    sl: float
    entry_ts: int          # ms
    signal_price: float
    signal_ts: int         # ms


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

    def _apply_slippage(self, price: float, side: str, is_entry: bool = True):
        """
        Aplica slippage aleatorio al precio.
        is_entry=True -> entrada
        is_entry=False -> salida
        """
        slippage_pct = random.uniform(0.01, 0.05) / 100  # 0.01% a 0.05%
        slippage = price * slippage_pct

        if side == "LONG":
            return price + slippage if is_entry else price - slippage
        if side == "SHORT":
            return price - slippage if is_entry else price + slippage

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

        # -------------------------------
        # Aplicar slippage a la entrada
        # -------------------------------
        entry_price = self._apply_slippage(plan.entry, plan.side, is_entry=True)

        self.position = Position(
            side=plan.side,
            entry_price=float(entry_price),
            tp=float(plan.tp),
            sl=float(plan.sl),
            entry_ts=int(plan.timestamp),
            signal_price=float(plan.signal_price),
            signal_ts=int(plan.signal_ts)
        )

        print(f"""
📈 POSITION OPENED
Side         : {plan.side}
Signal Price : {plan.signal_price:.2f}
Entry        : {entry_price:.2f}  ← slippage aplicado
TP           : {plan.tp:.2f}
SL           : {plan.sl:.2f}
""")

    # ----------------------------------
    # CLOSE POSITION
    # ----------------------------------
    def _close_position(self, price: float, timestamp: int, reason: str):

        pos = self.position
        if pos is None:
            return

        # -------------------------------
        # Aplicar slippage a la salida
        # -------------------------------
        price = self._apply_slippage(price, pos.side, is_entry=False)

        # --- PnL %
        if pos.side == "LONG":
            pnl_pct = ((price - pos.entry_price) / pos.entry_price) * 100
        else:
            pnl_pct = ((pos.entry_price - price) / pos.entry_price) * 100

        # --- FEES
        fees = 0.08  # 0.08%
        pnl_gross = pnl_pct
        pnl_net = pnl_gross - fees

        # --- Round
        pnl_pct   = round(pnl_pct, 4)
        pnl_gross = round(pnl_gross, 4)
        pnl_net   = round(pnl_net, 4)
        fees      = round(fees, 2)

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
Side        : {trade.side}
Signal      : {pos.signal_price:.2f}
Entry       : {pos.entry_price:.2f}
Exit        : {price:.2f}  ← slippage aplicado
PnL Gross   : {pnl_gross:.4f}%
PnL Net     : {pnl_net:.4f}%
Reason      : {reason}
""")

        # =========================
        # GUARDAR EN ISO
        # =========================
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
                exit_price=price,
                tp=pos.tp,
                sl=pos.sl,
                pnl=pnl_net,
                pnl_gross=pnl_gross,
                fees=fees,
                exit_reason=reason,
            )
        finally:
            self.position = None

    # ----------------------------------
    # STATE
    # ----------------------------------
    def get_state(self):
        return {
            "position": self.position,
            "total_trades": len(self.trades)
        }