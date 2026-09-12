import hashlib
import json
import os
import threading
import uuid
from datetime import (
    datetime,
    timezone,
)
from pathlib import Path

import pandas as pd


class GeometryObservationJournal:
    SCHEMA_VERSION = 1

    def __init__(
        self,
        path=(
            "reports/"
            "geometry_observations.jsonl"
        ),
    ):
        self.path = Path(path)

        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.write_lock = threading.Lock()
        self.last_error = None
        self.last_saved = None

        self.observation_keys = (
            self._load_observation_keys()
        )

    def save(
        self,
        symbol,
        timeframe,
        candidate,
        candles,
        scanner_parameters,
        source,
        figure_quality,
        breakout_quality,
        notes="",
        market_flow_at_review=None,
        market_flow_at_breakout=None,
    ):
        self.last_error = None
        self.last_saved = None

        if hasattr(candidate, "to_dict"):
            candidate = candidate.to_dict()
        else:
            candidate = dict(candidate)

        normalized_symbol = str(
            symbol
        ).upper()

        normalized_timeframe = str(
            timeframe
        ).lower()

        status = self._resolve_status(
            candidate
        )

        observation_key = (
            self._build_observation_key(
                symbol=normalized_symbol,
                timeframe=(
                    normalized_timeframe
                ),
                candidate=candidate,
                status=status,
            )
        )

        start_index = int(
            candidate["start_index"]
        )

        end_index = int(
            candidate["end_index"]
        )

        flagpole_lookback = int(
            scanner_parameters.get(
                "flagpole_lookback",
                10,
            )
        )

        breakout_lookahead = int(
            scanner_parameters.get(
                "breakout_lookahead",
                3,
            )
        )

        first_saved_index = max(
            0,
            start_index
            - flagpole_lookback,
        )

        last_saved_index = min(
            len(candles) - 1,
            end_index
            + breakout_lookahead,
        )

        candle_records = (
            self._serialize_candles(
                candles.iloc[
                    first_saved_index:
                    last_saved_index + 1
                ]
            )
        )

        now = datetime.now(
            timezone.utc
        )

        normalized_candidate = (
            self._normalize_value(
                candidate
            )
        )

        payload = {
            "schema_version": (
                self.SCHEMA_VERSION
            ),
            "observation_id": (
                str(uuid.uuid4())
            ),
            "observation_key": (
                observation_key
            ),
            "saved_at": now.isoformat(
                timespec="milliseconds"
            ),
            "saved_at_timestamp": int(
                now.timestamp() * 1000
            ),

            "symbol": normalized_symbol,
            "timeframe": (
                normalized_timeframe
            ),
            "source": str(source),

            "geometry": candidate.get(
                "geometry"
            ),
            "status_at_review": status,
            "breakout_direction": (
                normalized_candidate.get(
                    "breakout_direction"
                )
            ),

            "human_review": {
                "figure_quality": str(
                    figure_quality
                ).upper(),
                "breakout_quality": str(
                    breakout_quality
                ).upper(),
                "notes": str(
                    notes or ""
                ).strip(),
            },

            "scanner_parameters": (
                self._normalize_value(
                    scanner_parameters
                )
            ),

            "candidate": (
                normalized_candidate
            ),

            "saved_candle_range": {
                "first_source_index": int(
                    first_saved_index
                ),
                "last_source_index": int(
                    last_saved_index
                ),
                "candles": candle_records,
            },

            "market_flow_at_review": (
                self._normalize_value(
                    market_flow_at_review
                )
                if market_flow_at_review
                is not None
                else {
                    "available": False,
                    "reason": (
                        "context_not_provided"
                    ),
                }
            ),

            "market_flow_at_breakout": (
                self._normalize_value(
                    market_flow_at_breakout
                )
                if market_flow_at_breakout
                is not None
                else {
                    "available": False,
                    "reason": (
                        "historical_snapshot_unavailable"
                    ),
                }
            ),

            "outcome": {
                "status": "PENDING",
                "evaluated_at": None,
                "evaluation_bars": None,
                "reference_price": (
                    normalized_candidate.get(
                        "breakout_price"
                    )
                ),
                "forward_return_pct": None,
                "maximum_favorable_excursion_pct": None,
                "maximum_adverse_excursion_pct": None,
                "maximum_high_timestamp": None,
                "minimum_low_timestamp": None,
            },
        }

        try:
            serialized = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (
            TypeError,
            ValueError,
        ) as exc:
            self.last_error = (
                f"serialization_error:{exc}"
            )
            return None

        with self.write_lock:
            if (
                observation_key
                in self.observation_keys
            ):
                self.last_error = (
                    "duplicate_observation"
                )
                return None

            try:
                with open(
                    self.path,
                    "a",
                    encoding="utf-8",
                ) as journal:
                    journal.write(
                        serialized + "\n"
                    )
                    journal.flush()

                    os.fsync(
                        journal.fileno()
                    )

            except OSError as exc:
                self.last_error = (
                    f"write_error:{exc}"
                )
                return None

            self.observation_keys.add(
                observation_key
            )

        self.last_saved = payload
        return payload

    def contains(
        self,
        symbol,
        timeframe,
        candidate,
    ):
        if hasattr(candidate, "to_dict"):
            candidate = candidate.to_dict()
        else:
            candidate = dict(candidate)

        status = self._resolve_status(
            candidate
        )

        observation_key = (
            self._build_observation_key(
                symbol=str(symbol).upper(),
                timeframe=str(
                    timeframe
                ).lower(),
                candidate=candidate,
                status=status,
            )
        )

        return (
            observation_key
            in self.observation_keys
        )

    def _resolve_status(
        self,
        candidate,
    ):
        raw_status = candidate.get(
            "status"
        )

        if (
            raw_status is not None
            and pd.notna(raw_status)
        ):
            return str(
                raw_status
            ).upper()

        breakout_detected = (
            candidate.get(
                "breakout_detected",
                False,
            )
        )

        return (
            "BREAKOUT"
            if bool(breakout_detected)
            else "FORMING"
        )

    def _build_observation_key(
        self,
        symbol,
        timeframe,
        candidate,
        status,
    ):
        components = [
            str(symbol).upper(),
            str(timeframe).lower(),
            str(
                candidate.get(
                    "geometry"
                )
            ),
            str(
                candidate.get(
                    "start_timestamp"
                )
            ),
            str(
                candidate.get(
                    "end_timestamp"
                )
            ),
            str(status),
            str(
                self._nullable_integer(
                    candidate.get(
                        "breakout_timestamp"
                    )
                )
            ),
        ]

        raw_key = "|".join(
            components
        )

        return hashlib.sha256(
            raw_key.encode("utf-8")
        ).hexdigest()

    def _serialize_candles(
        self,
        candles,
    ):
        records = []

        for _, candle in candles.iterrows():
            record = {
                "timestamp": int(
                    candle["timestamp"]
                ),
                "open": float(
                    candle["open"]
                ),
                "high": float(
                    candle["high"]
                ),
                "low": float(
                    candle["low"]
                ),
                "close": float(
                    candle["close"]
                ),
                "volume": float(
                    candle["volume"]
                ),
            }

            close_timestamp = candle.get(
                "close_timestamp"
            )

            if pd.notna(close_timestamp):
                record[
                    "close_timestamp"
                ] = int(close_timestamp)

            quote_volume = candle.get(
                "quoteVolume"
            )

            if pd.notna(quote_volume):
                record[
                    "quoteVolume"
                ] = float(quote_volume)

            records.append(record)

        return records

    def _nullable_integer(
        self,
        value,
    ):
        if value is None:
            return None

        try:
            if pd.isna(value):
                return None
        except (
            TypeError,
            ValueError,
        ):
            pass

        return int(value)

    def _normalize_value(
        self,
        value,
    ):
        if isinstance(value, dict):
            return {
                str(key): (
                    self._normalize_value(
                        item
                    )
                )
                for key, item
                in value.items()
            }

        if isinstance(
            value,
            (
                list,
                tuple,
                set,
            ),
        ):
            return [
                self._normalize_value(item)
                for item in value
            ]

        if isinstance(value, pd.Timestamp):
            return value.isoformat()

        if value is None:
            return None

        try:
            if pd.isna(value):
                return None
        except (
            TypeError,
            ValueError,
        ):
            pass

        if hasattr(value, "item"):
            value = value.item()

        if isinstance(
            value,
            (
                str,
                int,
                float,
                bool,
            ),
        ):
            return value

        return str(value)

    def _load_observation_keys(self):
        keys = set()

        if not self.path.exists():
            return keys

        try:
            with open(
                self.path,
                "r",
                encoding="utf-8",
            ) as journal:
                for line in journal:
                    line = line.strip()

                    if not line:
                        continue

                    try:
                        payload = json.loads(
                            line
                        )
                    except (
                        TypeError,
                        ValueError,
                        json.JSONDecodeError,
                    ):
                        continue

                    observation_key = (
                        payload.get(
                            "observation_key"
                        )
                    )

                    if observation_key:
                        keys.add(
                            observation_key
                        )

        except OSError as exc:
            self.last_error = (
                f"journal_read_error:{exc}"
            )

        return keys