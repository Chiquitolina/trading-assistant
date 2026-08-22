from signals.indicators.compression_state_machine import (
    CompressionStateMachine,
)
from signals.indicators.bucket_v2_compression_state_machine import BucketV2CompressionStateMachine


def compression():
    return {"is_compression": True, "compression_high": 100, "compression_low": 98, "score": 1}


def candle(ts, close, low=None):
    return {"timestamp": ts, "close": close, "high": close + .1, "low": low if low is not None else close - .1}


def seed(machine):
    trend = {"trend_up": True, "score": 1}; comp = compression()
    machine.update("BTCUSDT", candle(1000, 99), trend, comp, {"breakout": False})
    machine.update("BTCUSDT", candle(2000, 99), trend, comp, {"breakout": False})
    return trend, comp


def test_v1_and_v2_own_independent_watch_objects():
    v1, v2 = CompressionStateMachine(), BucketV2CompressionStateMachine()
    trend, comp = seed(v1); seed(v2)
    assert v1.watches is not v2.watches
    v1.watches["BTCUSDT"].reason = "changed"
    assert v2.watches["BTCUSDT"].reason != "changed"
    assert not hasattr(v2.watches["BTCUSDT"], "moderate_breakout_candidate")
    assert not hasattr(v2.watches["BTCUSDT"], "entry_profile")


def test_v1_does_not_evaluate_breakout_candle_but_v2_does():
    v1, v2 = CompressionStateMachine(), BucketV2CompressionStateMachine()
    trend, comp = seed(v1); seed(v2)
    breakout = {"breakout": True, "volume_ratio": 2}
    # 100.6 is a moderate breakout; low remains within V2's 1.2% rule.
    one = v1.update("BTCUSDT", candle(3000, 100.6, 100.0), trend, comp, breakout, 1)
    two = v2.update("BTCUSDT", candle(3000, 100.6, 100.0), trend, comp, breakout, 1)
    assert one["state"] == "WAIT_PULLBACK"
    assert two["state"] == "ENTRY_READY"
    assert two["reason"] == "pullback_hold_and_continuation"


def test_v1_keeps_inclusive_lower_bound_and_v2_filter_is_not_in_state_machine():
    v1, v2 = CompressionStateMachine(), BucketV2CompressionStateMachine()
    trend, comp = seed(v1); seed(v2)
    breakout = {"breakout": True}
    assert v1.update("BTCUSDT", candle(3000, 100.5, 100), trend, comp, breakout, 1)["state"] == "WAIT_PULLBACK"
    assert v2.update("BTCUSDT", candle(3000, 100.5, 100), trend, comp, breakout, 1)["state"] == "ENTRY_READY"


def test_v2_golden_expiration_reason_and_timestamps_match_new_bucket():
    v2 = BucketV2CompressionStateMachine(max_pullback_candles=1)
    trend, comp = seed(v2)
    # Invalid on the breakout candle because close loses compression_high.
    result = v2.update("BTCUSDT", candle(3000, 99.9, 99.8), trend, comp,
                       {"breakout": True, "volume_ratio": 2}, 1)
    assert result["state"] == "WAIT_PULLBACK"
    assert result["reason"] == "waiting_valid_pullback"
    assert result["breakout_ts"] == 3000
    assert result["updated_ts"] == 3000
    v2.update("BTCUSDT", candle(4000, 99.9, 99.8), trend, comp,
              {"breakout": False}, 1)
    expired = v2.update("BTCUSDT", candle(5000, 99.9, 99.8), trend, comp,
                        {"breakout": False}, 1)
    assert expired["state"] == "EXPIRED"
    assert expired["reason"] == "pullback_expired"
    assert expired["candles_waiting"] == 2
    assert expired["updated_ts"] == 5000
