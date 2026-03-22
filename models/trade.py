from dataclasses import dataclass

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