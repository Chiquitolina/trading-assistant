from dataclasses import dataclass
from datetime import datetime

@dataclass
class MarketContext:
    symbol: str
    timeframe: str
    price: float
    atr: float
    trend: str
    direction: str
    momentum: str
    timestamp: datetime