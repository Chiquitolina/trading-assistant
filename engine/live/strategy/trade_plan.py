from dataclasses import dataclass, field
from typing import Dict, Any


def fmt_price(price):
    if price is None:
        return "N/A"

    price = float(price)

    if price >= 100:
        return f"{price:.2f}"
    elif price >= 1:
        return f"{price:.4f}"
    else:
        return f"{price:.8f}"


@dataclass
class TradePlan:
    symbol: str
    quantity: float

    side: str
    entry: float
    sl: float
    tp: float
    sl_pct: float
    tp_pct: float
    atr: float
    atr_pct: float
    timestamp: int
    reason: str

    signal_price: float
    signal_ts: int

    # contexto de señal
    signal_context: Dict[str, Any] = field(default_factory=dict)

    max_hold_candles: int = 10

    def pretty(self):
        ctx = self.signal_context or {}

        return f"""
\n\033[94m[ENTRY PLANNER]\033[0m
📥 TRADE PLAN
Symbol : {self.symbol}
Side   : {self.side}

🧠 SIGNAL
TS     : {self.signal_ts}
Price  : {fmt_price(self.signal_price)}
Dir    : {ctx.get("direction")}
Trend  : {ctx.get("trend")}
Mom    : {ctx.get("momentum")}
ATR    : {fmt_price(ctx.get("atr"))}
ATR%   : {ctx.get("atr_pct")}

📦 EXECUTION
Entry  : {fmt_price(self.entry)}
TP     : {fmt_price(self.tp)}
SL     : {fmt_price(self.sl)}

📊 RISK
TP%    : {self.tp_pct:.3f}
SL%    : {self.sl_pct:.3f}

Reason : {self.reason}
"""