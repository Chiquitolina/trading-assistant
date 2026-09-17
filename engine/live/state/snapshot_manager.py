import json
from pathlib import Path
from typing import Any
import threading

class SnapshotManager:
    _locks_guard = threading.Lock()
    _symbol_locks: dict[str, threading.RLock] = {}

    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or Path("snapshots")
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str) -> Path:
        return self.base_dir / f"{symbol}.json"

    @classmethod
    def _get_lock(cls, symbol: str) -> threading.RLock:
        with cls._locks_guard:
            lock = cls._symbol_locks.get(symbol)

            if lock is None:
                lock = threading.RLock()
                cls._symbol_locks[symbol] = lock

            return lock

    def _save_unlocked(
        self,
        symbol: str,
        data: dict[str, Any],
    ):
        path = self._path(symbol)

        tmp_path = path.with_name(
            f"{path.name}.{threading.get_ident()}.tmp"
        )

        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(
                data,
                f,
                indent=4,
                default=str,
            )
            f.flush()

        tmp_path.replace(path)

    def save(
        self,
        symbol: str,
        data: dict[str, Any],
    ):
        lock = self._get_lock(symbol)

        with lock:
            self._save_unlocked(symbol, data)

    def load(
        self,
        symbol: str,
    ) -> dict[str, Any] | None:
        lock = self._get_lock(symbol)

        with lock:
            path = self._path(symbol)

            if not path.exists():
                return None

            try:
                with open(
                    path,
                    "r",
                    encoding="utf-8",
                ) as f:
                    return json.load(f)
            except Exception:
                return None

    def update(
        self,
        symbol: str,
        section: str,
        data: dict[str, Any],
    ):
        lock = self._get_lock(symbol)

        with lock:
            path = self._path(symbol)

            snapshot = {}

            if path.exists():
                try:
                    with open(
                        path,
                        "r",
                        encoding="utf-8",
                    ) as f:
                        snapshot = json.load(f)
                except Exception:
                    snapshot = {}

            current_section = snapshot.get(
                section,
                {},
            )

            if not isinstance(current_section, dict):
                current_section = {}

            current_section.update(data)
            snapshot[section] = current_section

            self._save_unlocked(
                symbol,
                snapshot,
            )

    def clear(self, symbol: str):
        lock = self._get_lock(symbol)

        with lock:
            path = self._path(symbol)

            if path.exists():
                path.unlink()