from dataclasses import dataclass, asdict
from enum import Enum
from typing import Optional


class CompressionState(Enum):
    IDLE = "IDLE"
    WATCHING_COMPRESSION = "WATCHING_COMPRESSION"
    BREAKOUT_DETECTED = "BREAKOUT_DETECTED"
    WAIT_PULLBACK = "WAIT_PULLBACK"
    ENTRY_READY = "ENTRY_READY"
    EXPIRED = "EXPIRED"


def normalize_timestamp(ts):
    if hasattr(ts, "timestamp"):
        return int(ts.timestamp() * 1000)

    return int(ts)


@dataclass
class CompressionWatch:
    symbol: str
    state: CompressionState

    created_ts: int
    updated_ts: int

    compression_high: float
    compression_low: float
    compression_score: int
    trend_score: int

    breakout_ts: Optional[int] = None
    breakout_price: Optional[float] = None
    breakout_high: Optional[float] = None
    breakout_volume_ratio: Optional[float] = None

    candles_waiting: int = 0
    max_wait_candles: int = 5

    entry_price: Optional[float] = None
    reason: Optional[str] = None

    def to_dict(self):
        data = asdict(self)
        data["state"] = self.state.value
        return data


class CompressionStateMachine:
    def __init__(
        self,
        max_watch_candles=8,
        max_pullback_candles=5,
        pullback_max_pct=1.2,
        pullback_min_hold_high=True,
    ):
        self.watches = {}
        self.max_watch_candles = max_watch_candles
        self.max_pullback_candles = max_pullback_candles
        self.pullback_max_pct = pullback_max_pct
        self.pullback_min_hold_high = pullback_min_hold_high

    def get(self, symbol):
        return self.watches.get(symbol)

    def remove(self, symbol):
        if symbol in self.watches:
            del self.watches[symbol]

    def update(
        self,
        symbol: str,
        candle: dict,
        trend: dict,
        compression: dict,
        breakout: dict,
    ):
        ts = normalize_timestamp(candle["timestamp"])

        close = float(candle["close"])
        high = float(candle["high"])
        low = float(candle["low"])

        watch = self.watches.get(symbol)

        if watch is None:
            if trend.get("trend_up") and compression.get("is_compression"):
                watch = CompressionWatch(
                    symbol=symbol,
                    state=CompressionState.WATCHING_COMPRESSION,
                    created_ts=ts,
                    updated_ts=ts,
                    compression_high=float(compression["compression_high"]),
                    compression_low=float(compression["compression_low"]),
                    compression_score=int(compression.get("score", 0)),
                    trend_score=int(trend.get("score", 0)),
                    max_wait_candles=self.max_pullback_candles,
                    reason="trend_up_and_compression",
                )

                self.watches[symbol] = watch
                return watch.to_dict()

            return {
                "symbol": symbol,
                "state": CompressionState.IDLE.value,
                "reason": "no_watch",
            }

        watch.updated_ts = ts
        watch.candles_waiting += 1

        if (
            watch.state == CompressionState.WATCHING_COMPRESSION
            and watch.candles_waiting > self.max_watch_candles
        ):
            watch.state = CompressionState.EXPIRED
            watch.reason = "watch_expired_no_breakout"
            result = watch.to_dict()
            self.remove(symbol)
            return result

        if watch.state == CompressionState.WATCHING_COMPRESSION:
            if compression.get("is_compression"):
                watch.compression_high = max(
                    watch.compression_high,
                    float(compression["compression_high"]),
                )
                watch.compression_low = min(
                    watch.compression_low,
                    float(compression["compression_low"]),
                )
                watch.compression_score = int(compression.get("score", 0))
                watch.trend_score = int(trend.get("score", watch.trend_score))

            if breakout.get("breakout"):
                watch.state = CompressionState.BREAKOUT_DETECTED
                watch.breakout_ts = ts
                watch.breakout_price = close
                watch.breakout_high = high
                watch.breakout_volume_ratio = breakout.get("volume_ratio")
                watch.candles_waiting = 0
                watch.reason = "breakout_detected"

                return watch.to_dict()

            return watch.to_dict()

        if watch.state == CompressionState.BREAKOUT_DETECTED:
            watch.state = CompressionState.WAIT_PULLBACK
            watch.candles_waiting = 0
            watch.reason = "waiting_pullback"
            return watch.to_dict()

        if watch.state == CompressionState.WAIT_PULLBACK:

            if watch.candles_waiting > self.max_pullback_candles:
                watch.state = CompressionState.EXPIRED
                watch.reason = "pullback_expired"
                result = watch.to_dict()
                self.remove(symbol)
                return result

            if not watch.breakout_price:
                watch.state = CompressionState.EXPIRED
                watch.reason = "missing_breakout_price"
                result = watch.to_dict()
                self.remove(symbol)
                return result

            pullback_pct = (
                (watch.breakout_price - low) / watch.breakout_price
            ) * 100

            holds_compression_high = close >= watch.compression_high

            valid_pullback = (
                pullback_pct <= self.pullback_max_pct
                and (
                    holds_compression_high
                    if self.pullback_min_hold_high
                    else True
                )
            )

            continuation = close > watch.breakout_price

            print(
                f"[PULLBACK DEBUG] "
                f"{symbol} "
                f"waiting={watch.candles_waiting}/{self.max_pullback_candles} "
                f"pullback_pct={pullback_pct:.2f} "
                f"valid_pullback={valid_pullback} "
                f"hold_high={holds_compression_high} "
                f"continuation={continuation} "
                f"close={close:.8f} "  
                f"low={low:.8f} "
                f"breakout_price={watch.breakout_price:.8f} "
                f"compression_high={watch.compression_high:.8f}"
            )

            if valid_pullback and continuation:
                watch.state = CompressionState.ENTRY_READY
                watch.entry_price = close
                watch.reason = "pullback_hold_and_continuation"

                result = watch.to_dict()
                self.remove(symbol)
                return result

            watch.reason = "waiting_valid_pullback"
            return watch.to_dict()

        return watch.to_dict()