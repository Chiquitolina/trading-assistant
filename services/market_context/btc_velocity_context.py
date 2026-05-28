from dataclasses import dataclass
from typing import Optional


@dataclass
class BTCVelocityContext:
    symbol: str
    velocity_15m: Optional[float]
    velocity_1h: Optional[float]
    direction_15m: Optional[str]
    direction_1h: Optional[str]
    state: str
    reason: str


class BTCVelocityContextService:
    BTC_SYMBOL = "BTCUSDT"

    DANGER_15M = 0.80
    DANGER_1H = 1.50

    CAUTION_15M = 0.50
    CAUTION_1H = 1.00

    def evaluate(self, buffer) -> BTCVelocityContext:
        candles_1m = buffer.get_candles(self.BTC_SYMBOL, "1m")

        if candles_1m is None or len(candles_1m) < 61:
            return BTCVelocityContext(
                symbol=self.BTC_SYMBOL,
                velocity_15m=None,
                velocity_1h=None,
                direction_15m=None,
                direction_1h=None,
                state="UNKNOWN",
                reason="not_enough_btc_data"
            )

        current_close = float(candles_1m[-1]["close"])
        close_15m_ago = float(candles_1m[-16]["close"])
        close_1h_ago = float(candles_1m[-61]["close"])

        velocity_15m = self._pct_move(current_close, close_15m_ago)
        velocity_1h = self._pct_move(current_close, close_1h_ago)

        direction_15m = self._direction(current_close, close_15m_ago)
        direction_1h = self._direction(current_close, close_1h_ago)

        state, reason = self._classify(
            velocity_15m=velocity_15m,
            velocity_1h=velocity_1h
        )

        return BTCVelocityContext(
            symbol=self.BTC_SYMBOL,
            velocity_15m=round(velocity_15m, 4),
            velocity_1h=round(velocity_1h, 4),
            direction_15m=direction_15m,
            direction_1h=direction_1h,
            state=state,
            reason=reason
        )

    def _pct_move(self, current: float, previous: float) -> float:
        if previous <= 0:
            return 0.0

        return abs((current - previous) / previous) * 100

    def _direction(self, current: float, previous: float) -> str:
        if previous <= 0:
            return "unknown"

        if current > previous:
            return "up"

        if current < previous:
            return "down"

        return "flat"

    def _classify(self, velocity_15m: float, velocity_1h: float):
        if velocity_15m >= self.DANGER_15M:
            return "DANGEROUS", "btc_velocity_15m_danger"

        if velocity_1h >= self.DANGER_1H:
            return "DANGEROUS", "btc_velocity_1h_danger"

        if velocity_15m >= self.CAUTION_15M:
            return "CAUTION", "btc_velocity_15m_caution"

        if velocity_1h >= self.CAUTION_1H:
            return "CAUTION", "btc_velocity_1h_caution"

        return "HEALTHY", "btc_velocity_normal"