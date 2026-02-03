from dataclasses import dataclass
from datetime import datetime

@dataclass
class LiveTrade:
    side: str
    entry: float
    sl: float
    tp: float
    opened_at: datetime
    reason: str

    def hit_tp(self, price: float) -> bool:
        return price <= self.tp if self.side == "SHORT" else price >= self.tp

    def hit_sl(self, price: float) -> bool:
        return price >= self.sl if self.side == "SHORT" else price <= self.sl
