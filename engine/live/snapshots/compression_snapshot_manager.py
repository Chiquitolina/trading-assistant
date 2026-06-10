import json
from pathlib import Path
from dataclasses import asdict

from models.signal_compression_snapshot import SignalCompressionSnapshot


class CompressionSnapshotManager:

    def __init__(self, base_dir="compression_snapshots"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str) -> Path:
        return self.base_dir / f"{symbol}.json"

    def save(self, snapshot: SignalCompressionSnapshot):
        path = self._path(snapshot.symbol)

        data = asdict(snapshot)

        tmp_path = path.with_suffix(".json.tmp")

        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(
                data,
                f,
                indent=4,
                default=str
            )

        tmp_path.replace(path)

    def load(self, symbol: str) -> dict | None:
        path = self._path(symbol)

        if not path.exists():
            return None

        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None