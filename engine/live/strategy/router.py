from engine.live.strategy.modes.default_strategy import DefaultStrategy
from engine.live.strategy.modes.direction_strategy import DirectionStrategy
from engine.live.strategy.modes.aggressive_strategy import AggressiveStrategy


class StrategyRouter:

    def __init__(self, mode="default"):

        self.mode = mode

        self.strategies = {
            "default": DefaultStrategy(),
            "direction": DirectionStrategy(),
            "aggressive": AggressiveStrategy(),
        }

    def evaluate(
        self,
        signal,
        previous_direction=None,
        current_position=None,
    ):

        strategy = self.strategies.get(
            self.mode,
            self.strategies["default"],
        )

        return strategy.evaluate(
            signal=signal,
            previous_direction=previous_direction,
            current_position=current_position,
        )