import json
import math
import time

import pandas as pd
import redis

from engine.live.data.redis_market_data_protocol import (
    market_flow_key,
    normalize_symbol,
    normalize_timeframe,
)


class MarketFlowDashboardService:
    """Read current Market Flow snapshots from Redis for 1h and 4h.

    Existing 4h callers keep the same field names/API. 1h uses the same
    schema with timeframe-suffixed metric names (for example
    market_breadth_1h / return_pct_1h).
    """

    TIMEFRAME_MS = {
        "1h": 1 * 60 * 60 * 1000,
        "4h": 4 * 60 * 60 * 1000,
    }

    DEFAULT_MAX_AGE_SECONDS = {
        # Allow a completed snapshot to survive until the next close plus a
        # small settling margin, without presenting hours-old 1h flow as live.
        "1h": 2 * 60 * 60,
        "4h": 5 * 60 * 60,
    }

    def __init__(
        self,
        host="127.0.0.1",
        port=6379,
        db=0,
        redis_client=None,
    ):
        self.redis = redis_client or redis.Redis(
            host=host,
            port=port,
            db=db,
            decode_responses=True,
        )
        self.last_error = None

    @staticmethod
    def _metric_key(base, timeframe):
        return f"{base}_{timeframe}"

    def _snapshot_timeframe(self, snapshot):
        try:
            timeframe = normalize_timeframe(
                str(snapshot.get("timeframe") or "")
            )
        except (TypeError, ValueError):
            return None
        return timeframe if timeframe in self.TIMEFRAME_MS else None

    def get_snapshot(
        self,
        timeframe="4h",
        min_coverage_pct=95.0,
        max_age_seconds=None,
    ):
        self.last_error = None

        try:
            timeframe = normalize_timeframe(timeframe)
        except (TypeError, ValueError) as exc:
            return self._reject(f"invalid_timeframe:{exc}")

        timeframe_ms = self.TIMEFRAME_MS.get(timeframe)
        if timeframe_ms is None:
            return self._reject(f"unsupported_timeframe:{timeframe}")

        if max_age_seconds is None:
            max_age_seconds = self.DEFAULT_MAX_AGE_SECONDS[timeframe]

        try:
            raw_snapshot = self.redis.get(market_flow_key(timeframe))
        except redis.RedisError as exc:
            return self._reject(f"redis_error:{exc}")

        if not raw_snapshot:
            return self._reject(f"snapshot_unavailable:{timeframe}")

        try:
            snapshot = json.loads(raw_snapshot)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return self._reject(f"invalid_json:{exc}")

        if not isinstance(snapshot, dict):
            return self._reject("snapshot_must_be_dict")

        snapshot_timeframe = str(snapshot.get("timeframe") or "").lower()
        if snapshot_timeframe != timeframe:
            return self._reject(f"timeframe_mismatch:{snapshot_timeframe}")

        try:
            candle_timestamp = int(snapshot["candle_timestamp"])
            coverage_pct = float(snapshot["coverage_pct"])
        except (KeyError, TypeError, ValueError):
            return self._reject("invalid_snapshot_metadata")

        if not math.isfinite(coverage_pct):
            return self._reject("invalid_coverage")

        if coverage_pct < float(min_coverage_pct):
            return self._reject(f"insufficient_coverage:{coverage_pct:.3f}")

        close_timestamp = candle_timestamp + timeframe_ms
        now_ms = int(time.time() * 1000)
        age_seconds = (now_ms - close_timestamp) / 1000

        if age_seconds < -5:
            return self._reject(f"snapshot_from_future:{age_seconds:.1f}")

        age_seconds = max(age_seconds, 0.0)
        if age_seconds > float(max_age_seconds):
            return self._reject(f"stale_snapshot:{age_seconds:.1f}")

        symbols = snapshot.get("symbols")
        if not isinstance(symbols, dict):
            return self._reject("invalid_symbols_payload")

        snapshot = dict(snapshot)
        snapshot["market_flow_close_timestamp"] = close_timestamp
        snapshot["market_flow_age_seconds"] = round(age_seconds, 3)
        return snapshot

    def get_symbol_context(
        self,
        snapshot,
        symbol,
        reference_timestamp=None,
    ):
        self.last_error = None

        if not isinstance(snapshot, dict):
            self.last_error = "snapshot_must_be_dict"
            return None

        timeframe = self._snapshot_timeframe(snapshot)
        if timeframe is None:
            self.last_error = "unsupported_snapshot_timeframe"
            return None

        try:
            symbol = normalize_symbol(symbol)
        except (TypeError, ValueError) as exc:
            self.last_error = f"invalid_symbol:{exc}"
            return None

        symbols = snapshot.get("symbols")
        if not isinstance(symbols, dict):
            self.last_error = "invalid_symbols_payload"
            return None

        symbol_metrics = symbols.get(symbol)
        if not isinstance(symbol_metrics, dict):
            self.last_error = f"symbol_unavailable:{symbol}"
            return None

        try:
            candle_timestamp = int(snapshot["candle_timestamp"])
        except (KeyError, TypeError, ValueError):
            self.last_error = "invalid_candle_timestamp"
            return None

        close_timestamp = int(
            snapshot.get(
                "market_flow_close_timestamp",
                candle_timestamp + self.TIMEFRAME_MS[timeframe],
            )
        )

        calculated_at = snapshot.get("calculated_at")
        try:
            calculated_at = int(calculated_at)
        except (TypeError, ValueError):
            calculated_at = None

        temporal_relation = None
        if reference_timestamp is not None:
            try:
                reference_timestamp = int(reference_timestamp)
            except (TypeError, ValueError):
                self.last_error = "invalid_reference_timestamp"
                return None

            available_timestamp = (
                calculated_at if calculated_at is not None else close_timestamp
            )
            temporal_relation = (
                "AVAILABLE_AT_REFERENCE"
                if available_timestamp <= reference_timestamp
                else "CALCULATED_AFTER_REFERENCE"
            )

        breadth_key = self._metric_key("market_breadth", timeframe)
        btc_key = self._metric_key("btc_return_pct", timeframe)

        snapshot_context = {
            "candle_timestamp": candle_timestamp,
            "close_timestamp": close_timestamp,
            "calculated_at": calculated_at,
            "age_seconds": snapshot.get("market_flow_age_seconds"),
            "coverage_pct": snapshot.get("coverage_pct"),
            "configured_universe_size": snapshot.get("configured_universe_size"),
            "valid_universe_size": snapshot.get("valid_universe_size"),
            "positive_symbols": snapshot.get("positive_symbols"),
            "market_breadth": snapshot.get(breadth_key),
            "btc_return_pct": snapshot.get(btc_key),
            breadth_key: snapshot.get(breadth_key),
            btc_key: snapshot.get(btc_key),
            "sector_context_available": snapshot.get("sector_context_available"),
            "sector_context_error": snapshot.get("sector_context_error"),
            "sector_catalog_generated_at": snapshot.get("sector_catalog_generated_at"),
            "sector_assignment_method": snapshot.get("sector_assignment_method"),
            "sector_return_aggregation": snapshot.get("sector_return_aggregation"),
        }

        return {
            "available": True,
            "symbol": symbol,
            "timeframe": timeframe,
            "reference_timestamp": reference_timestamp,
            "temporal_relation": temporal_relation,
            "snapshot": snapshot_context,
            # Preserve the complete block so future metrics are automatically
            # available to research callers.
            "symbol_metrics": dict(symbol_metrics),
        }

    def build_symbol_table(self, snapshot):
        self.last_error = None

        if not isinstance(snapshot, dict):
            self.last_error = "snapshot_must_be_dict"
            return pd.DataFrame()

        timeframe = self._snapshot_timeframe(snapshot)
        if timeframe is None:
            self.last_error = "unsupported_snapshot_timeframe"
            return pd.DataFrame()

        symbols = snapshot.get("symbols")
        if not isinstance(symbols, dict):
            self.last_error = "invalid_symbols_payload"
            return pd.DataFrame()

        return_key = self._metric_key("return_pct", timeframe)
        return_rank_key = self._metric_key("return_rank_pct", timeframe)
        relative_volume_key = self._metric_key("relative_volume", timeframe)
        volume_rank_key = self._metric_key("volume_rank_pct", timeframe)

        sector_return_rank_key = self._metric_key(
            "sector_return_rank_pct", timeframe
        )
        sector_strength_key = self._metric_key(
            "sector_strength_vs_btc", timeframe
        )
        symbol_sector_strength_key = self._metric_key(
            "symbol_strength_vs_sector", timeframe
        )

        rows = []
        for symbol, metrics in symbols.items():
            if not isinstance(metrics, dict):
                continue

            try:
                return_pct = float(metrics[return_key])
                return_rank = float(metrics[return_rank_key])
                relative_volume = float(metrics[relative_volume_key])
                volume_rank = float(metrics[volume_rank_key])
            except (KeyError, TypeError, ValueError):
                continue

            numeric_values = (
                return_pct,
                return_rank,
                relative_volume,
                volume_rank,
            )
            if not all(math.isfinite(value) for value in numeric_values):
                continue

            row = {
                "symbol": symbol,
                return_key: return_pct,
                return_rank_key: return_rank,
                relative_volume_key: relative_volume,
                volume_rank_key: volume_rank,
                "flow_group": (
                    metrics.get("flow_group")
                    or self._classify_flow(
                        return_rank=return_rank,
                        volume_rank=volume_rank,
                    )
                ),
                "primary_sector": metrics.get("primary_sector") or "Other",
                sector_return_rank_key: self._optional_finite_float(
                    metrics.get(sector_return_rank_key)
                ),
                sector_strength_key: self._optional_finite_float(
                    metrics.get(sector_strength_key)
                ),
                symbol_sector_strength_key: self._optional_finite_float(
                    metrics.get(symbol_sector_strength_key)
                ),
            }
            rows.append(row)

        if not rows:
            self.last_error = "no_valid_symbol_metrics"
            return pd.DataFrame()

        result = pd.DataFrame(rows)
        return result.sort_values(
            [return_rank_key, volume_rank_key],
            ascending=[False, False],
        ).reset_index(drop=True)

    def build_sector_table(self, snapshot):
        self.last_error = None

        if not isinstance(snapshot, dict):
            self.last_error = "snapshot_must_be_dict"
            return pd.DataFrame()

        timeframe = self._snapshot_timeframe(snapshot)
        if timeframe is None:
            self.last_error = "unsupported_snapshot_timeframe"
            return pd.DataFrame()

        if not snapshot.get("sector_context_available"):
            self.last_error = (
                snapshot.get("sector_context_error")
                or "sector_context_unavailable"
            )
            return pd.DataFrame()

        sectors = snapshot.get("sectors")
        if not isinstance(sectors, dict):
            self.last_error = "invalid_sectors_payload"
            return pd.DataFrame()

        return_key = self._metric_key("sector_return_pct", timeframe)
        rank_key = self._metric_key("sector_return_rank_pct", timeframe)
        breadth_key = self._metric_key("sector_breadth", timeframe)
        rel_volume_key = self._metric_key("sector_relative_volume", timeframe)
        strength_key = self._metric_key("sector_strength_vs_btc", timeframe)

        rows = []
        for sector, metrics in sectors.items():
            if not isinstance(metrics, dict):
                continue

            try:
                configured_symbols = int(metrics["configured_symbols"])
                valid_symbols = int(metrics["valid_symbols"])
                coverage_pct = float(metrics["coverage_pct"])
                return_pct = float(metrics[return_key])
                return_rank = float(metrics[rank_key])
                breadth = float(metrics[breadth_key])
                relative_volume = float(metrics[rel_volume_key])
                strength_vs_btc = float(metrics[strength_key])
            except (KeyError, TypeError, ValueError):
                continue

            numeric_values = (
                coverage_pct,
                return_pct,
                return_rank,
                breadth,
                relative_volume,
                strength_vs_btc,
            )
            if not all(math.isfinite(value) for value in numeric_values):
                continue

            rows.append({
                "sector": sector,
                "configured_symbols": configured_symbols,
                "valid_symbols": valid_symbols,
                "coverage_pct": coverage_pct,
                return_key: return_pct,
                rank_key: return_rank,
                breadth_key: breadth,
                rel_volume_key: relative_volume,
                strength_key: strength_vs_btc,
                "sector_flow_state": self._classify_sector_flow(
                    return_rank=return_rank,
                    breadth=breadth,
                    relative_volume=relative_volume,
                    strength_vs_btc=strength_vs_btc,
                ),
            })

        if not rows:
            self.last_error = "no_valid_sector_metrics"
            return pd.DataFrame()

        result = pd.DataFrame(rows)
        return result.sort_values(
            [rank_key, breadth_key],
            ascending=[False, False],
        ).reset_index(drop=True)

    def classify_market_breadth(self, breadth):
        try:
            breadth = float(breadth)
        except (TypeError, ValueError):
            return "Unknown"

        if breadth < 30:
            return "Broad weakness"
        if breadth < 50:
            return "Weak / mixed market"
        if breadth < 70:
            return "Healthy participation"
        if breadth < 85:
            return "Broad expansion"
        return "Euphoria / possible extension"

    def _classify_sector_flow(
        self,
        return_rank,
        breadth,
        relative_volume,
        strength_vs_btc,
    ):
        if (
            return_rank >= 70
            and breadth >= 60
            and relative_volume >= 1
            and strength_vs_btc > 0
        ):
            return "Confirmed rotation"
        if return_rank >= 70 and breadth >= 60 and relative_volume < 1:
            return "Broad rise / low volume"
        if return_rank >= 70 and breadth < 60:
            return "Concentrated leadership"
        if relative_volume >= 1 and strength_vs_btc < 0:
            return "High-volume weakness"
        if strength_vs_btc < 0:
            return "Lagging BTC"
        return "Neutral / mixed"

    def _classify_flow(self, return_rank, volume_rank):
        if return_rank >= 80 and volume_rank >= 80:
            return "Confirmed leadership"
        if return_rank >= 80 and volume_rank < 50:
            return "Rise without volume confirmation"
        if volume_rank >= 80 and 40 <= return_rank < 80:
            return "Emerging activity"
        if volume_rank >= 80 and return_rank <= 20:
            return "High-volume weakness"
        return "Neutral / unclassified"

    def _optional_finite_float(self, value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value):
            return None
        return value

    def _reject(self, reason):
        self.last_error = reason
        return None
