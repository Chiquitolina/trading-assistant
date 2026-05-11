from engine.live.strategy.modes.default_strategy import DefaultStrategy
from engine.live.strategy.modes.direction_strategy import DirectionStrategy


class StrategyRouter:

    def __init__(self, mode="default"):

        self.mode = mode

        self.strategies = {
            "default": DefaultStrategy(),
            "direction": DirectionStrategy()
        }

    def evaluate(
        self,
        signal,
        previous_direction=None,
    ):

        strategy = self.strategies[self.mode]

        return strategy.evaluate(
            signal=signal,
            previous_direction=previous_direction,
        )