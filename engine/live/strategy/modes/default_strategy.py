from enums.actions import Action

from models.trade_action import TradeAction

from signals.strategy.entries import (
    LONG_RULES,
    SHORT_RULES
)


class DefaultStrategy:

    def evaluate(self, signal, previous_direction=None,):

        combo = (
            signal.trend,
            signal.direction,
            signal.momentum
        )

        if combo in LONG_RULES:

            return TradeAction(
                action=Action.LONG,
                signal=signal,
                strategy_name="default_strategy",
                reason="long_rule_match"
            )

        if combo in SHORT_RULES:

            return TradeAction(
                action=Action.SHORT,
                signal=signal,
                strategy_name="default_strategy",
                reason="short_rule_match"
            )

        return TradeAction(
            action=Action.HOLD,
            signal=signal,
            strategy_name="default_strategy",
            reason="no_setup"
        )