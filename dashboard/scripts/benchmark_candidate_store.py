#!/usr/bin/env python3
from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT
# When copied to /home/multiservices-1m/scripts, parent is project root.
if (ROOT / "dashboard").exists():
    PROJECT = ROOT
else:
    PROJECT = Path.cwd()

sys.path.insert(0, str(PROJECT))

from dashboard.services.candidate_research_store import CandidateResearchStore


def main() -> int:
    store = CandidateResearchStore(PROJECT)
    status = store.dependency_status()
    print("dependency_status:", status)
    print("root:", store.root)
    print("manifest:", store.read_manifest())
    if not status["available"]:
        print("ERROR: install duckdb and pyarrow first")
        return 2
    if not store.snapshot_exists():
        print("ERROR: Candidate snapshot does not exist. Open Candidate Research and refresh it first.")
        return 3

    tests = [
        ("v2 base", dict(profile="v2")),
        ("v2 strength >= 0.5", dict(profile="v2", strength_min=0.5)),
        ("v2 LONG + strength", dict(profile="v2", side="LONG", strength_min=0.5)),
        ("v1 raw legacy rule", dict(profile="v1_raw", room_min=1.0, rsi_min=1)),
    ]
    for name, kwargs in tests:
        samples = []
        matched = total = 0
        for _ in range(7):
            started = time.perf_counter()
            result = store.query(limit=2000, **kwargs)
            samples.append((time.perf_counter() - started) * 1000.0)
            matched, total = result["matched"], result["total"]
        print(
            f"{name:26s} matched={matched:6d}/{total:6d} "
            f"median={statistics.median(samples):8.2f}ms "
            f"pmax={max(samples):8.2f}ms"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
