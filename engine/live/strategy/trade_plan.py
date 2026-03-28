from dataclasses import dataclass, field
from typing import Dict, Any

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
    timestamp: int
    reason: str
    
    signal_price: float
    signal_ts: int
    
    # 🆕 contexto de señal (seguro y escalable)
    signal_context: Dict[str, Any] = field(default_factory=dict)

    def pretty(self):
        ctx = self.signal_context or {}

        return f"""
\n\033[94m[ENTRY PLANNER]\033[0m
📥 TRADE PLAN
Symbol : {self.symbol}
Side   : {self.side}

🧠 SIGNAL
TS     : {self.signal_ts}
Price  : {self.signal_price}
Dir    : {ctx.get("direction")}
Trend  : {ctx.get("trend")}
Mom    : {ctx.get("momentum")}
ATR    : {ctx.get("atr")}

📦 EXECUTION
Entry  : {self.entry:.2f}
TP     : {self.tp:.2f}
SL     : {self.sl:.2f}

📊 RISK
TP%    : {self.tp_pct:.3f}
SL%    : {self.sl_pct:.3f}

Reason : {self.reason}
"""