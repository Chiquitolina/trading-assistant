from types import SimpleNamespace
import pytest
from engine.live.execution.leg_protection import LegProtectionManager
from engine.live.execution.strategies.compression_execution_strategy import CompressionExecutionStrategy
from engine.live.strategy.trade_plan import TradePlan
from models.entry_leg import EntryLeg, EntryLegIdentity
from models.execution_variant import ExecutionVariant
from services.risk_manager import RiskManager


class Exchange:
    def __init__(self): self.orders = {}; self.next = 1; self.cancelled = []
    def get_mark_price(self, symbol): return 100.25
    def get_price_tick_size(self, symbol): return .1
    def normalize_price(self, symbol, price, side="DOWN"):
        import math
        value = math.ceil(price * 10) / 10 if side == "UP" else math.floor(price * 10) / 10
        return f"{value:.1f}"
    def _place(self, quantity, kind):
        oid = self.next; self.next += 1
        self.orders[oid] = {"orderId": oid, "origQty": quantity, "executedQty": 0,
                            "reduceOnly": True, "type": kind}
        return self.orders[oid]
    def place_stop_loss(self, symbol, side, quantity, stop_price, price_rounding): return self._place(quantity, "STOP_MARKET")
    def place_take_profit_limit(self, symbol, side, quantity, price, price_rounding): return self._place(quantity, "LIMIT")
    def get_open_orders(self, symbol): return [x for k, x in self.orders.items() if k not in self.cancelled]
    def cancel_order(self, symbol, order_id): self.cancelled.append(order_id)


def plan():
    return TradePlan("BTCUSDT", 1, "LONG", 100, 98, 101, 2, 1, 1, 1, 1000,
                     "compression_strategy", 100, 900, {"compression_low": 98})


def leg():
    identity = EntryLegIdentity("leg-1", "agg-1", "BTCUSDT", "setup-1",
                                ExecutionVariant.BUCKET_V1, "signal", None, "execution", 900, 1100)
    return EntryLeg(identity, 100, 100.2, 1, 1, .5, 1, 1)


def test_characterizes_bucket_v1_prepare_and_real_fill_translation():
    item = plan(); assert CompressionExecutionStrategy().prepare_plan(item)
    assert item.tp == pytest.approx(101) and item.sl == pytest.approx(98)
    tp, sl = RiskManager().calculate_tp_sl(item, 100.2, 100.25)
    assert tp == pytest.approx(101.2) and sl == pytest.approx(98.2)
    manager = LegProtectionManager(Exchange(), RiskManager()); protected = manager.place(leg(), item)
    assert protected.tp_price == 101.2 and protected.sl_price == 98.2


def test_virtual_oco_full_fill_cancels_sibling_idempotently():
    item = plan(); CompressionExecutionStrategy().prepare_plan(item)
    exchange = Exchange(); manager = LegProtectionManager(exchange, RiskManager())
    entry_leg = leg(); manager.place(entry_leg, item)
    position = SimpleNamespace(entry_legs=[entry_leg], quantity=1)
    fill = {"id": "fill-1", "order_id": entry_leg.tp_order_id, "quantity": 1}
    assert manager.process_fill(position, entry_leg, fill)
    assert entry_leg.sl_order_id in exchange.cancelled and position.quantity == 0
    assert not manager.process_fill(position, entry_leg, fill)


def test_virtual_oco_partial_fill_reduces_leg_before_sibling_replacement():
    item = plan(); CompressionExecutionStrategy().prepare_plan(item)
    exchange = Exchange(); manager = LegProtectionManager(exchange, RiskManager())
    entry_leg = leg(); manager.place(entry_leg, item)
    original_tp_id = entry_leg.tp_order_id
    position = SimpleNamespace(entry_legs=[entry_leg], quantity=1)
    fill = {"id": "fill-1", "order_id": entry_leg.sl_order_id, "quantity": .4}
    assert manager.process_fill(position, entry_leg, fill)
    assert entry_leg.remaining_quantity == pytest.approx(.6)
    assert original_tp_id in exchange.cancelled
    assert entry_leg.tp_order_id != original_tp_id


def test_virtual_oco_racing_sibling_fills_are_capped_and_idempotent():
    item = plan(); CompressionExecutionStrategy().prepare_plan(item)
    exchange = Exchange(); manager = LegProtectionManager(exchange, RiskManager())
    entry_leg = leg(); manager.place(entry_leg, item)
    position = SimpleNamespace(entry_legs=[entry_leg], quantity=1)
    tp_id, sl_id = entry_leg.tp_order_id, entry_leg.sl_order_id
    assert manager.process_fill(position, entry_leg, {
        "id": "tp-race", "order_id": tp_id, "quantity": .7, "price": 101.2,
    })
    assert manager.process_fill(position, entry_leg, {
        "id": "sl-race", "order_id": sl_id, "quantity": .7, "price": 98.2,
    })
    assert entry_leg.remaining_quantity == 0
    assert entry_leg.closed_quantity == pytest.approx(1)
    assert not manager.process_fill(position, entry_leg, {
        "id": "sl-race", "order_id": sl_id, "quantity": .7, "price": 98.2,
    })
