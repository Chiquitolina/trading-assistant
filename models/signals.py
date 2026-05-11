from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Signal:
    signal_price: float
    signal_ts: int

    trend: str
    direction: str
    momentum: str

    momentum_prev1: Optional[str] = None
    momentum_prev2: Optional[str] = None

    momentum_sequence: Optional[list] = None