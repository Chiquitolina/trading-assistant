from types import SimpleNamespace

from engine.live.research.volume_exhaustion_outcome_tracker import (
    VolumeExhaustionOutcomeTracker,
)


MINUTE_MS = 60_000


class FakeBuffer:
    def __init__(self):
        self.data = {}

    def set_candles(self, symbol, timeframe, candles):
        self.data[(symbol.upper(), timeframe)] = candles

    def get_candles(self, symbol, timeframe):
        return self.data.get(
            (symbol.upper(), timeframe),
            [],
        )


class FakeSwingDetector:
    def __init__(self, points=None):
        self.points = list(points or [])

    def detect_all(self, candles):
        return list(self.points)


def make_event(
    event_id,
    side,
    event_open_timestamp=0,
    event_close_timestamp=MINUTE_MS,
    close=100.0,
):
    # _potential_side derives LONG/SHORT from move_3m_pct.
    move_3m_pct = -1.0 if side == "LONG" else 1.0

    return {
        "event_id": event_id,
        "symbol": "TESTUSDT",
        "timeframe": "1m",
        "candle_open_timestamp": event_open_timestamp,
        "candle_close_timestamp": event_close_timestamp,
        "candle_open_time_utc": None,
        "candle_close_time_utc": None,
        "close": close,
        "move_3m_pct": move_3m_pct,
    }


def make_candle(price=100.0):
    return {
        "open": price,
        "high": price + 0.2,
        "low": price - 0.2,
        "close": price,
    }


def point(
    side,
    pivot_minute,
    confirmed_minute,
    price,
):
    return SimpleNamespace(
        side=side,
        pivot_timestamp=pivot_minute * MINUTE_MS,
        confirmed_timestamp=confirmed_minute * MINUTE_MS,
        price=price,
    )


def make_tracker(tmp_path):
    buffer = FakeBuffer()

    for timeframe in ("5m", "15m", "30m"):
        buffer.set_candles(
            "TESTUSDT",
            timeframe,
            [{"timestamp": 0}],
        )

    tracker = VolumeExhaustionOutcomeTracker(
        buffer=buffer,
        outcomes_path=str(tmp_path),
    )

    # Tests control detector outputs explicitly.
    tracker.swing_detectors = {
        "5m": FakeSwingDetector(),
        "15m": FakeSwingDetector(),
        "30m": FakeSwingDetector(),
    }

    return tracker


def test_not_labeled_before_confirmation(tmp_path):
    tracker = make_tracker(tmp_path / "outcomes.csv")

    event = make_event("long-anti-lookahead", "LONG")
    assert tracker.register(event)

    state = tracker.active_events[event["event_id"]]

    tracker.swing_detectors["15m"] = FakeSwingDetector([
        point(
            side="LOW",
            pivot_minute=10,
            confirmed_minute=46,
            price=98.0,
        )
    ])

    tracker._update_swing_research(
        state=state,
        close_time=45 * MINUTE_MS,
    )

    result = state["swing_research"]["15m"]

    assert result["became_swing"] is False

    tracker._update_swing_research(
        state=state,
        close_time=46 * MINUTE_MS,
    )

    result = state["swing_research"]["15m"]

    assert result["became_swing"] is True
    assert result["pivot_timestamp"] == 10 * MINUTE_MS
    assert result["confirmed_timestamp"] == 46 * MINUTE_MS
    assert result["confirmed_after_min"] == 45.0


def test_long_only_labels_low(tmp_path):
    tracker = make_tracker(tmp_path / "outcomes.csv")

    event = make_event("long-direction", "LONG")
    assert tracker.register(event)

    state = tracker.active_events[event["event_id"]]

    tracker.swing_detectors["5m"] = FakeSwingDetector([
        point("HIGH", 5, 10, 103.0),
        point("LOW", 6, 11, 97.0),
    ])

    tracker._update_swing_research(
        state=state,
        close_time=11 * MINUTE_MS,
    )

    result = state["swing_research"]["5m"]

    assert result["became_swing"] is True
    assert result["price"] == 97.0
    assert result["pivot_timestamp"] == 6 * MINUTE_MS


def test_short_only_labels_high(tmp_path):
    tracker = make_tracker(tmp_path / "outcomes.csv")

    event = make_event("short-direction", "SHORT")
    assert tracker.register(event)

    state = tracker.active_events[event["event_id"]]

    tracker.swing_detectors["5m"] = FakeSwingDetector([
        point("LOW", 5, 10, 97.0),
        point("HIGH", 6, 11, 103.0),
    ])

    tracker._update_swing_research(
        state=state,
        close_time=11 * MINUTE_MS,
    )

    result = state["swing_research"]["5m"]

    assert result["became_swing"] is True
    assert result["price"] == 103.0
    assert result["pivot_timestamp"] == 6 * MINUTE_MS


def test_pre_event_pivot_is_rejected(tmp_path):
    tracker = make_tracker(tmp_path / "outcomes.csv")

    # Event starts at minute 20 and closes at minute 21.
    event = make_event(
        "pre-event-rejected",
        "LONG",
        event_open_timestamp=20 * MINUTE_MS,
        event_close_timestamp=21 * MINUTE_MS,
    )
    assert tracker.register(event)

    state = tracker.active_events[event["event_id"]]

    tracker.swing_detectors["15m"] = FakeSwingDetector([
        # Confirmed later, but pivot itself predates T0.
        point("LOW", 15, 30, 98.0),
    ])

    tracker._update_swing_research(
        state=state,
        close_time=30 * MINUTE_MS,
    )

    assert (
        state["swing_research"]["15m"]["became_swing"]
        is False
    )


def test_event_remains_active_after_30m(tmp_path):
    tracker = make_tracker(tmp_path / "outcomes.csv")

    event = make_event("still-active-30m", "LONG")
    assert tracker.register(event)

    for minute in range(2, 32):
        tracker.on_candle(
            symbol="TESTUSDT",
            close_time=minute * MINUTE_MS,
            candle=make_candle(),
        )

    assert event["event_id"] in tracker.active_events

    state = tracker.active_events[event["event_id"]]

    assert state["age_candles"] == 30
    assert 30 in state["returns"]
    assert 30 in state["mfe"]
    assert 30 in state["mae"]


def test_event_completes_at_180m(tmp_path):
    outcomes_path = tmp_path / "outcomes.csv"
    tracker = make_tracker(outcomes_path)

    event = make_event("complete-180m", "LONG")
    assert tracker.register(event)

    completed = []

    # T+1 starts with the candle closing at minute 2,
    # because the event candle closes at minute 1.
    for minute in range(2, 182):
        completed.extend(
            tracker.on_candle(
                symbol="TESTUSDT",
                close_time=minute * MINUTE_MS,
                candle=make_candle(),
            )
        )

    assert event["event_id"] not in tracker.active_events
    assert tracker.completed_events == 1
    assert len(completed) == 1
    assert completed[0]["event_id"] == event["event_id"]
    assert outcomes_path.exists()


def run_without_pytest():
    """
    Lightweight runner for:
        python3 -m engine.live.research.test_volume_exhaustion_future_swings

    pytest can still discover and execute the test_* functions normally.
    """
    import tempfile

    tests = [
        (
            "future swing not labeled before confirmation",
            test_not_labeled_before_confirmation,
        ),
        (
            "LONG labels future swing LOW",
            test_long_only_labels_low,
        ),
        (
            "SHORT labels future swing HIGH",
            test_short_only_labels_high,
        ),
        (
            "pre-event pivot rejected",
            test_pre_event_pivot_is_rejected,
        ),
        (
            "event remains active after 30m",
            test_event_remains_active_after_30m,
        ),
        (
            "event completes at 180m",
            test_event_completes_at_180m,
        ),
    ]

    with tempfile.TemporaryDirectory() as temp_dir:
        base = Path(temp_dir)

        for index, (name, test_func) in enumerate(tests):
            case_dir = base / f"case_{index}"
            case_dir.mkdir(parents=True, exist_ok=True)

            test_func(case_dir)
            print(f"PASS: {name}")

    print()
    print("=" * 48)
    print("ALL FUTURE SWING TESTS PASS")
    print("=" * 48)


if __name__ == "__main__":
    run_without_pytest()
