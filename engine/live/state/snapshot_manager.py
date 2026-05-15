import json
from pathlib import Path
from typing import Any


class SnapshotManager:
    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or Path("snapshots")
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str) -> Path:
        return self.base_dir / f"{symbol}.json"

    def save(self, symbol: str, data: dict[str, Any]):
        path = self._path(symbol)

        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, default=str)

    def load(self, symbol: str) -> dict[str, Any] | None:
        path = self._path(symbol)

        if not path.exists():
            return None

        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def update(self, symbol: str, section: str, data: dict[str, Any]):
        snapshot = self.load(symbol) or {}

        current_section = snapshot.get(section, {})
        if not isinstance(current_section, dict):
            current_section = {}

        current_section.update(data)
        snapshot[section] = current_section

        self.save(symbol, snapshot)

    def clear(self, symbol: str):
        path = self._path(symbol)

        if path.exists():
            path.unlink()