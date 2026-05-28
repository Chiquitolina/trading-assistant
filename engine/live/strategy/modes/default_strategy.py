from enums.actions import Action
from models.trade_action import TradeAction

from signals.strategy.entries import long_setup, short_setup


class DefaultStrategy:

    def __init__(self, entry_rules="standard"):
        self.entry_rules = entry_rules

    def evaluate(
        self,
        signal,
        previous_direction=None,
        current_position=None
    ):

        if current_position:
            return TradeAction(
                action=Action.HOLD,
                signal=signal,
                strategy_name="default_strategy",
                reason="position_already_open"
            )

        long_ok = long_setup(
            signal.trend,
            signal.direction,
            signal.momentum,
            entry_rules=self.entry_rules
        )

        short_ok = short_setup(
            signal.trend,
            signal.direction,
            signal.momentum,
            entry_rules=self.entry_rules
        )

        if long_ok:
            return TradeAction(
                action=Action.LONG,
                signal=signal,
                strategy_name="default_strategy",
                reason=f"long_rule_match_{self.entry_rules}"
            )

        if short_ok:
            return TradeAction(
                action=Action.SHORT,
                signal=signal,
                strategy_name="default_strategy",
                reason=f"short_rule_match_{self.entry_rules}"
            )

        return TradeAction(
            action=Action.HOLD,
            signal=signal,
            strategy_name="default_strategy",
            reason=f"no_setup_{self.entry_rules}"
        )