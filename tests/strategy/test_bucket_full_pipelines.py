from types import SimpleNamespace

import pandas as pd

import engine.live.strategy.modes.bucket_v2_compression_strategy as v2_module
import engine.live.strategy.modes.compression_strategy as v1_module
from engine.live.execution.pending_entries.bucket_touch_entry_manager import BucketTouchEntryManager
from engine.live.strategy.modes.bucket_pipeline_coordinator import BucketPipelineCoordinator
from engine.live.strategy.trade_plan import TradePlan
from enums.actions import Action
from models.execution_variant import ExecutionVariant


class Value:
    def __init__(self, value):
        self.value = value


class CandleBuffer:
    def __init__(self):
        self.last = {"timestamp": 1000, "open": 99, "high": 99.1,
                     "low": 98.9, "close": 99, "volume": 10}

    def set(self, timestamp, close, low=None):
        self.last = {
            "timestamp": timestamp, "open": close, "high": close + .1,
            "low": close - .1 if low is None else low,
            "close": close, "volume": 10,
        }

    def get_candles(self, symbol, tf):
        prefix = [{"timestamp": index, "open": 99, "high": 99.1,
                   "low": 98.9, "close": 99, "volume": 10}
                  for index in range(1, 80)]
        return prefix + [dict(self.last)]


def _signal():
    return SimpleNamespace(
        symbol="BTCUSDT", signal_ts=900, signal_price=99,
        trend=Value("UP"), direction=Value("LONG"), momentum=Value("UP"),
    )


def _atr(frame, *args, **kwargs):
    result = frame.copy()
    result["atr"] = 1.0
    return result


def _compression(*args, **kwargs):
    return {
        "is_compression": True, "compression_high": 100,
        "compression_low": 98, "score": 3, "reasons": ["golden"],
    }


def _trend(*args, **kwargs):
    return {"trend_up": True, "score": 4, "reasons": ["golden"]}


def _install_golden_detectors(monkeypatch, buffer):
    def breakout(*args, **kwargs):
        active = buffer.last["timestamp"] == 3000
        return {"breakout": active, "volume_ratio": 2,
                "reason": "golden_breakout" if active else "none",
                "failed_reasons": []}

    for module in (v1_module, v2_module):
        monkeypatch.setattr(module, "add_atr", _atr)
        monkeypatch.setattr(module, "detect_trend_up", _trend)
        monkeypatch.setattr(module, "detect_compression", _compression)
        monkeypatch.setattr(module, "detect_compression_breakout", breakout)
        monkeypatch.setattr(module, "detect_breakout_from_watch", breakout)


def _plan(result, variant):
    context = dict(result.signal_context)
    setup = f"BTCUSDT:{context['compression_created_ts']}:{context['breakout_ts']}"
    reason = ("moderate_breakout_retrace_entry_ready"
              if variant is ExecutionVariant.BUCKET_V1 else "compression_breakout")
    return TradePlan(
        "BTCUSDT", 0, "LONG", 100.6, 98, 101, 2, 1, 1, 1,
        4000, "compression_strategy", 99, 900, context,
        execution_variant=variant, setup_id=setup, signal_reason=reason,
        arm_reason=("bucket_v2_compression_breakout_armed"
                    if variant is ExecutionVariant.BUCKET_V2 else None),
        execution_reason=("bucket_v2_retrace_band_triggered"
                          if variant is ExecutionVariant.BUCKET_V2
                          else "bucket_v1_moderate_breakout_retrace_entry_ready"),
        size_fraction=.5,
    )


def test_deterministic_candles_reproduce_both_original_pipeline_events(monkeypatch):
    buffer = CandleBuffer()
    _install_golden_detectors(monkeypatch, buffer)
    pipelines = BucketPipelineCoordinator(
        buffer, max_watch_candles=8, max_pullback_candles=5,
        moderate_breakout_min_pct=.5, moderate_breakout_max_pct=.75,
        entry_vs_breakout_min_pct=-.25, entry_vs_breakout_max_pct=0,
    )
    signal = _signal()

    # Golden steps common to bucket and new-bucket: create, then watch.
    first = pipelines.evaluate(symbol="BTCUSDT", signal=signal)
    assert [item.result.trade_action.action for item in first] == [Action.HOLD, Action.HOLD]
    buffer.set(2000, 99)
    second = pipelines.evaluate(symbol="BTCUSDT", signal=signal)
    assert [item.result.trade_action.reason for item in second] == [
        "watch_created_now_watching", "watch_created_now_watching",
    ]

    # On the breakout candle new-bucket reaches ENTRY_READY; bucket must wait.
    buffer.set(3000, 100.6, 100.0)
    breakout = pipelines.evaluate(symbol="BTCUSDT", signal=signal)
    v1, v2 = (item.result for item in breakout)
    assert v1.trade_action.action is Action.HOLD
    assert v1.trade_action.reason == "moderate_breakout_waiting_retrace"
    assert v2.trade_action.action is Action.LONG
    assert v2.trade_action.reason == "compression_breakout"
    assert v2.signal_context["compression_reason"] == "pullback_hold_and_continuation"
    assert v2.signal_context["breakout_ts"] == 3000

    v2_plan = _plan(v2, ExecutionVariant.BUCKET_V2)
    pending = BucketTouchEntryManager()
    assert pending.arm(v2_plan, v2.trade_action, 3000)
    armed = pending.get("BTCUSDT")
    assert armed.setup_id == "BTCUSDT:1000:3000"
    assert armed.armed_ts == 3_000_000
    assert pending.evaluate_price("BTCUSDT", 100.5, 3001).status == "TRIGGERED"

    # The following retrace is the original bucket ENTRY_READY event. V2 has
    # already consumed and removed its own watch, so its new watch stays HOLD.
    buffer.set(4000, 100.5, 100.2)
    retrace = pipelines.evaluate(symbol="BTCUSDT", signal=signal)
    v1, v2 = (item.result for item in retrace)
    assert v1.trade_action.action is Action.LONG
    assert v1.trade_action.reason == "moderate_breakout_retrace_entry_ready"
    assert v1.signal_context["entry_ready_ts"] == 4000
    assert v2.trade_action.action is Action.HOLD
    assert _plan(v1, ExecutionVariant.BUCKET_V1).setup_id == "BTCUSDT:1000:3000"


def test_coordinator_uses_distinct_machines_watches_and_contexts(monkeypatch):
    buffer = CandleBuffer()
    _install_golden_detectors(monkeypatch, buffer)
    pipelines = BucketPipelineCoordinator(buffer)
    pipelines.evaluate(symbol="BTCUSDT", signal=_signal())
    assert pipelines.v1.machine is not pipelines.v2.machine
    assert pipelines.v1.machine.watches is not pipelines.v2.machine.watches
    v1_watch = pipelines.v1.machine.get("BTCUSDT")
    v2_watch = pipelines.v2.machine.get("BTCUSDT")
    assert v1_watch is not v2_watch
    v1_watch.reason = "mutated-v1-only"
    assert v2_watch.reason == "trend_up_and_compression"

