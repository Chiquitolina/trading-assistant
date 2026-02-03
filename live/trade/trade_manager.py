from datetime import datetime
from live.trade.live_trade import LiveTrade


class TradeManager:
    def __init__(self, buffer, debug=True):
        self.buffer = buffer
        self.active_trade = None
        self.debug = debug

    def on_plan(self, plan):
        if self.active_trade:
            if self.debug:
                print("⏭️ TRADE IGNORED (already active)")
            return

        trade = LiveTrade(
            side=plan.side,
            entry=float(plan.entry),
            sl=float(plan.sl),
            tp=float(plan.tp),
            opened_at=datetime.utcnow(),
            reason=plan.reason
        )

        self.active_trade = trade

        print(
            f"🟢 TRADE OPENED [{trade.side}] "
            f"entry={trade.entry} sl={trade.sl} tp={trade.tp}"
        )

    def on_price(self, price: float):
        if not self.active_trade:
            return

        trade = self.active_trade

        if trade.hit_tp(price):
            self._close_trade(price, "TP")
        elif trade.hit_sl(price):
            self._close_trade(price, "SL")

    def _close_trade(self, price: float, reason: str):
        trade = self.active_trade
        self.active_trade = None

        pnl = (
            trade.entry - price
            if trade.side == "SHORT"
            else price - trade.entry
        )

        print(
            f"🏁 TRADE CLOSED [{reason}] "
            f"exit={price} pnl={round(pnl, 2)}"
        )
