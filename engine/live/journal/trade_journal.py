import csv
import os


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
                    "entry_time",
                    "exit_time",
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
    def log_trade(
        self,
        entry_time,
        exit_time,
        side,
        entry,
        exit_price,
        tp,
        sl,
        pnl_pct,
        reason
    ):

        with open(self.file_path, "a", newline="") as f:
            writer = csv.writer(f)

            writer.writerow([
                entry_time,
                exit_time,
                side,
                round(entry, 2),
                round(exit_price, 2),
                round(tp, 2),
                round(sl, 2),
                round(pnl_pct, 4),
                reason
            ])
