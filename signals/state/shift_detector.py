from typing import Optional


class ShiftDetector:
    def detect_potential(
        self,
        prev_direction: Optional[str],
        trend: str,
        momentum: str,
        close: Optional[float] = None,
        ema20: Optional[float] = None,
        ema50: Optional[float] = None,
    ) -> str:

        # =========================================
        # NORMALIZE ENUMS -> STRINGS
        # =========================================

        trend = (
            str(trend)
            .replace("Trend.", "")
            .lower()
        )

        momentum = (
            str(momentum)
            .replace("Momentum.", "")
            .lower()
        )

        prev_direction = (
            str(prev_direction)
            .replace("Direction.", "")
            .lower()
            if prev_direction is not None
            else None
        )

        print(
            "[SHIFT DEBUG]",
            prev_direction,
            trend,
            momentum,
        )

        near_ema20 = self._near_ema(
            close,
            ema20,
            max_distance=0.006,
        )

        near_ema50 = self._near_ema(
            close,
            ema50,
            max_distance=0.010,
        )

        far_from_ema50 = self._far_from_ema(
            close,
            ema50,
            min_distance=0.012,
        )

        very_far_from_ema50 = self._far_from_ema(
            close,
            ema50,
            min_distance=0.012,
        )

        above_ema50 = self._is_above(close, ema50)
        below_ema50 = self._is_below(close, ema50)

        bullish_value_zone = (
            (near_ema20 or near_ema50)
            and above_ema50
        )

        bearish_value_zone = (
            near_ema50
            and below_ema50
        )

        bullish_breakout_zone = (
            above_ema50
            and far_from_ema50
        )

        bearish_breakout_zone = (
            below_ema50
            and far_from_ema50
        )

        # =========================================
        # EXTREME POTENTIAL SHIFTS
        # =========================================

        if (
            prev_direction == "down"
            and momentum == "exhaustion_down"
            and very_far_from_ema50
            and below_ema50
        ):
            return "potential_bullish_extreme_shift"

        if (
            prev_direction in ["up", "neutral"]
            and momentum == "exhaustion_up"
            and trend != "bearish"
        ):
            return "potential_bearish_extreme_shift"

        # =========================================
        # VALUE POTENTIAL SHIFTS
        # =========================================

        if (
            prev_direction == "down"
            and momentum in [
                "exhaustion_down",
                "inside_bar",
                "inside_bullish_weak",
                "breakout_down_weak",
            ]
            and (
                (
                    trend != "bullish"
                    and above_ema50
                )
                or (
                    trend == "bullish"
                    and (
                        bullish_value_zone
                        or bullish_breakout_zone
                    )
                )
            )
        ):
            return "potential_bullish_value_shift"

        if (
            prev_direction == "up"
            and momentum in [
                "exhaustion_up",
                "inside_bar",
                "inside_bearish_weak",
                "breakout_up_weak",
            ]
            and (
                (
                    trend != "bearish"
                    and below_ema50
                )
                or (
                    trend == "bearish"
                    and (
                        bearish_value_zone
                        or bearish_breakout_zone
                    )
                )
            )
        ):
            return "potential_bearish_value_shift"

        return "no_shift"

    def confirm(
        self,
        potential_shift: str,
        current_momentum: str,
    ) -> str:

        # =========================================
        # NORMALIZE ENUMS -> STRINGS
        # =========================================

        potential_shift = str(potential_shift)

        current_momentum = (
            str(current_momentum)
            .replace("Momentum.", "")
            .lower()
        )

        print(
            "[SHIFT CONFIRM]",
            potential_shift,
            current_momentum,
        )

        # =========================================
        # VALUE CONFIRMATION
        # =========================================

        if (
            potential_shift == "potential_bullish_value_shift"
            and current_momentum in [
                "breakout_up_strong",
                "trend_continuation_up",
                "bullish_pressure",
            ]
        ):
            return "bullish_value_shift"

        if (
            potential_shift == "potential_bearish_value_shift"
            and current_momentum in [
                "breakout_down_strong",
                "trend_continuation_down",
                "bearish_pressure",
            ]
        ):
            return "bearish_value_shift"

        # =========================================
        # EXTREME CONFIRMATION
        # =========================================

        if (
            potential_shift == "potential_bullish_extreme_shift"
            and current_momentum in [
                "breakout_up_weak",
                "trend_continuation_up",
            ]
        ):
            return "bullish_extreme_shift"

        if (
            potential_shift == "potential_bearish_extreme_shift"
            and current_momentum in [
                "breakout_down_weak",
            ]
        ):
            return "bearish_extreme_shift"

        return "no_shift"

    def _near_ema(
        self,
        close,
        ema,
        max_distance=0.01,
    ) -> bool:
        try:
            close = float(close)
            ema = float(ema)

            if close <= 0 or ema <= 0:
                return False

            return (
                abs(close - ema) / close
                <= max_distance
            )

        except Exception:
            return False

    def _far_from_ema(
        self,
        close,
        ema,
        min_distance=0.012,
    ) -> bool:
        try:
            close = float(close)
            ema = float(ema)

            if close <= 0 or ema <= 0:
                return False

            return (
                abs(close - ema) / close
                >= min_distance
            )

        except Exception:
            return False

    def _is_above(self, close, ema) -> bool:
        try:
            return float(close) > float(ema)
        except Exception:
            return False

    def _is_below(self, close, ema) -> bool:
        try:
            return float(close) < float(ema)
        except Exception:
            return False