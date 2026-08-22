from copy import deepcopy
from dataclasses import dataclass
import math
from typing import Any, Optional


TERMINAL_STATUSES = {"EXPIRED", "INVALIDATED", "FILLED", "FAILED"}


@dataclass
class PendingBucketEntry:
    symbol: str
    setup_id: str
    plan: Any
    trade_action: Any
    signal_context: dict
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
    reason: str = "bucket_v2_compression_breakout_armed"

    def to_dict(self):
        plan = self.plan.to_dict() if hasattr(self.plan, "to_dict") else self.plan
        action = (
            {"reason": self.trade_action.reason,
             "strategy_name": self.trade_action.strategy_name,
             "execution_variant": self.trade_action.execution_variant.value if self.trade_action.execution_variant else None,
             "setup_id": self.trade_action.setup_id,
             "signal_reason": self.trade_action.signal_reason}
            if hasattr(self.trade_action, "reason") else self.trade_action
        )
        return {
            "symbol": self.symbol, "setup_id": self.setup_id,
            "plan": plan, "trade_action": action,
            "signal_context": deepcopy(self.signal_context),
            "breakout_price": self.breakout_price,
            "compression_high": self.compression_high,
            "breakout_extension_pct": self.breakout_extension_pct,
            "band_low": self.band_low, "band_high": self.band_high,
            "armed_ts": self.armed_ts, "expires_ts": self.expires_ts,
            "last_price": self.last_price, "last_update_ts": self.last_update_ts,
            "status": self.status, "reason": self.reason,
        }


class BucketTouchEntryManager:
    def __init__(self, breakout_min_pct=.50, breakout_max_pct=.75,
                 entry_band_min_pct=-.25, entry_band_max_pct=0,
                 expiry_minutes=150, on_change=None):
        self.breakout_min_pct = breakout_min_pct
        self.breakout_max_pct = breakout_max_pct
        self.entry_band_min_pct = entry_band_min_pct
        self.entry_band_max_pct = entry_band_max_pct
        self.expiry_ms = int(expiry_minutes * 60 * 1000)
        self.pending = {}
        self.history = []
        self.on_change = on_change

    @staticmethod
    def _ms(timestamp):
        timestamp = int(timestamp)
        return timestamp * 1000 if timestamp < 10_000_000_000 else timestamp

    def _changed(self):
        if self.on_change:
            self.on_change(self.snapshot())

    def get(self, symbol): return self.pending.get(symbol)
    def symbols(self): return list(self.pending)

    def arm(self, plan, trade_action, timestamp):
        context = deepcopy(plan.signal_context or {})
        try:
            breakout_price = float(context["breakout_price"])
            extension = float(context["breakout_extension_pct"])
            high = float(context["compression_high"])
        except (KeyError, TypeError, ValueError):
            return False
        if not all(math.isfinite(x) for x in (breakout_price, extension, high)):
            return False
        if not self.breakout_min_pct < extension <= self.breakout_max_pct:
            return False
        if plan.symbol in self.pending or not plan.setup_id:
            return False
        armed = self._ms(timestamp)
        item = PendingBucketEntry(
            symbol=plan.symbol, setup_id=plan.setup_id, plan=plan,
            trade_action=trade_action, signal_context=context,
            breakout_price=breakout_price, compression_high=high,
            breakout_extension_pct=extension,
            band_low=breakout_price * (1 + self.entry_band_min_pct / 100),
            band_high=breakout_price * (1 + self.entry_band_max_pct / 100),
            armed_ts=armed, expires_ts=armed + self.expiry_ms,
        )
        self.pending[plan.symbol] = item
        self._changed()
        return True

    def evaluate_price(self, symbol, price, timestamp):
        item = self.pending.get(symbol)
        if item is None: return None
        price, timestamp = float(price), self._ms(timestamp)
        if not math.isfinite(price) or price <= 0: return None
        item.last_price, item.last_update_ts = price, timestamp
        if timestamp >= item.expires_ts:
            return self.cancel(symbol, "EXPIRED", "bucket_touch_timeout")
        if price < item.compression_high:
            return self.cancel(symbol, "INVALIDATED", "compression_high_lost")
        if item.band_low <= price <= item.band_high:
            item.status = "TRIGGERED"
            item.reason = "bucket_v2_retrace_band_triggered"
            self._changed()
            return item
        item.reason = "waiting_pullback_to_band" if price > item.band_high else "below_band_waiting_recovery"
        self._changed()
        return None

    def cancel(self, symbol, status, reason):
        item = self.pending.pop(symbol, None)
        if item is None: return None
        item.status, item.reason = status, reason
        self.history.append(item)
        self._changed()
        return None

    def mark_executed(self, symbol): self.cancel(symbol, "FILLED", "execution_opened")
    def mark_failed(self, symbol, reason): self.cancel(symbol, "FAILED", reason)
    def shutdown(self):
        """Persist active waits without changing their state or expiry."""
        self._changed()

    def snapshot(self):
        return {"active": [x.to_dict() for x in self.pending.values()],
                "history": [x.to_dict() for x in self.history]}

    def restore(self, items, plan_loader=lambda value: value, action_loader=lambda value: value,
                history_items=None):
        self.pending.clear()
        self.history.clear()
        for raw in deepcopy(items):
            raw["plan"] = plan_loader(raw["plan"])
            raw["trade_action"] = action_loader(raw["trade_action"])
            item = PendingBucketEntry(**raw)
            if item.status not in TERMINAL_STATUSES:
                self.pending[item.symbol] = item
        for raw in deepcopy(history_items or []):
            raw["plan"] = plan_loader(raw["plan"])
            raw["trade_action"] = action_loader(raw["trade_action"])
            self.history.append(PendingBucketEntry(**raw))
        self._changed()
