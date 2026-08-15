from dataclasses import dataclass
from typing import Any, Optional
import math

@dataclass
class PendingBucketEntry:
    symbol: str
    plan: Any
    trade_action: Any

    breakout_price: float
    compression_high: float
    breakout_extension_pct: float

    band_low: float
    band_high: float

    armed_ts: int
    expires_ts: int

    last_price: Optional[float] = None
    last_update_ts: Optional[int] = None
    status: str = "ARMED"
    reason: str = "waiting_bucket_touch"


class BucketTouchEntryManager:

    def __init__(
        self,
        breakout_min_pct=0.50,
        breakout_max_pct=0.75,
        entry_band_min_pct=-0.25,
        entry_band_max_pct=0.00,
        expiry_minutes=150,
    ):
        self.breakout_min_pct = breakout_min_pct
        self.breakout_max_pct = breakout_max_pct

        self.entry_band_min_pct = entry_band_min_pct
        self.entry_band_max_pct = entry_band_max_pct

        self.expiry_ms = int(expiry_minutes * 60 * 1000)

        self.pending: dict[str, PendingBucketEntry] = {}
        
    @staticmethod
    def _to_milliseconds(timestamp: int) -> int:
        timestamp = int(timestamp)

        if timestamp < 10_000_000_000:
            timestamp *= 1000

        return timestamp

    def get(self, symbol):
        return self.pending.get(symbol)

    def symbols(self):
        return list(self.pending.keys())

    def remove(self, symbol):
        return self.pending.pop(symbol, None)

    def arm(
        self,
        plan,
        trade_action,
        timestamp: int,
    ) -> bool:
        context = plan.signal_context or {}

        breakout_price = context.get("breakout_price")
        breakout_extension_pct = context.get(
            "breakout_extension_pct"
        )
        compression_high = context.get("compression_high")

        try:
            breakout_price = float(breakout_price)
            breakout_extension_pct = float(
                breakout_extension_pct
            )
            compression_high = float(compression_high)
        except (TypeError, ValueError):
            print(
                f"[BUCKET V2 REJECT] "
                f"symbol={plan.symbol} "
                f"reason=missing_or_invalid_metrics "
                f"breakout_price={breakout_price} "
                f"breakout_extension_pct="
                f"{breakout_extension_pct} "
                f"compression_high={compression_high}"
            )
            return False
        
        if not all(
            math.isfinite(value)
            for value in [
                breakout_price,
                breakout_extension_pct,
                compression_high,
            ]
        ):
            print(
                f"[BUCKET V2 REJECT] "
                f"symbol={plan.symbol} "
                f"reason=non_finite_metrics"
            )
            return False

        # Mismos límites que pd.cut:
        # bucket 0.50%-0.75% significa (0.50, 0.75]
        valid_breakout = (
            self.breakout_min_pct
            < breakout_extension_pct
            <= self.breakout_max_pct
        )

        if not valid_breakout:
            print(
                f"[BUCKET V2 REJECT] "
                f"symbol={plan.symbol} "
                f"reason=breakout_outside_bucket "
                f"breakout_extension_pct="
                f"{breakout_extension_pct:.6f}"
            )
            return False

        if plan.symbol in self.pending:
            print(
                f"[BUCKET V2 REJECT] "
                f"symbol={plan.symbol} "
                f"reason=already_pending"
            )
            return False

        band_low = breakout_price * (
            1 + self.entry_band_min_pct / 100
        )

        band_high = breakout_price * (
            1 + self.entry_band_max_pct / 100
        )
        
        timestamp = self._to_milliseconds(timestamp)

        pending = PendingBucketEntry(
            symbol=plan.symbol,
            plan=plan,
            trade_action=trade_action,
            breakout_price=breakout_price,
            compression_high=compression_high,
            breakout_extension_pct=breakout_extension_pct,
            band_low=band_low,
            band_high=band_high,
            armed_ts=int(timestamp),
            expires_ts=int(timestamp) + self.expiry_ms,
        )

        self.pending[plan.symbol] = pending

        print(
            f"[BUCKET V2 ARMED] "
            f"symbol={plan.symbol} "
            f"breakout_extension_pct="
            f"{breakout_extension_pct:.6f} "
            f"breakout_price={breakout_price:.8f} "
            f"band=[{band_low:.8f}, {band_high:.8f}] "
            f"compression_high={compression_high:.8f} "
            f"expires_ts={pending.expires_ts}"
        )

        return True

    def evaluate_price(
        self,
        symbol: str,
        price: float,
        timestamp: int,
    ) -> Optional[PendingBucketEntry]:
        pending = self.pending.get(symbol)

        if pending is None:
            return None

        try:
            price = float(price)
            timestamp = self._to_milliseconds(timestamp)
        except (TypeError, ValueError, OverflowError):
            print(
                f"[BUCKET V2 PRICE IGNORED] "
                f"symbol={symbol} "
                f"reason=invalid_price_or_timestamp "
                f"price={price} "
                f"timestamp={timestamp}"
            )
            return None

        if not math.isfinite(price) or price <= 0:
            print(
                f"[BUCKET V2 PRICE IGNORED] "
                f"symbol={symbol} "
                f"reason=non_finite_or_non_positive_price "
                f"price={price}"
            )
            return None

        pending.last_price = price
        pending.last_update_ts = timestamp

        if timestamp >= pending.expires_ts:
            pending.status = "EXPIRED"
            pending.reason = "bucket_touch_timeout"

            self.remove(symbol)

            print(
                f"[BUCKET V2 EXPIRED] "
                f"symbol={symbol} "
                f"last_price={price:.8f}"
            )

            return None

        # La banda está arriba de compression_high.
        # Si pierde la estructura, invalidamos.
        if price < pending.compression_high:
            pending.status = "INVALIDATED"
            pending.reason = "compression_high_lost"

            self.remove(symbol)

            print(
                f"[BUCKET V2 INVALIDATED] "
                f"symbol={symbol} "
                f"price={price:.8f} "
                f"compression_high="
                f"{pending.compression_high:.8f}"
            )

            return None

        inside_band = (
            pending.band_low
            <= price
            <= pending.band_high
        )

        if inside_band:
            pending.status = "TRIGGERED"
            pending.reason = "bucket_band_touched"

            print(
                f"[BUCKET V2 BAND TOUCHED] "
                f"symbol={symbol} "
                f"price={price:.8f} "
                f"band=["
                f"{pending.band_low:.8f}, "
                f"{pending.band_high:.8f}"
                f"]"
            )

            return pending

        if price > pending.band_high:
            pending.reason = "waiting_pullback_to_band"
        else:
            # Pasó por debajo de band_low pero todavía conserva
            # compression_high: esperamos una posible recuperación.
            pending.reason = "below_band_waiting_recovery"

        return None

    def mark_executed(self, symbol: str):
        pending = self.remove(symbol)

        if pending is None:
            return

        pending.status = "FILLED"
        pending.reason = "execution_opened"

        print(
            f"[BUCKET V2 FILLED] "
            f"symbol={symbol} "
            f"trigger_price={pending.last_price}"
        )

    def mark_failed(self, symbol: str, reason: str):
        pending = self.remove(symbol)

        if pending is None:
            return

        pending.status = "FAILED"
        pending.reason = reason

        print(
            f"[BUCKET V2 FAILED] "
            f"symbol={symbol} "
            f"reason={reason}"
        )