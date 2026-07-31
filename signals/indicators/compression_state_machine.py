from dataclasses import dataclass, asdict
from enum import Enum
from typing import Optional


class CompressionState(Enum):
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
class CompressionWatch:
    symbol: str
    state: CompressionState

    created_ts: int
    updated_ts: int

    compression_high: float
    compression_low: float
    compression_score: int
    trend_score: int
    
    compression_height_pct: Optional[float] = None
    compression_duration: Optional[int] = None
    
    selected_lookback: Optional[int] = None
    selection_score: Optional[float] = None
    selection_reason: Optional[str] = None

    candidate_count: Optional[int] = None
    valid_candidate_count: Optional[int] = None
    compression_candidates_json: Optional[str] = None

    upper_slope: Optional[float] = None
    lower_slope: Optional[float] = None
    slope_difference: Optional[float] = None

    touches_high: Optional[int] = None
    touches_low: Optional[int] = None

    # Touch metrics normalized by compression duration
    touches_high_ratio: Optional[float] = None
    touches_low_ratio: Optional[float] = None

    # Positive means more high touches.
    # Negative means more low touches.
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

    base_mode: Optional[str] = None
    base_lookback: Optional[int] = None

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

    def active_watches(self):
        return [
            watch.to_dict()
            for watch in self.watches.values()
        ]

    def get(self, symbol):
        return self.watches.get(symbol)

    def remove(self, symbol):
        if symbol in self.watches:
            del self.watches[symbol]

    def _evaluate_pullback_entry(
        self,
        watch: CompressionWatch,
        symbol: str,
        ts: int,
        close: float,
        low: float,
        high: float,
    ):
        if not watch.breakout_price:
            watch.state = CompressionState.EXPIRED
            watch.reason = "missing_breakout_price"
            return watch.to_dict()

        pullback_from_breakout_pct = (
            (watch.breakout_price - low) / watch.breakout_price
        ) * 100

        distance_above_compression_high_pct = (
            (low - watch.compression_high) / watch.compression_high
        ) * 100

        # Por ahora mantenemos el comportamiento viejo
        pullback_pct = pullback_from_breakout_pct

        holds_compression_high = close >= watch.compression_high

        valid_pullback = (
            pullback_pct <= self.pullback_max_pct
            and (
                holds_compression_high
                if self.pullback_min_hold_high
                else True
            )
        )

        continuation = close >= watch.compression_high

        watch.pullback_pct = round(pullback_pct, 4)
        watch.pullback_from_breakout_pct = round(pullback_from_breakout_pct, 4)
        watch.distance_above_compression_high_pct = round(distance_above_compression_high_pct, 4)
        
        watch.valid_pullback = valid_pullback
        watch.holds_compression_high = holds_compression_high
        watch.continuation = continuation

        watch.pullback_detected = valid_pullback
        watch.continuation_detected = continuation
        
        # ============================================
        # PULLBACK EVENT INSTRUMENTATION
        # ============================================
        # No cambia la lógica actual.
        # Registra la primera vez que el sistema
        # considera válido el pullback.

        if valid_pullback:
            if watch.pullback_first_ts is None:
                watch.pullback_first_ts = ts

            if watch.pullback_valid_ts is None:
                watch.pullback_valid_ts = ts

            if watch.pullback_price is None:
                watch.pullback_price = low
        
        breakout_ext_pct_text = (
            f"{watch.breakout_extension_pct:.3f}%"
            if watch.breakout_extension_pct is not None
            else "None"
        )

        breakout_ext_atr_text = (
            f"{watch.breakout_extension_atr:.3f}"
            if watch.breakout_extension_atr is not None
            else "None"
        )
        
        print(
            "\n"
            "========================================\n"
            f"[COMPRESSION PIPELINE] {symbol}\n"
            "========================================\n"
            f"State               : {watch.state.value}\n"
            f"Waiting             : {watch.candles_waiting}/{self.max_pullback_candles}\n"
            "\n"
            "----- BREAKOUT -----\n"
            f"Breakout Ext %      : {breakout_ext_pct_text}\n"
            f"Breakout Ext ATR    : {breakout_ext_atr_text}\n"
            f"Breakout Price      : {watch.breakout_price:.8f}\n"
            f"Breakout High       : {watch.breakout_high:.8f}\n"
            f"Compression High    : {watch.compression_high:.8f}\n"
            f"Compression Low     : {watch.compression_low:.8f}\n"
            "\n"
            "----- CURRENT CANDLE -----\n"
            f"High                : {high:.8f}\n"
            f"Low                 : {low:.8f}\n"
            f"Close               : {close:.8f}\n"
            "\n"
            "----- EVALUATION -----\n"
            f"Pullback %          : {pullback_pct:.3f}%\n"
            f"Pullback From BO   : {pullback_from_breakout_pct:.3f}%\n"
            f"Distance Above Hi  : {distance_above_compression_high_pct:.3f}%\n"
            f"Hold Compression    : {holds_compression_high}\n"
            f"Continuation        : {continuation}\n"
            f"Valid Pullback      : {valid_pullback}\n"
            "\n"
            "----- FLAGS -----\n"
            f"breakout_detected   : {watch.breakout_detected}\n"
            f"pullback_detected   : {watch.pullback_detected}\n"
            f"continuation_detect : {watch.continuation_detected}\n"
            f"entry_ready         : {watch.entry_ready}\n"
            "\n"
            f"Reason              : {watch.reason}\n"
            "========================================\n"
        )

        print(
            f"[PULLBACK DEBUG] "
            f"{symbol} "
            f"waiting={watch.candles_waiting}/{self.max_pullback_candles} "
            f"pullback_pct={pullback_pct:.2f} "
            f"pullback_from_bo={pullback_from_breakout_pct:.2f} "
            f"dist_above_hi={distance_above_compression_high_pct:.2f} "
            f"valid_pullback={valid_pullback} "
            f"hold_high={holds_compression_high} "
            f"continuation={continuation} "
            f"close={close:.8f} "
            f"low={low:.8f} "
            f"breakout_price={watch.breakout_price:.8f} "
            f"compression_high={watch.compression_high:.8f}"
        )

        if valid_pullback and continuation:
            print(
                "\n"
                "########################################\n"
                f"[ENTRY READY] {symbol}\n"
                "########################################\n"
                f"Entry Price      : {close:.8f}\n"
                f"Current High     : {high:.8f}\n"
                f"Current Low      : {low:.8f}\n"
                f"Pullback %       : {pullback_pct:.3f}%\n"
                f"Pullback First TS: {watch.pullback_first_ts}\n"
                f"Pullback Valid TS: {watch.pullback_valid_ts}\n"
                f"Pullback Price   : {watch.pullback_price}\n"
                f"Entry Ready TS  : {watch.entry_ready_ts}\n"
                f"Compression High : {watch.compression_high:.8f}\n"
                f"Breakout Price   : {watch.breakout_price:.8f}\n"
                f"Breakout Ext %  : {breakout_ext_pct_text}\n"
                f"Breakout Ext ATR: {breakout_ext_atr_text}\n"
                "########################################\n"
            )

            watch.state = CompressionState.ENTRY_READY
            watch.entry_ready = True

            if watch.entry_ready_ts is None:
                watch.entry_ready_ts = ts

            watch.entry_price = close
            watch.reason = "pullback_hold_and_continuation"

            result = watch.to_dict()
            self.remove(symbol)
            return result

        print(
            "\n"
            "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n"
            f"[PULLBACK REJECTED] {symbol}\n"
            "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n"
            f"Pullback %     : {pullback_pct:.3f}%\n"
            f"Pullback BO    : {pullback_from_breakout_pct:.3f}%\n"
            f"Dist Above Hi  : {distance_above_compression_high_pct:.3f}%\n"
            f"Hold High      : {holds_compression_high}\n"
            f"Continuation   : {continuation}\n"
            f"Valid Pullback : {valid_pullback}\n"
            f"Close          : {close:.8f}\n"
            f"High           : {high:.8f}\n"
            f"Low            : {low:.8f}\n"
            f"Breakout Price : {watch.breakout_price:.8f}\n"
            f"Compression Hi : {watch.compression_high:.8f}\n"
            "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n"
        )

        watch.reason = "waiting_valid_pullback"
        return watch.to_dict()
    
    def _update_live_touch_metrics(
        self,
        watch: CompressionWatch,
        candle_high: float,
        candle_low: float,
    ):
        compression_height = (
            watch.compression_high
            - watch.compression_low
        )

        if compression_height <= 0:
            return

        tolerance_price = compression_height * 0.15

        touches_high = (
            watch.compression_high - tolerance_price
            <= candle_high
            <= watch.compression_high + tolerance_price
        )

        touches_low = (
            watch.compression_low - tolerance_price
            <= candle_low
            <= watch.compression_low + tolerance_price
        )

        if touches_high:
            watch.touches_high = (
                int(watch.touches_high or 0) + 1
            )

        if touches_low:
            watch.touches_low = (
                int(watch.touches_low or 0) + 1
            )

        watch.compression_duration = (
            int(watch.compression_duration or 0) + 1
        )

        duration = watch.compression_duration
        high_count = int(watch.touches_high or 0)
        low_count = int(watch.touches_low or 0)

        watch.touches_high_ratio = round(
            high_count / duration,
            4,
        )
        watch.touches_low_ratio = round(
            low_count / duration,
            4,
        )

        watch.touch_imbalance = (
            high_count - low_count
        )
        watch.touch_imbalance_ratio = round(
            watch.touch_imbalance / duration,
            4,
        )

    def update(
        self,
        symbol: str,
        candle: dict,
        trend: dict,
        compression: dict,
        breakout: dict,
        atr: float | None = None,
    ):
        ts = normalize_timestamp(candle["timestamp"])

        close = float(candle["close"])
        high = float(candle["high"])
        low = float(candle["low"])
        atr = float(atr) if atr is not None else None

        watch = self.watches.get(symbol)

        if watch is None:
            if trend.get("trend_up") and compression.get("is_compression"):
                watch = CompressionWatch(
                    symbol=symbol,
                    state=CompressionState.WATCH_CREATED,
                    created_ts=ts,
                    updated_ts=ts,
                    compression_high=float(compression["compression_high"]),
                    compression_low=float(compression["compression_low"]),
                    compression_score=int(compression.get("score", 0)),
                    trend_score=int(trend.get("score", 0)),
                    max_wait_candles=self.max_watch_candles,
                    reason="trend_up_and_compression",
                    range_ratio=compression.get("range_ratio"),
                    atr_ratio=compression.get("atr_ratio"),
                    volume_ratio=compression.get("volume_ratio"),
                    compression_range_pct=compression.get(
                        "compression_range_pct"
                    ),
                    avg_body_pct=compression.get(
                        "avg_body_pct"
                    ),

                    base_mode=compression.get("base_mode"),
                    base_lookback=compression.get("base_lookback"),
                    watch_age=0,
                    compression_height_pct=compression.get("compression_height_pct"),
                    compression_duration=compression.get("compression_duration"),
                    selected_lookback=compression.get(
                        "selected_lookback"
                    ),
                    selection_score=compression.get(
                        "selection_score"
                    ),
                    selection_reason=compression.get(
                        "selection_reason"
                    ),

                    candidate_count=compression.get(
                        "candidate_count"
                    ),
                    valid_candidate_count=compression.get(
                        "valid_candidate_count"
                    ),
                    compression_candidates_json=compression.get(
                        "candidates_json"
                    ),

                    upper_slope=compression.get("upper_slope"),
                    lower_slope=compression.get("lower_slope"),
                    slope_difference=compression.get("slope_difference"),

                    touches_high=compression.get("touches_high"),
                    touches_low=compression.get("touches_low"),

                    touches_high_ratio=compression.get(
                        "touches_high_ratio"
                    ),
                    touches_low_ratio=compression.get(
                        "touches_low_ratio"
                    ),

                    touch_imbalance=compression.get(
                        "touch_imbalance"
                    ),
                    touch_imbalance_ratio=compression.get(
                        "touch_imbalance_ratio"
                    ),

                    inside_ratio=compression.get("inside_ratio"),

                    compression_shape=compression.get("compression_shape"),
                    compression_quality_label=compression.get("compression_quality_label"),
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
        watch.watch_age += 1
        
        # ============================================
        # First candle after watch creation
        # ============================================

        if watch.state == CompressionState.WATCH_CREATED:
            watch.state = CompressionState.WATCHING_COMPRESSION
            watch.reason = "watch_created_now_watching"

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
            current_selected_lookback = compression.get(
                "selected_lookback"
            )

            if (
                current_selected_lookback
                != watch.selected_lookback
            ):
                print(
                    f"[WATCH WINDOW FROZEN] {symbol} "
                    f"frozen_window={watch.selected_lookback} "
                    f"current_window={current_selected_lookback}"
                )

            # La vela de breakout no cuenta como toque.
            if not breakout.get("breakout"):
                self._update_live_touch_metrics(
                    watch=watch,
                    candle_high=high,
                    candle_low=low,
                )

            # La tendencia sí puede evolucionar.
            watch.trend_score = int(
                trend.get("score", watch.trend_score)
            )

            if breakout.get("breakout"):
                # Mantener exactamente la lógica actual.
                watch.breakout_detected = True
                watch.breakout_confirmed = True

                watch.state = CompressionState.WAIT_PULLBACK

                watch.breakout_ts = ts
                watch.breakout_price = close
                watch.breakout_high = high
                watch.breakout_volume_ratio = breakout.get("volume_ratio")
                
                watch.breakout_extension_pct = (
                    (close - watch.compression_high) / watch.compression_high
                ) * 100 if watch.compression_high else None

                watch.breakout_extension_atr = (
                    (close - watch.compression_high) / atr
                ) if atr and atr > 0 else None

                watch.candles_waiting = 0
                watch.reason = "breakout_detected_waiting_pullback"

                return self._evaluate_pullback_entry(
                    watch=watch,
                    symbol=symbol,
                    ts=ts,
                    close=close,
                    low=low,
                    high=high
                )

            return watch.to_dict()

        if watch.state == CompressionState.BREAKOUT_DETECTED:
            watch.state = CompressionState.WAIT_PULLBACK
            watch.candles_waiting = 0
            watch.reason = "waiting_pullback"

            return self._evaluate_pullback_entry(
                watch=watch,
                symbol=symbol,
                ts=ts,
                close=close,
                low=low,
                high=high
            )

        if watch.state == CompressionState.WAIT_PULLBACK:

            if watch.candles_waiting > self.max_pullback_candles:
                watch.state = CompressionState.EXPIRED
                watch.reason = "pullback_expired"
                result = watch.to_dict()
                self.remove(symbol)
                return result

            return self._evaluate_pullback_entry(
                watch=watch,
                symbol=symbol,
                ts=ts,
                close=close,
                low=low,
                high=high
            )

        return watch.to_dict()