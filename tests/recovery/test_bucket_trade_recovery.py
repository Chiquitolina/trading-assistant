import csv

import pytest

from engine.live.recovery.bucket_trade_recovery import apply_recovery, build_recovery


@pytest.mark.parametrize("symbol,quantity,entry,exit_price", [
    ("CATUSDT", 65546.0, 0.05150, 0.05067),
    ("SANTOSUSDT", 6131.9, 0.544, 0.532),
])
def test_real_history_recovery_is_reported_and_idempotent(
    tmp_path, symbol, quantity, entry, exit_price,
):
    payload = {
        "account": "testnet-account", "environment": "testnet",
        "trades": [
            {"symbol": symbol, "orderId": 10, "id": 1, "side": "BUY", "qty": quantity,
             "price": entry, "commission": ".1", "time": 1000},
            {"symbol": symbol, "orderId": 20, "id": 2, "side": "SELL", "qty": quantity,
             "price": exit_price, "commission": ".2", "time": 2000},
        ],
        "algo_orders": [{
            "symbol": symbol, "algoId": 99, "clientAlgoId": "sl-real",
            "actualOrderId": 20, "triggerPrice": exit_price,
        }],
        "orders": [{
            "symbol": symbol, "orderId": 30, "type": "LIMIT", "side": "SELL",
            "reduceOnly": True, "price": entry * 1.01,
        }],
    }
    result = build_recovery(payload, symbol)
    assert result["can_apply"] and result["exit_reason"] == "SL"
    assert result["field_sources"]["exit.price/quantity/timestamp/commission"] == "REAL_ACCOUNT_TRADE"
    journal = tmp_path / "trades.csv"
    assert apply_recovery(result, journal)
    assert not apply_recovery(result, journal)
    with open(journal, newline="", encoding="utf-8") as stream:
        assert len(list(csv.DictReader(stream))) == 1


def test_ambiguous_history_cannot_be_applied(tmp_path):
    payload = {"trades": []}
    result = build_recovery(payload, "CATUSDT")
    assert result["status"] == "AMBIGUOUS" and not result["can_apply"]
    with pytest.raises(ValueError): apply_recovery(result, tmp_path / "trades.csv")
