import time

import pandas as pd

from config.strategies.v1 import (
    SYMBOLS,
)

from dashboard.analytics.geometry_scanner import (
    GeometryScanner,
)


class GeometryMarketScannerService:
    TARGET_GEOMETRIES = {
        "ascending_triangle",
        "descending_triangle",
        "ascending_wedge",
        "descending_wedge",
    }

    DEFAULT_SCANNER_PARAMETERS = {
        "min_window": 10,
        "max_window": 30,
        "pivot_order": 2,
        "min_touches": 2,
        "flat_slope_max": 0.02,
        "min_directional_slope": 0.03,
        "parallel_slope_difference_max": 0.05,
        "minimum_contraction_pct": 12.0,
        "flagpole_lookback": 10,
        "minimum_flagpole_return_pct": 2.0,
        "maximum_flag_retracement_pct": 70.0,
        "breakout_lookahead": 3,
    }

    def __init__(
        self,
        data_service,
    ):
        self.data_service = data_service
        self.last_error = None
        self.last_diagnostics = {}

    def scan_market(
        self,
        timeframe="30m",
        candle_limit=160,
        symbols=None,
        scanner_parameters=None,
        max_candidates_per_symbol=10,
    ):
        self.last_error = None
        self.last_diagnostics = {}

        started_at = time.monotonic()

        if symbols is None:
            symbols = SYMBOLS

        active_symbols = sorted(
            {
                str(symbol).upper()
                for symbol in symbols
                if symbol
            }
        )

        parameters = dict(
            self.DEFAULT_SCANNER_PARAMETERS
        )

        if scanner_parameters:
            parameters.update(
                scanner_parameters
            )

        try:
            scanner = GeometryScanner(
                **parameters
            )
        except (
            TypeError,
            ValueError,
        ) as exc:
            return self._reject(
                f"invalid_scanner_parameters:{exc}"
            )

        market_candles = (
            self.data_service
            .get_market_candles(
                timeframe=timeframe,
                limit=candle_limit,
                symbols=active_symbols,
            )
        )

        if not market_candles:
            return self._reject(
                "market_candles_unavailable:"
                f"{self.data_service.last_error}"
            )

        breakout_lookahead = int(
            parameters.get(
                "breakout_lookahead",
                3,
            )
        )

        recent_end_bars = max(
            1,
            breakout_lookahead + 1,
        )

        rows = []

        scanned_symbols = 0
        symbols_without_candidates = 0
        symbols_with_candidates = set()
        scan_errors = {}

        for symbol, candles in (
            market_candles.items()
        ):
            if candles.empty:
                continue

            scanned_symbols += 1

            try:
                candidates = scanner.scan(
                    candles=candles,
                    max_candidates=int(
                        max_candidates_per_symbol
                    ),
                    include_unclassified=False,
                    recent_end_bars=(
                        recent_end_bars
                    ),
                )
            except Exception as exc:
                scan_errors[symbol] = str(exc)
                continue

            if candidates.empty:
                symbols_without_candidates += 1
                continue

            last_candle_index = (
                len(candles) - 1
            )

            last_candle_timestamp = int(
                candles[
                    "timestamp"
                ].iloc[-1]
            )

            timestamp_to_index = {
                int(timestamp): int(index)
                for index, timestamp in enumerate(
                    candles["timestamp"]
                )
            }

            symbol_has_candidate = False

            for _, candidate in (
                candidates.iterrows()
            ):
                geometry = candidate.get(
                    "geometry"
                )

                if (
                    geometry
                    not in self.TARGET_GEOMETRIES
                ):
                    continue

                breakout_detected = bool(
                    candidate.get(
                        "breakout_detected",
                        False,
                    )
                )

                breakout_timestamp = (
                    candidate.get(
                        "breakout_timestamp"
                    )
                )

                if (
                    breakout_detected
                    and pd.notna(
                        breakout_timestamp
                    )
                ):
                    status = "BREAKOUT"

                    breakout_timestamp = int(
                        breakout_timestamp
                    )

                    breakout_index = (
                        timestamp_to_index.get(
                            breakout_timestamp
                        )
                    )

                    if breakout_index is None:
                        continue

                    breakout_age_bars = (
                        last_candle_index
                        - breakout_index
                    )

                    if (
                        breakout_age_bars
                        > breakout_lookahead
                    ):
                        continue

                elif (
                    int(
                        candidate[
                            "end_index"
                        ]
                    )
                    == last_candle_index
                ):
                    status = "FORMING"
                    breakout_age_bars = None

                else:
                    continue

                row = candidate.to_dict()

                row.update({
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "status": status,
                    "last_candle_timestamp": (
                        last_candle_timestamp
                    ),
                    "pattern_age_bars": (
                        last_candle_index
                        - int(
                            candidate[
                                "end_index"
                            ]
                        )
                    ),
                    "breakout_age_bars": (
                        breakout_age_bars
                    ),
                })

                rows.append(row)

                symbol_has_candidate = True
                symbols_with_candidates.add(
                    symbol
                )

            if not symbol_has_candidate:
                symbols_without_candidates += 1

        elapsed_seconds = (
            time.monotonic()
            - started_at
        )

        self.last_diagnostics = {
            "timeframe": timeframe,
            "candle_limit": int(
                candle_limit
            ),
            "configured_symbols": len(
                active_symbols
            ),
            "loaded_symbols": len(
                market_candles
            ),
            "scanned_symbols": (
                scanned_symbols
            ),
            "symbols_with_candidates": len(
                symbols_with_candidates
            ),
            "symbols_without_candidates": (
                symbols_without_candidates
            ),
            "candidate_count": len(rows),
            "target_geometries": sorted(
                self.TARGET_GEOMETRIES
            ),
            "recent_end_bars": (
                recent_end_bars
            ),
            "scan_errors_count": len(
                scan_errors
            ),
            "scan_errors": scan_errors,
            "elapsed_seconds": round(
                elapsed_seconds,
                3,
            ),
            "market_data": (
                self.data_service
                .last_market_diagnostics
            ),
        }

        if not rows:
            self.last_error = (
                "no_current_market_geometries"
            )
            return pd.DataFrame()

        result = pd.DataFrame(rows)

        result["status_priority"] = (
            result["status"].map({
                "BREAKOUT": 0,
                "FORMING": 1,
            })
        )

        result = result.sort_values(
            [
                "status_priority",
                "confidence",
                "end_timestamp",
            ],
            ascending=[
                True,
                False,
                False,
            ],
        )

        return (
            result
            .drop(
                columns=[
                    "status_priority"
                ]
            )
            .reset_index(drop=True)
        )

    def _reject(
        self,
        reason,
    ):
        self.last_error = str(reason)
        return pd.DataFrame()