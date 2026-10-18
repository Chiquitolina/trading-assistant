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
    KEY_PREFIX,
    MICRO_FLOW_COLLECTOR_HEARTBEAT_TTL_SECONDS,
    MICRO_FLOW_SECONDS_STREAM,
    MICRO_FLOW_SHARD_STATUS_PREFIX,
    MICRO_FLOW_TRANSPORT_STATE_KEY,
)


SCHEMA_VERSION = 1
WINDOWS_SECONDS = (1, 3, 5, 15)
OUTCOME_HORIZONS_MINUTES = (1, 3, 5)
FIRST_TOUCH_THRESHOLDS_PCT = (0.10, 0.15, 0.20, 0.30)
FIRST_TOUCH_SCHEMA_VERSION = 1

MICRO_FLOW_V2_STATUS_KEY = f"{KEY_PREFIX}:micro-flow:v2:status"
MICRO_FLOW_V2_HEARTBEAT_KEY = f"{KEY_PREFIX}:micro-flow:v2:heartbeat"
MICRO_FLOW_V2_CURSOR_KEY = f"{KEY_PREFIX}:micro-flow:v2:cursor"


def _first_touch_tag(value):
    return f"{int(round(float(value) * 100)):03d}"


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


EPISODE_COLUMNS = [
    "schema_version",
    "episode_id",
    "symbol",
    "burst_side",
    "start_timestamp",
    "start_time_utc",
    "end_timestamp",
    "end_time_utc",
    "end_reason",
    "valid",
    "duration_seconds",
    "start_price",
    "end_price",
    "peak_price",
    "peak_timestamp",
    "peak_time_utc",
    "peak_move_pct",
    "start_flow_imbalance_5s",
    "start_flow_imbalance_15s",
    "start_relative_activity_5s",
    "start_price_efficiency_5s",
    "peak_directional_flow_5s",
    "peak_relative_activity_5s",
    "peak_price_efficiency_5s",
    "cumulative_total_notional",
    "cumulative_directional_notional",
    "post_peak_directional_notional",
    "post_peak_aggression_x_baseline",
    "max_retrace_from_peak_pct",
    "max_seconds_since_price_extreme",
    "max_arm_score",
    "armed_timestamp",
    "armed_time_utc",
    "confirmed_timestamp",
    "confirmed_time_utc",
    "reversal_side",
    "arm_score_at_confirm",
    "confirm_score",
    "flow_retention_at_confirm",
    "efficiency_retention_at_confirm",
    "retrace_at_confirm_pct",
    "seconds_since_extreme_at_confirm",
    "post_peak_aggression_x_baseline_at_confirm",
]

TRANSITION_COLUMNS = [
    "schema_version",
    "episode_id",
    "symbol",
    "burst_side",
    "timestamp",
    "time_utc",
    "from_state",
    "to_state",
    "age_seconds",
    "price",
    "peak_move_pct",
    "flow_imbalance_1s",
    "flow_imbalance_5s",
    "flow_imbalance_15s",
    "relative_activity_5s",
    "trade_acceleration_5s",
    "price_efficiency_5s",
    "flow_retention_ratio",
    "efficiency_retention_ratio",
    "retrace_from_peak_pct",
    "seconds_since_price_extreme",
    "post_peak_aggression_x_baseline",
    "arm_score",
    "arm_flags",
    "confirm_score",
    "confirm_flags",
]

BASE_CANDIDATE_COLUMNS = [
    "schema_version",
    "event_id",
    "episode_id",
    "timestamp",
    "event_time_utc",
    "symbol",
    "event_type",
    "side",
    "burst_side",
    "burst_age_seconds",
    "burst_start_price",
    "burst_peak_price",
    "burst_peak_move_pct",
    "flow_imbalance_1s",
    "flow_imbalance_5s",
    "flow_imbalance_15s",
    "relative_activity_5s",
    "trade_acceleration_5s",
    "return_1s_pct",
    "return_5s_pct",
    "price_efficiency_5s",
    "peak_directional_flow_5s",
    "peak_price_efficiency_5s",
    "flow_retention_ratio",
    "efficiency_retention_ratio",
    "flow_delta_1_vs_5",
    "retrace_from_peak_pct",
    "seconds_since_price_extreme",
    "post_peak_directional_notional",
    "post_peak_aggression_x_baseline",
    "arm_score",
    "arm_flags",
    "confirm_score",
    "confirm_flags",
    "entry_timestamp",
    "entry_time_utc",
    "entry_price",
    "entry_trade_count",
    "entry_synthetic",
]

OUTCOME_COLUMNS = []
for _horizon in OUTCOME_HORIZONS_MINUTES:
    OUTCOME_COLUMNS.extend([
        f"complete_{_horizon}m",
        f"mfe_{_horizon}m_pct",
        f"mae_{_horizon}m_pct",
        f"return_{_horizon}m_pct",
    ])

CANDIDATE_COLUMNS = BASE_CANDIDATE_COLUMNS + OUTCOME_COLUMNS + [
    "completed_at_utc",
]

FIRST_TOUCH_COLUMNS = [
    "schema_version",
    "event_id",
    "timestamp",
    "event_time_utc",
    "symbol",
    "event_type",
    "side",
    "entry_timestamp",
    "entry_time_utc",
]
for _threshold in FIRST_TOUCH_THRESHOLDS_PCT:
    _tag = _first_touch_tag(_threshold)
    FIRST_TOUCH_COLUMNS.extend([
        f"tp_{_tag}_timestamp",
        f"sl_{_tag}_timestamp",
    ])


class MicroFlowV2EpisodeCollector:
    """Research-only flow episode / exhaustion collector.

    V2 intentionally consumes the same already-isolated 1-second stream as V1.
    It never opens Binance sockets and never places orders. Instead of treating
    each 10-second burst as another independent signal, it models one episode
    per symbol and records its causal lifecycle:

        IDLE -> BURST_STARTED -> EXPANDING -> MATURE
             -> EXHAUSTION_ARMED -> REVERSAL_CONFIRMED -> ENDED

    Thresholds are deliberately broad and configurable. The purpose is to build
    a dataset that can tell us which *changes through the episode* carry edge,
    not to declare the defaults a trading strategy.
    """

    def __init__(
        self,
        redis_host="127.0.0.1",
        redis_port=6379,
        redis_db=0,
        episodes_path="micro_flow_v2_episodes.csv",
        transitions_path="micro_flow_v2_transitions.csv",
        candidate_snapshots_path="micro_flow_v2_candidate_snapshots.csv",
        candidates_path="micro_flow_v2_candidates.csv",
        first_touch_path="micro_flow_v2_first_touch.csv",
        baseline_seconds=60,
        baseline_min_samples=20,
        max_fill_gap_seconds=5,
        start_flow_threshold=0.45,
        start_activity_threshold=1.25,
        start_return_threshold_pct=0.02,
        mature_min_age_seconds=3,
        mature_move_threshold_pct=0.06,
        arm_min_score=2,
        confirm_min_score=2,
        max_episode_seconds=90,
        quiet_seconds=4,
        cooldown_seconds=5,
    ):
        self.redis = redis.Redis(
            host=redis_host,
            port=int(redis_port),
            db=int(redis_db),
            decode_responses=True,
        )

        self.episodes_path = Path(episodes_path)
        self.transitions_path = Path(transitions_path)
        self.candidate_snapshots_path = Path(candidate_snapshots_path)
        self.candidates_path = Path(candidates_path)
        self.first_touch_path = Path(first_touch_path)

        self.baseline_seconds = max(20, int(baseline_seconds))
        self.baseline_min_samples = max(5, int(baseline_min_samples))
        self.max_fill_gap_seconds = max(1, int(max_fill_gap_seconds))

        self.start_flow_threshold = float(start_flow_threshold)
        self.start_activity_threshold = float(start_activity_threshold)
        self.start_return_threshold_pct = float(start_return_threshold_pct)
        self.mature_min_age_seconds = max(1, int(mature_min_age_seconds))
        self.mature_move_threshold_pct = float(mature_move_threshold_pct)
        self.arm_min_score = max(1, int(arm_min_score))
        self.confirm_min_score = max(1, int(confirm_min_score))
        self.max_episode_seconds = max(10, int(max_episode_seconds))
        self.quiet_seconds = max(1, int(quiet_seconds))
        self.cooldown_seconds = max(0, int(cooldown_seconds))

        self.running = False
        self.cursor = "0-0"
        self.started_at_ms = int(time.time() * 1000)
        self.last_status_at = 0.0

        self.bars = defaultdict(lambda: deque(maxlen=900))
        self.features = defaultdict(lambda: deque(maxlen=900))
        self.last_timestamp = {}
        self.active_episodes = {}
        self.last_episode_end_timestamp = {}
        self.pending_candidates = defaultdict(list)

        self.transport_global_gap_open = False
        self.transport_open_shards = {}
        self.transport_gap_symbols = set()

        self.stream_messages = 0
        self.real_seconds = 0
        self.synthetic_seconds = 0
        self.large_gaps = 0
        self.invalid_payloads = 0
        self.transport_markers = 0
        self.transport_gaps = 0
        self.transport_gap_seconds_skipped = 0
        self.transport_episodes_invalidated = 0
        self.transport_candidates_invalidated = 0

        self.episodes_started = 0
        self.episodes_completed = 0
        self.episodes_invalidated = 0
        self.transitions_written = 0
        self.candidates_created = 0
        self.candidates_completed = 0
        self.candidates_invalidated = 0
        self.synthetic_entry_invalidations = 0
        self.csv_errors = 0
        self.last_message_timestamp = None

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
    def _append_csv(path, columns, row):
        path.parent.mkdir(parents=True, exist_ok=True)
        exists = path.exists() and path.stat().st_size > 0
        if exists:
            with path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.reader(handle)
                header = next(reader, [])
            if header != list(columns):
                raise RuntimeError(
                    f"CSV schema mismatch for {path}. "
                    "Move/rename the old V2 research file before starting."
                )

        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=list(columns),
                extrasaction="ignore",
            )
            if not exists:
                writer.writeheader()
            writer.writerow({key: row.get(key, "") for key in columns})

    @staticmethod
    def _flow_imbalance(buy, sell):
        buy = float(buy)
        sell = float(sell)
        total = buy + sell
        if total <= 0:
            return 0.0
        return (buy - sell) / total

    def _normalize_real_bar(self, payload):
        if not isinstance(payload, dict):
            return None
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
            "shard_id": payload.get("shard_id"),
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
            "shard_id": None,
            "synthetic": True,
        }

    def _window(self, symbol, seconds):
        history = self.bars[symbol]
        if len(history) < seconds:
            return []
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
        if len(self.bars[symbol]) < 16:
            return None

        result = dict(bar)
        for window in WINDOWS_SECONDS:
            items = self._window(symbol, window)
            if not items:
                return None
            buy = sum(float(item["buy_notional"]) for item in items)
            sell = sum(float(item["sell_notional"]) for item in items)
            result[f"flow_imbalance_{window}s"] = self._flow_imbalance(buy, sell)
            result[f"buy_notional_{window}s"] = buy
            result[f"sell_notional_{window}s"] = sell
            result[f"notional_{window}s"] = buy + sell
            result[f"trades_{window}s"] = sum(int(item["trade_count"]) for item in items)
            result[f"return_{window}s_pct"] = self._return_pct(
                symbol,
                window,
                current_close,
            )

        previous = list(self.features[symbol])[-self.baseline_seconds:]
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

        median_notional = None
        median_trades = None
        if len(baseline_notional_values) >= self.baseline_min_samples:
            median_notional = statistics.median(baseline_notional_values)
        if len(baseline_trade_values) >= self.baseline_min_samples:
            median_trades = statistics.median(baseline_trade_values)

        result["baseline_median_notional_5s"] = median_notional
        result["baseline_median_trades_5s"] = median_trades
        result["relative_activity_5s"] = (
            float(result["notional_5s"]) / median_notional
            if median_notional is not None and median_notional > 0
            else None
        )
        result["trade_acceleration_5s"] = (
            float(result["trades_5s"]) / median_trades
            if median_trades is not None and median_trades > 0
            else None
        )

        flow5 = float(result["flow_imbalance_5s"])
        flow1 = float(result["flow_imbalance_1s"])
        ret5 = result.get("return_5s_pct")
        if ret5 is not None:
            ret5 = float(ret5)
            result["flow_price_alignment_5s"] = flow5 * ret5
            result["flow_delta_1_vs_5"] = flow1 - flow5
            result["price_efficiency_5s"] = abs(ret5) / max(abs(flow5), 0.05)
        else:
            result["flow_price_alignment_5s"] = None
            result["flow_delta_1_vs_5"] = None
            result["price_efficiency_5s"] = None
        return result

    @staticmethod
    def _directional_price_move(direction, start_price, price):
        if start_price <= 0:
            return 0.0
        return direction * (float(price) / float(start_price) - 1.0) * 100.0

    def _episode_metrics(self, episode, feature):
        direction = int(episode["direction"])
        peak_flow = max(float(episode.get("peak_directional_flow_5s") or 0.0), 1e-9)
        directional_flow = direction * float(feature.get("flow_imbalance_5s") or 0.0)
        flow_retention = max(0.0, directional_flow) / peak_flow

        peak_eff = max(float(episode.get("peak_price_efficiency_5s") or 0.0), 1e-9)
        current_eff = max(float(feature.get("price_efficiency_5s") or 0.0), 0.0)
        efficiency_retention = current_eff / peak_eff

        close_price = float(feature["close"])
        peak_price = float(episode["peak_price"])
        if direction > 0:
            retrace = max(0.0, (peak_price - close_price) / peak_price * 100.0)
        else:
            retrace = max(0.0, (close_price - peak_price) / peak_price * 100.0)

        seconds_since_extreme = max(
            0,
            int((int(feature["timestamp"]) - int(episode["peak_timestamp"])) / 1000),
        )

        baseline_notional = feature.get("baseline_median_notional_5s")
        post_peak_x = None
        if baseline_notional is not None and float(baseline_notional) > 0:
            post_peak_x = (
                float(episode.get("post_peak_directional_notional") or 0.0)
                / float(baseline_notional)
            )

        age_seconds = max(
            0,
            int((int(feature["timestamp"]) - int(episode["start_timestamp"])) / 1000),
        )
        return {
            "age_seconds": age_seconds,
            "directional_flow_5s": directional_flow,
            "flow_retention_ratio": flow_retention,
            "efficiency_retention_ratio": efficiency_retention,
            "retrace_from_peak_pct": retrace,
            "seconds_since_price_extreme": seconds_since_extreme,
            "post_peak_aggression_x_baseline": post_peak_x,
        }

    def _arm_flags(self, episode, feature, metrics):
        flags = []
        if metrics["flow_retention_ratio"] <= 0.70:
            flags.append("flow_decay")
        if metrics["efficiency_retention_ratio"] <= 0.65:
            flags.append("efficiency_decay")
        post_peak_x = metrics.get("post_peak_aggression_x_baseline")
        if (
            metrics["seconds_since_price_extreme"] >= 2
            and post_peak_x is not None
            and post_peak_x >= 0.50
        ):
            flags.append("aggression_without_progress")
        if metrics["retrace_from_peak_pct"] >= 0.03:
            flags.append("price_retrace")
        return flags

    def _confirm_flags(self, episode, feature, metrics):
        direction = int(episode["direction"])
        flags = []
        flow1 = float(feature.get("flow_imbalance_1s") or 0.0)
        if direction * flow1 < 0:
            flags.append("flow_1s_flip")

        flow_delta = feature.get("flow_delta_1_vs_5")
        if flow_delta is not None and direction * float(flow_delta) <= -0.15:
            flags.append("flow_delta_reversal")

        ret1 = feature.get("return_1s_pct")
        if ret1 is not None and direction * float(ret1) <= -0.02:
            flags.append("price_1s_reversal")

        if metrics["retrace_from_peak_pct"] >= 0.05:
            flags.append("peak_retrace")
        return flags

    def _transition(self, episode, feature, to_state, arm_flags=None, confirm_flags=None):
        from_state = episode["state"]
        if from_state == to_state:
            return
        episode["state"] = to_state
        episode["state_changed_at"] = int(feature["timestamp"])
        metrics = self._episode_metrics(episode, feature)
        arm_flags = list(arm_flags or [])
        confirm_flags = list(confirm_flags or [])
        row = {
            "schema_version": SCHEMA_VERSION,
            "episode_id": episode["episode_id"],
            "symbol": episode["symbol"],
            "burst_side": episode["burst_side"],
            "timestamp": int(feature["timestamp"]),
            "time_utc": _iso_utc(feature["timestamp"]),
            "from_state": from_state,
            "to_state": to_state,
            "age_seconds": metrics["age_seconds"],
            "price": float(feature["close"]),
            "peak_move_pct": float(episode["peak_move_pct"]),
            "flow_imbalance_1s": feature.get("flow_imbalance_1s"),
            "flow_imbalance_5s": feature.get("flow_imbalance_5s"),
            "flow_imbalance_15s": feature.get("flow_imbalance_15s"),
            "relative_activity_5s": feature.get("relative_activity_5s"),
            "trade_acceleration_5s": feature.get("trade_acceleration_5s"),
            "price_efficiency_5s": feature.get("price_efficiency_5s"),
            "flow_retention_ratio": metrics["flow_retention_ratio"],
            "efficiency_retention_ratio": metrics["efficiency_retention_ratio"],
            "retrace_from_peak_pct": metrics["retrace_from_peak_pct"],
            "seconds_since_price_extreme": metrics["seconds_since_price_extreme"],
            "post_peak_aggression_x_baseline": metrics["post_peak_aggression_x_baseline"],
            "arm_score": len(arm_flags),
            "arm_flags": "|".join(arm_flags),
            "confirm_score": len(confirm_flags),
            "confirm_flags": "|".join(confirm_flags),
        }
        try:
            self._append_csv(self.transitions_path, TRANSITION_COLUMNS, row)
            self.transitions_written += 1
        except Exception as exc:
            self.csv_errors += 1
            print(
                "[MICRO FLOW V2] transition write error "
                f"error={type(exc).__name__}:{exc}"
            )

    def _can_start_episode(self, symbol, feature):
        if bool(feature.get("synthetic", False)):
            return False
        if int(feature.get("trade_count", 0) or 0) <= 0:
            return False
        activity = feature.get("relative_activity_5s")
        flow5 = feature.get("flow_imbalance_5s")
        ret5 = feature.get("return_5s_pct")
        if activity is None or flow5 is None or ret5 is None:
            return False
        if float(activity) < self.start_activity_threshold:
            return False
        if abs(float(flow5)) < self.start_flow_threshold:
            return False
        direction = 1 if float(flow5) > 0 else -1
        if direction * float(ret5) < self.start_return_threshold_pct:
            return False

        last_end = self.last_episode_end_timestamp.get(symbol)
        if last_end is not None:
            elapsed = (int(feature["timestamp"]) - int(last_end)) / 1000.0
            if elapsed < self.cooldown_seconds:
                return False
        return True

    def _start_episode(self, feature):
        symbol = str(feature["symbol"]).upper()
        timestamp = int(feature["timestamp"])
        flow5 = float(feature["flow_imbalance_5s"])
        direction = 1 if flow5 > 0 else -1
        burst_side = "UP" if direction > 0 else "DOWN"
        price = float(feature["close"])
        directional_notional = (
            float(feature.get("buy_notional") or 0.0)
            if direction > 0
            else float(feature.get("sell_notional") or 0.0)
        )
        episode = {
            "episode_id": f"{symbol}|{burst_side}|{timestamp}",
            "symbol": symbol,
            "direction": direction,
            "burst_side": burst_side,
            "state": "BURST_STARTED",
            "state_changed_at": timestamp,
            "start_timestamp": timestamp,
            "start_price": price,
            "start_flow_imbalance_5s": flow5,
            "start_flow_imbalance_15s": feature.get("flow_imbalance_15s"),
            "start_relative_activity_5s": feature.get("relative_activity_5s"),
            "start_price_efficiency_5s": feature.get("price_efficiency_5s"),
            "peak_price": price,
            "peak_timestamp": timestamp,
            "peak_move_pct": 0.0,
            "peak_directional_flow_5s": max(0.0, direction * flow5),
            "peak_relative_activity_5s": float(feature.get("relative_activity_5s") or 0.0),
            "peak_price_efficiency_5s": float(feature.get("price_efficiency_5s") or 0.0),
            "cumulative_total_notional": float(feature.get("total_notional") or 0.0),
            "cumulative_directional_notional": directional_notional,
            "post_peak_directional_notional": 0.0,
            "max_retrace_from_peak_pct": 0.0,
            "max_seconds_since_price_extreme": 0,
            "max_arm_score": 0,
            "armed_timestamp": None,
            "confirmed_timestamp": None,
            "arm_score_at_confirm": None,
            "confirm_score": None,
            "confirm_metrics": {},
            "quiet_count": 0,
        }
        self.active_episodes[symbol] = episode
        self.episodes_started += 1

        # Write the initial state explicitly. _transition() is for changes.
        row = {
            "schema_version": SCHEMA_VERSION,
            "episode_id": episode["episode_id"],
            "symbol": symbol,
            "burst_side": burst_side,
            "timestamp": timestamp,
            "time_utc": _iso_utc(timestamp),
            "from_state": "IDLE",
            "to_state": "BURST_STARTED",
            "age_seconds": 0,
            "price": price,
            "peak_move_pct": 0.0,
            "flow_imbalance_1s": feature.get("flow_imbalance_1s"),
            "flow_imbalance_5s": feature.get("flow_imbalance_5s"),
            "flow_imbalance_15s": feature.get("flow_imbalance_15s"),
            "relative_activity_5s": feature.get("relative_activity_5s"),
            "trade_acceleration_5s": feature.get("trade_acceleration_5s"),
            "price_efficiency_5s": feature.get("price_efficiency_5s"),
            "flow_retention_ratio": 1.0,
            "efficiency_retention_ratio": 1.0,
            "retrace_from_peak_pct": 0.0,
            "seconds_since_price_extreme": 0,
            "post_peak_aggression_x_baseline": 0.0,
            "arm_score": 0,
            "arm_flags": "",
            "confirm_score": 0,
            "confirm_flags": "",
        }
        try:
            self._append_csv(self.transitions_path, TRANSITION_COLUMNS, row)
            self.transitions_written += 1
        except Exception as exc:
            self.csv_errors += 1
            print(
                "[MICRO FLOW V2] start transition write error "
                f"error={type(exc).__name__}:{exc}"
            )

    def _update_episode_peaks(self, episode, feature):
        direction = int(episode["direction"])
        bar_high = float(feature["high"])
        bar_low = float(feature["low"])
        previous_peak_price = float(episode["peak_price"])
        new_peak_price = previous_peak_price
        if direction > 0 and bar_high > previous_peak_price:
            new_peak_price = bar_high
        elif direction < 0 and bar_low < previous_peak_price:
            new_peak_price = bar_low

        directional_notional = (
            float(feature.get("buy_notional") or 0.0)
            if direction > 0
            else float(feature.get("sell_notional") or 0.0)
        )
        episode["cumulative_total_notional"] += float(feature.get("total_notional") or 0.0)
        episode["cumulative_directional_notional"] += directional_notional

        if new_peak_price != previous_peak_price:
            episode["peak_price"] = new_peak_price
            episode["peak_timestamp"] = int(feature["timestamp"])
            episode["peak_move_pct"] = max(
                float(episode["peak_move_pct"]),
                self._directional_price_move(
                    direction,
                    float(episode["start_price"]),
                    new_peak_price,
                ),
            )
            episode["post_peak_directional_notional"] = 0.0
        else:
            episode["post_peak_directional_notional"] += directional_notional

        dir_flow5 = direction * float(feature.get("flow_imbalance_5s") or 0.0)
        episode["peak_directional_flow_5s"] = max(
            float(episode["peak_directional_flow_5s"]),
            dir_flow5,
        )
        episode["peak_relative_activity_5s"] = max(
            float(episode["peak_relative_activity_5s"]),
            float(feature.get("relative_activity_5s") or 0.0),
        )
        episode["peak_price_efficiency_5s"] = max(
            float(episode["peak_price_efficiency_5s"]),
            float(feature.get("price_efficiency_5s") or 0.0),
        )

    def _create_candidate(self, episode, feature, arm_flags, confirm_flags):
        timestamp = int(feature["timestamp"])
        symbol = episode["symbol"]
        side = "SHORT" if int(episode["direction"]) > 0 else "LONG"
        event_type = f"REVERSAL_{side}"
        metrics = self._episode_metrics(episode, feature)
        event_id = f"{symbol}|{event_type}|{timestamp}"
        candidate = {
            "schema_version": SCHEMA_VERSION,
            "event_id": event_id,
            "episode_id": episode["episode_id"],
            "timestamp": timestamp,
            "event_time_utc": _iso_utc(timestamp),
            "symbol": symbol,
            "event_type": event_type,
            "side": side,
            "burst_side": episode["burst_side"],
            "burst_age_seconds": metrics["age_seconds"],
            "burst_start_price": episode["start_price"],
            "burst_peak_price": episode["peak_price"],
            "burst_peak_move_pct": episode["peak_move_pct"],
            "flow_imbalance_1s": feature.get("flow_imbalance_1s"),
            "flow_imbalance_5s": feature.get("flow_imbalance_5s"),
            "flow_imbalance_15s": feature.get("flow_imbalance_15s"),
            "relative_activity_5s": feature.get("relative_activity_5s"),
            "trade_acceleration_5s": feature.get("trade_acceleration_5s"),
            "return_1s_pct": feature.get("return_1s_pct"),
            "return_5s_pct": feature.get("return_5s_pct"),
            "price_efficiency_5s": feature.get("price_efficiency_5s"),
            "peak_directional_flow_5s": episode.get("peak_directional_flow_5s"),
            "peak_price_efficiency_5s": episode.get("peak_price_efficiency_5s"),
            "flow_retention_ratio": metrics["flow_retention_ratio"],
            "efficiency_retention_ratio": metrics["efficiency_retention_ratio"],
            "flow_delta_1_vs_5": feature.get("flow_delta_1_vs_5"),
            "retrace_from_peak_pct": metrics["retrace_from_peak_pct"],
            "seconds_since_price_extreme": metrics["seconds_since_price_extreme"],
            "post_peak_directional_notional": episode.get("post_peak_directional_notional"),
            "post_peak_aggression_x_baseline": metrics.get("post_peak_aggression_x_baseline"),
            "arm_score": len(arm_flags),
            "arm_flags": "|".join(arm_flags),
            "confirm_score": len(confirm_flags),
            "confirm_flags": "|".join(confirm_flags),
            "entry_timestamp": timestamp + 1000,
            "entry_time_utc": _iso_utc(timestamp + 1000),
            "entry_price": None,
            "entry_trade_count": None,
            "entry_synthetic": None,
            "_max_high": None,
            "_min_low": None,
            "_tp_first_touch_ms": {},
            "_sl_first_touch_ms": {},
        }
        for horizon in OUTCOME_HORIZONS_MINUTES:
            candidate[f"complete_{horizon}m"] = False
            candidate[f"mfe_{horizon}m_pct"] = None
            candidate[f"mae_{horizon}m_pct"] = None
            candidate[f"return_{horizon}m_pct"] = None

        self.pending_candidates[symbol].append(candidate)
        self.candidates_created += 1
        snapshot = {
            key: value
            for key, value in candidate.items()
            if not key.startswith("_")
        }
        snapshot["completed_at_utc"] = ""
        try:
            self._append_csv(
                self.candidate_snapshots_path,
                CANDIDATE_COLUMNS,
                snapshot,
            )
        except Exception as exc:
            self.csv_errors += 1
            print(
                "[MICRO FLOW V2] candidate snapshot write error "
                f"error={type(exc).__name__}:{exc}"
            )

    def _finalize_episode(self, symbol, feature, reason, valid=True):
        episode = self.active_episodes.pop(symbol, None)
        if episode is None:
            return
        timestamp = int(feature["timestamp"]) if feature else int(time.time() * 1000)
        close_price = float(feature["close"]) if feature else float(episode["peak_price"])
        metrics = self._episode_metrics(episode, feature) if feature else {
            "age_seconds": max(0, int((timestamp - int(episode["start_timestamp"])) / 1000)),
            "flow_retention_ratio": None,
            "efficiency_retention_ratio": None,
            "retrace_from_peak_pct": None,
            "seconds_since_price_extreme": None,
            "post_peak_aggression_x_baseline": None,
        }
        confirm_metrics = episode.get("confirm_metrics") or {}
        row = {
            "schema_version": SCHEMA_VERSION,
            "episode_id": episode["episode_id"],
            "symbol": symbol,
            "burst_side": episode["burst_side"],
            "start_timestamp": episode["start_timestamp"],
            "start_time_utc": _iso_utc(episode["start_timestamp"]),
            "end_timestamp": timestamp,
            "end_time_utc": _iso_utc(timestamp),
            "end_reason": str(reason),
            "valid": bool(valid),
            "duration_seconds": metrics["age_seconds"],
            "start_price": episode["start_price"],
            "end_price": close_price,
            "peak_price": episode["peak_price"],
            "peak_timestamp": episode["peak_timestamp"],
            "peak_time_utc": _iso_utc(episode["peak_timestamp"]),
            "peak_move_pct": episode["peak_move_pct"],
            "start_flow_imbalance_5s": episode["start_flow_imbalance_5s"],
            "start_flow_imbalance_15s": episode["start_flow_imbalance_15s"],
            "start_relative_activity_5s": episode["start_relative_activity_5s"],
            "start_price_efficiency_5s": episode["start_price_efficiency_5s"],
            "peak_directional_flow_5s": episode["peak_directional_flow_5s"],
            "peak_relative_activity_5s": episode["peak_relative_activity_5s"],
            "peak_price_efficiency_5s": episode["peak_price_efficiency_5s"],
            "cumulative_total_notional": episode["cumulative_total_notional"],
            "cumulative_directional_notional": episode["cumulative_directional_notional"],
            "post_peak_directional_notional": episode["post_peak_directional_notional"],
            "post_peak_aggression_x_baseline": metrics.get("post_peak_aggression_x_baseline"),
            "max_retrace_from_peak_pct": episode["max_retrace_from_peak_pct"],
            "max_seconds_since_price_extreme": episode["max_seconds_since_price_extreme"],
            "max_arm_score": episode["max_arm_score"],
            "armed_timestamp": episode.get("armed_timestamp"),
            "armed_time_utc": _iso_utc(episode.get("armed_timestamp")),
            "confirmed_timestamp": episode.get("confirmed_timestamp"),
            "confirmed_time_utc": _iso_utc(episode.get("confirmed_timestamp")),
            "reversal_side": (
                ("SHORT" if int(episode["direction"]) > 0 else "LONG")
                if episode.get("confirmed_timestamp") is not None
                else ""
            ),
            "arm_score_at_confirm": episode.get("arm_score_at_confirm"),
            "confirm_score": episode.get("confirm_score"),
            "flow_retention_at_confirm": confirm_metrics.get("flow_retention_ratio"),
            "efficiency_retention_at_confirm": confirm_metrics.get("efficiency_retention_ratio"),
            "retrace_at_confirm_pct": confirm_metrics.get("retrace_from_peak_pct"),
            "seconds_since_extreme_at_confirm": confirm_metrics.get("seconds_since_price_extreme"),
            "post_peak_aggression_x_baseline_at_confirm": confirm_metrics.get("post_peak_aggression_x_baseline"),
        }
        try:
            self._append_csv(self.episodes_path, EPISODE_COLUMNS, row)
            if valid:
                self.episodes_completed += 1
            else:
                self.episodes_invalidated += 1
        except Exception as exc:
            self.csv_errors += 1
            print(
                "[MICRO FLOW V2] episode write error "
                f"error={type(exc).__name__}:{exc}"
            )
        self.last_episode_end_timestamp[symbol] = timestamp

    def _update_episode(self, feature):
        symbol = str(feature["symbol"]).upper()
        episode = self.active_episodes.get(symbol)
        if episode is None:
            if self._can_start_episode(symbol, feature):
                self._start_episode(feature)
            return

        self._update_episode_peaks(episode, feature)
        metrics = self._episode_metrics(episode, feature)
        episode["max_retrace_from_peak_pct"] = max(
            float(episode["max_retrace_from_peak_pct"]),
            float(metrics["retrace_from_peak_pct"]),
        )
        episode["max_seconds_since_price_extreme"] = max(
            int(episode["max_seconds_since_price_extreme"]),
            int(metrics["seconds_since_price_extreme"]),
        )

        arm_flags = self._arm_flags(episode, feature, metrics)
        confirm_flags = self._confirm_flags(episode, feature, metrics)
        episode["max_arm_score"] = max(
            int(episode["max_arm_score"]),
            len(arm_flags),
        )

        state = episode["state"]
        timestamp = int(feature["timestamp"])
        state_age = timestamp - int(episode["state_changed_at"])

        if state == "BURST_STARTED" and metrics["age_seconds"] >= 2:
            self._transition(episode, feature, "EXPANDING", arm_flags, confirm_flags)
            state = episode["state"]
            state_age = 0

        if (
            state in {"BURST_STARTED", "EXPANDING"}
            and metrics["age_seconds"] >= self.mature_min_age_seconds
            and float(episode["peak_move_pct"]) >= self.mature_move_threshold_pct
        ):
            self._transition(episode, feature, "MATURE", arm_flags, confirm_flags)
            state = episode["state"]
            state_age = 0

        # Require at least one later second between MATURE -> ARMED -> CONFIRMED.
        if (
            state == "MATURE"
            and state_age >= 1000
            and len(arm_flags) >= self.arm_min_score
            and not bool(feature.get("synthetic", False))
        ):
            episode["armed_timestamp"] = timestamp
            self._transition(
                episode,
                feature,
                "EXHAUSTION_ARMED",
                arm_flags,
                confirm_flags,
            )
            state = episode["state"]
            state_age = 0

        if (
            state == "EXHAUSTION_ARMED"
            and state_age >= 1000
            and len(confirm_flags) >= self.confirm_min_score
            and not bool(feature.get("synthetic", False))
            and int(feature.get("trade_count", 0) or 0) > 0
        ):
            episode["confirmed_timestamp"] = timestamp
            episode["arm_score_at_confirm"] = len(arm_flags)
            episode["confirm_score"] = len(confirm_flags)
            episode["confirm_metrics"] = dict(metrics)
            self._transition(
                episode,
                feature,
                "REVERSAL_CONFIRMED",
                arm_flags,
                confirm_flags,
            )
            self._create_candidate(episode, feature, arm_flags, confirm_flags)
            state = episode["state"]

        flow5 = abs(float(feature.get("flow_imbalance_5s") or 0.0))
        activity = feature.get("relative_activity_5s")
        ret5 = abs(float(feature.get("return_5s_pct") or 0.0))
        quiet = (
            flow5 < 0.20
            and (activity is None or float(activity) < 1.0)
            and ret5 < 0.02
        )
        episode["quiet_count"] = int(episode["quiet_count"]) + 1 if quiet else 0

        if metrics["age_seconds"] >= self.max_episode_seconds:
            self._transition(episode, feature, "ENDED", arm_flags, confirm_flags)
            self._finalize_episode(symbol, feature, "max_duration", valid=True)
            return

        if (
            episode.get("confirmed_timestamp") is not None
            and timestamp - int(episode["confirmed_timestamp"]) >= 5000
        ):
            self._transition(episode, feature, "ENDED", arm_flags, confirm_flags)
            self._finalize_episode(symbol, feature, "post_confirm_window", valid=True)
            return

        if int(episode["quiet_count"]) >= self.quiet_seconds:
            self._transition(episode, feature, "ENDED", arm_flags, confirm_flags)
            self._finalize_episode(symbol, feature, "quiet", valid=True)

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

    def _update_first_touch(self, event, bar):
        entry = event.get("entry_price")
        if entry is None:
            return
        entry = float(entry)
        if entry <= 0:
            return
        timestamp = int(bar["timestamp"])
        high = float(bar["high"])
        low = float(bar["low"])
        if event["side"] == "LONG":
            favorable = max(0.0, (high / entry - 1.0) * 100.0)
            adverse = max(0.0, (1.0 - low / entry) * 100.0)
        else:
            favorable = max(0.0, (1.0 - low / entry) * 100.0)
            adverse = max(0.0, (high / entry - 1.0) * 100.0)

        tp_touches = event.setdefault("_tp_first_touch_ms", {})
        sl_touches = event.setdefault("_sl_first_touch_ms", {})
        for threshold in FIRST_TOUCH_THRESHOLDS_PCT:
            tag = _first_touch_tag(threshold)
            if tp_touches.get(tag) is None and favorable >= threshold:
                tp_touches[tag] = timestamp
            if sl_touches.get(tag) is None and adverse >= threshold:
                sl_touches[tag] = timestamp

    def _write_first_touch_event(self, event):
        row = {
            "schema_version": FIRST_TOUCH_SCHEMA_VERSION,
            "event_id": event.get("event_id"),
            "timestamp": event.get("timestamp"),
            "event_time_utc": event.get("event_time_utc"),
            "symbol": event.get("symbol"),
            "event_type": event.get("event_type"),
            "side": event.get("side"),
            "entry_timestamp": event.get("entry_timestamp"),
            "entry_time_utc": event.get("entry_time_utc"),
        }
        tp_touches = event.get("_tp_first_touch_ms", {}) or {}
        sl_touches = event.get("_sl_first_touch_ms", {}) or {}
        for threshold in FIRST_TOUCH_THRESHOLDS_PCT:
            tag = _first_touch_tag(threshold)
            row[f"tp_{tag}_timestamp"] = tp_touches.get(tag)
            row[f"sl_{tag}_timestamp"] = sl_touches.get(tag)
        try:
            self._append_csv(self.first_touch_path, FIRST_TOUCH_COLUMNS, row)
        except Exception as exc:
            self.csv_errors += 1
            print(
                "[MICRO FLOW V2] first-touch write error "
                f"error={type(exc).__name__}:{exc}"
            )

    def _write_completed_candidate(self, event):
        row = {
            key: value
            for key, value in event.items()
            if not key.startswith("_")
        }
        row["completed_at_utc"] = _iso_utc(int(time.time() * 1000))
        try:
            self._append_csv(self.candidates_path, CANDIDATE_COLUMNS, row)
            self.candidates_completed += 1
            self._write_first_touch_event(event)
        except Exception as exc:
            self.csv_errors += 1
            print(
                "[MICRO FLOW V2] candidate write error "
                f"error={type(exc).__name__}:{exc}"
            )

    def _update_pending_candidates(self, bar):
        symbol = str(bar["symbol"]).upper()
        timestamp = int(bar["timestamp"])
        pending = self.pending_candidates.get(symbol)
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
                    self.candidates_invalidated += 1
                    continue
                if bool(bar.get("synthetic", False)) or int(bar.get("trade_count", 0) or 0) <= 0:
                    self.synthetic_entry_invalidations += 1
                    self.candidates_invalidated += 1
                    continue
                event["entry_price"] = float(bar["open"])
                event["entry_trade_count"] = int(bar["trade_count"])
                event["entry_synthetic"] = False

            if timestamp < entry_ts:
                keep.append(event)
                continue

            self._update_first_touch(event, bar)
            max_end_ts = event_ts + 5 * 60 * 1000
            if timestamp <= max_end_ts:
                high = float(bar["high"])
                low = float(bar["low"])
                event["_max_high"] = high if event.get("_max_high") is None else max(float(event["_max_high"]), high)
                event["_min_low"] = low if event.get("_min_low") is None else min(float(event["_min_low"]), low)

            for horizon in OUTCOME_HORIZONS_MINUTES:
                horizon_end = event_ts + horizon * 60 * 1000
                if timestamp == horizon_end and not event.get(f"complete_{horizon}m"):
                    self._apply_outcome(event, horizon, bar["close"])

            if timestamp >= max_end_ts:
                if event.get("complete_5m"):
                    self._write_completed_candidate(event)
                else:
                    self.candidates_invalidated += 1
                continue
            keep.append(event)
        self.pending_candidates[symbol] = keep

    def _process_contiguous_bar(self, bar):
        symbol = bar["symbol"]
        self._update_pending_candidates(bar)
        self.bars[symbol].append(bar)
        feature = self._build_features(bar)
        if feature is None:
            return
        self.features[symbol].append(feature)
        self._update_episode(feature)

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
                self._reset_symbol_after_gap(symbol, timestamp, reason="large_stream_gap")
            else:
                previous_price = (
                    float(self.bars[symbol][-1]["close"])
                    if self.bars[symbol]
                    else float(bar["open"])
                )
                for missing in range(1, delta_seconds):
                    synthetic = self._synthetic_bar(
                        symbol,
                        last_ts + missing * 1000,
                        previous_price,
                    )
                    self.synthetic_seconds += 1
                    self._process_contiguous_bar(synthetic)

        self.real_seconds += 1
        self._process_contiguous_bar(bar)
        self.last_timestamp[symbol] = timestamp
        self.last_message_timestamp = timestamp

    @staticmethod
    def _transport_symbols(payload):
        raw = payload.get("symbols")
        if not isinstance(raw, (list, tuple, set)):
            return set()
        return {str(symbol).upper() for symbol in raw if symbol}

    def _refresh_transport_scope(self):
        symbols = set()
        for item in self.transport_open_shards.values():
            symbols.update(item.get("symbols", set()))
        self.transport_gap_symbols = symbols

    def _reset_symbol_after_gap(self, symbol, timestamp=None, reason="transport_gap"):
        symbol = str(symbol).upper()
        self.bars.pop(symbol, None)
        self.features.pop(symbol, None)
        self.last_timestamp.pop(symbol, None)

        if symbol in self.active_episodes:
            episode = self.active_episodes.get(symbol)
            fallback_feature = None
            if episode is not None:
                fallback_feature = {
                    "timestamp": int(timestamp or time.time() * 1000),
                    "close": float(episode["peak_price"]),
                    "flow_imbalance_5s": 0.0,
                    "price_efficiency_5s": 0.0,
                    "baseline_median_notional_5s": None,
                }
            self._finalize_episode(symbol, fallback_feature, reason, valid=False)
            self.transport_episodes_invalidated += 1

        pending = self.pending_candidates.pop(symbol, [])
        if pending:
            self.candidates_invalidated += len(pending)
            self.transport_candidates_invalidated += len(pending)

    def _reset_symbols_after_transport_gap(self, symbols, timestamp, reason):
        for symbol in {str(s).upper() for s in symbols if s}:
            self._reset_symbol_after_gap(symbol, timestamp, reason)

    def _reset_all_after_transport_gap(self, timestamp, reason):
        symbols = set(self.bars) | set(self.features) | set(self.active_episodes) | set(self.pending_candidates)
        self._reset_symbols_after_transport_gap(symbols, timestamp, reason)

    def _load_transport_state(self):
        shard_pattern = f"{MICRO_FLOW_SHARD_STATUS_PREFIX}:*:transport-state"
        shard_keys = list(self.redis.scan_iter(match=shard_pattern, count=50))
        if shard_keys:
            for key in shard_keys:
                raw = self.redis.get(key)
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                if payload.get("type") != "micro_flow_transport":
                    continue
                if str(payload.get("event") or "").lower() != "transport_gap_start":
                    continue
                shard_id = str(payload.get("shard_id"))
                self.transport_open_shards[shard_id] = {
                    "symbols": self._transport_symbols(payload),
                    "started_at_ms": int(payload.get("timestamp") or 0) or None,
                }
            self._refresh_transport_scope()
            return

        raw = self.redis.get(MICRO_FLOW_TRANSPORT_STATE_KEY)
        if not raw:
            return
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        if isinstance(payload, dict) and payload.get("type") == "micro_flow_transport":
            self.transport_global_gap_open = (
                str(payload.get("event") or "").lower() == "transport_gap_start"
            )

    def _handle_transport_marker(self, payload):
        if not isinstance(payload, dict):
            return
        self.transport_markers += 1
        event = str(payload.get("event") or "").lower()
        scope = str(payload.get("scope") or "global").lower()
        timestamp = int(payload.get("timestamp") or 0) or int(time.time() * 1000)

        if scope == "shard":
            shard_id = str(payload.get("shard_id"))
            symbols = self._transport_symbols(payload)
            if event == "transport_gap_start":
                already_open = shard_id in self.transport_open_shards
                self.transport_open_shards[shard_id] = {
                    "symbols": symbols,
                    "started_at_ms": timestamp,
                }
                if not already_open:
                    self.transport_gaps += 1
                    self._reset_symbols_after_transport_gap(
                        symbols,
                        timestamp,
                        reason=f"shard_{shard_id}_transport_gap_start",
                    )
                self._refresh_transport_scope()
            elif event == "transport_gap_end":
                previous = self.transport_open_shards.pop(shard_id, None)
                affected = symbols or (previous or {}).get("symbols", set())
                self._reset_symbols_after_transport_gap(
                    affected,
                    timestamp,
                    reason=f"shard_{shard_id}_transport_gap_end",
                )
                self._refresh_transport_scope()
            return

        if event == "transport_gap_start":
            self.transport_global_gap_open = True
            self.transport_gaps += 1
            self._reset_all_after_transport_gap(timestamp, "global_transport_gap_start")
        elif event == "transport_gap_end":
            self._reset_all_after_transport_gap(timestamp, "global_transport_gap_end")
            self.transport_global_gap_open = False

    def _load_cursor(self):
        raw = self.redis.get(MICRO_FLOW_V2_CURSOR_KEY)
        self.cursor = str(raw) if raw else "0-0"

    def _save_cursor(self):
        self.redis.set(MICRO_FLOW_V2_CURSOR_KEY, self.cursor)

    def _status_payload(self):
        return {
            "running": bool(self.running),
            "schema_version": SCHEMA_VERSION,
            "stream": MICRO_FLOW_SECONDS_STREAM,
            "cursor": self.cursor,
            "stream_messages": int(self.stream_messages),
            "real_seconds": int(self.real_seconds),
            "synthetic_seconds": int(self.synthetic_seconds),
            "large_gaps": int(self.large_gaps),
            "invalid_payloads": int(self.invalid_payloads),
            "symbols_seen": len(self.last_timestamp),
            "symbols_warm": sum(1 for values in self.features.values() if len(values) >= 20),
            "active_episodes": len(self.active_episodes),
            "episodes_started": int(self.episodes_started),
            "episodes_completed": int(self.episodes_completed),
            "episodes_invalidated": int(self.episodes_invalidated),
            "transitions_written": int(self.transitions_written),
            "candidates_created": int(self.candidates_created),
            "candidates_completed": int(self.candidates_completed),
            "candidates_invalidated": int(self.candidates_invalidated),
            "pending_candidates": sum(len(items) for items in self.pending_candidates.values()),
            "synthetic_entry_invalidations": int(self.synthetic_entry_invalidations),
            "transport_global_gap_open": bool(self.transport_global_gap_open),
            "transport_open_shards": sorted(self.transport_open_shards.keys()),
            "transport_open_symbol_count": len(self.transport_gap_symbols),
            "transport_gaps": int(self.transport_gaps),
            "transport_markers": int(self.transport_markers),
            "transport_gap_seconds_skipped": int(self.transport_gap_seconds_skipped),
            "transport_episodes_invalidated": int(self.transport_episodes_invalidated),
            "transport_candidates_invalidated": int(self.transport_candidates_invalidated),
            "csv_errors": int(self.csv_errors),
            "last_message_timestamp": self.last_message_timestamp,
            "updated_at": int(time.time() * 1000),
            "started_at": self.started_at_ms,
            "paths": {
                "episodes": str(self.episodes_path),
                "transitions": str(self.transitions_path),
                "candidate_snapshots": str(self.candidate_snapshots_path),
                "candidates": str(self.candidates_path),
                "first_touch": str(self.first_touch_path),
            },
            "research_policy": {
                "orders_enabled": False,
                "v1_collector_untouched": True,
                "event_requires_real_second": True,
                "entry_requires_immediate_next_real_second": True,
                "transport_gap_resets_impacted_symbols_only": True,
                "episode_unit_of_analysis": True,
            },
            "thresholds": {
                "start_flow_threshold": self.start_flow_threshold,
                "start_activity_threshold": self.start_activity_threshold,
                "start_return_threshold_pct": self.start_return_threshold_pct,
                "mature_min_age_seconds": self.mature_min_age_seconds,
                "mature_move_threshold_pct": self.mature_move_threshold_pct,
                "arm_min_score": self.arm_min_score,
                "confirm_min_score": self.confirm_min_score,
                "max_episode_seconds": self.max_episode_seconds,
                "quiet_seconds": self.quiet_seconds,
                "cooldown_seconds": self.cooldown_seconds,
            },
        }

    def _write_status(self):
        now = time.monotonic()
        if now - self.last_status_at < 5.0:
            return
        self.last_status_at = now
        payload = self._status_payload()
        serialized = json.dumps(payload, separators=(",", ":"))
        self.redis.set(MICRO_FLOW_V2_STATUS_KEY, serialized)
        self.redis.set(
            MICRO_FLOW_V2_HEARTBEAT_KEY,
            str(payload["updated_at"]),
            ex=MICRO_FLOW_COLLECTOR_HEARTBEAT_TTL_SECONDS,
        )
        print(
            "[MICRO FLOW V2] "
            f"messages={self.stream_messages} "
            f"symbols={payload['symbols_seen']} "
            f"warm={payload['symbols_warm']} "
            f"active={payload['active_episodes']} "
            f"episodes={self.episodes_started}/{self.episodes_completed} "
            f"candidates={self.candidates_created}/{self.candidates_completed} "
            f"pending={payload['pending_candidates']} "
            f"cursor={self.cursor}"
        )

    def run(self):
        if not self.redis.ping():
            raise RuntimeError("Redis connection failed")
        self._load_cursor()
        self._load_transport_state()
        self.running = True
        print(
            "[MICRO FLOW V2] started "
            f"stream={MICRO_FLOW_SECONDS_STREAM} cursor={self.cursor} "
            f"episodes={self.episodes_path} candidates={self.candidates_path}"
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

                        if (
                            isinstance(payload, dict)
                            and payload.get("type") == "micro_flow_transport"
                        ):
                            self._handle_transport_marker(payload)
                            continue

                        if self.transport_global_gap_open:
                            self.transport_gap_seconds_skipped += 1
                            continue

                        bar = self._normalize_real_bar(payload)
                        if bar is None:
                            self.invalid_payloads += 1
                            continue
                        if bar["symbol"] in self.transport_gap_symbols:
                            self.transport_gap_seconds_skipped += 1
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
    parser.add_argument("--redis-host", default=os.getenv("REDIS_HOST", "127.0.0.1"))
    parser.add_argument("--redis-port", type=int, default=int(os.getenv("REDIS_PORT", "6379")))
    parser.add_argument("--redis-db", type=int, default=int(os.getenv("REDIS_DB", "0")))
    parser.add_argument("--episodes-path", default=os.getenv("MICRO_FLOW_V2_EPISODES_PATH", "micro_flow_v2_episodes.csv"))
    parser.add_argument("--transitions-path", default=os.getenv("MICRO_FLOW_V2_TRANSITIONS_PATH", "micro_flow_v2_transitions.csv"))
    parser.add_argument("--candidate-snapshots-path", default=os.getenv("MICRO_FLOW_V2_CANDIDATE_SNAPSHOTS_PATH", "micro_flow_v2_candidate_snapshots.csv"))
    parser.add_argument("--candidates-path", default=os.getenv("MICRO_FLOW_V2_CANDIDATES_PATH", "micro_flow_v2_candidates.csv"))
    parser.add_argument("--first-touch-path", default=os.getenv("MICRO_FLOW_V2_FIRST_TOUCH_PATH", "micro_flow_v2_first_touch.csv"))
    parser.add_argument("--start-flow-threshold", type=float, default=float(os.getenv("MICRO_FLOW_V2_START_FLOW", "0.45")))
    parser.add_argument("--start-activity-threshold", type=float, default=float(os.getenv("MICRO_FLOW_V2_START_ACTIVITY", "1.25")))
    parser.add_argument("--start-return-threshold-pct", type=float, default=float(os.getenv("MICRO_FLOW_V2_START_RETURN_PCT", "0.02")))
    parser.add_argument("--mature-min-age-seconds", type=int, default=int(os.getenv("MICRO_FLOW_V2_MATURE_MIN_AGE", "3")))
    parser.add_argument("--mature-move-threshold-pct", type=float, default=float(os.getenv("MICRO_FLOW_V2_MATURE_MOVE_PCT", "0.06")))
    parser.add_argument("--arm-min-score", type=int, default=int(os.getenv("MICRO_FLOW_V2_ARM_MIN_SCORE", "2")))
    parser.add_argument("--confirm-min-score", type=int, default=int(os.getenv("MICRO_FLOW_V2_CONFIRM_MIN_SCORE", "2")))
    parser.add_argument("--max-episode-seconds", type=int, default=int(os.getenv("MICRO_FLOW_V2_MAX_EPISODE_SECONDS", "90")))
    parser.add_argument("--quiet-seconds", type=int, default=int(os.getenv("MICRO_FLOW_V2_QUIET_SECONDS", "4")))
    parser.add_argument("--cooldown-seconds", type=int, default=int(os.getenv("MICRO_FLOW_V2_COOLDOWN_SECONDS", "5")))
    return parser.parse_args()


def main():
    args = parse_args()
    collector = MicroFlowV2EpisodeCollector(
        redis_host=args.redis_host,
        redis_port=args.redis_port,
        redis_db=args.redis_db,
        episodes_path=args.episodes_path,
        transitions_path=args.transitions_path,
        candidate_snapshots_path=args.candidate_snapshots_path,
        candidates_path=args.candidates_path,
        first_touch_path=args.first_touch_path,
        start_flow_threshold=args.start_flow_threshold,
        start_activity_threshold=args.start_activity_threshold,
        start_return_threshold_pct=args.start_return_threshold_pct,
        mature_min_age_seconds=args.mature_min_age_seconds,
        mature_move_threshold_pct=args.mature_move_threshold_pct,
        arm_min_score=args.arm_min_score,
        confirm_min_score=args.confirm_min_score,
        max_episode_seconds=args.max_episode_seconds,
        quiet_seconds=args.quiet_seconds,
        cooldown_seconds=args.cooldown_seconds,
    )

    def request_stop(signum, frame):
        print(f"[MICRO FLOW V2] received signal={signum}")
        collector.stop()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    collector.run()


if __name__ == "__main__":
    main()
