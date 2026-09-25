from engine.live.research.swing_detector import SwingDetector


def candle(
    minute,
    high,
    low,
):
    """
    Candle mínima para test.

    Usamos timestamps de 1 minuto para poder
    comprobar pivot_timestamp y confirmed_timestamp.
    """
    return {
        "timestamp": minute * 60_000,
        "high": float(high),
        "low": float(low),
    }


def test_clear_swing_high():
    candles = [
        candle(0, 100, 90),
        candle(1, 102, 91),
        candle(2, 104, 92),

        # Pivot HIGH
        candle(3, 110, 93),

        candle(4, 106, 92),
        candle(5, 105, 91),
        candle(6, 103, 90),
    ]

    detector = SwingDetector(
        left_bars=3,
        right_bars=3,
    )

    swings = detector.detect_all(candles)

    highs = [
        swing
        for swing in swings
        if swing.side == "HIGH"
    ]

    assert len(highs) == 1

    swing = highs[0]

    assert swing.price == 110.0

    # Pivot ocurrió en minuto 3.
    assert swing.pivot_timestamp == 3 * 60_000

    # Con right=3 recién se confirma
    # cuando cierra/llega la vela minuto 6.
    assert swing.confirmed_timestamp == 6 * 60_000

    print("PASS: clear swing high")


def test_clear_swing_low():
    candles = [
        candle(0, 110, 100),
        candle(1, 109, 98),
        candle(2, 108, 96),

        # Pivot LOW
        candle(3, 107, 90),

        candle(4, 108, 94),
        candle(5, 109, 96),
        candle(6, 110, 98),
    ]

    detector = SwingDetector(
        left_bars=3,
        right_bars=3,
    )

    swings = detector.detect_all(candles)

    lows = [
        swing
        for swing in swings
        if swing.side == "LOW"
    ]

    assert len(lows) == 1

    swing = lows[0]

    assert swing.price == 90.0
    assert swing.pivot_timestamp == 3 * 60_000
    assert swing.confirmed_timestamp == 6 * 60_000

    print("PASS: clear swing low")


def test_not_confirmed_without_right_bars():
    """
    Tenemos un posible máximo, pero todavía
    no existen las 3 velas posteriores.

    NO debe existir swing confirmado.
    """
    candles = [
        candle(0, 100, 90),
        candle(1, 102, 91),
        candle(2, 104, 92),
        candle(3, 110, 93),

        # Sólo dos velas posteriores.
        candle(4, 106, 92),
        candle(5, 105, 91),
    ]

    detector = SwingDetector(
        left_bars=3,
        right_bars=3,
    )

    swings = detector.detect_all(candles)

    assert len(swings) == 0

    print("PASS: unconfirmed pivot rejected")


def test_future_higher_high_invalidates_pivot():
    """
    El supuesto 110 parece máximo inicialmente,
    pero dentro de su ventana derecha aparece 112.

    Por lo tanto 110 NO es swing high.
    """
    candles = [
        candle(0, 100, 90),
        candle(1, 102, 91),
        candle(2, 104, 92),

        # Falso candidato
        candle(3, 110, 93),

        candle(4, 106, 92),

        # Lo invalida
        candle(5, 112, 94),

        candle(6, 105, 91),
    ]

    detector = SwingDetector(
        left_bars=3,
        right_bars=3,
    )

    swings = detector.detect_all(candles)

    high_at_110 = [
        swing
        for swing in swings
        if (
            swing.side == "HIGH"
            and swing.price == 110.0
        )
    ]

    assert len(high_at_110) == 0

    print("PASS: future higher high invalidates pivot")


def test_as_of_timestamp_prevents_lookahead():
    candles = [
        candle(0, 100, 90),
        candle(1, 102, 91),
        candle(2, 104, 92),

        # Swing candidate
        candle(3, 110, 93),

        candle(4, 106, 92),
        candle(5, 105, 91),
        candle(6, 103, 90),
    ]

    detector = SwingDetector(
        left_bars=3,
        right_bars=3,
    )

    # En minuto 5 todavía NO estaba confirmado.
    before_confirmation = (
        detector.last_confirmed_high(
            candles,
            as_of_timestamp=5 * 60_000,
        )
    )

    assert before_confirmation is None

    # En minuto 6 ya puede conocerse.
    after_confirmation = (
        detector.last_confirmed_high(
            candles,
            as_of_timestamp=6 * 60_000,
        )
    )

    assert after_confirmation is not None
    assert after_confirmation.price == 110.0
    assert (
        after_confirmation.pivot_timestamp
        == 3 * 60_000
    )
    assert (
        after_confirmation.confirmed_timestamp
        == 6 * 60_000
    )

    print("PASS: as_of_timestamp prevents lookahead")


def test_prominence_filter():
    """
    El máximo es técnicamente un pivot,
    pero apenas supera a los vecinos.

    Con prominence mínima alta debe rechazarse.
    """
    candles = [
        candle(0, 100.00, 90),
        candle(1, 100.02, 91),
        candle(2, 100.04, 92),

        # Sólo ligeramente superior.
        candle(3, 100.05, 93),

        candle(4, 100.04, 92),
        candle(5, 100.03, 91),
        candle(6, 100.02, 90),
    ]

    detector_without_filter = SwingDetector(
        left_bars=3,
        right_bars=3,
        min_prominence_pct=0.0,
    )

    detector_with_filter = SwingDetector(
        left_bars=3,
        right_bars=3,
        min_prominence_pct=0.10,
    )

    swings_without_filter = (
        detector_without_filter.detect_all(
            candles
        )
    )

    swings_with_filter = (
        detector_with_filter.detect_all(
            candles
        )
    )

    highs_without_filter = [
        swing
        for swing in swings_without_filter
        if swing.side == "HIGH"
    ]

    highs_with_filter = [
        swing
        for swing in swings_with_filter
        if swing.side == "HIGH"
    ]

    assert len(highs_without_filter) == 1
    assert len(highs_with_filter) == 0

    print("PASS: prominence filter")


def run_tests():
    test_clear_swing_high()
    test_clear_swing_low()

    test_not_confirmed_without_right_bars()

    test_future_higher_high_invalidates_pivot()

    test_as_of_timestamp_prevents_lookahead()

    test_prominence_filter()

    print()
    print("==============================")
    print("ALL SWING DETECTOR TESTS PASS")
    print("==============================")


if __name__ == "__main__":
    run_tests() 