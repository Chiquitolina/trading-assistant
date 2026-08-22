from dataclasses import dataclass

from enums.actions import Action
from models.signals import Signal
from models.execution_variant import ExecutionVariant


@dataclass(frozen=True)
class TradeAction:

    action: Action
    signal: Signal

    strategy_name: str
    reason: str
    execution_variant: ExecutionVariant | None = None
    setup_id: str | None = None
    signal_reason: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "execution_variant", ExecutionVariant.parse(self.execution_variant))
