import bisect
import json
import statistics
import time

from engine.live.data.redis_market_data_protocol import (
    history_key,
    normalize_symbol,
    normalize_timeframe,
)


class MarketFlowAnalyzer:
    def __init__(
        self,
        redis_client,
        baseline_candles=42,
    ):
        self.redis = redis_client
        self.baseline_candles = int(
            baseline_candles
        )

        if self.baseline_candles < 1:
            raise ValueError(
                "baseline_candles must be >= 1"
            )

    def calculate(
        self,
        symbols,
        timeframe,
        candle_timestamp,
    ):
        timeframe = normalize_timeframe(
            timeframe
        )

        candle_timestamp = int(
            candle_timestamp
        )

        unique_symbols = sorted({
            normalize_symbol(symbol)
            for symbol in symbols
            if symbol
        })
        
        # Una vela adicional permite ignorar
        # una posible vela todavía abierta
        # cargada por el histórico REST.
        history_size = (
            self.baseline_candles + 2
        )

        pipeline = self.redis.pipeline(
            transaction=False,
        )

        for symbol in unique_symbols:
            pipeline.lrange(
                history_key(
                    symbol,
                    timeframe,
                ),
                -history_size,
                -1,
            )

        raw_histories = pipeline.execute()

        symbol_metrics = {}
        excluded_symbols = {}

        for symbol, raw_history in zip(
            unique_symbols,
            raw_histories,
        ):
            metrics, exclusion_reason = (
                self._calculate_symbol_metrics(
                    symbol=symbol,
                    raw_history=raw_history,
                    candle_timestamp=candle_timestamp,
                )
            )

            if metrics is None:
                excluded_symbols[symbol] = (
                    exclusion_reason
                )
                continue

            symbol_metrics[symbol] = metrics

        valid_symbols = list(
            symbol_metrics.keys()
        )

        return_values = [
            symbol_metrics[symbol][
                "return_pct_4h"
            ]
            for symbol in valid_symbols
        ]

        relative_volume_values = [
            symbol_metrics[symbol][
                "relative_volume_4h"
            ]
            for symbol in valid_symbols
        ]

        return_ranks = self._percentile_ranks(
            return_values
        )

        volume_ranks = self._percentile_ranks(
            relative_volume_values
        )

        for index, symbol in enumerate(
            valid_symbols
        ):
            symbol_metrics[symbol][
                "return_rank_pct_4h"
            ] = return_ranks[index]

            symbol_metrics[symbol][
                "volume_rank_pct_4h"
            ] = volume_ranks[index]

        valid_universe_size = len(
            symbol_metrics
        )

        configured_universe_size = len(
            unique_symbols
        )

        if valid_universe_size:
            positive_symbols = sum(
                1
                for value in return_values
                if value > 0
            )

            market_breadth_4h = (
                positive_symbols
                / valid_universe_size
                * 100
            )
        else:
            positive_symbols = 0
            market_breadth_4h = None

        if configured_universe_size:
            coverage_pct = (
                valid_universe_size
                / configured_universe_size
                * 100
            )
        else:
            coverage_pct = 0.0

        btc_metrics = symbol_metrics.get(
            "BTCUSDT"
        )

        return {
            "type": "market_flow_snapshot",
            "timeframe": timeframe,
            "candle_timestamp": (
                candle_timestamp
            ),
            "calculated_at": int(
                time.time() * 1000
            ),
            "baseline_candles": (
                self.baseline_candles
            ),
            "configured_universe_size": (
                configured_universe_size
            ),
            "valid_universe_size": (
                valid_universe_size
            ),
            "excluded_universe_size": len(
                excluded_symbols
            ),
            "coverage_pct": round(
                coverage_pct,
                4,
            ),
            "positive_symbols": (
                positive_symbols
            ),
            "market_breadth_4h": (
                round(
                    market_breadth_4h,
                    4,
                )
                if market_breadth_4h
                is not None
                else None
            ),
            "btc_return_pct_4h": (
                btc_metrics.get(
                    "return_pct_4h"
                )
                if btc_metrics
                else None
            ),
            "symbols": symbol_metrics,
            "excluded_symbols": (
                excluded_symbols
            ),
        }

    def _calculate_symbol_metrics(
        self,
        symbol,
        raw_history,
        candle_timestamp,
    ):
        minimum_size = (
            self.baseline_candles + 1
        )

        if len(raw_history) < minimum_size:
            return (
                None,
                "insufficient_history",
            )

        candles = []

        for raw_candle in raw_history:
            try:
                candle = json.loads(
                    raw_candle
                )

                candles.append(candle)

            except (
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                return (
                    None,
                    "invalid_history_json",
                )

        current_index = None

        for index in range(
            len(candles) - 1,
            -1,
            -1,
        ):
            try:
                timestamp = int(
                    candles[index][
                        "timestamp"
                    ]
                )
            except (
                KeyError,
                TypeError,
                ValueError,
            ):
                continue

            if timestamp == candle_timestamp:
                current_index = index
                break

        if current_index is None:
            return (
                None,
                "missing_batch_candle",
            )

        if (
            current_index
            < self.baseline_candles
        ):
            return (
                None,
                "insufficient_history_before_batch",
            )

        current_candle = candles[
            current_index
        ]

        previous_candle = candles[
            current_index - 1
        ]

        try:
            previous_close = float(
                previous_candle["close"]
            )

            current_close = float(
                current_candle["close"]
            )
        except (
            KeyError,
            TypeError,
            ValueError,
        ):
            return (
                None,
                "invalid_close",
            )

        if previous_close <= 0:
            return (
                None,
                "non_positive_previous_close",
            )

        return_pct_4h = (
            current_close
            / previous_close
            - 1
        ) * 100

        historical_candles = candles[
            current_index
            - self.baseline_candles:
            current_index
        ]

        historical_quote_volumes = []

        for candle in historical_candles:
            quote_volume = (
                self._extract_quote_volume(
                    candle
                )
            )

            if quote_volume is not None:
                historical_quote_volumes.append(
                    quote_volume
                )

        if (
            len(historical_quote_volumes)
            < self.baseline_candles
        ):
            return (
                None,
                "insufficient_volume_history",
            )

        current_quote_volume = (
            self._extract_quote_volume(
                current_candle
            )
        )

        if current_quote_volume is None:
            return (
                None,
                "invalid_current_quote_volume",
            )

        median_quote_volume = (
            statistics.median(
                historical_quote_volumes
            )
        )

        if median_quote_volume <= 0:
            return (
                None,
                "non_positive_volume_baseline",
            )

        relative_volume_4h = (
            current_quote_volume
            / median_quote_volume
        )

        return (
            {
                "return_pct_4h": round(
                    return_pct_4h,
                    4,
                ),
                "quote_volume_4h": round(
                    current_quote_volume,
                    4,
                ),
                "median_quote_volume_4h": round(
                    median_quote_volume,
                    4,
                ),
                "relative_volume_4h": round(
                    relative_volume_4h,
                    4,
                ),
            },
            None,
        )

    def _extract_quote_volume(
        self,
        candle,
    ):
        quote_volume = (
            candle.get("quoteVolume")
            or candle.get("quote_volume")
            or candle.get(
                "quote_asset_volume"
            )
        )

        if quote_volume is not None:
            try:
                quote_volume = float(
                    quote_volume
                )

                if quote_volume > 0:
                    return quote_volume

            except (
                TypeError,
                ValueError,
            ):
                pass

        try:
            volume = float(
                candle["volume"]
            )

            close = float(
                candle["close"]
            )

            fallback_quote_volume = (
                volume * close
            )

            if fallback_quote_volume > 0:
                return fallback_quote_volume

        except (
            KeyError,
            TypeError,
            ValueError,
        ):
            pass

        return None

    def _percentile_ranks(
        self,
        values,
    ):
        if not values:
            return []

        if len(values) == 1:
            return [100.0]

        sorted_values = sorted(values)
        denominator = len(values) - 1

        ranks = []

        for value in values:
            left_index = bisect.bisect_left(
                sorted_values,
                value,
            )

            right_index = bisect.bisect_right(
                sorted_values,
                value,
            )

            average_index = (
                left_index
                + right_index
                - 1
            ) / 2

            percentile = (
                average_index
                / denominator
                * 100
            )

            ranks.append(
                round(
                    percentile,
                    4,
                )
            )

        return ranks