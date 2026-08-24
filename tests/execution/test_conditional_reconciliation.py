from types import SimpleNamespace

import pytest

from engine.live.execution.bucket_execution_service import BucketExecutionService
from engine.live.journal.trade_journal import TradeJournal
from engine.live.state.snapshot_manager import SnapshotManager
from exchange.fill_query import FillQueryResult
from exchange.order_history import OrderHistoryResult
from models.entry_leg import EntryLeg, EntryLegIdentity
from models.execution_variant import ExecutionVariant
from models.position import Position


REGRESSIONS = [
    ("CATUSDT", 65546.0, 0.05150, 0.05067),
    ("SANTOSUSDT", 6131.9, 0.544, 0.532),
]


class Exchange:
    def __init__(self, fill, history):
        self.fill = fill
        self.history = history
        self.cancelled = []

    def get_recent_fills(self, symbol, limit=1000):
        return FillQueryResult.success([self.fill])

    def get_reconciliation_history(self, symbol):
        return self.history


class Snapshots:
    def __init__(self): self.saved = []; self.cleared = []
    def save_position(self, item): self.saved.append(item.to_dict())
    def clear(self, symbol): self.cleared.append(symbol)


class Protection:
    def __init__(self): self.order_to_leg = {}; self.processed_fill_ids = set()
    def process_fill(self, position, leg, fill):
        if fill["id"] in self.processed_fill_ids or str(fill["id"]) in leg.processed_fill_ids:
            return False
        self.processed_fill_ids.add(fill["id"])
        leg.processed_fill_ids.append(str(fill["id"]))
        leg.remaining_quantity = 0
        leg.closed_quantity = leg.initial_quantity
        leg.exit_fills.append(fill)
        leg.exit_fees += fill["fee"]
        leg.status = "CLOSED"
        position.quantity = 0
        return True


def make_position(symbol, quantity, entry, algo_id=700, client_id="sl-regression"):
    identity = EntryLegIdentity(
        f"leg-{symbol}", f"agg-{symbol}", symbol, f"setup-{symbol}",
        ExecutionVariant.BUCKET_V1, "signal", None, "execution", 1000, 1100,
    )
    item = EntryLeg(identity, entry, entry, quantity, quantity, .5, entry * 1.01, entry * .98)
    item.tp_order_id = 600
    item.sl_algo_id = algo_id
    item.sl_client_algo_id = client_id
    pos = Position(
        symbol, "LONG", quantity, entry, entry, item.tp, item.sl, 1100, entry, 1000,
        aggregate_position_id=identity.aggregate_position_id,
    )
    pos.add_entry_leg(item)
    return pos, item


@pytest.mark.parametrize("symbol,quantity,entry,exit_price", REGRESSIONS)
def test_regression_conditional_algo_id_maps_to_distinct_fill_order_id(
    symbol, quantity, entry, exit_price,
):
    pos, item = make_position(symbol, quantity, entry)
    fill_order_id = 900
    fill = {
        "id": 1, "orderId": fill_order_id, "side": "SELL", "qty": str(quantity),
        "price": str(exit_price), "commission": "0.12", "time": 1200,
    }
    history = OrderHistoryResult.success(
        orders=[{
            "orderId": fill_order_id, "symbol": symbol, "side": "SELL",
            "origQty": str(quantity), "reduceOnly": True,
        }],
        algo_orders=[{
            "algoId": 700, "clientAlgoId": "sl-regression",
            "actualOrderId": fill_order_id,
        }],
    )
    exchange = Exchange(fill, history); snapshots = Snapshots(); rows = []
    engine = SimpleNamespace(
        exchange=exchange, positions={symbol: pos},
        position_manager=SimpleNamespace(sync=lambda ignored: None),
        snapshot_manager=snapshots,
        journal=SimpleNamespace(log_leg=lambda position, leg: rows.append(leg.to_dict())),
    )
    service = BucketExecutionService(engine, Protection())
    service.reconcile_fills(symbol)
    assert item.sl_algo_id == 700
    assert item.sl_order_id is None
    assert item.sl_materialized_order_id == fill_order_id
    assert item.exit_reason == "SL"
    assert item.exit_fills[0]["price"] == pytest.approx(exit_price)
    assert len(rows) == 1


def test_ambiguous_heuristic_match_stays_pending():
    pos, first = make_position("CATUSDT", 10, 0.05150, algo_id=701, client_id="one")
    identity = EntryLegIdentity(
        "leg-two", pos.aggregate_position_id, pos.symbol, "setup-two",
        ExecutionVariant.BUCKET_V2, "signal", None, "execution", 1000, 1100,
    )
    second = EntryLeg(identity, .0515, .0515, 10, 10, .5, .052, .05)
    second.sl_algo_id = 702; second.sl_client_algo_id = "two"
    pos.add_entry_leg(second); pos.quantity = 20
    fill = {"id": 1, "orderId": 999, "side": "SELL", "qty": "10", "price": ".05", "time": 1200}
    history = OrderHistoryResult.success(orders=[{
        "orderId": 999, "symbol": pos.symbol, "side": "SELL", "reduceOnly": True,
    }])
    exchange = Exchange(fill, history); snapshots = Snapshots()
    engine = SimpleNamespace(
        exchange=exchange, positions={pos.symbol: pos},
        position_manager=SimpleNamespace(sync=lambda ignored: None), snapshot_manager=snapshots,
        journal=SimpleNamespace(log_leg=lambda *args: None),
    )
    service = BucketExecutionService(engine, Protection())
    service.reconcile_fills(pos.symbol)
    assert pos.reconciliation_status == "PENDING_RECONCILIATION"
    assert first.remaining_quantity == second.remaining_quantity == 10
    assert not snapshots.cleared


def test_crash_after_journal_append_is_idempotent_after_snapshot_restore(tmp_path):
    symbol, quantity, entry, exit_price = REGRESSIONS[0]
    pos, item = make_position(symbol, quantity, entry)
    fill = {
        "id": 77, "orderId": 900, "side": "SELL", "qty": str(quantity),
        "price": str(exit_price), "commission": ".12", "time": 1200,
    }
    history = OrderHistoryResult.success(algo_orders=[{
        "algoId": item.sl_algo_id, "clientAlgoId": item.sl_client_algo_id,
        "actualOrderId": 900,
    }])
    exchange = Exchange(fill, history)
    snapshots = SnapshotManager(tmp_path / "snapshots")
    journal = TradeJournal(tmp_path / "trades.csv")

    class CrashAfterWrite:
        def log_leg(self, position, leg):
            journal.log_leg(position, leg)
            raise RuntimeError("simulated crash after append")

    engine = SimpleNamespace(
        exchange=exchange, positions={symbol: pos},
        position_manager=SimpleNamespace(sync=lambda ignored: None),
        snapshot_manager=snapshots, journal=CrashAfterWrite(),
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        BucketExecutionService(engine, Protection()).reconcile_fills(symbol)
    raw = snapshots.load(symbol)
    restored = Position.from_dict(raw["position"])
    assert restored.entry_legs[0].status == "CLOSED"
    assert restored.entry_legs[0].processed_fill_ids == ["900:77"]

    retry_engine = SimpleNamespace(
        exchange=exchange, positions={symbol: restored},
        position_manager=SimpleNamespace(sync=lambda ignored: None),
        snapshot_manager=snapshots, journal=journal,
    )
    BucketExecutionService(retry_engine, Protection()).reconcile_fills(symbol)
    import csv
    with open(journal.file_path, newline="", encoding="utf-8") as stream:
        assert len(list(csv.DictReader(stream))) == 1
    assert snapshots.load(symbol) is None


def test_manual_close_requires_positive_unique_history_evidence(tmp_path):
    symbol, quantity, entry, _ = REGRESSIONS[1]
    pos, _ = make_position(symbol, quantity, entry)
    history = OrderHistoryResult.success(orders=[{
        "orderId": 1234, "symbol": symbol, "side": "SELL", "reduceOnly": True,
        "status": "FILLED", "executedQty": str(quantity), "avgPrice": "0.54",
        "updateTime": 1300,
    }])
    exchange = Exchange({}, history)
    exchange.fill = None
    exchange.get_recent_fills = lambda symbol, limit=1000: FillQueryResult.success([])
    snapshots = SnapshotManager(tmp_path / "snapshots")
    journal = TradeJournal(tmp_path / "trades.csv")
    engine = SimpleNamespace(
        exchange=exchange, positions={symbol: pos},
        position_manager=SimpleNamespace(sync=lambda ignored: None),
        snapshot_manager=snapshots, journal=journal,
    )
    service = BucketExecutionService(engine, Protection())
    service.reconcile_fills(symbol)
    assert symbol in service.manual_closed_symbols
    assert symbol not in engine.positions and snapshots.load(symbol) is None


def test_history_api_error_is_unknown_not_manual():
    symbol, quantity, entry, _ = REGRESSIONS[1]
    pos, _ = make_position(symbol, quantity, entry)
    from exchange.order_history import HistoryQueryStatus
    exchange = Exchange({}, OrderHistoryResult(
        HistoryQueryStatus.RETRYABLE_ERROR, error="502 history",
    ))
    exchange.get_recent_fills = lambda symbol, limit=1000: FillQueryResult.success([])
    snapshots = Snapshots()
    engine = SimpleNamespace(
        exchange=exchange, positions={symbol: pos},
        position_manager=SimpleNamespace(sync=lambda ignored: None),
        snapshot_manager=snapshots, journal=SimpleNamespace(log_leg=lambda *args: None),
    )
    service = BucketExecutionService(engine, Protection())
    service.reconcile_fills(symbol)
    assert pos.reconciliation_status == "PENDING_RECONCILIATION"
    assert symbol in engine.positions and symbol not in service.manual_closed_symbols
    assert not snapshots.cleared
