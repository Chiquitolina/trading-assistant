from types import SimpleNamespace

from engine.live.research.confirmed_swing_sweep_replay_strategy import (
    ConfirmedSwingSweepReplayStrategy,
    MINUTE_MS,
)


class FakeBuffer:
    def __init__(self):
        self.data = {}

    def set_candles(self, symbol, timeframe, candles):
        self.data[(symbol.upper(), timeframe)] = list(candles)

    def append_candle(self, symbol, timeframe, candle):
        self.data.setdefault((symbol.upper(), timeframe), []).append(dict(candle))

    def get_candles(self, symbol, timeframe):
        return list(self.data.get((symbol.upper(), timeframe), []))


class FakeDetector:
    def __init__(self, points):
        self.points = list(points)

    def detect_all(self, candles):
        return list(self.points)


def point(side="LOW", price=100.0, pivot_ts=0, confirmed_ts=0):
    return SimpleNamespace(
        side=side,
        price=price,
        pivot_timestamp=pivot_ts,
        confirmed_timestamp=confirmed_ts,
        prominence_pct=0.1,
    )


def candle(ts, open_=100.0, high=100.1, low=99.9, close=100.0):
    return {
        "timestamp": int(ts),
        "open": float(open_),
        "high": float(high),
        "low": float(low),
        "close": float(close),
    }


def send(strategy, buffer, ts, **kwargs):
    c = candle(ts, **kwargs)
    buffer.append_candle("TESTUSDT", "1m", c)
    strategy.on_candle("TESTUSDT", ts + MINUTE_MS - 1, c)


def make_strategy(tmp_path, swing_point):
    buffer = FakeBuffer()
    buffer.set_candles("TESTUSDT", "15m", [{
        "timestamp": int(swing_point.confirmed_timestamp),
        "open": 100.0,
        "high": 100.2,
        "low": 99.8,
        "close": 100.1,
    }])
    strategy = ConfirmedSwingSweepReplayStrategy(
        buffer=buffer,
        events_path=tmp_path / "events.csv",
        trades_path=tmp_path / "trades.csv",
        detector=FakeDetector([swing_point]),
    )
    return strategy, buffer


def test_departure_and_sweep_same_candle_does_not_fill(tmp_path):
    p = point(side="LOW", confirmed_ts=0)
    strategy, buffer = make_strategy(tmp_path, p)
    actionable = 15 * MINUTE_MS

    # Same candle both moves >=0.20% away and sweeps below the swing.
    # OHLC cannot order those actions, so it is departure only.
    send(strategy, buffer, actionable, high=100.30, low=99.80, close=100.1)
    assert strategy.stats["departures"] == 1
    assert strategy.stats["fills"] == 0

    # A later first sweep can fill the resting 0.05% limit.
    send(
        strategy,
        buffer,
        actionable + MINUTE_MS,
        high=100.10,
        low=99.94,
        close=100.0,
    )
    assert strategy.stats["fills"] == 1
    assert len(strategy.positions["TESTUSDT"]) == 1


def test_first_sweep_no_fill_consumes_setup(tmp_path):
    p = point(side="LOW", confirmed_ts=0)
    strategy, buffer = make_strategy(tmp_path, p)
    t = 15 * MINUTE_MS

    send(strategy, buffer, t, high=100.30, low=100.05, close=100.2)
    # First sweep only penetrates 0.02%: NO_FILL, setup consumed.
    send(strategy, buffer, t + MINUTE_MS, high=100.1, low=99.98, close=100.0)
    assert strategy.stats["no_fills"] == 1
    assert strategy.stats["fills"] == 0

    # Deeper second sweep is intentionally ignored.
    send(strategy, buffer, t + 2 * MINUTE_MS, high=100.0, low=99.70, close=99.9)
    assert strategy.stats["fills"] == 0


def test_entry_candle_tp_is_not_credited(tmp_path):
    p = point(side="LOW", confirmed_ts=0)
    strategy, buffer = make_strategy(tmp_path, p)
    t = 15 * MINUTE_MS

    send(strategy, buffer, t, high=100.30, low=100.05, close=100.2)
    # Fills 99.95; high also exceeds TP, but same-candle TP is unknowable.
    send(strategy, buffer, t + MINUTE_MS, high=100.90, low=99.94, close=100.2)

    assert strategy.stats["fills"] == 1
    assert strategy.stats["trades"] == 0
    assert len(strategy.positions["TESTUSDT"]) == 1


def test_entry_candle_immediate_sl_is_counted(tmp_path):
    p = point(side="LOW", confirmed_ts=0)
    strategy, buffer = make_strategy(tmp_path, p)
    t = 15 * MINUTE_MS

    send(strategy, buffer, t, high=100.30, low=100.05, close=100.2)
    send(strategy, buffer, t + MINUTE_MS, high=101.0, low=98.80, close=99.4)

    assert strategy.stats["fills"] == 1
    assert strategy.stats["trades"] == 1
    assert strategy.stats["SL"] == 1
    assert strategy.closed_trade_returns == [-1.1]


def test_later_same_candle_tp_sl_is_sl_ambiguous(tmp_path):
    p = point(side="LOW", confirmed_ts=0)
    strategy, buffer = make_strategy(tmp_path, p)
    t = 15 * MINUTE_MS

    send(strategy, buffer, t, high=100.30, low=100.05, close=100.2)
    send(strategy, buffer, t + MINUTE_MS, high=100.2, low=99.94, close=100.0)
    assert strategy.stats["trades"] == 0

    send(strategy, buffer, t + 2 * MINUTE_MS, high=101.0, low=98.8, close=100.0)
    assert strategy.stats["SL_AMBIGUOUS"] == 1
    assert strategy.stats["trades"] == 1


def test_time_exit_after_180_full_candles(tmp_path):
    p = point(side="LOW", confirmed_ts=0)
    strategy, buffer = make_strategy(tmp_path, p)
    t = 15 * MINUTE_MS

    send(strategy, buffer, t, high=100.30, low=100.05, close=100.2)
    send(strategy, buffer, t + MINUTE_MS, high=100.20, low=99.94, close=100.0)

    for i in range(1, 181):
        send(
            strategy,
            buffer,
            t + (1 + i) * MINUTE_MS,
            open_=100.0,
            high=100.20,
            low=99.80,
            close=100.10,
        )

    assert strategy.stats["TIME_EXIT"] == 1
    assert strategy.stats["trades"] == 1


def test_short_is_exact_mirror(tmp_path):
    p = point(side="HIGH", confirmed_ts=0)
    strategy, buffer = make_strategy(tmp_path, p)
    t = 15 * MINUTE_MS

    send(strategy, buffer, t, high=99.95, low=99.70, close=99.8)
    send(strategy, buffer, t + MINUTE_MS, high=100.06, low=99.8, close=100.0)
    assert strategy.stats["fills"] == 1

    send(strategy, buffer, t + 2 * MINUTE_MS, high=100.2, low=99.20, close=99.4)
    assert strategy.stats["TP"] == 1
    assert strategy.closed_trade_returns[0] == 0.65


def test_1m_gap_invalidates_open_trade_without_scoring_it(tmp_path):
    p = point(side="LOW", confirmed_ts=0)
    strategy, buffer = make_strategy(tmp_path, p)
    t = 15 * MINUTE_MS

    send(strategy, buffer, t, high=100.30, low=100.05, close=100.2)
    send(strategy, buffer, t + MINUTE_MS, high=100.2, low=99.94, close=100.0)
    assert strategy.stats["fills"] == 1

    # Skip one full minute.
    send(strategy, buffer, t + 3 * MINUTE_MS, high=100.1, low=99.9, close=100.0)

    assert strategy.stats["trade_gaps"] == 1
    assert strategy.stats["trades"] == 0
    assert "TESTUSDT" not in strategy.positions


def test_real_5x5_detector_becomes_actionable_after_confirming_15m_close(tmp_path):
    buffer = FakeBuffer()

    fifteen = []
    for i in range(11):
        ts = i * 15 * MINUTE_MS
        low = 100.0 if i == 5 else 101.0
        high = 103.0 + (0.01 * i)
        fifteen.append({
            "timestamp": ts,
            "open": 102.0,
            "high": high,
            "low": low,
            "close": 102.0,
        })

    buffer.set_candles("TESTUSDT", "15m", fifteen)
    strategy = ConfirmedSwingSweepReplayStrategy(
        buffer=buffer,
        events_path=tmp_path / "events.csv",
        trades_path=tmp_path / "trades.csv",
    )

    # Pivot is index 5, confirming candle is index 10 at t=150m.
    # Since the timestamp is the 15m candle OPEN, it becomes actionable at
    # t=165m, after that confirming candle has fully closed.
    actionable = 11 * 15 * MINUTE_MS
    send(strategy, buffer, actionable, high=100.30, low=100.05, close=100.2)

    assert strategy.stats["setups"] == 1
    setup = next(iter(strategy.active_setups["TESTUSDT"].values()))
    assert setup["pivot_timestamp"] == 5 * 15 * MINUTE_MS
    assert setup["confirmed_timestamp"] == 10 * 15 * MINUTE_MS
    assert setup["actionable_timestamp"] == actionable
    assert setup["departure_timestamp"] == actionable
