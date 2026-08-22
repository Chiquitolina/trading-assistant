from types import SimpleNamespace
from engine.live.execution.pending_entries.bucket_touch_entry_manager import BucketTouchEntryManager


def plan(extension=.6):
    return SimpleNamespace(symbol="BTCUSDT", setup_id="setup-1", signal_context={
        "breakout_price": 100, "breakout_extension_pct": extension,
        "compression_high": 99.5,
    })


def test_bucket_bounds_are_left_open_right_closed():
    assert not BucketTouchEntryManager().arm(plan(.5), object(), 1_700_000_000_000)
    assert BucketTouchEntryManager().arm(plan(.75), object(), 1_700_000_000_000)


def test_pending_touch_and_original_expiry_survive_restore():
    manager = BucketTouchEntryManager(); assert manager.arm(plan(), "action", 1_700_000_000_000)
    original = manager.get("BTCUSDT")
    saved = manager.snapshot()["active"]
    restored = BucketTouchEntryManager(); restored.restore(saved)
    item = restored.get("BTCUSDT")
    assert item.armed_ts == original.armed_ts and item.expires_ts == original.expires_ts
    assert restored.evaluate_price("BTCUSDT", 99.8, item.armed_ts + 1).status == "TRIGGERED"


def test_expiry_and_invalidation_are_terminal_and_auditable():
    manager = BucketTouchEntryManager(); manager.arm(plan(), None, 1_700_000_000_000)
    expiry = manager.get("BTCUSDT").expires_ts
    assert manager.evaluate_price("BTCUSDT", 101, expiry) is None
    assert manager.history[-1].status == "EXPIRED"
    manager.arm(plan(), None, 1_700_010_000_000)
    manager.evaluate_price("BTCUSDT", 99.4, 1_700_010_000_001)
    assert manager.history[-1].status == "INVALIDATED"
    saved = manager.snapshot()
    restored = BucketTouchEntryManager()
    restored.restore(saved["active"], history_items=saved["history"])
    assert [item.status for item in restored.history] == ["EXPIRED", "INVALIDATED"]


def test_armed_shutdown_restore_preserves_original_identity_and_expiry():
    persisted = []
    manager = BucketTouchEntryManager(on_change=lambda data: persisted.append(data))
    assert manager.arm(plan(), "action", 1_700_000_000_000)
    original = manager.get("BTCUSDT")

    manager.shutdown()

    assert original.status == "ARMED"
    saved = persisted[-1]
    restored = BucketTouchEntryManager()
    restored.restore(saved["active"])
    item = restored.get("BTCUSDT")

    assert item.status == "ARMED"
    assert item.setup_id == original.setup_id
    assert item.armed_ts == original.armed_ts
    assert item.expires_ts == original.expires_ts
