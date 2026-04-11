import json
from pathlib import Path
from datetime import datetime
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent.parent
STATUS_FILE = BASE_DIR / "status.json"


class StatusWriter:
    def __init__(self, status_file: Path | None = None):
        self.status_file = status_file or STATUS_FILE

    def write(self, payload: dict[str, Any]) -> None:
        data = {
            "engine_online": False,
            "ws_online": False,
            "symbol": "BTCUSDT",
            "balance": 0.0,
            "position_side": "NONE",
            "position_qty": 0.0,
            "entry_price": 0.0,
            "unpnl": 0.0,
            "last_signal": "N/A",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            **payload,
        }

        with open(self.status_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)