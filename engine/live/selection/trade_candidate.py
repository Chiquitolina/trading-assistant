from dataclasses import dataclass, field
from typing import Any, Optional

from engine.live.strategy.trade_plan import TradePlan
from models.trade_action import TradeAction


@dataclass
class TradeCandidate:
    """
    Representa un TradePlan que todavía no fue autorizado
    para pasar a ejecución.

    El TradePlan contiene la operación.
    El TradeCandidate contiene el estado de selección.
    """

    plan: TradePlan
    trade_action: TradeAction
    window_id: int

    score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)

    rank: Optional[int] = None

    eligible: bool = True
    selected: bool = False
    rejection_reason: Optional[str] = None

    @property
    def symbol(self) -> str:
        return self.plan.symbol

    @property
    def side(self) -> str:
        return self.plan.side

    def reject(self, reason: str) -> None:
        self.eligible = False
        self.selected = False
        self.rejection_reason = reason

    def mark_selected(self, rank: int) -> None:
        self.rank = rank
        self.selected = True
        self.rejection_reason = None