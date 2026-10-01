from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SwingPoint:
    """
    Swing confirmado sin lookahead.

    pivot_timestamp:
        Timestamp de la vela donde ocurrió el máximo/mínimo.

    confirmed_timestamp:
        Timestamp de la última vela necesaria para confirmar
        que el pivot efectivamente era un swing.

    Importante:
        El swing NO debe considerarse disponible antes de
        confirmed_timestamp.
    """

    side: str

    price: float

    pivot_timestamp: int
    confirmed_timestamp: int

    left_bars: int
    right_bars: int

    prominence_pct: Optional[float] = None


class SwingDetector:
    """
    Detector research-only de pivots simétricos confirmados.

    Un SWING HIGH requiere que el high del pivot sea mayor
    que todos los highs de N velas anteriores y N posteriores.

    Un SWING LOW requiere que el low del pivot sea menor
    que todos los lows de N velas anteriores y N posteriores.

    No ejecuta órdenes.
    No genera señales.
    No modifica ningún estado del trading engine.
    """

    def __init__(
        self,
        left_bars: int = 3,
        right_bars: int = 3,
        min_prominence_pct: float = 0.0,
    ):
        if left_bars < 1:
            raise ValueError(
                "left_bars must be >= 1"
            )

        if right_bars < 1:
            raise ValueError(
                "right_bars must be >= 1"
            )

        if min_prominence_pct < 0:
            raise ValueError(
                "min_prominence_pct must be >= 0"
            )

        self.left_bars = int(left_bars)
        self.right_bars = int(right_bars)

        self.min_prominence_pct = float(
            min_prominence_pct
        )

    # ==========================================
    # BASIC VALIDATION
    # ==========================================

    @staticmethod
    def _validate_candle(candle):
        required = (
            "timestamp",
            "high",
            "low",
        )

        for field in required:
            if field not in candle:
                raise ValueError(
                    f"Missing candle field: {field}"
                )

    def _can_evaluate(
        self,
        candles,
        index,
    ):
        if index < self.left_bars:
            return False

        if (
            index + self.right_bars
            >= len(candles)
        ):
            return False

        return True

    # ==========================================
    # PROMINENCE
    # ==========================================

    def _high_prominence_pct(
        self,
        candles,
        index,
    ):
        pivot = float(
            candles[index]["high"]
        )

        if pivot <= 0:
            return None

        surrounding_highs = [
            float(candles[i]["high"])
            for i in range(
                index - self.left_bars,
                index + self.right_bars + 1,
            )
            if i != index
        ]

        if not surrounding_highs:
            return None

        reference = max(
            surrounding_highs
        )

        return (
            (pivot / reference) - 1
        ) * 100

    def _low_prominence_pct(
        self,
        candles,
        index,
    ):
        pivot = float(
            candles[index]["low"]
        )

        if pivot <= 0:
            return None

        surrounding_lows = [
            float(candles[i]["low"])
            for i in range(
                index - self.left_bars,
                index + self.right_bars + 1,
            )
            if i != index
        ]

        if not surrounding_lows:
            return None

        reference = min(
            surrounding_lows
        )

        if reference <= 0:
            return None

        return (
            (reference / pivot) - 1
        ) * 100

    # ==========================================
    # DETECTION
    # ==========================================

    def is_swing_high(
        self,
        candles,
        index,
    ):
        if not self._can_evaluate(
            candles,
            index,
        ):
            return False

        pivot_high = float(
            candles[index]["high"]
        )

        left = candles[
            index - self.left_bars:index
        ]

        right = candles[
            index + 1:
            index + self.right_bars + 1
        ]

        if not all(
            pivot_high
            > float(candle["high"])
            for candle in left
        ):
            return False

        if not all(
            pivot_high
            > float(candle["high"])
            for candle in right
        ):
            return False

        prominence = (
            self._high_prominence_pct(
                candles,
                index,
            )
        )

        if prominence is None:
            return False

        return (
            prominence
            >= self.min_prominence_pct
        )

    def is_swing_low(
        self,
        candles,
        index,
    ):
        if not self._can_evaluate(
            candles,
            index,
        ):
            return False

        pivot_low = float(
            candles[index]["low"]
        )

        left = candles[
            index - self.left_bars:index
        ]

        right = candles[
            index + 1:
            index + self.right_bars + 1
        ]

        if not all(
            pivot_low
            < float(candle["low"])
            for candle in left
        ):
            return False

        if not all(
            pivot_low
            < float(candle["low"])
            for candle in right
        ):
            return False

        prominence = (
            self._low_prominence_pct(
                candles,
                index,
            )
        )

        if prominence is None:
            return False

        return (
            prominence
            >= self.min_prominence_pct
        )

    # ==========================================
    # BUILD SWING
    # ==========================================

    def _build_swing(
        self,
        candles,
        index,
        side,
    ):
        pivot = candles[index]

        confirmation_index = (
            index + self.right_bars
        )

        confirmation = candles[
            confirmation_index
        ]

        if side == "HIGH":
            price = float(
                pivot["high"]
            )

            prominence = (
                self._high_prominence_pct(
                    candles,
                    index,
                )
            )

        elif side == "LOW":
            price = float(
                pivot["low"]
            )

            prominence = (
                self._low_prominence_pct(
                    candles,
                    index,
                )
            )

        else:
            raise ValueError(
                f"Invalid side: {side}"
            )

        return SwingPoint(
            side=side,
            price=price,
            pivot_timestamp=int(
                pivot["timestamp"]
            ),
            confirmed_timestamp=int(
                confirmation["timestamp"]
            ),
            left_bars=self.left_bars,
            right_bars=self.right_bars,
            prominence_pct=prominence,
        )

    # ==========================================
    # DETECT ALL
    # ==========================================

    def detect_all(
        self,
        candles,
    ):
        """
        Devuelve todos los swings confirmados
        contenidos en la secuencia.
        """

        candles = list(candles)

        if not candles:
            return []

        for candle in candles:
            self._validate_candle(
                candle
            )

        swings = []

        start = self.left_bars

        end = (
            len(candles)
            - self.right_bars
        )

        for index in range(
            start,
            end,
        ):
            if self.is_swing_high(
                candles,
                index,
            ):
                swings.append(
                    self._build_swing(
                        candles,
                        index,
                        "HIGH",
                    )
                )

            if self.is_swing_low(
                candles,
                index,
            ):
                swings.append(
                    self._build_swing(
                        candles,
                        index,
                        "LOW",
                    )
                )

        swings.sort(
            key=lambda swing: (
                swing.pivot_timestamp,
                swing.side,
            )
        )

        return swings

    # ==========================================
    # LAST CONFIRMED SWINGS
    # ==========================================

    def last_confirmed_high(
        self,
        candles,
        as_of_timestamp=None,
    ):
        swings = self.detect_all(
            candles
        )

        highs = [
            swing
            for swing in swings
            if swing.side == "HIGH"
        ]

        if as_of_timestamp is not None:
            highs = [
                swing
                for swing in highs
                if (
                    swing.confirmed_timestamp
                    <= int(as_of_timestamp)
                )
            ]

        if not highs:
            return None

        return max(
            highs,
            key=lambda swing:
                swing.confirmed_timestamp,
        )

    def last_confirmed_low(
        self,
        candles,
        as_of_timestamp=None,
    ):
        swings = self.detect_all(
            candles
        )

        lows = [
            swing
            for swing in swings
            if swing.side == "LOW"
        ]

        if as_of_timestamp is not None:
            lows = [
                swing
                for swing in lows
                if (
                    swing.confirmed_timestamp
                    <= int(as_of_timestamp)
                )
            ]

        if not lows:
            return None

        return max(
            lows,
            key=lambda swing:
                swing.confirmed_timestamp,
        )