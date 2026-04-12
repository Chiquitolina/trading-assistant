import json
from pathlib import Path
from datetime import datetime
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent.parent
STATUS_FILE = BASE_DIR / "status.json"


class StatusWriter:
    def __init__(self, status_file: Path | None = None):
        self.status_file = status_file or STATUS_FILE

    def _default_data(self) -> dict[str, Any]:
        return {
            "engine_online": False,
            "ws_online": False,
            "symbol": "BTCUSDT",
            "balance": 0.0,
            "position_side": "NONE",
            "position_qty": 0.0,
            "entry_price": 0.0,
            "unpnl": 0.0,

            # SIGNAL
            "last_signal": "N/A",
            "signal_trend": None,
            "signal_direction": None,
            "signal_momentum": None,

            # PLAN
            "last_plan_status": None,      # READY | DISCARDED | EXECUTED | SKIPPED
            "last_plan_reason": None,      # min_tp_not_met | low_atr | etc
            "last_plan_side": None,
            "last_plan_entry": None,
            "last_plan_tp": None,
            "last_plan_sl": None,
            "last_plan_atr": None,

            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

    def _read_current(self) -> dict[str, Any]:
        if not self.status_file.exists():
            return self._default_data()

        try:
            with open(self.status_file, "r", encoding="utf-8") as f:
                current = json.load(f)

            if not isinstance(current, dict):
                return self._default_data()

            data = self._default_data()
            data.update(current)
            return data

        except Exception:
            return self._default_data()

    def write(self, payload: dict[str, Any]) -> None:
        data = self._read_current()
        data.update(payload)
        data["updated_at"] = datetime.now().isoformat(timespec="seconds")

        with open(self.status_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def write_signal(
        self,
        side: str | None = None,
        trend: str | None = None,
        direction: str | None = None,
        momentum: str | None = None,
    ) -> None:
        self.write({
            "last_signal": side,
            "signal_trend": trend,
            "signal_direction": direction,
            "signal_momentum": momentum,
        })

    def write_plan(
        self,
        status: str | None = None,
        reason: str | None = None,
        side: str | None = None,
        entry: float | None = None,
        tp: float | None = None,
        sl: float | None = None,
        atr: float | None = None,
    ) -> None:
        self.write({
            "last_plan_status": status,
            "last_plan_reason": reason,
            "last_plan_side": side,
            "last_plan_entry": entry,
            "last_plan_tp": tp,
            "last_plan_sl": sl,
            "last_plan_atr": atr,
        })