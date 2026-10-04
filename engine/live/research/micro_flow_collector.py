import argparse
import csv
import json
import math
import os
import signal
import statistics
import time
from collections import defaultdict, deque
from pathlib import Path

import redis

from engine.live.data.redis_market_data_protocol import (
    MICRO_FLOW_COLLECTOR_CURSOR_KEY,
    MICRO_FLOW_COLLECTOR_HEARTBEAT_KEY,
    MICRO_FLOW_COLLECTOR_HEARTBEAT_TTL_SECONDS,
    MICRO_FLOW_COLLECTOR_STATUS_KEY,
    MICRO_FLOW_SECONDS_STREAM,
)


SCHEMA_VERSION = 2
WINDOWS_SECONDS = (1, 3, 5, 15)
OUTCOME_HORIZONS_MINUTES = (1, 3, 5)


BASE_EVENT_COLUMNS = [
    "schema_version",
    "event_id",
    "timestamp",
    "event_time_utc",
    "symbol",
    "event_type",
    "side",
    "open",
    "high",
    "low",
    "close",
    "trade_count",
    "buy_trades",
    "sell_trades",
    "buy_notional",
    "sell_notional",
    "total_notional",
    "flow_imbalance_1s",
    "flow_imbalance_3s",
    "flow_imbalance_5s",
    "flow_imbalance_15s",
    "return_1s_pct",
    "return_3s_pct",
    "return_5s_pct",
    "return_15s_pct",
    "notional_5s",
    "trades_5s",
    "relative_activity_5s",
    "trade_acceleration_5s",
    "flow_price_alignment_5s",
    "flow_delta_1_vs_5",
    "price_efficiency_5s",
    "directional_price_response_5s_pct",
    "btc_return_5s_pct",
    "btc_return_15s_pct",
    "residual_vs_btc_5s_pct",
    "residual_vs_btc_15s_pct",
    "entry_timestamp",
    "entry_time_utc",
    "entry_price",
    "entry_trade_count",
    "entry_synthetic",
]

OUTCOME_COLUMNS = []
for _horizon in OUTCOME_HORIZONS_MINUTES:
    OUTCOME_COLUMNS.extend(
        [
            f"complete_{_horizon}m",
            f"mfe_{_horizon}m_pct",
            f"mae_{_horizon}m_pct",
            f"return_{_horizon}m_pct",
        ]
    )

EVENT_COLUMNS = BASE_EVENT_COLUMNS + OUTCOME_COLUMNS + [
    "completed_at_utc",
]


class MicroFlowCollector:
    """Consume completed 1-second aggressive-flow states from Redis.

    The collector is deliberately independent from the live trading engine.
    It builds causal 1/3/5/15-second features, creates sparse exploratory
    momentum/absorption events, and completes 1m/3m/5m outcomes from the next
    consecutive second. No live orders are created here.
    """

    def __init__(
        self,
        redis_host="127.0.0.1",
        redis_port=6379,
        redis_db=0,
        snapshots_path="micro_flow_snapshots.csv",
        events_path="micro_flow_events.csv",
        flow_threshold=0.55,
        min_relative_activity=1.50,
        min_momentum_return_pct=0.04,
        absorption_flow_threshold=0.70,
        absorption_max_return_pct=0.02,
        cooldown_seconds=10,
        max_fill_gap_seconds=5,
        baseline_seconds=60,
        baseline_min_samples=20,
    ):
        self.redis = redis.Redis(
            host=redis_host,
            port=int(redis_port),
            db=int(redis_db),
            decode_responses=True,
        )

        self.snapshots_path = Path(snapshots_path)
        self.events_path = Path(events_path)

        self.flow_threshold = float(flow_threshold)
        self.min_relative_activity = float(min_relative_activity)
        self.min_momentum_return_pct = float(min_momentum_return_pct)
        self.absorption_flow_threshold = float(absorption_flow_threshold)
        self.absorption_max_return_pct = float(absorption_max_return_pct)
        self.cooldown_seconds = max(1, int(cooldown_seconds))
        self.max_fill_gap_seconds = max(1, int(max_fill_gap_seconds))
        self.baseline_seconds = max(20, int(baseline_seconds))
        self.baseline_min_samples = max(5, int(baseline_min_samples))

        self.running = False
        self.cursor = "0-0"

        self.bars = defaultdict(lambda: deque(maxlen=900))
        self.features = defaultdict(lambda: deque(maxlen=900))
        self.last_timestamp = {}
        self.last_event_timestamp = {}
        self.pending_events = defaultdict(list)
        self.btc_features = {}
        self.btc_feature_order = deque(maxlen=1200)

        self.stream_messages = 0
        self.real_seconds = 0
        self.synthetic_seconds = 0
        self.large_gaps = 0
        self.invalid_payloads = 0
        self.events_created = 0
        self.events_completed = 0
        self.events_invalidated = 0
        self.synthetic_event_candidates_skipped = 0
        self.synthetic_entry_invalidations = 0
        self.csv_errors = 0
        self.last_message_timestamp = None
        self.started_at_ms = int(time.time() * 1000)
        self.last_status_at = 0.0

    @staticmethod
    def _iso_utc(timestamp_ms):
        if timestamp_ms in (None, ""):
            return ""
        try:
            timestamp_ms = int(timestamp_ms)
        except (TypeError, ValueError):
            return ""
        return time.strftime(
            "%Y-%m-%dT%H:%M:%S",
            time.gmtime(timestamp_ms / 1000.0),
        ) + ".%03dZ" % (timestamp_ms % 1000)

    @staticmethod
    def _safe_float(value, default=0.0):
        try:
            value = float(value)
            if math.isfinite(value):
                return value
        except (TypeError, ValueError):
            pass
        return float(default)

    @staticmethod
    def _flow_imbalance(buy, sell):
        buy = float(buy)
        sell = float(sell)
        total = buy + sell
        if total <= 0:
            return 0.0
        return (buy - sell) / total

    @staticmethod
    def _append_csv(path, columns, row):
        path.parent.mkdir(parents=True, exist_ok=True)
        exists = path.exists() and path.stat().st_size > 0

        if exists:
            try:
                with path.open("r", encoding="utf-8", newline="") as handle:
                    reader = csv.reader(handle)
                    header = next(reader, [])
                if header != list(columns):
                    raise RuntimeError(
                        f"CSV schema mismatch for {path}. "
                        "Move/rename the old research file before starting "
                        "this collector version."
                    )
            except StopIteration:
                exists = False

        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=list(columns),
                extrasaction="ignore",
            )
            if not exists:
                writer.writeheader()
            writer.writerow({key: row.get(key, "") for key in columns})

    def _write_snapshot(self, event):
        row = dict(event)
        for column in OUTCOME_COLUMNS:
            row.setdefault(column, "")
        row.setdefault("completed_at_utc", "")
        try:
            self._append_csv(
                self.snapshots_path,
                EVENT_COLUMNS,
                row,
            )
        except Exception as exc:
            self.csv_errors += 1
            print(
                "[MICRO FLOW COLLECTOR] snapshot write error "
                f"error={type(exc).__name__}:{exc}"
            )

    def _write_completed_event(self, event):
        row = dict(event)
        row["completed_at_utc"] = self._iso_utc(
            int(time.time() * 1000)
        )
        try:
            self._append_csv(
                self.events_path,
                EVENT_COLUMNS,
                row,
            )
            self.events_completed += 1
        except Exception as exc:
            self.csv_errors += 1
            print(
                "[MICRO FLOW COLLECTOR] event write error "
                f"error={type(exc).__name__}:{exc}"
            )

    def _normalize_real_bar(self, payload):
        try:
            symbol = str(payload["symbol"]).upper()
            timestamp = int(payload["timestamp"])
            open_price = float(payload["open"])
            high = float(payload["high"])
            low = float(payload["low"])
            close = float(payload["close"])
        except (KeyError, TypeError, ValueError):
            return None

        return {
            "symbol": symbol,
            "timestamp": timestamp,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "trade_count": int(payload.get("trade_count", 0) or 0),
            "buy_trades": int(payload.get("buy_trades", 0) or 0),
            "sell_trades": int(payload.get("sell_trades", 0) or 0),
            "buy_notional": self._safe_float(payload.get("buy_notional")),
            "sell_notional": self._safe_float(payload.get("sell_notional")),
            "total_notional": self._safe_float(payload.get("total_notional")),
            "synthetic": False,
        }

    @staticmethod
    def _synthetic_bar(symbol, timestamp, price):
        return {
            "symbol": symbol,
            "timestamp": int(timestamp),
            "open": float(price),
            "high": float(price),
            "low": float(price),
            "close": float(price),
            "trade_count": 0,
            "buy_trades": 0,
            "sell_trades": 0,
            "buy_notional": 0.0,
            "sell_notional": 0.0,
            "total_notional": 0.0,
            "synthetic": True,
        }

    def _reset_symbol_after_gap(self, symbol):
        self.bars[symbol].clear()
        self.features[symbol].clear()
        pending = self.pending_events.pop(symbol, [])
        self.events_invalidated += len(pending)

    def _window(self, symbol, seconds):
        history = self.bars[symbol]
        if len(history) < seconds:
            return None
        return list(history)[-seconds:]

    def _return_pct(self, symbol, seconds, current_close):
        history = self.bars[symbol]
        if len(history) < seconds + 1:
            return None
        reference = float(list(history)[-(seconds + 1)]["close"])
        if reference <= 0:
            return None
        return (float(current_close) / reference - 1.0) * 100.0

    def _build_features(self, bar):
        symbol = bar["symbol"]
        current_close = float(bar["close"])
        history = self.bars[symbol]

        if len(history) < 16:
            return None

        result = dict(bar)

        for window in WINDOWS_SECONDS:
            items = self._window(symbol, window)
            if not items:
                return None
            buy = sum(float(item["buy_notional"]) for item in items)
            sell = sum(float(item["sell_notional"]) for item in items)
            result[f"flow_imbalance_{window}s"] = self._flow_imbalance(
                buy,
                sell,
            )
            result[f"buy_notional_{window}s"] = buy
            result[f"sell_notional_{window}s"] = sell
            result[f"notional_{window}s"] = buy + sell
            result[f"trades_{window}s"] = sum(
                int(item["trade_count"]) for item in items
            )
            result[f"return_{window}s_pct"] = self._return_pct(
                symbol,
                window,
                current_close,
            )

        previous = list(self.features[symbol])[-self.baseline_seconds :]
        baseline_notional_values = [
            float(item["notional_5s"])
            for item in previous
            if item.get("notional_5s") is not None
        ]
        baseline_trade_values = [
            float(item["trades_5s"])
            for item in previous
            if item.get("trades_5s") is not None
        ]

        relative_activity = None
        trade_acceleration = None

        if len(baseline_notional_values) >= self.baseline_min_samples:
            median_notional = statistics.median(baseline_notional_values)
            if median_notional > 0:
                relative_activity = (
                    float(result["notional_5s"]) / median_notional
                )

        if len(baseline_trade_values) >= self.baseline_min_samples:
            median_trades = statistics.median(baseline_trade_values)
            if median_trades > 0:
                trade_acceleration = (
                    float(result["trades_5s"]) / median_trades
                )

        result["relative_activity_5s"] = relative_activity
        result["trade_acceleration_5s"] = trade_acceleration

        flow5 = float(result["flow_imbalance_5s"])
        flow1 = float(result["flow_imbalance_1s"])
        ret5 = result.get("return_5s_pct")

        if ret5 is not None:
            ret5 = float(ret5)
            result["flow_price_alignment_5s"] = flow5 * ret5
            result["flow_delta_1_vs_5"] = flow1 - flow5
            result["price_efficiency_5s"] = (
                abs(ret5) / max(abs(flow5), 0.05)
            )
            result["directional_price_response_5s_pct"] = (
                ret5 * (1.0 if flow5 >= 0 else -1.0)
            )
        else:
            result["flow_price_alignment_5s"] = None
            result["flow_delta_1_vs_5"] = None
            result["price_efficiency_5s"] = None
            result["directional_price_response_5s_pct"] = None

        btc = self._btc_context_for_timestamp(
            int(bar["timestamp"])
        )
        if btc is None:
            result["btc_return_5s_pct"] = None
            result["btc_return_15s_pct"] = None
            result["residual_vs_btc_5s_pct"] = None
            result["residual_vs_btc_15s_pct"] = None
        else:
            btc5 = btc.get("return_5s_pct")
            btc15 = btc.get("return_15s_pct")
            result["btc_return_5s_pct"] = btc5
            result["btc_return_15s_pct"] = btc15
            result["residual_vs_btc_5s_pct"] = (
                float(result["return_5s_pct"]) - float(btc5)
                if result.get("return_5s_pct") is not None and btc5 is not None
                else None
            )
            result["residual_vs_btc_15s_pct"] = (
                float(result["return_15s_pct"]) - float(btc15)
                if result.get("return_15s_pct") is not None and btc15 is not None
                else None
            )

        return result

    def _remember_btc_feature(self, feature):
        timestamp = int(feature["timestamp"])
        self.btc_features[timestamp] = {
            "return_5s_pct": feature.get("return_5s_pct"),
            "return_15s_pct": feature.get("return_15s_pct"),
        }
        self.btc_feature_order.append(timestamp)

        while len(self.btc_features) > 1200 and self.btc_feature_order:
            oldest = self.btc_feature_order.popleft()
            self.btc_features.pop(oldest, None)

    def _btc_context_for_timestamp(self, timestamp):
        timestamp = int(timestamp)
        exact = self.btc_features.get(timestamp)
        if exact is not None:
            return exact

        # Combined streams can deliver an alt second before BTC's matching
        # second. Use only an already-completed BTC state from <=1 second ago;
        # never look forward.
        previous = self.btc_features.get(timestamp - 1000)
        return previous

    def _event_types_for_feature(self, feature):
        activity = feature.get("relative_activity_5s")
        flow5 = feature.get("flow_imbalance_5s")
        ret5 = feature.get("return_5s_pct")

        if activity is None or flow5 is None or ret5 is None:
            return []

        activity = float(activity)
        flow5 = float(flow5)
        ret5 = float(ret5)

        if activity < self.min_relative_activity:
            return []

        events = []

        if (
            flow5 >= self.flow_threshold
            and ret5 >= self.min_momentum_return_pct
        ):
            events.append(("MOMENTUM_LONG", "LONG"))

        if (
            flow5 <= -self.flow_threshold
            and ret5 <= -self.min_momentum_return_pct
        ):
            events.append(("MOMENTUM_SHORT", "SHORT"))

        if (
            flow5 >= self.absorption_flow_threshold
            and abs(ret5) <= self.absorption_max_return_pct
        ):
            events.append(("ABSORPTION_SHORT", "SHORT"))

        if (
            flow5 <= -self.absorption_flow_threshold
            and abs(ret5) <= self.absorption_max_return_pct
        ):
            events.append(("ABSORPTION_LONG", "LONG"))

        return events

    def _make_event(self, feature, event_type, side):
        timestamp = int(feature["timestamp"])
        symbol = str(feature["symbol"]).upper()
        event_id = f"{symbol}|{event_type}|{timestamp}"

        event = {
            "schema_version": SCHEMA_VERSION,
            "event_id": event_id,
            "timestamp": timestamp,
            "event_time_utc": self._iso_utc(timestamp),
            "symbol": symbol,
            "event_type": event_type,
            "side": side,
            "entry_timestamp": timestamp + 1000,
            "entry_time_utc": self._iso_utc(timestamp + 1000),
            "entry_price": None,
            "entry_trade_count": None,
            "entry_synthetic": None,
            "_max_high": None,
            "_min_low": None,
        }

        for key in BASE_EVENT_COLUMNS:
            if key in event:
                continue
            event[key] = feature.get(key)

        for horizon in OUTCOME_HORIZONS_MINUTES:
            event[f"complete_{horizon}m"] = False
            event[f"mfe_{horizon}m_pct"] = None
            event[f"mae_{horizon}m_pct"] = None
            event[f"return_{horizon}m_pct"] = None

        return event

    def _maybe_create_events(self, feature):
        symbol = str(feature["symbol"]).upper()
        timestamp = int(feature["timestamp"])
        event_types = self._event_types_for_feature(feature)

        # Synthetic seconds are useful inside rolling time windows because
        # "no trade happened" is real microstructure information. They must
        # never become signal timestamps, however: there was no executed trade
        # in that second and a stale rolling imbalance can otherwise look like
        # fresh absorption/momentum. A real aggTrade second must anchor every
        # research event.
        if bool(feature.get("synthetic", False)) or int(
            feature.get("trade_count", 0) or 0
        ) <= 0:
            self.synthetic_event_candidates_skipped += len(event_types)
            return

        for event_type, side in event_types:
            key = (symbol, event_type)
            last_ts = self.last_event_timestamp.get(key)
            if (
                last_ts is not None
                and timestamp - int(last_ts) < self.cooldown_seconds * 1000
            ):
                continue

            event = self._make_event(
                feature,
                event_type,
                side,
            )
            self.pending_events[symbol].append(event)
            self.last_event_timestamp[key] = timestamp
            self.events_created += 1
            self._write_snapshot(event)

    @staticmethod
    def _apply_outcome(event, horizon_min, close_price):
        entry = event.get("entry_price")
        max_high = event.get("_max_high")
        min_low = event.get("_min_low")

        if entry is None or max_high is None or min_low is None:
            return

        entry = float(entry)
        max_high = float(max_high)
        min_low = float(min_low)
        close_price = float(close_price)

        if entry <= 0:
            return

        if event["side"] == "LONG":
            mfe = max(0.0, (max_high / entry - 1.0) * 100.0)
            mae = max(0.0, (1.0 - min_low / entry) * 100.0)
            ret = (close_price / entry - 1.0) * 100.0
        else:
            mfe = max(0.0, (1.0 - min_low / entry) * 100.0)
            mae = max(0.0, (max_high / entry - 1.0) * 100.0)
            ret = (1.0 - close_price / entry) * 100.0

        event[f"complete_{horizon_min}m"] = True
        event[f"mfe_{horizon_min}m_pct"] = mfe
        event[f"mae_{horizon_min}m_pct"] = mae
        event[f"return_{horizon_min}m_pct"] = ret

    def _update_pending_events(self, bar):
        symbol = str(bar["symbol"]).upper()
        timestamp = int(bar["timestamp"])
        pending = self.pending_events.get(symbol)
        if not pending:
            return

        keep = []

        for event in pending:
            event_ts = int(event["timestamp"])
            entry_ts = int(event["entry_timestamp"])

            if event.get("entry_price") is None:
                if timestamp < entry_ts:
                    keep.append(event)
                    continue
                if timestamp > entry_ts:
                    self.events_invalidated += 1
                    continue

                # Entry is deliberately strict: the immediately following
                # one-second bucket must contain a real executed aggTrade. If
                # that second was synthetic, using its carried-forward price
                # would invent executable liquidity and contaminate MFE/MAE.
                if bool(bar.get("synthetic", False)) or int(
                    bar.get("trade_count", 0) or 0
                ) <= 0:
                    self.synthetic_entry_invalidations += 1
                    self.events_invalidated += 1
                    continue

                event["entry_price"] = float(bar["open"])
                event["entry_trade_count"] = int(bar["trade_count"])
                event["entry_synthetic"] = False

            if timestamp < entry_ts:
                keep.append(event)
                continue

            max_end_ts = event_ts + 5 * 60 * 1000
            if timestamp <= max_end_ts:
                high = float(bar["high"])
                low = float(bar["low"])
                event["_max_high"] = (
                    high
                    if event.get("_max_high") is None
                    else max(float(event["_max_high"]), high)
                )
                event["_min_low"] = (
                    low
                    if event.get("_min_low") is None
                    else min(float(event["_min_low"]), low)
                )

            for horizon in OUTCOME_HORIZONS_MINUTES:
                horizon_end = event_ts + horizon * 60 * 1000
                if (
                    timestamp == horizon_end
                    and not event.get(f"complete_{horizon}m")
                ):
                    self._apply_outcome(
                        event,
                        horizon,
                        bar["close"],
                    )

            if timestamp >= max_end_ts:
                if event.get("complete_5m"):
                    clean = {
                        key: value
                        for key, value in event.items()
                        if not key.startswith("_")
                    }
                    self._write_completed_event(clean)
                else:
                    self.events_invalidated += 1
                continue

            keep.append(event)

        self.pending_events[symbol] = keep

    def _process_contiguous_bar(self, bar):
        symbol = bar["symbol"]

        self._update_pending_events(bar)
        self.bars[symbol].append(bar)

        feature = self._build_features(bar)
        if feature is None:
            return

        self.features[symbol].append(feature)

        if symbol == "BTCUSDT":
            self._remember_btc_feature(feature)

        self._maybe_create_events(feature)

    def process_bar(self, bar):
        symbol = bar["symbol"]
        timestamp = int(bar["timestamp"])
        last_ts = self.last_timestamp.get(symbol)

        if last_ts is not None:
            delta_seconds = int((timestamp - last_ts) / 1000)

            if delta_seconds <= 0:
                return

            if delta_seconds > self.max_fill_gap_seconds + 1:
                self.large_gaps += 1
                self._reset_symbol_after_gap(symbol)
            else:
                previous_price = (
                    float(self.bars[symbol][-1]["close"])
                    if self.bars[symbol]
                    else float(bar["open"])
                )
                for missing in range(1, delta_seconds):
                    synthetic = self._synthetic_bar(
                        symbol=symbol,
                        timestamp=last_ts + missing * 1000,
                        price=previous_price,
                    )
                    self.synthetic_seconds += 1
                    self._process_contiguous_bar(synthetic)

        self.real_seconds += 1
        self._process_contiguous_bar(bar)
        self.last_timestamp[symbol] = timestamp
        self.last_message_timestamp = timestamp

    def _load_cursor(self):
        raw = self.redis.get(MICRO_FLOW_COLLECTOR_CURSOR_KEY)
        if raw:
            self.cursor = str(raw)
        else:
            self.cursor = "0-0"

    def _save_cursor(self):
        self.redis.set(
            MICRO_FLOW_COLLECTOR_CURSOR_KEY,
            self.cursor,
        )

    def _status_payload(self):
        pending_count = sum(
            len(items) for items in self.pending_events.values()
        )
        return {
            "running": bool(self.running),
            "schema_version": SCHEMA_VERSION,
            "stream": MICRO_FLOW_SECONDS_STREAM,
            "cursor": self.cursor,
            "stream_messages": self.stream_messages,
            "real_seconds": self.real_seconds,
            "synthetic_seconds": self.synthetic_seconds,
            "large_gaps": self.large_gaps,
            "invalid_payloads": self.invalid_payloads,
            "symbols_seen": len(self.last_timestamp),
            "symbols_warm": sum(
                1 for values in self.features.values() if len(values) >= 20
            ),
            "events_created": self.events_created,
            "events_completed": self.events_completed,
            "events_invalidated": self.events_invalidated,
            "synthetic_event_candidates_skipped": (
                self.synthetic_event_candidates_skipped
            ),
            "synthetic_entry_invalidations": (
                self.synthetic_entry_invalidations
            ),
            "pending_events": pending_count,
            "csv_errors": self.csv_errors,
            "last_message_timestamp": self.last_message_timestamp,
            "updated_at": int(time.time() * 1000),
            "started_at": self.started_at_ms,
            "execution_policy": {
                "event_requires_real_second": True,
                "entry_requires_immediate_next_real_second": True,
                "synthetic_seconds_allowed_in_rolling_windows": True,
            },
            "thresholds": {
                "flow_threshold": self.flow_threshold,
                "min_relative_activity": self.min_relative_activity,
                "min_momentum_return_pct": self.min_momentum_return_pct,
                "absorption_flow_threshold": self.absorption_flow_threshold,
                "absorption_max_return_pct": self.absorption_max_return_pct,
                "cooldown_seconds": self.cooldown_seconds,
            },
        }

    def _write_status(self):
        now = time.monotonic()
        if now - self.last_status_at < 5.0:
            return
        self.last_status_at = now

        payload = self._status_payload()
        serialized = json.dumps(
            payload,
            separators=(",", ":"),
        )
        self.redis.set(
            MICRO_FLOW_COLLECTOR_STATUS_KEY,
            serialized,
        )
        self.redis.set(
            MICRO_FLOW_COLLECTOR_HEARTBEAT_KEY,
            str(payload["updated_at"]),
            ex=MICRO_FLOW_COLLECTOR_HEARTBEAT_TTL_SECONDS,
        )

        print(
            "[MICRO FLOW COLLECTOR] "
            f"messages={self.stream_messages} "
            f"symbols={payload['symbols_seen']} "
            f"warm={payload['symbols_warm']} "
            f"events={self.events_created} "
            f"completed={self.events_completed} "
            f"pending={payload['pending_events']} "
            f"gaps={self.large_gaps} "
            f"cursor={self.cursor}"
        )

    def run(self):
        if not self.redis.ping():
            raise RuntimeError("Redis connection failed")

        self._load_cursor()
        self.running = True

        print(
            "[MICRO FLOW COLLECTOR] started "
            f"stream={MICRO_FLOW_SECONDS_STREAM} "
            f"cursor={self.cursor} "
            f"snapshots={self.snapshots_path} "
            f"events={self.events_path}"
        )

        try:
            while self.running:
                rows = self.redis.xread(
                    {MICRO_FLOW_SECONDS_STREAM: self.cursor},
                    count=2000,
                    block=1000,
                )

                if not rows:
                    self._write_status()
                    continue

                for _stream_name, messages in rows:
                    for stream_id, fields in messages:
                        self.cursor = stream_id
                        self.stream_messages += 1

                        raw_payload = fields.get("payload")
                        if not raw_payload:
                            self.invalid_payloads += 1
                            continue

                        try:
                            payload = json.loads(raw_payload)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            self.invalid_payloads += 1
                            continue

                        bar = self._normalize_real_bar(payload)
                        if bar is None:
                            self.invalid_payloads += 1
                            continue

                        self.process_bar(bar)

                self._save_cursor()
                self._write_status()

        finally:
            self.running = False
            try:
                self._write_status()
            except Exception:
                pass

    def stop(self):
        self.running = False


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--redis-host",
        default=os.getenv("REDIS_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--redis-port",
        type=int,
        default=int(os.getenv("REDIS_PORT", "6379")),
    )
    parser.add_argument(
        "--redis-db",
        type=int,
        default=int(os.getenv("REDIS_DB", "0")),
    )
    parser.add_argument(
        "--snapshots-path",
        default=os.getenv(
            "MICRO_FLOW_SNAPSHOTS_PATH",
            "micro_flow_snapshots.csv",
        ),
    )
    parser.add_argument(
        "--events-path",
        default=os.getenv(
            "MICRO_FLOW_EVENTS_PATH",
            "micro_flow_events.csv",
        ),
    )
    parser.add_argument(
        "--flow-threshold",
        type=float,
        default=float(os.getenv("MICRO_FLOW_FLOW_THRESHOLD", "0.55")),
    )
    parser.add_argument(
        "--min-relative-activity",
        type=float,
        default=float(os.getenv("MICRO_FLOW_MIN_RELATIVE_ACTIVITY", "1.50")),
    )
    parser.add_argument(
        "--min-momentum-return-pct",
        type=float,
        default=float(os.getenv("MICRO_FLOW_MIN_MOMENTUM_RETURN_PCT", "0.04")),
    )
    parser.add_argument(
        "--absorption-flow-threshold",
        type=float,
        default=float(os.getenv("MICRO_FLOW_ABSORPTION_FLOW_THRESHOLD", "0.70")),
    )
    parser.add_argument(
        "--absorption-max-return-pct",
        type=float,
        default=float(os.getenv("MICRO_FLOW_ABSORPTION_MAX_RETURN_PCT", "0.02")),
    )
    parser.add_argument(
        "--cooldown-seconds",
        type=int,
        default=int(os.getenv("MICRO_FLOW_COOLDOWN_SECONDS", "10")),
    )
    return parser.parse_args()


def main():
    args = parse_args()
    collector = MicroFlowCollector(
        redis_host=args.redis_host,
        redis_port=args.redis_port,
        redis_db=args.redis_db,
        snapshots_path=args.snapshots_path,
        events_path=args.events_path,
        flow_threshold=args.flow_threshold,
        min_relative_activity=args.min_relative_activity,
        min_momentum_return_pct=args.min_momentum_return_pct,
        absorption_flow_threshold=args.absorption_flow_threshold,
        absorption_max_return_pct=args.absorption_max_return_pct,
        cooldown_seconds=args.cooldown_seconds,
    )

    def request_stop(signum, frame):
        print(
            "[MICRO FLOW COLLECTOR] "
            f"received signal={signum}"
        )
        collector.stop()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    collector.run()


if __name__ == "__main__":
    main()
