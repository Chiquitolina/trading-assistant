import math

import numpy as np
import pandas as pd


class GeometryScanner:
    REQUIRED_COLUMNS = {
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
    }

    def __init__(
        self,
        min_window=10,
        max_window=30,
        pivot_order=2,
        min_touches=2,
        flat_slope_max=0.03,
        min_directional_slope=0.02,
        parallel_slope_difference_max=0.05,
        minimum_contraction_pct=12.0,
        flagpole_lookback=10,
        minimum_flagpole_return_pct=2.0,
        maximum_flag_retracement_pct=70.0,
        breakout_lookahead=3,
        boundary_tolerance_pct=0.10,
    ):
        self.min_window = int(min_window)
        self.max_window = int(max_window)
        self.pivot_order = int(pivot_order)
        self.min_touches = int(min_touches)

        self.flat_slope_max = float(
            flat_slope_max
        )

        self.min_directional_slope = float(
            min_directional_slope
        )

        self.parallel_slope_difference_max = (
            float(
                parallel_slope_difference_max
            )
        )

        self.minimum_contraction_pct = float(
            minimum_contraction_pct
        )

        self.flagpole_lookback = int(
            flagpole_lookback
        )

        self.minimum_flagpole_return_pct = (
            float(
                minimum_flagpole_return_pct
            )
        )

        self.maximum_flag_retracement_pct = (
            float(
                maximum_flag_retracement_pct
            )
        )

        self.breakout_lookahead = int(
            breakout_lookahead
        )
        
        self.boundary_tolerance_pct = float(
            boundary_tolerance_pct
        )

        self.last_error = None

        self._validate_parameters()

    def scan(
        self,
        candles,
        max_candidates=50,
        include_unclassified=False,
        recent_end_bars=None,
    ):
        self.last_error = None

        if not isinstance(
            candles,
            pd.DataFrame,
        ):
            return self._reject(
                "candles_must_be_dataframe"
            )

        missing_columns = (
            self.REQUIRED_COLUMNS
            - set(candles.columns)
        )

        if missing_columns:
            return self._reject(
                "missing_columns:"
                + ",".join(
                    sorted(missing_columns)
                )
            )

        if len(candles) < (
            self.min_window
            + self.flagpole_lookback
        ):
            return self._reject(
                "insufficient_candles"
            )

        candles = (
            candles
            .sort_values("timestamp")
            .drop_duplicates(
                "timestamp",
                keep="last",
            )
            .reset_index(drop=True)
        )

        candidates = []

        first_end_index = (
            self.flagpole_lookback
            + self.min_window
            - 1
        )

        if recent_end_bars is not None:
            try:
                recent_end_bars = int(
                    recent_end_bars
                )
            except (TypeError, ValueError):
                return self._reject(
                    "invalid_recent_end_bars"
                )

            if recent_end_bars < 1:
                return self._reject(
                    "recent_end_bars_must_be_positive"
                )

            first_end_index = max(
                first_end_index,
                len(candles)
                - recent_end_bars,
            )

        for end_index in range(
            first_end_index,
            len(candles),
        ):

            for window_size in range(
                self.min_window,
                self.max_window + 1,
            ):
                start_index = (
                    end_index
                    - window_size
                    + 1
                )

                if (
                    start_index
                    < self.flagpole_lookback
                ):
                    continue

                candidate = (
                    self._analyze_window(
                        candles=candles,
                        start_index=start_index,
                        end_index=end_index,
                    )
                )

                if candidate is None:
                    continue

                if (
                    not include_unclassified
                    and candidate["geometry"]
                    == "unclassified"
                ):
                    continue

                candidates.append(
                    candidate
                )

        if not candidates:
            self.last_error = (
                "no_geometry_candidates"
            )

            return pd.DataFrame()

        candidates.sort(
            key=lambda item: (
                item["confidence"],
                item["breakout_detected"],
                item["end_timestamp"],
            ),
            reverse=True,
        )

        candidates = self._deduplicate(
            candidates
        )

        candidates = candidates[
            :int(max_candidates)
        ]

        return pd.DataFrame(
            candidates
        )

    def _analyze_window(
        self,
        candles,
        start_index,
        end_index,
    ):
        window = candles.iloc[
            start_index:
            end_index + 1
        ].reset_index(drop=True)

        reference_price = float(
            window["close"].iloc[0]
        )

        if (
            not math.isfinite(
                reference_price
            )
            or reference_price <= 0
        ):
            return None

        high_pivots = self._find_pivots(
            window["high"].to_numpy(
                dtype=float
            ),
            mode="high",
        )

        low_pivots = self._find_pivots(
            window["low"].to_numpy(
                dtype=float
            ),
            mode="low",
        )

        if (
            len(high_pivots)
            < self.min_touches
            or len(low_pivots)
            < self.min_touches
        ):
            return None

        upper_line = self._fit_line(
            indexes=high_pivots,
            prices=window[
                "high"
            ].to_numpy(dtype=float),
            reference_price=(
                reference_price
            ),
        )

        lower_line = self._fit_line(
            indexes=low_pivots,
            prices=window[
                "low"
            ].to_numpy(dtype=float),
            reference_price=(
                reference_price
            ),
        )

        if (
            upper_line is None
            or lower_line is None
        ):
            return None

        last_x = len(window) - 1

        upper_start_pct = (
            upper_line["intercept"]
        )

        upper_end_pct = (
            upper_line["intercept"]
            + upper_line["slope"]
            * last_x
        )

        lower_start_pct = (
            lower_line["intercept"]
        )

        lower_end_pct = (
            lower_line["intercept"]
            + lower_line["slope"]
            * last_x
        )

        width_start_pct = (
            upper_start_pct
            - lower_start_pct
        )

        width_end_pct = (
            upper_end_pct
            - lower_end_pct
        )

        if (
            width_start_pct <= 0
            or width_end_pct <= 0
        ):
            return None
        
        window_x = np.arange(
            len(window),
            dtype=float,
        )

        window_close_pct = (
            (
                window[
                    "close"
                ].to_numpy(dtype=float)
                / reference_price
            )
            - 1
        ) * 100

        projected_upper_pct = (
            upper_line["intercept"]
            + upper_line["slope"]
            * window_x
        )

        projected_lower_pct = (
            lower_line["intercept"]
            + lower_line["slope"]
            * window_x
        )

        upper_close_breaches = (
            window_close_pct
            - projected_upper_pct
        )

        lower_close_breaches = (
            projected_lower_pct
            - window_close_pct
        )

        maximum_upper_close_breach_pct = (
            float(
                np.max(
                    upper_close_breaches
                )
            )
        )

        maximum_lower_close_breach_pct = (
            float(
                np.max(
                    lower_close_breaches
                )
            )
        )

        if (
            maximum_upper_close_breach_pct
            > self.boundary_tolerance_pct
            or maximum_lower_close_breach_pct
            > self.boundary_tolerance_pct
        ):
            return None

        width_contraction_ratio = (
            width_end_pct
            / width_start_pct
        )

        contraction_pct = (
            1
            - width_contraction_ratio
        ) * 100

        flagpole = candles.iloc[
            start_index
            - self.flagpole_lookback:
            start_index + 1
        ]

        flagpole_start = float(
            flagpole["close"].iloc[0]
        )

        flagpole_end = float(
            flagpole["close"].iloc[-1]
        )

        flagpole_return_pct = (
            flagpole_end
            / flagpole_start
            - 1
        ) * 100

        flagpole_height = (
            flagpole_end
            - flagpole_start
        )

        if flagpole_height > 0:
            flag_retracement_pct = max(
                0.0,
                (
                    flagpole_end
                    - float(
                        window["low"].min()
                    )
                )
                / flagpole_height
                * 100,
            )
        else:
            flag_retracement_pct = None

        geometry, reasons = (
            self._classify_geometry(
                upper_slope=(
                    upper_line["slope"]
                ),
                lower_slope=(
                    lower_line["slope"]
                ),
                contraction_pct=(
                    contraction_pct
                ),
                flagpole_return_pct=(
                    flagpole_return_pct
                ),
                flag_retracement_pct=(
                    flag_retracement_pct
                ),
            )
        )

        confidence = self._confidence(
            geometry=geometry,
            upper_r2=upper_line["r2"],
            lower_r2=lower_line["r2"],
            touches_high=len(
                high_pivots
            ),
            touches_low=len(
                low_pivots
            ),
            contraction_pct=(
                contraction_pct
            ),
            flagpole_return_pct=(
                flagpole_return_pct
            ),
            flag_retracement_pct=(
                flag_retracement_pct
            ),
        )

        upper_start_price = (
            self._pct_to_price(
                upper_start_pct,
                reference_price,
            )
        )

        upper_end_price = (
            self._pct_to_price(
                upper_end_pct,
                reference_price,
            )
        )

        lower_start_price = (
            self._pct_to_price(
                lower_start_pct,
                reference_price,
            )
        )

        lower_end_price = (
            self._pct_to_price(
                lower_end_pct,
                reference_price,
            )
        )

        breakout = self._find_breakout(
            candles=candles,
            end_index=end_index,
            reference_price=(
                reference_price
            ),
            upper_line=upper_line,
            lower_line=lower_line,
            pattern_window_size=(
                len(window)
            ),
        )

        return {
            "geometry": geometry,
            "confidence": round(
                confidence,
                2,
            ),
            "start_index": int(
                start_index
            ),
            "end_index": int(
                end_index
            ),
            "window_size": int(
                len(window)
            ),
            "start_timestamp": int(
                window[
                    "timestamp"
                ].iloc[0]
            ),
            "end_timestamp": int(
                window[
                    "timestamp"
                ].iloc[-1]
            ),
            "upper_slope_pct_per_bar": (
                round(
                    upper_line["slope"],
                    6,
                )
            ),
            "lower_slope_pct_per_bar": (
                round(
                    lower_line["slope"],
                    6,
                )
            ),
            "slope_difference_pct_per_bar": (
                round(
                    upper_line["slope"]
                    - lower_line["slope"],
                    6,
                )
            ),
            "upper_r2": round(
                upper_line["r2"],
                4,
            ),
            "lower_r2": round(
                lower_line["r2"],
                4,
            ),
            "touches_high": len(
                high_pivots
            ),
            "touches_low": len(
                low_pivots
            ),
            "high_pivot_indexes": [
                int(
                    start_index
                    + pivot_index
                )
                for pivot_index
                in high_pivots
            ],
            "low_pivot_indexes": [
                int(
                    start_index
                    + pivot_index
                )
                for pivot_index
                in low_pivots
            ],
            "width_start_pct": round(
                width_start_pct,
                4,
            ),
            "width_end_pct": round(
                width_end_pct,
                4,
            ),
            "width_contraction_ratio": (
                round(
                    width_contraction_ratio,
                    4,
                )
            ),
            "contraction_pct": round(
                contraction_pct,
                4,
            ),
            "boundary_tolerance_pct": (
                round(
                    self.boundary_tolerance_pct,
                    4,
                )
            ),
            "maximum_upper_close_breach_pct": (
                round(
                    maximum_upper_close_breach_pct,
                    4,
                )
            ),
            "maximum_lower_close_breach_pct": (
                round(
                    maximum_lower_close_breach_pct,
                    4,
                )
            ),
            "flagpole_return_pct": round(
                flagpole_return_pct,
                4,
            ),
            "flag_retracement_pct": (
                round(
                    flag_retracement_pct,
                    4,
                )
                if flag_retracement_pct
                is not None
                else None
            ),
            "upper_start_price": round(
                upper_start_price,
                10,
            ),
            "upper_end_price": round(
                upper_end_price,
                10,
            ),
            "lower_start_price": round(
                lower_start_price,
                10,
            ),
            "lower_end_price": round(
                lower_end_price,
                10,
            ),
            "breakout_detected": (
                breakout is not None
            ),
            "breakout_timestamp": (
                breakout[
                    "timestamp"
                ]
                if breakout
                else None
            ),
            "breakout_price": (
                breakout["price"]
                if breakout
                else None
            ),
            "breakout_direction": (
                breakout["direction"]
                if breakout
                else None
            ),
            "breakout_boundary_price": (
                breakout["boundary_price"]
                if breakout
                else None
            ),
            "breakout_score": (
                breakout["score"]
                if breakout
                else None
            ),
            "breakout_close_distance_pct": (
                breakout[
                    "close_distance_pct"
                ]
                if breakout
                else None
            ),
            "breakout_body_distance_pct": (
                breakout[
                    "body_distance_pct"
                ]
                if breakout
                else None
            ),
            "breakout_body_pct": (
                breakout["body_pct"]
                if breakout
                else None
            ),
            "breakout_volume_ratio": (
                breakout["volume_ratio"]
                if breakout
                else None
            ),
            "breakout_atr_extension": (
                breakout["atr_extension"]
                if breakout
                else None
            ),
            "breakout_close_location_pct": (
                breakout[
                    "close_location_pct"
                ]
                if breakout
                else None
            ),

            "reasons": reasons,
            
            "reasons": reasons,
        }

    def _find_pivots(
        self,
        values,
        mode,
    ):
        pivots = []

        order = self.pivot_order

        for index in range(
            order,
            len(values) - order,
        ):
            neighborhood = values[
                index - order:
                index + order + 1
            ]

            value = values[index]

            if (
                mode == "high"
                and value
                >= np.max(neighborhood)
            ):
                pivots.append(index)

            elif (
                mode == "low"
                and value
                <= np.min(neighborhood)
            ):
                pivots.append(index)

        return pivots

    def _fit_line(
        self,
        indexes,
        prices,
        reference_price,
    ):
        x_values = np.asarray(
            indexes,
            dtype=float,
        )

        y_values = np.asarray(
            [
                (
                    prices[index]
                    / reference_price
                    - 1
                )
                * 100
                for index in indexes
            ],
            dtype=float,
        )

        if len(x_values) < 2:
            return None

        slope, intercept = np.polyfit(
            x_values,
            y_values,
            1,
        )

        predictions = (
            slope * x_values
            + intercept
        )

        residual_sum = float(
            np.sum(
                (
                    y_values
                    - predictions
                ) ** 2
            )
        )

        total_sum = float(
            np.sum(
                (
                    y_values
                    - np.mean(y_values)
                ) ** 2
            )
        )

        if total_sum <= 0:
            r2 = 1.0
        else:
            r2 = max(
                0.0,
                1
                - residual_sum
                / total_sum,
            )

        return {
            "slope": float(slope),
            "intercept": float(
                intercept
            ),
            "r2": float(r2),
        }

    def _classify_geometry(
        self,
        upper_slope,
        lower_slope,
        contraction_pct,
        flagpole_return_pct,
        flag_retracement_pct,
    ):
        upper_flat = (
            abs(upper_slope)
            <= self.flat_slope_max
        )

        lower_flat = (
            abs(lower_slope)
            <= self.flat_slope_max
        )

        upper_down = (
            upper_slope
            <= -self.min_directional_slope
        )

        lower_down = (
            lower_slope
            <= -self.min_directional_slope
        )

        upper_up = (
            upper_slope
            >= self.min_directional_slope
        )

        lower_up = (
            lower_slope
            >= self.min_directional_slope
        )

        contracts = (
            contraction_pct
            >= self.minimum_contraction_pct
        )

        parallel = (
            abs(
                upper_slope
                - lower_slope
            )
            <= self
            .parallel_slope_difference_max
        )

        bullish_flagpole = (
            flagpole_return_pct
            >= self.minimum_flagpole_return_pct
        )

        controlled_retracement = (
            flag_retracement_pct
            is not None
            and flag_retracement_pct
            <= self.maximum_flag_retracement_pct
        )

        reasons = [
            (
                "upper_slope="
                f"{upper_slope:.4f}%/bar"
            ),
            (
                "lower_slope="
                f"{lower_slope:.4f}%/bar"
            ),
            (
                "contraction="
                f"{contraction_pct:.2f}%"
            ),
            (
                "flagpole_return="
                f"{flagpole_return_pct:.2f}%"
            ),
        ]

        if upper_flat and lower_flat:
            return (
                "horizontal_box",
                reasons,
            )

        # Resistencia horizontal y mínimos
        # progresivamente más altos.
        if (
            upper_flat
            and lower_up
            and contracts
        ):
            return (
                "ascending_triangle",
                reasons,
            )

        # Máximos progresivamente más bajos
        # y soporte aproximadamente horizontal.
        if (
            upper_down
            and lower_flat
            and contracts
        ):
            return (
                "descending_triangle",
                reasons,
            )

        if (
            upper_down
            and lower_up
            and contracts
            and bullish_flagpole
            and controlled_retracement
        ):
            return (
                "symmetric_pennant",
                reasons,
            )

        if (
            upper_down
            and lower_down
            and parallel
            and bullish_flagpole
            and controlled_retracement
        ):
            return (
                "bull_flag",
                reasons,
            )

        # Ambas fronteras descienden, pero
        # la superior cae más rápidamente.
        if (
            upper_down
            and lower_down
            and upper_slope < lower_slope
            and contracts
        ):
            return (
                "descending_wedge",
                reasons,
            )

        # Ambas fronteras ascienden, pero
        # la inferior sube más rápidamente.
        if (
            upper_up
            and lower_up
            and lower_slope > upper_slope
            and contracts
        ):
            return (
                "ascending_wedge",
                reasons,
            )

        if (
            upper_up
            and lower_up
            and parallel
        ):
            return (
                "ascending_base",
                reasons,
            )

        return (
            "unclassified",
            reasons,
        )

    def _confidence(
        self,
        geometry,
        upper_r2,
        lower_r2,
        touches_high,
        touches_low,
        contraction_pct,
        flagpole_return_pct,
        flag_retracement_pct,
    ):
        if geometry == "unclassified":
            return 0.0

        score = 30.0

        score += min(
            touches_high
            + touches_low,
            8,
        ) / 8 * 20

        score += (
            upper_r2
            + lower_r2
        ) / 2 * 20

        if contraction_pct > 0:
            score += min(
                contraction_pct,
                50,
            ) / 50 * 15

        if (
            flagpole_return_pct
            >= self.minimum_flagpole_return_pct
        ):
            score += 10

        if (
            flag_retracement_pct
            is not None
            and flag_retracement_pct
            <= self.maximum_flag_retracement_pct
        ):
            score += 5

        return min(
            score,
            100.0,
        )

    def _find_breakout(
        self,
        candles,
        end_index,
        reference_price,
        upper_line,
        lower_line,
        pattern_window_size,
    ):
        first_future_index = (
            end_index + 1
        )

        last_future_index = min(
            len(candles),
            first_future_index
            + self.breakout_lookahead,
        )

        for future_index in range(
            first_future_index,
            last_future_index,
        ):
            offset = (
                future_index
                - end_index
            )

            projected_x = (
                pattern_window_size
                - 1
                + offset
            )

            projected_upper_pct = (
                upper_line["intercept"]
                + upper_line["slope"]
                * projected_x
            )

            projected_lower_pct = (
                lower_line["intercept"]
                + lower_line["slope"]
                * projected_x
            )

            projected_upper_price = (
                self._pct_to_price(
                    projected_upper_pct,
                    reference_price,
                )
            )

            projected_lower_price = (
                self._pct_to_price(
                    projected_lower_pct,
                    reference_price,
                )
            )

            close_price = float(
                candles["close"].iloc[
                    future_index
                ]
            )

            timestamp = int(
                candles["timestamp"].iloc[
                    future_index
                ]
            )

            if (
                close_price
                > projected_upper_price
            ):
                return (
                    self._build_breakout_result(
                        candles=candles,
                        breakout_index=(
                            future_index
                        ),
                        timestamp=timestamp,
                        direction="UP",
                        boundary_price=(
                            projected_upper_price
                        ),
                    )
                )

            if (
                close_price
                < projected_lower_price
            ):
                return (
                    self._build_breakout_result(
                        candles=candles,
                        breakout_index=(
                            future_index
                        ),
                        timestamp=timestamp,
                        direction="DOWN",
                        boundary_price=(
                            projected_lower_price
                        ),
                    )
                )

        return None

    def _build_breakout_result(
        self,
        candles,
        breakout_index,
        timestamp,
        direction,
        boundary_price,
    ):
        candle = candles.iloc[
            breakout_index
        ]

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
            candle["volume"]
        )

        candle_range = max(
            high_price - low_price,
            0.0,
        )

        candle_body = abs(
            close_price - open_price
        )

        body_low = min(
            open_price,
            close_price,
        )

        body_high = max(
            open_price,
            close_price,
        )

        if direction == "UP":
            close_distance = (
                close_price
                - boundary_price
            )

            body_outside = max(
                0.0,
                body_high
                - max(
                    body_low,
                    boundary_price,
                ),
            )

            close_location_pct = (
                (
                    close_price
                    - low_price
                )
                / candle_range
                * 100
                if candle_range > 0
                else 50.0
            )

        else:
            close_distance = (
                boundary_price
                - close_price
            )

            body_outside = max(
                0.0,
                min(
                    body_high,
                    boundary_price,
                )
                - body_low,
            )

            close_location_pct = (
                (
                    high_price
                    - close_price
                )
                / candle_range
                * 100
                if candle_range > 0
                else 50.0
            )

        close_distance_pct = (
            close_distance
            / boundary_price
            * 100
        )

        body_distance_pct = (
            body_outside
            / boundary_price
            * 100
        )

        body_pct = (
            candle_body
            / candle_range
            * 100
            if candle_range > 0
            else 0.0
        )

        volume_ratio = (
            self._calculate_breakout_volume_ratio(
                candles=candles,
                breakout_index=(
                    breakout_index
                ),
                breakout_volume=volume,
            )
        )

        atr = self._calculate_prior_atr(
            candles=candles,
            breakout_index=(
                breakout_index
            ),
            period=14,
        )

        atr_extension = (
            close_distance
            / atr
            if (
                atr is not None
                and atr > 0
            )
            else None
        )

        score = self._calculate_breakout_score(
            close_distance_pct=(
                close_distance_pct
            ),
            body_distance_pct=(
                body_distance_pct
            ),
            body_pct=body_pct,
            volume_ratio=volume_ratio,
            atr_extension=atr_extension,
            close_location_pct=(
                close_location_pct
            ),
        )

        return {
            "timestamp": int(timestamp),
            "price": round(
                close_price,
                10,
            ),
            "direction": direction,
            "boundary_price": round(
                boundary_price,
                10,
            ),
            "score": round(
                score,
                2,
            ),
            "close_distance_pct": round(
                max(
                    close_distance_pct,
                    0.0,
                ),
                4,
            ),
            "body_distance_pct": round(
                max(
                    body_distance_pct,
                    0.0,
                ),
                4,
            ),
            "body_pct": round(
                body_pct,
                2,
            ),
            "volume_ratio": (
                round(
                    volume_ratio,
                    4,
                )
                if volume_ratio
                is not None
                else None
            ),
            "atr_extension": (
                round(
                    atr_extension,
                    4,
                )
                if atr_extension
                is not None
                else None
            ),
            "close_location_pct": round(
                close_location_pct,
                2,
            ),
        }


    def _calculate_breakout_volume_ratio(
        self,
        candles,
        breakout_index,
        breakout_volume,
        lookback=20,
    ):
        first_index = max(
            0,
            breakout_index
            - int(lookback),
        )

        previous_volumes = (
            candles["volume"]
            .iloc[
                first_index:
                breakout_index
            ]
            .astype(float)
        )

        previous_volumes = (
            previous_volumes[
                np.isfinite(
                    previous_volumes
                )
                & (
                    previous_volumes
                    >= 0
                )
            ]
        )

        if previous_volumes.empty:
            return None

        median_volume = float(
            previous_volumes.median()
        )

        if median_volume <= 0:
            return None

        return (
            breakout_volume
            / median_volume
        )


    def _calculate_prior_atr(
        self,
        candles,
        breakout_index,
        period=14,
    ):
        last_index = (
            breakout_index - 1
        )

        if last_index < 1:
            return None

        first_index = max(
            1,
            last_index
            - int(period)
            + 1,
        )

        true_ranges = []

        for index in range(
            first_index,
            last_index + 1,
        ):
            high_price = float(
                candles["high"].iloc[
                    index
                ]
            )

            low_price = float(
                candles["low"].iloc[
                    index
                ]
            )

            previous_close = float(
                candles["close"].iloc[
                    index - 1
                ]
            )

            true_range = max(
                high_price - low_price,
                abs(
                    high_price
                    - previous_close
                ),
                abs(
                    low_price
                    - previous_close
                ),
            )

            if math.isfinite(
                true_range
            ):
                true_ranges.append(
                    true_range
                )

        if not true_ranges:
            return None

        return float(
            np.mean(true_ranges)
        )


    def _calculate_breakout_score(
        self,
        close_distance_pct,
        body_distance_pct,
        body_pct,
        volume_ratio,
        atr_extension,
        close_location_pct,
    ):
        close_distance_score = min(
            max(
                close_distance_pct,
                0.0,
            )
            / 0.30,
            1.0,
        )

        body_distance_score = min(
            max(
                body_distance_pct,
                0.0,
            )
            / 0.10,
            1.0,
        )

        body_score = min(
            max(
                body_pct,
                0.0,
            )
            / 70.0,
            1.0,
        )

        volume_score = (
            min(
                max(
                    volume_ratio,
                    0.0,
                )
                / 1.50,
                1.0,
            )
            if volume_ratio is not None
            else 0.0
        )

        atr_score = (
            min(
                max(
                    atr_extension,
                    0.0,
                )
                / 0.35,
                1.0,
            )
            if atr_extension is not None
            else 0.0
        )

        close_location_score = min(
            max(
                close_location_pct,
                0.0,
            )
            / 100.0,
            1.0,
        )

        return (
            close_distance_score * 25
            + body_distance_score * 15
            + body_score * 20
            + volume_score * 20
            + atr_score * 10
            + close_location_score * 10
        )

    def _deduplicate(
        self,
        candidates,
    ):
        selected = []

        for candidate in candidates:
            duplicate = False

            for existing in selected:
                if (
                    candidate["geometry"]
                    != existing["geometry"]
                ):
                    continue

                overlap_start = max(
                    candidate[
                        "start_index"
                    ],
                    existing[
                        "start_index"
                    ],
                )

                overlap_end = min(
                    candidate[
                        "end_index"
                    ],
                    existing[
                        "end_index"
                    ],
                )

                overlap_size = max(
                    0,
                    overlap_end
                    - overlap_start
                    + 1,
                )

                minimum_size = min(
                    candidate[
                        "window_size"
                    ],
                    existing[
                        "window_size"
                    ],
                )

                overlap_ratio = (
                    overlap_size
                    / minimum_size
                    if minimum_size
                    else 0
                )

                if overlap_ratio >= 0.70:
                    duplicate = True
                    break

            if not duplicate:
                selected.append(
                    candidate
                )

        return selected

    def _pct_to_price(
        self,
        percentage,
        reference_price,
    ):
        return reference_price * (
            1
            + percentage / 100
        )

    def _validate_parameters(self):
        if self.min_window < 5:
            raise ValueError(
                "min_window must be >= 5"
            )

        if self.max_window < self.min_window:
            raise ValueError(
                "max_window must be >= min_window"
            )

        if self.pivot_order < 1:
            raise ValueError(
                "pivot_order must be >= 1"
            )

        if self.min_touches < 2:
            raise ValueError(
                "min_touches must be >= 2"
            )

        if self.flagpole_lookback < 2:
            raise ValueError(
                "flagpole_lookback must be >= 2"
            )

        if self.breakout_lookahead < 0:
            raise ValueError(
                "breakout_lookahead cannot be negative"
            )
        
        if self.boundary_tolerance_pct < 0:
            raise ValueError(
                "boundary_tolerance_pct "
                "cannot be negative"
            )

    def _reject(
        self,
        reason,
    ):
        self.last_error = str(reason)
        return pd.DataFrame()