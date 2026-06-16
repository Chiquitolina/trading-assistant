from enums.actions import Action
from enums.direction import Direction
from models.trade_action import TradeAction


class DirectionStrategy:

    def evaluate(
        self,
        signal,
        previous_direction=None,
        current_position=None
    ):

        current_direction = signal.direction

        #print(
        #    "[DIRECTION BREAKOUT STRATEGY]",
        #    f"prev={previous_direction}",
        #    f"current={current_direction}",
        #)

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
        # NORMALIZE PREVIOUS DIRECTION
        # =========================================
        if isinstance(previous_direction, str):
            previous_direction_value = previous_direction.lower()
        else:
            previous_direction_value = previous_direction.value

        # =========================================
        # RANGE -> UP = LONG
        # =========================================
        if (
            previous_direction_value == Direction.RANGE.value
            and current_direction == Direction.UP
        ):
            return TradeAction(
                action=Action.LONG,
                signal=signal,
                strategy_name="direction_strategy",
                reason="range_breakout_up"
            )

        # =========================================
        # RANGE -> DOWN = SHORT
        # =========================================
        if (
            previous_direction_value == Direction.RANGE.value
            and current_direction == Direction.DOWN
        ):
            return TradeAction(
                action=Action.SHORT,
                signal=signal,
                strategy_name="direction_strategy",
                reason="range_breakout_down"
            )

        # =========================================
        # EVERYTHING ELSE = HOLD
        # =========================================
        return TradeAction(
            action=Action.HOLD,
            signal=signal,
            strategy_name="direction_strategy",
            reason="not_range_breakout"
        )