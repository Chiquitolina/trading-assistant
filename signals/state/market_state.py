from dataclasses import dataclass
from typing import Optional

from signals.state.shift_detector import ShiftDetector


@dataclass
class MarketState:
    timestamp: object
    open: float
    high: float
    low: float
    close: float

    ema20: float | None
    ema50: float | None

    trend_1h: str
    direction_15m: str
    momentum_5m: str

    trend_changed: bool
    direction_changed: bool

    potential_shift: str
    trend_shift: str


class MarketStateBuilder:
    def __init__(self, shift_detector: ShiftDetector | None = None):
        self.prev_trend: Optional[str] = None
        self.prev_direction: Optional[str] = None

        self.pending_potential_shift: str = "no_shift"

        self.shift_detector = shift_detector or ShiftDetector()

    def build(
        self,
        candle,
        trend: str,
        direction: str,
        momentum: str,
    ) -> dict:
        trend_changed = (
            trend != self.prev_trend
            if self.prev_trend is not None
            else False
        )

        direction_changed = (
            direction != self.prev_direction
            if self.prev_direction is not None
            else False
        )

        # 1) Confirmar el potential anterior con el momentum actual
        confirmed_shift = self.shift_detector.confirm(
            potential_shift=self.pending_potential_shift,
            current_momentum=momentum,
        )

        # 2) Detectar nuevo potential con la vela actual
        new_potential_shift = self.shift_detector.detect_potential(
            prev_direction=self.prev_direction,
            trend=trend,
            momentum=momentum,
            close=candle.get("close"),
            ema20=candle.get("ema20"),
            ema50=candle.get("ema50"),
        )

        state = MarketState(
            timestamp=candle["timestamp"],
            open=candle["open"],
            high=candle["high"],
            low=candle["low"],
            close=candle["close"],

            ema20=candle.get("ema20"),
            ema50=candle.get("ema50"),

            trend_1h=trend.value if hasattr(trend, "value") else trend,
            direction_15m=direction.value if hasattr(direction, "value") else direction,
            momentum_5m=momentum.value if hasattr(momentum, "value") else momentum,

            trend_changed=trend_changed,
            direction_changed=direction_changed,

            potential_shift=new_potential_shift,
            trend_shift=confirmed_shift,
        )

        self.pending_potential_shift = new_potential_shift
        self.prev_trend = trend
        self.prev_direction = direction

        return state.__dict__