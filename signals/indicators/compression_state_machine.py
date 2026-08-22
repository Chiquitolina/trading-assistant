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
    
    moderate_breakout_candidate: bool = False

    entry_vs_breakout_pct: Optional[float] = None
    entry_condition_matched: bool = False
    entry_profile: Optional[str] = None

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


class CompressionStateMachine:
    def __init__(
        self,
        max_watch_candles=8,
        max_pullback_candles=5,
        moderate_breakout_min_pct=0.50,
        moderate_breakout_max_pct=0.75,
        entry_vs_breakout_min_pct=-0.25,
        entry_vs_breakout_max_pct=0.00,
    ):
        self.watches = {}

        self.max_watch_candles = max_watch_candles
        self.max_pullback_candles = max_pullback_candles

        self.moderate_breakout_min_pct = (
            moderate_breakout_min_pct
        )
        self.moderate_breakout_max_pct = (
            moderate_breakout_max_pct
        )

        self.entry_vs_breakout_min_pct = (
            entry_vs_breakout_min_pct
        )
        self.entry_vs_breakout_max_pct = (
            entry_vs_breakout_max_pct
        )

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

            result = watch.to_dict()
            self.remove(symbol)
            return result

        # Distancia del low respecto del breakout.
        # Se conserva como métrica descriptiva del retroceso real.
        pullback_from_breakout_pct = (
            (
                watch.breakout_price
                - low
            )
            / watch.breakout_price
            * 100
        )

        # Distancia del cierre potencial de entrada respecto del breakout.
        # Esta es la métrica equivalente al bucket analizado.
        entry_vs_breakout_pct = (
            (
                close
                - watch.breakout_price
            )
            / watch.breakout_price
            * 100
        )

        distance_above_compression_high_pct = (
            (
                low
                - watch.compression_high
            )
            / watch.compression_high
            * 100
        )

        holds_compression_high = (
            close >= watch.compression_high
        )

        entry_inside_retrace_band = (
            self.entry_vs_breakout_min_pct
            <= entry_vs_breakout_pct
            <= self.entry_vs_breakout_max_pct
        )

        entry_condition = (
            watch.moderate_breakout_candidate
            and entry_inside_retrace_band
            and holds_compression_high
        )

        # Métricas del estado actual.
        watch.pullback_pct = round(
            pullback_from_breakout_pct,
            4,
        )

        watch.pullback_from_breakout_pct = round(
            pullback_from_breakout_pct,
            4,
        )

        watch.entry_vs_breakout_pct = round(
            entry_vs_breakout_pct,
            4,
        )

        watch.distance_above_compression_high_pct = round(
            distance_above_compression_high_pct,
            4,
        )

        watch.valid_pullback = entry_condition
        watch.holds_compression_high = holds_compression_high
        watch.continuation = holds_compression_high

        watch.pullback_detected = (
            entry_inside_retrace_band
        )

        watch.continuation_detected = (
            holds_compression_high
        )

        watch.entry_condition_matched = (
            entry_condition
        )

        # Registrar el primer low observado después del breakout.
        if watch.pullback_first_ts is None:
            watch.pullback_first_ts = ts
            watch.pullback_price = low
        else:
            # Conserva el low más profundo observado durante WAIT_PULLBACK.
            if (
                watch.pullback_price is None
                or low < watch.pullback_price
            ):
                watch.pullback_price = low

        print(
            "\n"
            "========================================\n"
            f"[MODERATE BREAKOUT RETRACE] {symbol}\n"
            "========================================\n"
            f"State                  : {watch.state.value}\n"
            f"Waiting                : "
            f"{watch.candles_waiting}/"
            f"{self.max_pullback_candles}\n"
            "\n"
            f"Breakout Price         : "
            f"{watch.breakout_price:.8f}\n"
            f"Breakout Extension %   : "
            f"{watch.breakout_extension_pct}\n"
            f"Moderate Candidate     : "
            f"{watch.moderate_breakout_candidate}\n"
            "\n"
            f"Current High           : {high:.8f}\n"
            f"Current Low            : {low:.8f}\n"
            f"Current Close          : {close:.8f}\n"
            "\n"
            f"Pullback Low %         : "
            f"{pullback_from_breakout_pct:.4f}%\n"
            f"Entry vs Breakout %    : "
            f"{entry_vs_breakout_pct:.4f}%\n"
            f"Required Entry Band    : "
            f"[{self.entry_vs_breakout_min_pct:.2f}%, "
            f"{self.entry_vs_breakout_max_pct:.2f}%]\n"
            f"Inside Entry Band      : "
            f"{entry_inside_retrace_band}\n"
            f"Holds Compression High : "
            f"{holds_compression_high}\n"
            f"Entry Condition        : "
            f"{entry_condition}\n"
            "========================================\n"
        )

        if entry_condition:
            watch.state = CompressionState.ENTRY_READY
            watch.entry_ready = True
            watch.entry_condition_matched = True

            if watch.pullback_valid_ts is None:
                watch.pullback_valid_ts = ts

            if watch.entry_ready_ts is None:
                watch.entry_ready_ts = ts

            watch.entry_price = close
            watch.entry_profile = (
                "MODERATE_BREAKOUT_RETRACE"
            )
            watch.reason = (
                "moderate_breakout_retrace_entry_ready"
            )

            result = watch.to_dict()
            self.remove(symbol)
            return result

        # Todavía está arriba del breakout: esperar retroceso.
        if (
            entry_vs_breakout_pct
            > self.entry_vs_breakout_max_pct
        ):
            watch.reason = (
                "waiting_retrace_to_breakout_band"
            )

        # Retrocedió más de 0,25%, pero podría recuperar
        # la banda dentro del máximo de velas.
        elif (
            entry_vs_breakout_pct
            < self.entry_vs_breakout_min_pct
        ):
            watch.reason = (
                "price_below_retrace_band_waiting_recovery"
            )

        elif not holds_compression_high:
            watch.reason = (
                "compression_high_not_held"
            )

        else:
            watch.reason = (
                "waiting_entry_condition"
            )

        return watch.to_dict()

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
                    watch_age=0,
                    compression_height_pct=compression.get("compression_height_pct"),
                    compression_duration=compression.get("compression_duration"),

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
            return watch.to_dict()

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
                
                old_high = watch.compression_high
                old_low = watch.compression_low

                new_high = float(compression["compression_high"])
                new_low = float(compression["compression_low"])

                if new_high != old_high or new_low != old_low:
                    print(
                        f"[WATCH LEVEL WOULD_UPDATE_BUT_FROZEN] {symbol} "
                        f"frozen_high={old_high:.8f} detected_high={new_high:.8f} "
                        f"frozen_low={old_low:.8f} detected_low={new_low:.8f}"
                    )
                    
                # ============================================
                # Freeze compression levels after watch creation
                # ============================================


                #watch.compression_high = max(
                #    watch.compression_high,
                #    float(compression["compression_high"]),
                #)
                #watch.compression_low = min(
                #    watch.compression_low,
                #    float(compression["compression_low"]),
                #)
                
                watch.trend_score = int(trend.get("score", watch.trend_score))

            if breakout.get("breakout"):
                watch.breakout_detected = True
                watch.breakout_confirmed = True

                watch.breakout_ts = ts
                watch.breakout_price = close
                watch.breakout_high = high
                watch.breakout_volume_ratio = (
                    breakout.get("volume_ratio")
                )

                watch.breakout_extension_pct = (
                    (
                        close
                        - watch.compression_high
                    )
                    / watch.compression_high
                    * 100
                    if watch.compression_high
                    else None
                )

                watch.breakout_extension_atr = (
                    (
                        close
                        - watch.compression_high
                    )
                    / atr
                    if atr and atr > 0
                    else None
                )

                watch.moderate_breakout_candidate = (
                    watch.breakout_extension_pct is not None
                    and self.moderate_breakout_min_pct
                    <= watch.breakout_extension_pct
                    <= self.moderate_breakout_max_pct
                )

                watch.entry_profile = (
                    "MODERATE_BREAKOUT_RETRACE"
                )

                # Esta rama solamente acepta breakouts moderados.
                if not watch.moderate_breakout_candidate:
                    watch.state = CompressionState.EXPIRED
                    watch.reason = (
                        "breakout_outside_moderate_band"
                    )

                    result = watch.to_dict()
                    self.remove(symbol)
                    return result

                # El breakout es válido, pero no se evalúa
                # como pullback en la misma vela.
                watch.state = CompressionState.WAIT_PULLBACK
                watch.candles_waiting = 0
                watch.reason = (
                    "moderate_breakout_waiting_retrace"
                )

                return watch.to_dict()

        if watch.state == CompressionState.BREAKOUT_DETECTED:
            watch.state = CompressionState.WAIT_PULLBACK
            watch.candles_waiting = 0
            watch.reason = "waiting_retrace"

            return watch.to_dict()

        if watch.state == CompressionState.WAIT_PULLBACK:
            if (
                watch.candles_waiting
                > self.max_pullback_candles
            ):
                watch.state = CompressionState.EXPIRED
                watch.reason = (
                    "moderate_retrace_entry_expired"
                )

                result = watch.to_dict()
                self.remove(symbol)
                return result

            return self._evaluate_pullback_entry(
                watch=watch,
                symbol=symbol,
                ts=ts,
                close=close,
                low=low,
                high=high,
            )

        return watch.to_dict()
