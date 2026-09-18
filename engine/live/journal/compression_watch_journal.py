import json
import os
from pathlib import Path
from datetime import datetime


class CompressionWatchJournal:
    def __init__(self, base_dir=None):
        if base_dir is None:
            clock_mode = os.getenv(
                "MARKET_CLOCK_MODE",
                "real",
            ).strip().lower()

            mode_dir = (
                "replay"
                if clock_mode == "replay"
                else "live"
            )

            base_dir = (
                Path("compression_watch_journal")
                / mode_dir
            )

        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

    def log(self, symbol: str, event: str, data: dict):
        path = self.base_dir / f"{symbol}.jsonl"

        payload = {
            "logged_at": datetime.utcnow().isoformat(
                timespec="milliseconds"
            ),
            "symbol": symbol,
            "event": event,
            **data,
        }

        with open(path, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(payload, default=str)
                + "\n"
            )