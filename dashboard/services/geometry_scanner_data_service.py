import json
import math
import time

import pandas as pd
import redis

from engine.live.data.redis_market_data_protocol import (
    HISTORY_MAXLEN,
    history_key,
    normalize_symbol,
    normalize_timeframe,
)


class GeometryScannerDataService:
    TIMEFRAME_MS = {
        "1m": 60 * 1000,
        "5m": 5 * 60 * 1000,
        "15m": 15 * 60 * 1000,
        "30m": 30 * 60 * 1000,
        "1h": 60 * 60 * 1000,
        "4h": 4 * 60 * 60 * 1000,
        "1d": 24 * 60 * 60 * 1000,
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
        self.last_diagnostics = {}

    def get_closed_candles(
        self,
        symbol,
        timeframe="30m",
        limit=400,
    ):
        self.last_error = None
        self.last_diagnostics = {}

        try:
            symbol = normalize_symbol(
                symbol
            )

            timeframe = normalize_timeframe(
                timeframe
            )

        except (TypeError, ValueError) as exc:
            return self._reject(
                f"invalid_input:{exc}"
            )

        timeframe_ms = self.TIMEFRAME_MS.get(
            timeframe
        )

        if timeframe_ms is None:
            return self._reject(
                f"unsupported_timeframe:{timeframe}"
            )

        try:
            limit = int(limit)
        except (TypeError, ValueError):
            return self._reject(
                "invalid_limit"
            )

        limit = max(
            1,
            min(
                limit,
                HISTORY_MAXLEN,
            ),
        )

        try:
            raw_candles = self.redis.lrange(
                history_key(
                    symbol,
                    timeframe,
                ),
                -limit,
                -1,
            )

        except redis.RedisError as exc:
            return self._reject(
                f"redis_error:{exc}"
            )

        if not raw_candles:
            return self._reject(
                "history_unavailable:"
                f"{symbol}:{timeframe}"
            )

        now_ms = int(
            time.time() * 1000
        )

        candles_by_timestamp = {}

        invalid_json = 0
        invalid_candles = 0
        open_candles = 0

        for raw_candle in raw_candles:
            try:
                candle = json.loads(
                    raw_candle
                )
            except (
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                invalid_json += 1
                continue

            normalized = (
                self._normalize_candle(
                    candle=candle,
                    requested_symbol=symbol,
                    requested_timeframe=(
                        timeframe
                    ),
                    timeframe_ms=(
                        timeframe_ms
                    ),
                    now_ms=now_ms,
                )
            )

            if normalized is None:
                invalid_candles += 1
                continue

            if normalized.pop(
                "_still_open",
                False,
            ):
                open_candles += 1
                continue

            candles_by_timestamp[
                normalized["timestamp"]
            ] = normalized

        candles = sorted(
            candles_by_timestamp.values(),
            key=lambda item: (
                item["timestamp"]
            ),
        )

        if not candles:
            return self._reject(
                "no_valid_closed_candles:"
                f"{symbol}:{timeframe}"
            )

        dataframe = pd.DataFrame(
            candles
        )

        dataframe[
            "datetime"
        ] = pd.to_datetime(
            dataframe["timestamp"],
            unit="ms",
            utc=True,
        )

        dataframe = dataframe[
            [
                "symbol",
                "timeframe",
                "timestamp",
                "close_timestamp",
                "datetime",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "quoteVolume",
            ]
        ].reset_index(drop=True)

        self.last_diagnostics = {
            "symbol": symbol,
            "timeframe": timeframe,
            "requested_limit": limit,
            "redis_items": len(
                raw_candles
            ),
            "valid_closed_candles": len(
                dataframe
            ),
            "invalid_json": invalid_json,
            "invalid_candles": (
                invalid_candles
            ),
            "open_candles_excluded": (
                open_candles
            ),
            "first_timestamp": int(
                dataframe[
                    "timestamp"
                ].iloc[0]
            ),
            "last_timestamp": int(
                dataframe[
                    "timestamp"
                ].iloc[-1]
            ),
        }

        return dataframe

    def _normalize_candle(
        self,
        candle,
        requested_symbol,
        requested_timeframe,
        timeframe_ms,
        now_ms,
    ):
        if not isinstance(candle, dict):
            return None

        try:
            timestamp = int(
                candle["timestamp"]
            )

            open_price = float(
                candle["open"]
            )

            high_price = float(
                candle["high"]
            )

            low_price = float(
                candle["low"]
            )

            close_price = float(
                candle["close"]
            )

            volume = float(
                candle.get(
                    "volume",
                    0.0,
                )
            )

        except (
            KeyError,
            TypeError,
            ValueError,
        ):
            return None

        numeric_values = (
            open_price,
            high_price,
            low_price,
            close_price,
            volume,
        )

        if not all(
            math.isfinite(value)
            for value in numeric_values
        ):
            return None

        if (
            open_price <= 0
            or high_price <= 0
            or low_price <= 0
            or close_price <= 0
            or volume < 0
        ):
            return None

        if high_price < max(
            open_price,
            close_price,
            low_price,
        ):
            return None

        if low_price > min(
            open_price,
            close_price,
            high_price,
        ):
            return None

        close_timestamp = candle.get(
            "close_timestamp"
        )

        try:
            close_timestamp = int(
                close_timestamp
            )
        except (TypeError, ValueError):
            close_timestamp = (
                timestamp
                + timeframe_ms
                - 1
            )

        still_open = (
            close_timestamp
            > now_ms + 5_000
        )

        quote_volume = (
            candle.get("quoteVolume")
            or candle.get("quote_volume")
            or candle.get(
                "quote_asset_volume"
            )
        )

        try:
            quote_volume = float(
                quote_volume
            )
        except (TypeError, ValueError):
            quote_volume = (
                volume * close_price
            )

        if (
            not math.isfinite(
                quote_volume
            )
            or quote_volume < 0
        ):
            quote_volume = (
                volume * close_price
            )

        return {
            "symbol": requested_symbol,
            "timeframe": requested_timeframe,
            "timestamp": timestamp,
            "close_timestamp": (
                close_timestamp
            ),
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": volume,
            "quoteVolume": (
                quote_volume
            ),
            "_still_open": still_open,
        }

    def _reject(
        self,
        reason,
    ):
        self.last_error = str(reason)
        return pd.DataFrame()