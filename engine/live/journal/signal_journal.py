import csv
import os
from datetime import datetime


class SignalJournal:

    def __init__(self, file_path="live_signals.csv"):
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
                    "timestamp",
                    "tf",
                    "side",
                    "signal_price",
                    "dir",
                    "trend",
                    "momentum"
                ])

    # -------------------------
    # guardar señal
    # -------------------------
    def log_signal(
        self,
        timestamp,
        tf,
        side,
        signal_price,
        direction,
        trend,
        momentum
    ):

        # convertir timestamp ms → ISO
        if isinstance(timestamp, (int, float)):
            timestamp = datetime.utcfromtimestamp(
                timestamp / 1000
            ).isoformat(timespec="seconds")

        with open(self.file_path, "a", newline="", encoding="utf-8") as f:

            writer = csv.writer(f)

            writer.writerow([
                timestamp,
                tf,
                side,
                round(signal_price, 2),
                direction,
                trend,
                momentum
            ])