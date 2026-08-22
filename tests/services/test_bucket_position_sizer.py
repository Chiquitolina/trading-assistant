import pytest
from services.position_sizer import PositionSizer


@pytest.fixture
def sizer():
    return PositionSizer(total_usage_pct=.30, max_positions=2, buffer=.90, min_notional=105)


def test_reference_account_bucket_slot_and_leg(sizer):
    result = sizer.calculate(1000, 100, 10, size_fraction=.5, fixed_max_positions=2)
    assert result["usable_balance"] == pytest.approx(270)
    assert result["slot_margin"] == pytest.approx(135)
    assert result["leg_margin"] == pytest.approx(67.5)
    assert result["notional"] == pytest.approx(675)
    assert result["required_margin"] == pytest.approx(67.5)


def test_second_leg_uses_same_slot_fraction_without_double_division(sizer):
    first = sizer.calculate(1000, 100, 10, 0, .5, 2)
    second = sizer.calculate(1000, 100, 10, 1, .5, 2)
    assert second["notional"] == first["notional"] == pytest.approx(675)


def test_third_unique_symbol_is_blocked(sizer):
    result = sizer.calculate(1000, 100, 10, 2, .5, 2)
    assert result["reason"] == "max_positions_reached"


def test_legacy_call_keeps_full_slot_semantics():
    sizer = PositionSizer(total_usage_pct=.65, max_positions=10, buffer=.90)
    result = sizer.calculate(1000, 100, 5, 0)
    assert result["size_fraction"] == 1
