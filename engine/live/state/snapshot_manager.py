import json
import os

class SnapshotManager:

    def __init__(self, path="state/position_snapshot.json"):
        self.path = path

    # =========================
    # SAVE FULL SNAPSHOT
    # =========================
    def save(self, snapshot: dict):
        with open(self.path, "w") as f:
            json.dump(snapshot, f, indent=2)

    # =========================
    # LOAD SNAPSHOT
    # =========================
    def load(self):
        if not os.path.exists(self.path):
            return None

        with open(self.path, "r") as f:
            return json.load(f)

    # =========================
    # UPDATE PARTIAL (clave)
    # =========================
    def update(self, section: str, data: dict):
        snapshot = self.load() or {}
        snapshot.setdefault(section, {})
        snapshot[section].update(data)

        self.save(snapshot)