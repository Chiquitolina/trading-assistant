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
        # High conviction LONG setups
        # =========================================
        if (
            previous_direction_value == Direction.RANGE.value
            and current_direction == Direction.UP
        ):
            ctx = signal.context or {}

            def to_float(value):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None

            dist_low_15m = to_float(ctx.get("dist_swing_low_15m_pct"))
            dist_high_15m = to_float(ctx.get("dist_swing_high_15m_pct"))
            dist_high_4h = to_float(ctx.get("dist_swing_high_4h_pct"))

            # LONG dist swing high 15m 1%-2%
            if dist_high_15m is not None and 1 <= dist_high_15m < 2:
                return TradeAction(
                    action=Action.LONG,
                    signal=signal,
                    strategy_name="direction_strategy",
                    reason="long_high_15m_1_2"
                )

            # LONG dist swing low 15m 4%-8%
            if dist_low_15m is not None and 4 <= dist_low_15m < 8:
                return TradeAction(
                    action=Action.LONG,
                    signal=signal,
                    strategy_name="direction_strategy",
                    reason="long_low_15m_4_8"
                )

            # LONG dist swing high 4h >8%
            if dist_high_4h is not None and dist_high_4h >= 8:
                return TradeAction(
                    action=Action.LONG,
                    signal=signal,
                    strategy_name="direction_strategy",
                    reason="long_high_4h_gt_8"
                )

            return TradeAction(
                action=Action.HOLD,
                signal=signal,
                strategy_name="direction_strategy",
                reason="long_blocked_not_high_conviction_swing_setup"
            )

        # =========================================
        # RANGE -> DOWN = SHORT
        # Only allow:
        # range_breakout_down + SHORT + near_swing_high_4h
        # =========================================
        if (
            previous_direction_value == Direction.RANGE.value
            and current_direction == Direction.DOWN
        ):
            ctx = signal.context or {}

            if not bool(ctx.get("near_swing_high_4h")):
                return TradeAction(
                    action=Action.HOLD,
                    signal=signal,
                    strategy_name="direction_strategy",
                    reason="short_blocked_not_near_swing_high_4h"
                )

            return TradeAction(
                action=Action.SHORT,
                signal=signal,
                strategy_name="direction_strategy",
                reason="short_near_swing_high_4h"
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