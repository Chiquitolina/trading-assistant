from types import SimpleNamespace

from engine.live.execution.bucket_execution_service import BucketExecutionService
from exchange.fill_query import FillQueryResult, FillQueryStatus
from tests.models.test_multileg_models import leg, position


class Snapshots:
    def __init__(self):
        self.saved = []
        self.cleared = []

    def save_position(self, item):
        self.saved.append(item.to_dict())

    def clear(self, symbol):
        self.cleared.append(symbol)


class Exchange:
    def __init__(self, results, flat=True):
        self.results = list(results)
        self.flat = flat
        self.calls = 0

    def get_recent_fills(self, symbol, limit=1000):
        self.calls += 1
        return self.results.pop(0)
    def get_reconciliation_history(self, symbol):
        from exchange.order_history import OrderHistoryResult
        return OrderHistoryResult.success()


class PositionManager:
    def __init__(self, exchange): self.exchange = exchange
    def sync(self, symbol):
        if self.exchange.flat: return None
        return {"quantity": 1, "entry_price": 100}


class Protection:
    def __init__(self): self.order_to_leg = {}


def service(results):
    exchange = Exchange(results)
    snapshots = Snapshots()
    item = position(); item.add_entry_leg(leg())
    engine = SimpleNamespace(
        exchange=exchange, positions={item.symbol: item},
        position_manager=PositionManager(exchange), snapshot_manager=snapshots,
        journal=SimpleNamespace(log_leg=lambda *args: None),
    )
    svc = BucketExecutionService(engine, Protection())
    svc._clock = lambda: 100.0
    return svc, engine, exchange, snapshots, item


def retryable(message="502 Bad Gateway"):
    return FillQueryResult(FillQueryStatus.RETRYABLE_ERROR, error=message)


def test_flat_with_fill_query_error_keeps_position_and_snapshot():
    svc, engine, _, snapshots, item = service([retryable()])
    result = svc.reconcile_fills(item.symbol)
    assert result.status is FillQueryStatus.RETRYABLE_ERROR
    assert engine.positions[item.symbol] is item
    assert not snapshots.cleared and snapshots.saved
    assert item.reconciliation_status == "PENDING_RECONCILIATION"
    assert item.reconciliation_attempts == 1 and item.next_reconciliation_ts == 101.0
    assert item.symbol not in svc.manual_closed_symbols


def test_two_failures_survive_snapshot_round_trip_and_backoff():
    svc, engine, exchange, snapshots, item = service([retryable(), retryable("timeout")])
    svc.reconcile_fills(item.symbol)
    restored = type(item).from_dict(snapshots.saved[-1]["position"] if "position" in snapshots.saved[-1] else snapshots.saved[-1])
    engine.positions[item.symbol] = restored
    svc._clock = lambda: 101.0
    svc.reconcile_fills(item.symbol)
    assert exchange.calls == 2
    assert restored.reconciliation_attempts == 2
    assert restored.next_reconciliation_ts == 103.0
    assert item.symbol in engine.positions and not snapshots.cleared


def test_backoff_skips_query_until_due():
    svc, _, exchange, _, item = service([retryable(), FillQueryResult.success([])])
    svc.reconcile_fills(item.symbol)
    svc._clock = lambda: 100.5
    assert svc.reconcile_fills(item.symbol) is None
    assert exchange.calls == 1


def test_successful_empty_queries_and_flat_are_not_enough_for_manual_close():
    svc, engine, _, snapshots, item = service([FillQueryResult.success([])])
    svc.reconcile_fills(item.symbol)
    assert item.symbol in engine.positions
    assert item.reconciliation_status == "PENDING_RECONCILIATION"
    assert item.symbol not in svc.manual_closed_symbols
    assert not snapshots.cleared
