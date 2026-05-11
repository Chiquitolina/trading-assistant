from enums.actions import Action
from enums.direction import Direction
from models.trade_action import TradeAction


class DirectionStrategy:

    def evaluate(
        self,
        signal,
        previous_direction=None,
    ):

        current_direction = signal.direction.value

        print(
            "[DIRECTION STRATEGY]",
            f"prev={previous_direction}",
            f"current={current_direction}",
        )

        # =========================================
        # FIRST BOOT / NO PREVIOUS SIGNAL
        # =========================================

        if previous_direction is None:

            return TradeAction(
                action=Action.HOLD,
                signal=signal,
                strategy_name="direction_strategy",
                reason="no_previous_direction"
            )

        # =========================================
        # SAME DIRECTION → NO TRADE
        # =========================================

        if current_direction == previous_direction:

            return TradeAction(
                action=Action.HOLD,
                signal=signal,
                strategy_name="direction_strategy",
                reason="same_direction_as_previous"
            )

        # =========================================
        # FLIP TO UP
        # =========================================

        if signal.direction == Direction.UP:

            return TradeAction(
                action=Action.LONG,
                signal=signal,
                strategy_name="direction_strategy",
                reason="direction_flip_up"
            )

        # =========================================
        # FLIP TO DOWN
        # =========================================

        if signal.direction == Direction.DOWN:

            return TradeAction(
                action=Action.SHORT,
                signal=signal,
                strategy_name="direction_strategy",
                reason="direction_flip_down"
            )

        # =========================================
        # NEUTRAL
        # =========================================

        return TradeAction(
            action=Action.HOLD,
            signal=signal,
            strategy_name="direction_strategy",
            reason="neutral_direction"
        )