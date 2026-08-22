from engine.live.state.snapshot_manager import SnapshotManager
from models.position import Position
from tests.models.test_multileg_models import leg, position
from models.entry_leg import EntryLegIdentity
from models.execution_variant import ExecutionVariant


def test_position_snapshot_round_trip_preserves_virtual_legs(tmp_path):
    manager = SnapshotManager(tmp_path); original = position(); original.add_entry_leg(leg())
    manager.save_position(original)
    raw = manager.load("BTCUSDT")
    restored = Position.from_dict(raw["position"])
    assert restored.aggregate_position_id == original.aggregate_position_id
    assert restored.entry_legs[0].deduplication_key == original.entry_legs[0].deduplication_key


def test_pending_snapshot_has_dedicated_atomic_file(tmp_path):
    manager = SnapshotManager(tmp_path); manager.save_bucket_pending({"active": []})
    assert manager.load_bucket_pending() == {"active": []}


def test_combined_snapshot_preserves_both_variants(tmp_path):
    original = position(); original.add_entry_leg(leg())
    second = leg(identity=EntryLegIdentity(
        "leg-2", "agg-1", "BTCUSDT", "setup-2", ExecutionVariant.BUCKET_V2,
        "compression_breakout", "bucket_v2_compression_breakout_armed",
        "bucket_v2_retrace_band_triggered", 1000, 1200,
    ))
    original.add_entry_leg(second)
    manager = SnapshotManager(tmp_path); manager.save_position(original)
    restored = Position.from_dict(manager.load("BTCUSDT")["position"])
    assert [x.variant for x in restored.entry_legs] == [
        ExecutionVariant.BUCKET_V1, ExecutionVariant.BUCKET_V2,
    ]
    assert restored.execution_variant is ExecutionVariant.BUCKET_V1_V2
