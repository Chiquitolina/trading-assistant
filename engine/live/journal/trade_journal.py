# engine/live/journal/trade_journal.py

import csv
import os


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