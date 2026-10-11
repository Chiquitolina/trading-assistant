#!/usr/bin/env python3
"""Safely snapshot an active V2 historical replay and run frozen A/B portfolio on it.

Read-only against the ongoing source run. Exports a distinct partial run ID and
report tag; leaves the 290-symbol replay and its checkpoint/candles untouched.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys


DEFAULT_SOURCE_RUN = "20260907_20260930_dd94dc31235f"


def make_snapshot(root: Path, source_run_id: str, *, min_symbols: int = 1):
    base = root / "reports" / "candidate_long_horizon" / "runs"
    source_dir = base / source_run_id
    config_file = source_dir / "config.json"
    state_file = source_dir / "state.json"
    events_file = source_dir / "events.csv"
    for required in (config_file, state_file, events_file):
        if not required.is_file():
            raise RuntimeError(f"Missing source replay file: {required}")
    config = json.loads(config_file.read_text(encoding="utf-8"))
    # The writer uses atomic os.replace for state.json, then read this completed
    # checkpoint BEFORE reading events.csv to exclude the in-progress symbol.
    state = json.loads(state_file.read_text(encoding="utf-8"))
    original_symbols = config.get("symbols", [])
    months = config.get("months", [])
    if not isinstance(original_symbols, list) or not original_symbols:
        raise RuntimeError("No frozen symbols in replay config")
    if len(months) != 1 or months[0].get("label") != "2026-09":
        raise RuntimeError("This partial-backtest wrapper supports a single 2026-09 replay month")
    if config.get("run_id") != source_run_id:
        raise RuntimeError("Source run ID and config.json disagree")
    processed = state.get("processed", {})
    if not isinstance(processed, dict):
        raise RuntimeError("Unexpected processed-symbol checkpoint format")
    done_list = processed.get("2026-09", [])
    if not isinstance(done_list, list):
        raise RuntimeError("Expected a list of processed symbols")
    done = set(done_list)
    if not done.issubset(set(original_symbols)):
        raise RuntimeError("Checkpoint includes unknown symbols")
    selected_symbols = [s for s in original_symbols if s in done]
    if len(selected_symbols) < min_symbols:
        raise RuntimeError(f"Only {len(selected_symbols)} symbols completed; requested minimum {min_symbols}")

    # The producer atomically replaces events.csv after every completed symbol.
    # An open file descriptor sees one complete version despite os.replace.
    # CSV streaming preserves original decimal strings exactly (no float
    # round-trip) and keeps memory use low while the live replay runs.
    frozen_rows = []
    seen_event_ids = set()
    with events_file.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        required_cols = {"symbol", "candidate_v1_event_key", "entry_timestamp"}
        missing = required_cols.difference(fieldnames)
        if missing:
            raise RuntimeError(f"Source replay is missing event columns: {sorted(missing)}")
        for row in reader:
            if row["symbol"] not in done:
                continue
            key = row.get("candidate_v1_event_key", "").strip()
            if not key or key in seen_event_ids:
                raise RuntimeError("Empty or duplicate Event ID in completed snapshot")
            seen_event_ids.add(key)
            frozen_rows.append(row)
    if not frozen_rows:
        raise RuntimeError("No completed-symbol events available")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    report_tag = f"partial_{len(selected_symbols):03d}_{stamp}"
    frozen_run_id = f"20260907_20260930_{report_tag}"
    destination = base / frozen_run_id
    destination.mkdir(parents=True, exist_ok=False)
    new_config = dict(config)
    new_config["symbols"] = selected_symbols
    new_config["run_id"] = frozen_run_id
    new_config["parent_run_id"] = source_run_id
    new_config["snapshot_at_utc"] = datetime.now(timezone.utc).isoformat()
    new_config["source_universe_count"] = len(original_symbols)
    new_config["partial_snapshot"] = True
    (destination / "config.json").write_text(json.dumps(new_config, indent=2, ensure_ascii=False), encoding="utf-8")
    # Never modify the source run's events.csv. This is an independent file.
    with (destination / "events.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(frozen_rows)
    manifest = {
        "source_run_id": source_run_id,
        "partial_run_id": frozen_run_id,
        "report_tag": report_tag,
        "snapshot_utc": new_config["snapshot_at_utc"],
        "source_universe_count": len(original_symbols),
        "completed_symbols": len(selected_symbols),
        "snapshot_events": len(frozen_rows),
        "symbols": selected_symbols,
        "source_state_errors": state.get("errors", {}),
        "events_sha256": hashlib.sha256((destination / "events.csv").read_bytes()).hexdigest(),
        "note": "PARTIAL alphabetical-prefix cohort; not a 290-symbol result. Context Breadth retains 290 symbol universe.",
    }
    (destination / "partial_snapshot.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return manifest


def run_pipeline(root: Path, manifest: dict):
    run_id = manifest["partial_run_id"]
    tag = manifest["report_tag"]
    sys.stdout.flush()
    commands = [
        [sys.executable, "-u", "prepare_v2_sep_context.py", "--project", str(root), "--run-id", run_id, "--output-tag", tag],
        [sys.executable, "-u", "backtest_v2_sep_frozen_ab.py", "--project", str(root), "--run-id", run_id, "--report-tag", tag, "--parity-only"],
        [sys.executable, "-u", "backtest_v2_sep_frozen_ab.py", "--project", str(root), "--run-id", run_id, "--report-tag", tag, "--run"],
    ]
    for cmd in commands:
        print("\n=== RUNNING:", " ".join(cmd), "===", flush=True)
        subprocess.run(cmd, check=True, cwd=root)
    print("\nPARTIAL BACKTEST FINISHED", flush=True)
    print("Portfolio report:", root / "reports/candidate_long_horizon/diagnostics/v2_sep_2026" / tag / "portfolio_frozen_ab", flush=True)
    print("Source 290-symbol replay remains untouched.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--source-run-id", default=DEFAULT_SOURCE_RUN)
    parser.add_argument("--min-symbols", type=int, default=50,
                        help="Require at least this many completed symbols before taking snapshot (default: 50)")
    parser.add_argument("--snapshot-only", action="store_true", help="Only freeze the current events/checkpoint, do not backtest")
    args = parser.parse_args()
    if args.min_symbols < 1:
        parser.error("--min-symbols must be >= 1")
    root = args.project.resolve()
    manifest = make_snapshot(root, args.source_run_id, min_symbols=args.min_symbols)
    print("=== SAFE PARTIAL SNAPSHOT ===")
    print(json.dumps({k: manifest[k] for k in ("source_run_id", "partial_run_id", "report_tag", "source_universe_count", "completed_symbols", "snapshot_events")}, indent=2), flush=True)
    if not args.snapshot_only:
        run_pipeline(root, manifest)
    else:
        print("SNAPSHOT ONLY: no context reconstruction or portfolio backtest executed.")


if __name__ == "__main__":
    main()
