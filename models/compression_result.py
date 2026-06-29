from dataclasses import dataclass
from models.trade_action import TradeAction


@dataclass
class CompressionResult:
    trade_action: TradeAction
    signal_context: dict