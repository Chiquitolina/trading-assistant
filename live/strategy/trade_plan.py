# strategy/trade_plan.py
from dataclasses import dataclass

@dataclass
class TradePlan:
    side: str
    entry: float
    sl: float
    tp: float
    sl_pct: float
    tp_pct: float
    atr: float
    reason: str