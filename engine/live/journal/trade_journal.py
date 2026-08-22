# engine/live/journal/trade_journal.py

import csv
import os
import json


class TradeJournal:
    def __init__(self, file_path="trades.csv"):
        self.file_path = file_path

    def _read_existing_headers(self) -> list[str]:
        if not os.path.exists(self.file_path):
            return []

        with open(self.file_path, "r", newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            return next(reader, [])

    def _rewrite_with_new_headers(self, new_headers: list[str]):
        if not os.path.exists(self.file_path):
            return

        with open(self.file_path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        with open(self.file_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=new_headers)
            writer.writeheader()

            for row in rows:
                writer.writerow({
                    key: row.get(key)
                    for key in new_headers
                })

    def log_trade(self, **kwargs):
        existing_headers = self._read_existing_headers()

        incoming_headers = list(kwargs.keys())

        if not existing_headers:
            fieldnames = incoming_headers

            with open(self.file_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerow(kwargs)

            return

        new_headers = existing_headers.copy()

        for key in incoming_headers:
            if key not in new_headers:
                new_headers.append(key)

        if new_headers != existing_headers:
            self._rewrite_with_new_headers(new_headers)

        with open(self.file_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=new_headers)
            writer.writerow({
                key: kwargs.get(key)
                for key in new_headers
            })

    def log_leg(self, position, leg):
        if leg.remaining_quantity > 1e-9:
            raise ValueError("cannot journal an open or partially open leg")
        fills = leg.exit_fills or []
        quantity_closed = sum(float(x["quantity"]) for x in fills) or leg.closed_quantity
        if quantity_closed <= 0:
            raise ValueError("closed leg has no closed quantity")
        real_exit = sum(float(x["price"]) * float(x["quantity"]) for x in fills) / quantity_closed
        direction = 1 if position.side == "LONG" else -1
        gross_pnl = direction * (real_exit - leg.real_entry) * quantity_closed
        fees = float(leg.entry_fees) + float(leg.exit_fees)
        self.log_trade(
            symbol=leg.symbol, aggregate_position_id=leg.aggregate_position_id,
            execution_variant=leg.variant.value, setup_id=leg.setup_id, leg_id=leg.leg_id,
            signal_reason=leg.signal_reason, arm_reason=leg.arm_reason,
            execution_reason=leg.execution_reason, signal_ts=leg.signal_ts,
            entry_ts=leg.entry_ts, requested_entry=leg.requested_entry,
            real_entry=leg.real_entry, initial_quantity=leg.initial_quantity,
            quantity_closed=quantity_closed, exit_ts=leg.exit_ts,
            real_exit=real_exit, tp=leg.tp, sl=leg.sl, exit_reason=leg.exit_reason,
            gross_pnl=round(gross_pnl, 12), net_pnl=round(gross_pnl - fees, 12),
            fees=round(fees, 12),
            signal_context=json.dumps(leg.signal_context, sort_keys=True, default=str),
            position_increased=position.position_increased,
            ever_combined=leg.ever_combined,
            overlapped_with_other_leg=leg.overlapped_with_other_leg,
        )
