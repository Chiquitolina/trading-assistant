import pytest
from config.strategies.v1 import LONG
from services.risk_manager import RiskManager
from signals.strategy.risk import compute_levels
from engine.live.strategy.trade_plan import TradePlan
from engine.live.execution.leg_protection import LegProtectionManager
from models.entry_leg import EntryLeg, EntryLegIdentity
from models.execution_variant import ExecutionVariant
from tests.execution.test_leg_protection import Exchange


def test_new_bucket_atr_levels_and_real_fill_translation_are_frozen():
    sl, tp, sl_pct, tp_pct = compute_levels("LONG", 100, 2, LONG)
    assert sl == pytest.approx(95.8)
    assert tp == pytest.approx(102.4)
    plan = TradePlan("BTCUSDT", 1, "LONG", 100, sl, tp, sl_pct, tp_pct,
                     2, 2, 1000, "compression_strategy", 100, 900)
    translated_tp, translated_sl = RiskManager().calculate_tp_sl(
        plan, real_entry=100.3, mark_price=100.4,
    )
    assert translated_tp == pytest.approx(102.7)
    assert translated_sl == pytest.approx(96.1)
    identity = EntryLegIdentity(
        "v2-leg", "aggregate", "BTCUSDT", "setup", ExecutionVariant.BUCKET_V2,
        "compression_breakout", "bucket_v2_compression_breakout_armed",
        "bucket_v2_retrace_band_triggered", 900, 1100,
    )
    leg = EntryLeg(identity, 100, 100.3, 1, 1, .5, tp, sl)
    protection = LegProtectionManager(Exchange(), RiskManager()).place(leg, plan)
    assert protection.tp_price == pytest.approx(102.7)
    assert protection.sl_price == pytest.approx(96.1)
