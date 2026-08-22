import csv
from engine.live.journal.trade_journal import TradeJournal
from tests.models.test_multileg_models import leg, position
from models.entry_leg import EntryLegIdentity
from models.execution_variant import ExecutionVariant


def test_journal_writes_one_independent_row_per_closed_leg(tmp_path):
    path = tmp_path / "trades.csv"; journal = TradeJournal(path)
    pos = position(); first = leg(); pos.add_entry_leg(first)
    first.remaining_quantity = 0; first.closed_quantity = 1
    first.exit_fills = [{"quantity": 1, "price": 101}]
    first.entry_fees = .05; first.exit_fees = .05
    first.exit_ts = 2000; first.exit_reason = "TAKE_PROFIT"
    journal.log_leg(pos, first)
    with open(path, newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1 and rows[0]["leg_id"] == first.leg_id
    assert float(rows[0]["gross_pnl"]) == 0.9
    assert float(rows[0]["net_pnl"]) == .8


def test_combined_position_writes_two_rows_with_shared_aggregate_id(tmp_path):
    path = tmp_path / "trades.csv"; journal = TradeJournal(path); pos = position()
    first = leg(); second = leg(identity=EntryLegIdentity(
        "leg-2", "agg-1", "BTCUSDT", "setup-2", ExecutionVariant.BUCKET_V2,
        "compression_breakout", "bucket_v2_compression_breakout_armed",
        "bucket_v2_retrace_band_triggered", 1000, 1200,
    ))
    pos.add_entry_leg(first); pos.add_entry_leg(second)
    for item, price in ((first, 101), (second, 99)):
        item.remaining_quantity = 0; item.closed_quantity = 1
        item.exit_fills = [{"quantity": 1, "price": price}]
        item.exit_ts = 2000; item.exit_reason = "TEST"
        journal.log_leg(pos, item)
    with open(path, newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 2
    assert {row["execution_variant"] for row in rows} == {"BUCKET_V1", "BUCKET_V2"}
    assert {row["aggregate_position_id"] for row in rows} == {"agg-1"}
