from dataclasses import FrozenInstanceError
import pytest
from models.entry_leg import EntryLeg, EntryLegIdentity, EntryLegIdentityMutationError
from models.execution_variant import ExecutionVariant
from models.position import Position


def identity(variant="BUCKET_V1", aggregate="agg-1", symbol="BTCUSDT", setup="setup-1", leg="leg-1"):
    return EntryLegIdentity(leg, aggregate, symbol, setup, variant, "signal", None, "execution", 1000, 1100)


def leg(**kwargs):
    values = dict(identity=identity(), requested_entry=100, real_entry=100.1,
                  initial_quantity=1, remaining_quantity=1, size_fraction=.5,
                  tp=101, sl=98, signal_context={})
    values.update(kwargs)
    return EntryLeg(**values)


def position(aggregate="agg-1"):
    return Position("BTCUSDT", "LONG", 1, 100, 100.1, 101, 98, 1100, 100, 1000,
                    aggregate_position_id=aggregate)


def test_identity_cannot_be_replaced_or_mutated():
    item = leg()
    with pytest.raises(EntryLegIdentityMutationError): item.identity = identity(leg="other")
    with pytest.raises(FrozenInstanceError): item.identity.variant = ExecutionVariant.BUCKET_V2
    for name, value in (("variant", ExecutionVariant.BUCKET_V2), ("setup_id", "x"),
                        ("leg_id", "x"), ("aggregate_position_id", "x")):
        with pytest.raises(AttributeError): setattr(item, name, value)
    item.remaining_quantity = .5
    item.status = "PARTIALLY_FILLED"


@pytest.mark.parametrize("variant", ["BUCKET_V1", "BUCKET_V2", "LEGACY_UNKNOWN"])
def test_allowed_leg_variants(variant): assert identity(variant=variant).variant.value == variant


@pytest.mark.parametrize("variant", [None, "BUCKET_V1_V2", "UNKNOWN"])
def test_invalid_leg_variants(variant):
    with pytest.raises(ValueError): identity(variant=variant)


@pytest.mark.parametrize("field", ["leg_id", "aggregate_position_id", "symbol", "setup_id", "signal_reason", "execution_reason"])
def test_identity_rejects_empty_strings(field):
    values = identity().__dict__.copy(); values[field] = " "
    with pytest.raises(ValueError): EntryLegIdentity(**values)


@pytest.mark.parametrize("field", ["signal_ts", "entry_ts"])
@pytest.mark.parametrize("value", [0, -1, 1.5, "1000", None, True])
def test_identity_rejects_invalid_timestamps(field, value):
    values = identity().__dict__.copy(); values[field] = value
    with pytest.raises(ValueError): EntryLegIdentity(**values)


@pytest.mark.parametrize("changes", [
    {"initial_quantity": 0}, {"remaining_quantity": -1}, {"closed_quantity": -1},
    {"remaining_quantity": .8, "closed_quantity": .3}, {"size_fraction": 0},
    {"size_fraction": 1.1}, {"tp": 0}, {"sl": 0},
])
def test_invalid_operational_state(changes):
    with pytest.raises(ValueError): leg(**changes)


def test_round_trip_with_empty_context_and_order_ids():
    item = leg(tp_order_id=11, sl_order_id=12, ever_combined=True)
    restored = EntryLeg.from_dict(item.to_dict())
    assert restored.deduplication_key == ("BTCUSDT", ExecutionVariant.BUCKET_V1, "setup-1")
    assert restored.signal_context == {} and restored.tp_order_id == 11 and restored.sl_order_id == 12


def test_context_is_deeply_independent():
    source = {"nested": {"value": 1}}
    item = leg(signal_context=source); restored = EntryLeg.from_dict(item.to_dict())
    item.signal_context["nested"]["value"] = 2
    assert source["nested"]["value"] == restored.signal_context["nested"]["value"] == 1


def test_single_then_overlapping_second_leg_sets_historical_flags():
    pos = position(); first = leg(); pos.add_entry_leg(first)
    assert not pos.ever_combined and not pos.overlapped_with_other_leg
    second = leg(identity=identity(variant="BUCKET_V2", setup="setup-2", leg="leg-2"))
    pos.add_entry_leg(second)
    assert pos.execution_variant is ExecutionVariant.BUCKET_V1_V2
    assert pos.position_increased and pos.ever_combined and pos.overlapped_with_other_leg
    assert all(x.ever_combined and x.overlapped_with_other_leg for x in pos.entry_legs)
    first.remaining_quantity = 0; first.closed_quantity = 1; pos.refresh_aggregate_identity()
    assert pos.overlapped_with_other_leg


def test_flat_then_new_entry_requires_new_aggregate_position():
    old = position(); first = leg(); old.add_entry_leg(first)
    first.remaining_quantity = 0; first.closed_quantity = 1
    with pytest.raises(ValueError): old.add_entry_leg(leg(identity=identity(variant="BUCKET_V2", setup="setup-1", leg="leg-2")))
    new = position("agg-2")
    second = leg(identity=identity(variant="BUCKET_V2", aggregate="agg-2", setup="setup-1", leg="leg-2"))
    new.add_entry_leg(second)
    assert old.aggregate_position_id != new.aggregate_position_id and first.setup_id == second.setup_id
    assert not new.position_increased and not new.ever_combined and not new.overlapped_with_other_leg


@pytest.mark.parametrize("kind", ["symbol", "aggregate", "leg_id", "dedup"])
def test_position_rejects_inconsistent_leg_membership(kind):
    pos = position(); pos.add_entry_leg(leg())
    ident = identity(variant="BUCKET_V2", setup="setup-2", leg="leg-2")
    if kind == "symbol": ident = identity(variant="BUCKET_V2", symbol="ETHUSDT", setup="setup-2", leg="leg-2")
    elif kind == "aggregate": ident = identity(variant="BUCKET_V2", aggregate="agg-2", setup="setup-2", leg="leg-2")
    elif kind == "leg_id": ident = identity(variant="BUCKET_V2", setup="setup-2", leg="leg-1")
    elif kind == "dedup": ident = identity(variant="BUCKET_V1", setup="setup-1", leg="leg-2")
    with pytest.raises(ValueError): pos.add_entry_leg(leg(identity=ident))
