import csv
import os
from datetime import datetime


class TradeJournal:

    def __init__(self, file_path="trades.csv"):
        self.file_path = file_path
        self._ensure_file()

    # -------------------------
    # crear archivo si no existe
    # -------------------------
    def _ensure_file(self):
        if not os.path.exists(self.file_path):
            with open(self.file_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "date",
                    "side",
                    "entry",
                    "exit",
                    "tp",
                    "sl",
                    "pnl_pct",
                    "reason"
                ])

    # -------------------------
    # guardar trade
    # -------------------------
    def log_trade(self, side, entry, exit_price, tp, sl, pnl_pct, reason):

        with open(self.file_path, "a", newline="") as f:
            writer = csv.writer(f)

            writer.writerow([
                datetime.utcnow().isoformat(),
                side,
                round(entry, 2),
                round(exit_price, 2),
                round(tp, 2),
                round(sl, 2),
                round(pnl_pct, 3),
                reason
            ])
