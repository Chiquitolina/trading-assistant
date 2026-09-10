import json
import math
import time

import pandas as pd
import redis

from engine.live.data.redis_market_data_protocol import (
    market_flow_key,
    normalize_timeframe,
)


class MarketFlowDashboardService:
    TIMEFRAME_MS = {
        "4h": 4 * 60 * 60 * 1000,
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

    def get_snapshot(
        self,
        timeframe="4h",
        min_coverage_pct=95.0,
        max_age_seconds=18_000,
    ):
        self.last_error = None

        try:
            timeframe = normalize_timeframe(
                timeframe
            )
        except (TypeError, ValueError) as exc:
            return self._reject(
                f"invalid_timeframe:{exc}"
            )

        timeframe_ms = self.TIMEFRAME_MS.get(
            timeframe
        )

        if timeframe_ms is None:
            return self._reject(
                f"unsupported_timeframe:{timeframe}"
            )

        try:
            raw_snapshot = self.redis.get(
                market_flow_key(timeframe)
            )
        except redis.RedisError as exc:
            return self._reject(
                f"redis_error:{exc}"
            )

        if not raw_snapshot:
            return self._reject(
                f"snapshot_unavailable:{timeframe}"
            )

        try:
            snapshot = json.loads(
                raw_snapshot
            )
        except (
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            return self._reject(
                f"invalid_json:{exc}"
            )

        if not isinstance(snapshot, dict):
            return self._reject(
                "snapshot_must_be_dict"
            )

        snapshot_timeframe = str(
            snapshot.get("timeframe") or ""
        ).lower()

        if snapshot_timeframe != timeframe:
            return self._reject(
                "timeframe_mismatch:"
                f"{snapshot_timeframe}"
            )

        try:
            candle_timestamp = int(
                snapshot["candle_timestamp"]
            )

            coverage_pct = float(
                snapshot["coverage_pct"]
            )
        except (
            KeyError,
            TypeError,
            ValueError,
        ):
            return self._reject(
                "invalid_snapshot_metadata"
            )

        if not math.isfinite(coverage_pct):
            return self._reject(
                "invalid_coverage"
            )

        if coverage_pct < min_coverage_pct:
            return self._reject(
                "insufficient_coverage:"
                f"{coverage_pct:.3f}"
            )

        close_timestamp = (
            candle_timestamp
            + timeframe_ms
        )

        now_ms = int(time.time() * 1000)

        age_seconds = (
            now_ms - close_timestamp
        ) / 1000

        if age_seconds < -5:
            return self._reject(
                "snapshot_from_future:"
                f"{age_seconds:.1f}"
            )

        age_seconds = max(
            age_seconds,
            0.0,
        )

        if age_seconds > max_age_seconds:
            return self._reject(
                "stale_snapshot:"
                f"{age_seconds:.1f}"
            )

        symbols = snapshot.get("symbols")

        if not isinstance(symbols, dict):
            return self._reject(
                "invalid_symbols_payload"
            )

        snapshot = dict(snapshot)

        snapshot[
            "market_flow_close_timestamp"
        ] = close_timestamp

        snapshot[
            "market_flow_age_seconds"
        ] = round(age_seconds, 3)

        return snapshot

    def build_symbol_table(
        self,
        snapshot,
    ):
        if not isinstance(snapshot, dict):
            self.last_error = (
                "snapshot_must_be_dict"
            )
            return pd.DataFrame()

        symbols = snapshot.get("symbols")

        if not isinstance(symbols, dict):
            self.last_error = (
                "invalid_symbols_payload"
            )
            return pd.DataFrame()

        rows = []

        for symbol, metrics in symbols.items():
            if not isinstance(metrics, dict):
                continue

            try:
                return_pct = float(
                    metrics["return_pct_4h"]
                )
                return_rank = float(
                    metrics["return_rank_pct_4h"]
                )
                relative_volume = float(
                    metrics["relative_volume_4h"]
                )
                volume_rank = float(
                    metrics["volume_rank_pct_4h"]
                )
            except (
                KeyError,
                TypeError,
                ValueError,
            ):
                continue

            if not all(
                math.isfinite(value)
                for value in (
                    return_pct,
                    return_rank,
                    relative_volume,
                    volume_rank,
                )
            ):
                continue

            rows.append({
                "symbol": symbol,
                "return_pct_4h": return_pct,
                "return_rank_pct_4h": return_rank,
                "relative_volume_4h": relative_volume,
                "volume_rank_pct_4h": volume_rank,
                "flow_group": self._classify_flow(
                    return_rank=return_rank,
                    volume_rank=volume_rank,
                ),
            })

        if not rows:
            self.last_error = (
                "no_valid_symbol_metrics"
            )
            return pd.DataFrame()

        result = pd.DataFrame(rows)

        return result.sort_values(
            [
                "return_rank_pct_4h",
                "volume_rank_pct_4h",
            ],
            ascending=[False, False],
        ).reset_index(drop=True)

    def classify_market_breadth(
        self,
        breadth,
    ):
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

    def _classify_flow(
        self,
        return_rank,
        volume_rank,
    ):
        if (
            return_rank >= 80
            and volume_rank >= 80
        ):
            return "Confirmed leadership"

        if (
            return_rank >= 80
            and volume_rank < 50
        ):
            return "Rise without volume confirmation"

        if (
            volume_rank >= 80
            and 40 <= return_rank < 80
        ):
            return "Emerging activity"

        if (
            volume_rank >= 80
            and return_rank <= 20
        ):
            return "High-volume weakness"

        return "Neutral / unclassified"

    def _reject(
        self,
        reason,
    ):
        self.last_error = reason
        return None