import argparse
import hashlib
import json
from pathlib import Path

from engine.live.journal.trade_journal import TradeJournal
from models.entry_leg import EntryLeg, EntryLegIdentity
from models.execution_variant import ExecutionVariant
from models.position import Position


def _group_trades(trades):
    grouped = {}
    for trade in trades:
        grouped.setdefault(int(trade["orderId"]), []).append(trade)
    return grouped


def _summary(order_id, trades):
    quantity = sum(abs(float(x["qty"])) for x in trades)
    price = sum(abs(float(x["qty"])) * float(x["price"]) for x in trades) / quantity
    return {
        "order_id": order_id, "quantity": quantity, "price": price,
        "timestamp": max(int(x["time"]) for x in trades),
        "commission": sum(abs(float(x.get("commission", 0))) for x in trades),
        "trade_ids": [str(x.get("id", x.get("tradeId"))) for x in trades],
        "fills": [{
            "id": str(x.get("id", x.get("tradeId"))),
            "quantity": abs(float(x["qty"])), "price": float(x["price"]),
            "timestamp": int(x["time"]),
            "fee": abs(float(x.get("commission", 0))),
        } for x in trades],
    }


def build_recovery(payload, symbol):
    trades = [x for x in payload.get("trades", []) if x.get("symbol") == symbol]
    groups = _group_trades(trades)
    entries = [
        _summary(oid, group) for oid, group in groups.items()
        if group[0].get("side") == "BUY"
    ]
    exits = [
        _summary(oid, group) for oid, group in groups.items()
        if group[0].get("side") == "SELL"
    ]
    pairs = [
        (entry, exit_) for entry in entries for exit_ in exits
        if entry["timestamp"] <= exit_["timestamp"]
        and abs(entry["quantity"] - exit_["quantity"])
        <= max(1e-9, entry["quantity"] * 1e-6)
    ]
    if len(pairs) != 1:
        return {
            "symbol": symbol, "status": "AMBIGUOUS", "can_apply": False,
            "error": f"expected one entry/exit pair, found {len(pairs)}",
        }
    entry, exit_ = pairs[0]
    algo = next((
        x for x in payload.get("algo_orders", [])
        if x.get("symbol") == symbol
        and int(x.get("actualOrderId", x.get("triggeredOrderId", 0)) or 0) == exit_["order_id"]
    ), None)
    tp_order = next((
        x for x in payload.get("orders", [])
        if x.get("symbol") == symbol and x.get("type") == "LIMIT"
        and x.get("side") == "SELL" and bool(x.get("reduceOnly"))
    ), None)
    exit_reason = "SL" if algo else None
    sl = float((algo or {}).get("triggerPrice", 0) or 0)
    tp = float((tp_order or {}).get("price", 0) or 0)
    stable = ":".join(map(str, (
        payload.get("account", "unknown"), payload.get("environment", "unknown"),
        symbol, entry["order_id"], exit_["order_id"], *exit_["trade_ids"],
    )))
    recovery_key = "binance:" + hashlib.sha256(stable.encode("utf-8")).hexdigest()
    unknown = []
    if not exit_reason: unknown.append("exit_reason")
    if not tp: unknown.append("tp")
    if not sl: unknown.append("sl")
    unknown.extend(["original_leg_id", "setup_id", "signal_context"])
    return {
        "symbol": symbol, "status": "READY" if not unknown[:3] else "INCOMPLETE",
        "can_apply": bool(exit_reason and tp and sl), "recovery_key": recovery_key,
        "entry": entry, "exit": exit_, "exit_reason": exit_reason, "tp": tp, "sl": sl,
        "algo_id": (algo or {}).get("algoId"),
        "client_algo_id": (algo or {}).get("clientAlgoId"),
        "field_sources": {
            "entry.price/quantity/timestamp/commission": "REAL_ACCOUNT_TRADE",
            "exit.price/quantity/timestamp/commission": "REAL_ACCOUNT_TRADE",
            "exit_reason/sl/algo_id/client_algo_id": "RECOVERED_ALGO_HISTORY" if algo else "UNKNOWN",
            "tp": "RECOVERED_ORDER_HISTORY" if tp_order else "UNKNOWN",
            "leg_id/setup_id/signal_context": "RECOVERED_SYNTHETIC_IDENTITY",
        },
        "unknown_fields": unknown,
    }


def apply_recovery(result, journal_path):
    if not result.get("can_apply"):
        raise ValueError("recovery is incomplete or ambiguous")
    key = result["recovery_key"]
    leg_id = "recovery-" + key.rsplit(":", 1)[-1][:24]
    aggregate_id = "recovery-agg-" + key.rsplit(":", 1)[-1][:20]
    entry, exit_ = result["entry"], result["exit"]
    identity = EntryLegIdentity(
        leg_id, aggregate_id, result["symbol"], f"recovery-{entry['order_id']}",
        ExecutionVariant.LEGACY_UNKNOWN, "recovered_from_account_history", None,
        "recovered_missing_journal", entry["timestamp"], entry["timestamp"],
    )
    leg = EntryLeg(
        identity, entry["price"], entry["price"], entry["quantity"], 0, 1.0,
        result["tp"], result["sl"], closed_quantity=entry["quantity"],
        status="CLOSED", exit_fills=[{
            "id": f"recovery:{fill['id']}", "order_id": exit_["order_id"],
            "quantity": fill["quantity"], "price": fill["price"],
            "timestamp": fill["timestamp"], "fee": fill["fee"],
        } for fill in exit_["fills"]],
        entry_fees=entry["commission"], exit_fees=exit_["commission"],
        exit_reason=result["exit_reason"], exit_ts=exit_["timestamp"],
        processed_fill_ids=[f"recovery:{x}" for x in exit_["trade_ids"]],
        recovery_key=key,
    )
    position = Position(
        result["symbol"], "LONG", 0, entry["price"], entry["price"],
        result["tp"], result["sl"], entry["timestamp"], entry["price"], entry["timestamp"],
        aggregate_position_id=aggregate_id,
    )
    position.add_entry_leg(leg)
    return TradeJournal(journal_path).log_leg(position, leg)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Recover missing bucket journal rows")
    parser.add_argument("--history-file", required=True, type=Path)
    parser.add_argument("--symbol", action="append", required=True)
    parser.add_argument("--journal", type=Path, default=Path("trades.csv"))
    parser.add_argument("--apply", action="store_true", help="write idempotent journal rows")
    args = parser.parse_args(argv)
    payload = json.loads(args.history_file.read_text(encoding="utf-8"))
    results = [build_recovery(payload, symbol) for symbol in args.symbol]
    if args.apply:
        for result in results:
            result["journal_written"] = apply_recovery(result, args.journal)
    print(json.dumps({"dry_run": not args.apply, "results": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
