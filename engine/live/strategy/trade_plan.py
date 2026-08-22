from copy import deepcopy
from dataclasses import dataclass, field
from typing import Dict, Any
from models.execution_variant import ExecutionVariant


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
    execution_variant: ExecutionVariant | None = None
    setup_id: str | None = None
    signal_reason: str | None = None
    arm_reason: str | None = None
    execution_reason: str | None = None
    size_fraction: float = 1.0
    aggregate_position_id: str | None = None

    def __post_init__(self):
        self.execution_variant = ExecutionVariant.parse(self.execution_variant)
        self.signal_context = deepcopy(self.signal_context or {})

    @property
    def deduplication_key(self):
        if self.execution_variant is None or self.setup_id is None:
            return None
        return self.symbol, self.execution_variant, self.setup_id

    def to_dict(self):
        data = deepcopy(self.__dict__)
        if self.execution_variant is not None:
            data["execution_variant"] = self.execution_variant.value
        return data

    @classmethod
    def from_dict(cls, data):
        return cls(**deepcopy(data))

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
