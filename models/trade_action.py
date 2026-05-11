from dataclasses import dataclass

from enums.actions import Action
from models.signals import Signal


@dataclass(frozen=True)
class TradeAction:

    action: Action
    signal: Signal

    strategy_name: str
    reason: str