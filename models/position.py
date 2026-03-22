from dataclasses import dataclass

@dataclass
class Position:
    symbol: str
    side: str
    quantity: float
    entry_price: float
    real_entry: float
    tp: float
    sl: float
    entry_ts: int
    signal_price: float
    signal_ts: int