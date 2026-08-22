from dataclasses import asdict, dataclass
from enum import Enum
from typing import Optional


class BucketV2CompressionState(Enum):
    IDLE = "IDLE"
    WATCH_CREATED = "WATCH_CREATED"
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
class BucketV2CompressionWatch:
    symbol: str
    state: BucketV2CompressionState
    created_ts: int
    updated_ts: int
    compression_high: float
    compression_low: float
    compression_score: int
    trend_score: int
    compression_height_pct: Optional[float] = None
    compression_duration: Optional[int] = None
    upper_slope: Optional[float] = None
    lower_slope: Optional[float] = None
    slope_difference: Optional[float] = None
    touches_high: Optional[int] = None
    touches_low: Optional[int] = None
    touches_high_ratio: Optional[float] = None
    touches_low_ratio: Optional[float] = None
    touch_imbalance: Optional[int] = None
    touch_imbalance_ratio: Optional[float] = None
    inside_ratio: Optional[float] = None
    compression_shape: Optional[str] = None
    compression_quality_label: Optional[str] = None
    breakout_ts: Optional[int] = None
    breakout_price: Optional[float] = None
    breakout_high: Optional[float] = None
    breakout_volume_ratio: Optional[float] = None
    breakout_extension_pct: Optional[float] = None
    breakout_extension_atr: Optional[float] = None
    candles_waiting: int = 0
    max_wait_candles: int = 5
    entry_price: Optional[float] = None
    reason: Optional[str] = None
    range_ratio: Optional[float] = None
    atr_ratio: Optional[float] = None
    volume_ratio: Optional[float] = None
    compression_range_pct: Optional[float] = None
    avg_body_pct: Optional[float] = None
    watch_age: int = 0
    pullback_pct: Optional[float] = None
    pullback_from_breakout_pct: Optional[float] = None
    distance_above_compression_high_pct: Optional[float] = None
    pullback_first_ts: Optional[int] = None
    pullback_valid_ts: Optional[int] = None
    pullback_price: Optional[float] = None
    entry_ready_ts: Optional[int] = None
    valid_pullback: Optional[bool] = None
    holds_compression_high: Optional[bool] = None
    continuation: Optional[bool] = None
    entry_ready: bool = False
    entry_attempted: bool = False
    breakout_detected: bool = False
    breakout_confirmed: bool = False
    pullback_detected: bool = False
    continuation_detected: bool = False

    def to_dict(self):
        data = asdict(self)
        data["state"] = self.state.value
        return data


class BucketV2CompressionStateMachine:
    """Exact state and transition model from the ``new-bucket`` branch."""

    def __init__(self, max_watch_candles=8, max_pullback_candles=5,
                 pullback_max_pct=1.2, pullback_min_hold_high=True):
        self.watches = {}
        self.max_watch_candles = max_watch_candles
        self.max_pullback_candles = max_pullback_candles
        self.pullback_max_pct = pullback_max_pct
        self.pullback_min_hold_high = pullback_min_hold_high

    def active_watches(self):
        return [watch.to_dict() for watch in self.watches.values()]

    def get(self, symbol): return self.watches.get(symbol)
    def remove(self, symbol): self.watches.pop(symbol, None)

    def _evaluate_pullback_entry(self, watch, symbol, ts, close, low, high):
        if not watch.breakout_price:
            watch.state = BucketV2CompressionState.EXPIRED
            watch.reason = "missing_breakout_price"
            return watch.to_dict()
        pullback = ((watch.breakout_price - low) / watch.breakout_price) * 100
        distance = ((low - watch.compression_high) / watch.compression_high) * 100
        holds_high = close >= watch.compression_high
        valid = pullback <= self.pullback_max_pct and (
            holds_high if self.pullback_min_hold_high else True
        )
        continuation = close >= watch.compression_high
        watch.pullback_pct = round(pullback, 4)
        watch.pullback_from_breakout_pct = round(pullback, 4)
        watch.distance_above_compression_high_pct = round(distance, 4)
        watch.valid_pullback = valid
        watch.holds_compression_high = holds_high
        watch.continuation = continuation
        watch.pullback_detected = valid
        watch.continuation_detected = continuation
        if valid:
            if watch.pullback_first_ts is None: watch.pullback_first_ts = ts
            if watch.pullback_valid_ts is None: watch.pullback_valid_ts = ts
            if watch.pullback_price is None: watch.pullback_price = low
        if valid and continuation:
            watch.state = BucketV2CompressionState.ENTRY_READY
            watch.entry_ready = True
            if watch.entry_ready_ts is None: watch.entry_ready_ts = ts
            watch.entry_price = close
            watch.reason = "pullback_hold_and_continuation"
            result = watch.to_dict()
            self.remove(symbol)
            return result
        watch.reason = "waiting_valid_pullback"
        return watch.to_dict()

    def update(self, symbol, candle, trend, compression, breakout, atr=None):
        ts = normalize_timestamp(candle["timestamp"])
        close, high, low = map(float, (candle["close"], candle["high"], candle["low"]))
        atr = float(atr) if atr is not None else None
        watch = self.watches.get(symbol)
        if watch is None:
            if trend.get("trend_up") and compression.get("is_compression"):
                watch = BucketV2CompressionWatch(
                    symbol=symbol, state=BucketV2CompressionState.WATCH_CREATED,
                    created_ts=ts, updated_ts=ts,
                    compression_high=float(compression["compression_high"]),
                    compression_low=float(compression["compression_low"]),
                    compression_score=int(compression.get("score", 0)),
                    trend_score=int(trend.get("score", 0)),
                    max_wait_candles=self.max_watch_candles,
                    reason="trend_up_and_compression",
                    range_ratio=compression.get("range_ratio"),
                    atr_ratio=compression.get("atr_ratio"),
                    volume_ratio=compression.get("volume_ratio"),
                    compression_range_pct=compression.get("compression_range_pct"),
                    avg_body_pct=compression.get("avg_body_pct"), watch_age=0,
                    compression_height_pct=compression.get("compression_height_pct"),
                    compression_duration=compression.get("compression_duration"),
                    upper_slope=compression.get("upper_slope"),
                    lower_slope=compression.get("lower_slope"),
                    slope_difference=compression.get("slope_difference"),
                    touches_high=compression.get("touches_high"),
                    touches_low=compression.get("touches_low"),
                    touches_high_ratio=compression.get("touches_high_ratio"),
                    touches_low_ratio=compression.get("touches_low_ratio"),
                    touch_imbalance=compression.get("touch_imbalance"),
                    touch_imbalance_ratio=compression.get("touch_imbalance_ratio"),
                    inside_ratio=compression.get("inside_ratio"),
                    compression_shape=compression.get("compression_shape"),
                    compression_quality_label=compression.get("compression_quality_label"),
                )
                self.watches[symbol] = watch
                return watch.to_dict()
            return {"symbol": symbol, "state": "IDLE", "reason": "no_watch"}
        watch.updated_ts = ts
        watch.candles_waiting += 1
        watch.watch_age += 1
        if watch.state == BucketV2CompressionState.WATCH_CREATED:
            watch.state = BucketV2CompressionState.WATCHING_COMPRESSION
            watch.reason = "watch_created_now_watching"
            return watch.to_dict()
        if (watch.state == BucketV2CompressionState.WATCHING_COMPRESSION
                and watch.candles_waiting > self.max_watch_candles):
            watch.state = BucketV2CompressionState.EXPIRED
            watch.reason = "watch_expired_no_breakout"
            result = watch.to_dict(); self.remove(symbol); return result
        if watch.state == BucketV2CompressionState.WATCHING_COMPRESSION:
            if compression.get("is_compression"):
                watch.trend_score = int(trend.get("score", watch.trend_score))
            if breakout.get("breakout"):
                watch.breakout_detected = watch.breakout_confirmed = True
                watch.state = BucketV2CompressionState.WAIT_PULLBACK
                watch.breakout_ts, watch.breakout_price, watch.breakout_high = ts, close, high
                watch.breakout_volume_ratio = breakout.get("volume_ratio")
                watch.breakout_extension_pct = ((close - watch.compression_high) / watch.compression_high) * 100 if watch.compression_high else None
                watch.breakout_extension_atr = ((close - watch.compression_high) / atr) if atr and atr > 0 else None
                watch.candles_waiting = 0
                watch.reason = "breakout_detected_waiting_pullback"
                return self._evaluate_pullback_entry(watch, symbol, ts, close, low, high)
            return watch.to_dict()
        if watch.state == BucketV2CompressionState.BREAKOUT_DETECTED:
            watch.state = BucketV2CompressionState.WAIT_PULLBACK
            watch.candles_waiting = 0
            watch.reason = "waiting_pullback"
            return self._evaluate_pullback_entry(watch, symbol, ts, close, low, high)
        if watch.state == BucketV2CompressionState.WAIT_PULLBACK:
            if watch.candles_waiting > self.max_pullback_candles:
                watch.state = BucketV2CompressionState.EXPIRED
                watch.reason = "pullback_expired"
                result = watch.to_dict(); self.remove(symbol); return result
            return self._evaluate_pullback_entry(watch, symbol, ts, close, low, high)
        return watch.to_dict()
