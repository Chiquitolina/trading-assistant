from dataclasses import dataclass

@dataclass
class TradePlan:
    symbol: str           # 👈 NUEVO
    quantity: float       # 👈 NUEVO

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

    def pretty(self):
        return f"""
📥 TRADE PLAN
Symbol: {self.symbol}
Side  : {self.side}
Entry : {self.entry:.2f}
TP    : {self.tp:.2f}
SL    : {self.sl:.2f}
ATR   : {self.atr:.2f}
TP%   : {self.tp_pct:.3f}
SL%   : {self.sl_pct:.3f}
Reason: {self.reason}
"""