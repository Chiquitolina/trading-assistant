#!/usr/bin/env python3
"""Candidate V2 historical replay with the dashboard's Redis HTF Room window caps.

Sidecar runner. Does NOT edit dashboard/app.py, replay_v2_all_symbols.py,
Redis, or original historical run files. The canonical historical detector and
execution remain unchanged; only the *room* window inside
_lh_exact_native_context is reduced to the confirmed producer retention caps.

Usage (dry-run):
    python3 replay_v2_dashboard_fidelity.py --start 2026-10-03 --end 2026-10-10 \
      --symbols-file /tmp/v2_oct_pilot_symbols.txt --expect-symbols 1

Usage (execute): add --run
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pandas as pd

MODE = "dashboard-fidelity"
MODE_VERSION = "room_caps_v1"
FROZEN_CAPS = {"30m": 240, "1h": 168, "4h": 150}
HISTORICAL_REQUESTS = {"30m": 1000, "1h": 750, "4h": 500}
RUN_SUFFIX = "_df_room_240_168_150_v1"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_original_runner(root: Path):
    path = root / "replay_v2_all_symbols.py"
    if not path.is_file():
        raise RuntimeError(f"Original canonical runner not found: {path}")
    spec = importlib.util.spec_from_file_location("_canonical_replay_v2_original", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load canonical runner: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("run", "extract_canonical_engine", "atomic_json", "load_symbols"):
        if not hasattr(module, name):
            raise RuntimeError(f"Original runner no longer exposes {name}; STOP")
    return module, path


def verify_frozen_retention():
    # Read-only import of the same constants used by the running publisher.
    from engine.live.data.redis_market_data_protocol import HISTORY_MAXLEN
    from engine.live.data.redis_market_data_publisher import HISTORY_TARGET_CANDLES

    actual = {
        tf: min(
            int(HISTORY_MAXLEN),
            int(HISTORY_TARGET_CANDLES.get(tf, HISTORY_MAXLEN)),
            int(HISTORICAL_REQUESTS[tf]),
        )
        for tf in FROZEN_CAPS
    }
    if actual != FROZEN_CAPS:
        raise RuntimeError(
            "Redis retention configuration changed; refusing to silently change "
            f"dashboard-fidelity contract. Expected {FROZEN_CAPS}; actual {actual}."
        )
    return actual


def patch_room_only(app_path: Path, namespace: dict) -> str:
    """Replace ONE AST reference in the HTF Room block; leave RSI unchanged.

    Validate the exact original structure so a dashboard refactor cannot
    accidentally redirect the patch to RSI or another computation.
    """
    tree = ast.parse(app_path.read_text(encoding="utf-8"), filename=str(app_path))
    matches = [
        x for x in tree.body
        if isinstance(x, ast.FunctionDef) and x.name == "_lh_exact_native_context"
    ]
    if len(matches) != 1:
        raise RuntimeError("Cannot locate unique canonical native context function")

    node = copy.deepcopy(matches[0])
    node.decorator_list = []
    found_loops = 0
    patched_calls = 0
    for loop in ast.walk(node):
        if not isinstance(loop, ast.For) or not isinstance(loop.target, ast.Name):
            continue
        if loop.target.id != "tf" or not isinstance(loop.iter, (ast.List, ast.Tuple)):
            continue
        try:
            values = [ast.literal_eval(e) for e in loop.iter.elts]
        except (ValueError, TypeError):
            continue
        if values != ["30m", "1h", "4h"]:
            continue
        found_loops += 1
        for child in ast.walk(loop):
            if not isinstance(child, ast.Call) or not isinstance(child.func, ast.Name):
                continue
            if child.func.id != "_lh_closed_tail" or len(child.args) != 4:
                continue
            arg = child.args[3]
            if (
                not isinstance(arg, ast.Subscript)
                or not isinstance(arg.value, ast.Name)
                or arg.value.id != "_LH_CONTEXT_LIMITS"
                or not isinstance(arg.slice, ast.Name)
                or arg.slice.id != "tf"
            ):
                raise RuntimeError("Native Room tail source changed; STOP, inspect manually")
            child.args[3] = ast.copy_location(
                ast.Subscript(
                    value=ast.Name(id="_DASHBOARD_ROOM_CAPS", ctx=ast.Load()),
                    slice=ast.Name(id="tf", ctx=ast.Load()),
                    ctx=ast.Load(),
                ),
                arg,
            )
            patched_calls += 1

    if (found_loops, patched_calls) != (1, 1):
        raise RuntimeError(
            "Canonical room loop changed: "
            f"found_loops={found_loops}, patched_calls={patched_calls}. STOP."
        )

    namespace["_DASHBOARD_ROOM_CAPS"] = dict(FROZEN_CAPS)
    ast.fix_missing_locations(node)
    mod = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, str(app_path), "exec"), namespace)

    patched = namespace.get("_lh_exact_native_context")
    if patched is None or patched.__globals__ is not namespace:
        raise RuntimeError("Native context override was not installed")

    return hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()


def audit_against_monitor(events_path: Path, monitor_path: Path, symbols: set[str],
                          start_ms: int, end_ms: int) -> None:
    if not events_path.is_file():
        print("AUDIT: replay events.csv does not exist yet")
        return
    if not monitor_path.is_file():
        raise RuntimeError(f"Monitor not found: {monitor_path}")
    hist = pd.read_csv(events_path, low_memory=False)
    live = pd.read_csv(monitor_path, low_memory=False)
    col = "candidate_v2_event_key"
    required = {col, "symbol", "retest_timestamp", "nearest_opposing_room_pct"}
    for label, frame in (("replay", hist), ("monitor", live)):
        if not required.issubset(frame.columns):
            raise RuntimeError(f"{label} lacks {sorted(required - set(frame.columns))}")

    def scoped(frame):
        timestamps = pd.to_numeric(frame["retest_timestamp"], errors="coerce")
        return frame.loc[
            frame["symbol"].astype(str).isin(symbols)
            & timestamps.ge(start_ms)
            & timestamps.le(end_ms),
            [col, "nearest_opposing_room_pct"],
        ].copy()

    hist = scoped(hist)
    live = scoped(live)
    if hist[col].duplicated().any() or live[col].duplicated().any():
        raise RuntimeError("Duplicate Event ID in replay or monitor; STOP")
    joined = live.merge(hist, on=col, how="left", indicator=True,
                        suffixes=("_monitor", "_replay"), validate="one_to_one")
    import numpy as np
    monitor_room = pd.to_numeric(joined["nearest_opposing_room_pct_monitor"], errors="coerce")
    replay_room = pd.to_numeric(joined["nearest_opposing_room_pct_replay"], errors="coerce")
    both_missing = monitor_room.isna() & replay_room.isna()
    room_match = both_missing | (monitor_room.sub(replay_room).abs().le(1e-6))
    found = joined["_merge"].eq("both")
    correct = found & room_match
    print(f"AUDIT: replay events={len(hist)}; persisted monitor events={len(live)}")
    print(f"AUDIT: Event ID recall={int(found.sum())}/{len(live)}")
    print(f"AUDIT: HTF Room matched={int(correct.sum())}/{len(live)} (absolute tolerance 1e-6 pct pts)")
    for row in joined.loc[~correct].head(15).itertuples(index=False):
        print("AUDIT DIFFERENCE:", row)
    if len(live) == 0:
        print("AUDIT: no reference monitor events in selected window")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--start", default="2026-10-03")
    parser.add_argument("--end", default="2026-10-10")
    parser.add_argument("--symbols-file", type=Path)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--expect-symbols", type=int, default=290)
    parser.add_argument("--batch-size", type=int, default=290)
    parser.add_argument("--run", action="store_true", help="Actually reconstruct historical events")
    parser.add_argument("--verify-monitor", type=Path,
                        help="After execution compare persisted V2 monitor Event IDs and HTF Room")
    args = parser.parse_args()
    if args.batch_size < 1 or args.limit < 0:
        parser.error("batch-size must be >=1 and limit >=0")
    root = args.project.resolve()

    import sys
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    caps = verify_frozen_retention()
    base_runner, base_path = load_original_runner(root)
    app_path = root / "dashboard/app.py"
    if not app_path.is_file():
        raise RuntimeError(f"Missing dashboard source: {app_path}")

    original_extract = base_runner.extract_canonical_engine
    original_atomic = base_runner.atomic_json
    base_run_sha = sha256_file(base_path)
    app_sha = sha256_file(app_path)

    def isolated_extract(app, project):
        namespace, canonical_hash = original_extract(app, project)
        new_context_hash = patch_room_only(app, namespace)
        original_id = namespace["_lh_run_id"]
        namespace["_lh_run_id"] = lambda *a: original_id(*a) + RUN_SUFFIX
        combined_hash = hashlib.sha256(json.dumps({
            "canonical_ast": canonical_hash,
            "patched_context_ast": new_context_hash,
            "mode": MODE_VERSION,
            "caps": FROZEN_CAPS,
            "original_runner_sha256": base_run_sha,
        }, sort_keys=True).encode()).hexdigest()
        return namespace, combined_hash

    def tagged_atomic(data, destination):
        if destination.name == "config.json" and isinstance(data, dict):
            data = {**data, "room_mode": MODE, "room_mode_version": MODE_VERSION,
                    "htf_room_caps": dict(caps), "original_runner_sha256": base_run_sha,
                    "dashboard_app_sha256": app_sha}
        return original_atomic(data, destination)

    base_runner.extract_canonical_engine = isolated_extract
    base_runner.atomic_json = tagged_atomic
    try:
        symbols = base_runner.load_symbols(root, args.symbols_file)
        if args.limit:
            symbols = symbols[:args.limit]
        if args.expect_symbols and len(symbols) != args.expect_symbols:
            raise RuntimeError(f"Expected {args.expect_symbols} symbols, got {len(symbols)}")
        start_ms = int(pd.Timestamp(args.start, tz="UTC").timestamp() * 1000)
        end_ms = int((pd.Timestamp(args.end, tz="UTC") + pd.Timedelta(days=1)
                      - pd.Timedelta(milliseconds=1)).timestamp() * 1000)
        ns, _ = isolated_extract(app_path, root)
        run_id = ns["_lh_run_id"](start_ms, end_ms,
                                  base_runner.UNIVERSE_MODE, symbols,
                                  base_runner.ROOM_WARMUP_DAYS)
        run_dir = root / "reports/candidate_long_horizon/runs" / run_id
        cfg = run_dir / "config.json"
        if cfg.is_file():
            previous = json.loads(cfg.read_text(encoding="utf-8"))
            if previous.get("room_mode") != MODE or previous.get("htf_room_caps") != caps:
                raise RuntimeError(f"Unexpected previous run configuration: {cfg}; STOP")
        print("ROOM MODE:", MODE)
        print("FROZEN HTF ROOM CAPS:", caps)
        print("RUN ISOLATION:", run_id)
        base_runner.run(
            root, start=args.start, end=args.end, limit=args.limit,
            expect_symbols=args.expect_symbols, batch_size=args.batch_size,
            symbols_path=args.symbols_file, execute=args.run,
        )
        if args.verify_monitor and args.run:
            audit_against_monitor(run_dir / "events.csv", args.verify_monitor,
                                  set(symbols), start_ms, end_ms)
    finally:
        base_runner.extract_canonical_engine = original_extract
        base_runner.atomic_json = original_atomic


if __name__ == "__main__":
    main()
