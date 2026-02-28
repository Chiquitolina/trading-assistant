# engine/live/journal/trade_journal.py

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
            with open(self.file_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    # 🆕 señal
                    "signal_ts",
                    "signal_price",

                    # trade
                    "entry_ts",
                    "exit_ts",
                    "side",
                    "entry",
                    "exit",
                    "tp",
                    "sl",

                    # pnl
                    "pnl_pct",
                    "pnl",
                    "pnl_gross",
                    "fees",

                    # meta
                    "exit_reason"
                ])

    # -------------------------
    # guardar trade
    # -------------------------
    def log_trade(
        self,
        signal_ts,
        signal_price,
        entry_ts,
        exit_ts,
        side,
        entry,
        exit_price,
        tp,
        sl,
        pnl,
        pnl_gross,
        fees,
        exit_reason
    ):
        with open(self.file_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)

            writer.writerow([
                # señal
                signal_ts,
                round(signal_price, 2),

                # trade
                entry_ts,
                exit_ts,
                side,
                round(entry, 2),
                round(exit_price, 2),
                round(tp, 2),
                round(sl, 2),

                # pnl
                round(pnl, 4),
                round(pnl_gross, 4),
                round(fees, 2),

                # meta
                exit_reason
            ])