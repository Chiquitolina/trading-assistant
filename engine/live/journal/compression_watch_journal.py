import json
from pathlib import Path
from datetime import datetime


class CompressionWatchJournal:
    def __init__(self, base_dir="compression_watch_journal"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def log(self, symbol: str, event: str, data: dict):
        path = self.base_dir / f"{symbol}.jsonl"

        payload = {
            "logged_at": datetime.utcnow().isoformat(timespec="milliseconds"),
            "symbol": symbol,
            "event": event,
            **data,
        }

        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, default=str) + "\n")