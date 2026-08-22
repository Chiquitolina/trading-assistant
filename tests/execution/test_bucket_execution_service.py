from types import SimpleNamespace
from engine.live.execution.bucket_execution_service import BucketExecutionService
from engine.live.strategy.trade_plan import TradePlan
from models.execution_variant import ExecutionVariant
from services.position_sizer import PositionSizer


class PM:
    def __init__(self, exchange): self.exchange = exchange
    def sync(self, symbol):
        if not self.exchange.qty: return None
        return {"symbol": symbol, "side": self.exchange.side, "quantity": self.exchange.qty, "entry_price": self.exchange.average}


class Exchange:
    def __init__(self): self.qty = 0; self.average = 0; self.side = "LONG"; self.timeout = False; self.timeout_without_fill = False; self.closed = 0
    def get_wallet_balance(self): return 1000
    def get_price(self, symbol): return 100
    def normalize_quantity(self, symbol, quantity): return str(quantity)
    def set_leverage(self, symbol, leverage): assert leverage == 10
    def place_market_order(self, symbol, side, quantity):
        if self.timeout_without_fill:
            error = RuntimeError("timeout"); error.code = -1007; raise error
        old = self.qty; self.qty += float(quantity); self.average = ((self.average * old) + 100 * float(quantity)) / self.qty
        if self.timeout:
            error = RuntimeError("timeout"); error.code = -1007; raise error
        return {"status": "FILLED"}
    def close_position(self, symbol, side, quantity): self.qty -= float(quantity); self.closed += float(quantity)
    def get_recent_fills(self, symbol, limit=100): return []


class Protection:
    def __init__(self, fail=False): self.fail = fail; self.legs = []
    def place(self, leg, plan):
        if self.fail: raise RuntimeError("protection failed")
        self.legs.append(leg)
    def verify_leg(self, leg): return True
    def verify_position(self, position): return True


def plan(variant, setup, symbol="BTCUSDT"):
    reason = "moderate_breakout_retrace_entry_ready" if variant == ExecutionVariant.BUCKET_V1 else "compression_breakout"
    return TradePlan(symbol, 0, "LONG", 100, 98, 101, 2, 1, 1, 1, 1000,
                     "compression_strategy", 100, 900, {"management_profile": "MODERATE_BO_TP1_STRUCTURAL_SL"},
                     execution_variant=variant, setup_id=setup, signal_reason=reason,
                     arm_reason=None if variant == ExecutionVariant.BUCKET_V1 else "bucket_v2_compression_breakout_armed",
                     execution_reason="exec", size_fraction=.5)


def service(fail=False):
    exchange = Exchange()
    snapshots = SimpleNamespace(save_position=lambda position: None)
    engine = SimpleNamespace(exchange=exchange, positions={}, position_manager=PM(exchange),
                             position_sizer=PositionSizer(.30, 2, .90, 105),
                             snapshot_manager=snapshots)
    protection = Protection(fail)
    return BucketExecutionService(engine, protection), engine, exchange, protection


def test_v1_then_v2_and_v2_then_v1_increase_one_real_position():
    for first, second in ((ExecutionVariant.BUCKET_V1, ExecutionVariant.BUCKET_V2),
                          (ExecutionVariant.BUCKET_V2, ExecutionVariant.BUCKET_V1)):
        svc, engine, exchange, protection = service()
        assert svc.execute(plan(first, "one")); first_qty = exchange.qty
        assert svc.execute(plan(second, "two")); assert exchange.qty == first_qty * 2
        pos = engine.positions["BTCUSDT"]
        assert pos.execution_variant is ExecutionVariant.BUCKET_V1_V2
        assert len(pos.entry_legs) == 2 and len(protection.legs) == 2


def test_duplicate_variant_setup_and_third_leg_are_rejected():
    svc, engine, exchange, _ = service()
    assert svc.execute(plan(ExecutionVariant.BUCKET_V1, "one"))
    assert not svc.execute(plan(ExecutionVariant.BUCKET_V1, "one"))
    assert not svc.execute(plan(ExecutionVariant.BUCKET_V1, "different-setup"))
    assert svc.execute(plan(ExecutionVariant.BUCKET_V2, "two"))
    assert not svc.execute(plan(ExecutionVariant.BUCKET_V1, "three"))


def test_timeout_with_real_quantity_is_treated_as_filled():
    svc, engine, exchange, _ = service(); exchange.timeout = True
    assert svc.execute(plan(ExecutionVariant.BUCKET_V1, "one"))


def test_timeout_without_real_quantity_does_not_create_virtual_leg():
    svc, engine, exchange, _ = service(); exchange.timeout_without_fill = True
    assert not svc.execute(plan(ExecutionVariant.BUCKET_V1, "one"))
    assert not engine.positions and exchange.qty == 0


def test_second_leg_is_allowed_with_two_unique_symbols_but_third_symbol_is_not():
    svc, engine, exchange, _ = service()
    assert svc.execute(plan(ExecutionVariant.BUCKET_V1, "one"))
    engine.positions["ETHUSDT"] = object()
    assert svc.execute(plan(ExecutionVariant.BUCKET_V2, "two"))
    svc2, engine2, _, _ = service()
    engine2.positions.update({"BTCUSDT": object(), "ETHUSDT": object()})
    assert not svc2.execute(plan(ExecutionVariant.BUCKET_V1, "three", "XRPUSDT"))


def test_protection_failure_closes_added_quantity():
    svc, engine, exchange, _ = service(True)
    assert not svc.execute(plan(ExecutionVariant.BUCKET_V1, "one"))
    assert exchange.qty == 0 and exchange.closed > 0
