from types import SimpleNamespace

from engine.live.strategy.modes.bucket_v2_compression_strategy import BucketV2CompressionStrategy


class Value:
    def __init__(self, value): self.value = value


def test_v2_golden_context_matches_new_bucket_builder_without_v1_fields():
    signal = SimpleNamespace(trend=Value("up"), direction=Value("up"), momentum=Value("inside_bar"))
    state = {
        "state": "ENTRY_READY", "reason": "pullback_hold_and_continuation",
        "created_ts": 1000, "updated_ts": 3000, "candles_waiting": 0,
        "compression_high": 100, "compression_low": 98,
        "breakout_ts": 3000, "breakout_price": 100.6,
        "breakout_high": 100.7, "breakout_volume_ratio": 2,
        "breakout_extension_pct": .6, "breakout_extension_atr": .6,
        "entry_price": 100.6,
    }
    context = BucketV2CompressionStrategy._build_signal_context(
        signal, state, {"trend_up": True, "score": 4},
        {"is_compression": True, "score": 3}, {"breakout": True}, None,
    )
    assert context["compression_reason"] == "pullback_hold_and_continuation"
    assert context["compression_created_ts"] == 1000
    assert context["breakout_detected"] is True
    assert context["breakout_ts"] == 3000
    assert context["entry_ready_price"] == 100.6
    for forbidden in ("moderate_breakout_candidate", "entry_profile",
                      "entry_vs_breakout_pct", "entry_condition_matched"):
        assert forbidden not in context
