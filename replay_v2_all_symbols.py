#!/usr/bin/env python3
"""CLI for canonical Candidate V2 historical REACTION replay, without Streamlit.

The event calculation is extracted as-is from dashboard/app.py's _lh_* functions.
Runs are resumable by symbol and share the Historical Replay on-disk format.
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import fcntl

import numpy as np
import pandas as pd
import requests

START_DEFAULT = "2026-09-07"
END_DEFAULT = "2026-09-30"
UNIVERSE_MODE = "Current research universe"
ROOM_WARMUP_DAYS = 120


def atomic_json(data, destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, destination)


def extract_canonical_engine(app_path: Path, root: Path):
    """Use original engine functions verbatim; do not execute app.py's UI."""
    code = app_path.read_text(encoding="utf-8")
    tree = ast.parse(code, filename=str(app_path))
    funcs = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    requested = {"_lh_symbol_month_events", "_lh_run_id", "_lh_month_ranges",
                 "_lh_append_events", "_lh_atomic_json_write"}
    selected = set()
    while requested:
        name = requested.pop()
        if name in selected:
            continue
        if name not in funcs:
            raise RuntimeError(f"Missing canonical dashboard function: {name}")
        selected.add(name)
        referenced = {node.id for node in ast.walk(funcs[name])
                      if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
        requested.update(referenced.intersection(funcs).difference(selected))

    method_hash = hashlib.sha256(
        "".join(ast.dump(funcs[name]) for name in sorted(selected)).encode()
    ).hexdigest()
    module = ast.Module(body=[copy.deepcopy(funcs[name]) for name in sorted(selected)], type_ignores=[])
    for node in module.body:
        node.decorator_list = []
    ast.fix_missing_locations(module)

    sys.path.insert(0, str(root))
    from engine.live.research.swing_detector import SwingDetector
    globals_ = {
        "pd": pd, "np": np, "Path": Path, "requests": requests,
        "time": time, "json": json, "hashlib": hashlib,
        "SwingDetector": SwingDetector,
        "CANDIDATE_LONG_HORIZON_CACHE": root / "reports/candidate_long_horizon/candle_cache",
        "_LH_BINANCE_KLINES_URL": "https://fapi.binance.com/fapi/v1/klines",
        "_LH_CONTEXT_LIMITS": {"1m":5000, "5m":2500, "15m":1500,
                               "30m":1000, "1h":750, "4h":500},
    }
    # Refuse silent drift of the two constants replicated from app.py.
    app_constants = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        t = node.targets[0]
        if isinstance(t, ast.Name) and t.id in ("_LH_BINANCE_KLINES_URL", "_LH_CONTEXT_LIMITS"):
            app_constants[t.id] = ast.literal_eval(node.value)
    for key in ("_LH_BINANCE_KLINES_URL", "_LH_CONTEXT_LIMITS"):
        if app_constants.get(key) != globals_[key]:
            raise RuntimeError(f"Dashboard historical setting {key} changed; update runner after review")

    exec(compile(module, str(app_path), "exec"), globals_)
    return globals_, method_hash


def load_symbols(root: Path, symbols_path: Path | None):
    if symbols_path:
        if symbols_path.suffix.lower() == ".json":
            doc = json.loads(symbols_path.read_text(encoding="utf-8"))
            items = doc["symbols"] if isinstance(doc, dict) else doc
        else:
            items = symbols_path.read_text(encoding="utf-8").replace(",", "\n").splitlines()
    else:
        sys.path.insert(0, str(root))
        from config.strategies.v1 import SYMBOLS
        items = SYMBOLS
    return sorted({str(s).strip().upper() for s in items if str(s).strip()})


def run(root: Path, *, start: str, end: str, limit: int, expect_symbols: int,
        batch_size: int, symbols_path: Path | None, execute: bool):
    app = root / "dashboard/app.py"
    functions, method_hash = extract_canonical_engine(app, root)
    symbols = load_symbols(root, symbols_path)
    if limit:
        symbols = symbols[:limit]
    if expect_symbols and len(symbols) != expect_symbols:
        raise RuntimeError(f"Expected {expect_symbols} symbols, got {len(symbols)}. Stopping; no silent subset replay.")
    start_ms = int(pd.Timestamp(start, tz="UTC").timestamp()*1000)
    end_ms = int((pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1)).timestamp()*1000)
    if start_ms > end_ms:
        raise RuntimeError("Start must be <= end")
    if end_ms >= pd.Timestamp.now(tz="UTC").timestamp()*1000:
        raise RuntimeError("The end date must be fully historical")
    months = functions["_lh_month_ranges"](start_ms, end_ms)
    run_id = functions["_lh_run_id"](start_ms,end_ms,UNIVERSE_MODE,symbols,ROOM_WARMUP_DAYS)
    home = root / "reports/candidate_long_horizon/runs" / run_id
    config_file = home / "config.json"
    state_file = home / "state.json"
    events_file = home / "events.csv"
    settings = {
        "run_id": run_id,
        "start_ms": start_ms, "end_ms": end_ms, "start_date": start,
        "end_date": end, "universe_mode": UNIVERSE_MODE,
        "symbols": symbols, "room_warmup_days": ROOM_WARMUP_DAYS,
        "months": months,
        "strategy": {"v1": "Candidate V1 + Strength > 0", "v2": "REACTION Base",
                     "tp_pct": .5, "sl_pct": 3., "horizon_min": 180,
                     "priority": "Most HTF Room → Strength", "starting_equity":200.,
                     "slots":1, "leverage":3., "margin_pct":80.},
        "canonical_engine_ast_sha256": method_hash,
    }
    if config_file.exists():
        saved = json.loads(config_file.read_text(encoding="utf-8"))
        for field in ("run_id", "start_ms", "end_ms", "symbols", "room_warmup_days", "months"):
            if saved.get(field) != settings[field]:
                raise RuntimeError(f"Existing run {run_id} has mismatched {field}; refusing to overwrite")
        pinned = saved.get("canonical_engine_ast_sha256")
        if pinned and pinned != method_hash:
            raise RuntimeError("Canonical historical engine changed during this run; review before resuming")
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {"run_id":run_id,"processed":{},"errors":{}}
    if not isinstance(state.get("processed"), dict):
        raise RuntimeError("Unexpected historical run state format")
    done = sum(len(set(state["processed"].get(m["label"],[]))) for m in months)
    total = len(symbols)*len(months)
    print(f"RUN ID: {run_id}", flush=True)
    print(f"DATE: {start} -> {end} | MONTHS: {len(months)} | SYMBOLS: {len(symbols)}", flush=True)
    print(f"PROGRESS: {done}/{total} | FILE: {events_file}", flush=True)
    if not execute:
        print("DRY RUN. Execute with --run to rebuild the missing symbols.")
        return run_id

    home.mkdir(parents=True, exist_ok=True)
    # Lock prevents two console workers corrupting the same files. Do not run UI
    # Historical Replay concurrently; Streamlit does not use this lock.
    with (home / ".cli.lock").open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("This run already has a CLI worker") from exc
        if not config_file.exists():
            settings["created_at_utc"] = pd.Timestamp.now(tz="UTC").isoformat()
            atomic_json(settings, config_file)
        errors = state.setdefault("errors", {})
        processed = state["processed"]
        pending = [(m,s) for m in months for s in symbols if s not in set(processed.get(m["label"],[]))]
        attempted=0; successful=0; failed=0; empty_success=0; total_fresh=0
        for m, symbol in pending[:batch_size]:
            attempted += 1
            label = m["label"]
            try:
                frame = functions["_lh_symbol_month_events"](
                    symbol, int(m["start_ms"]), int(m["end_ms"]),
                    room_warmup_days=ROOM_WARMUP_DAYS, max_age_minutes=360,
                    retest_tolerance_pct=.10, min_departure_pct=.20,
                )
                # Persist the events BEFORE marking the symbol processed. A crash
                # between these writes safely replays this symbol (dedup Event ID).
                n = 0 if frame is None else len(frame)
                if n:
                    functions["_lh_append_events"](events_file, frame)
                processed.setdefault(label,[]).append(symbol)
                errors.pop(f"{label}|{symbol}", None)
                successful += 1; total_fresh += n
                if n == 0:
                    empty_success += 1
                msg = f"OK {label} {symbol} | events={n}"
            except Exception as exc:
                failed += 1
                msg = f"ERROR {label} {symbol}: {type(exc).__name__}: {exc}"
                errors[f"{label}|{symbol}"] = f"{type(exc).__name__}: {exc}"
            state["processed"] = processed
            state["last_batch"] = {
                "month":label,"attempted":attempted,"success":successful,
                "empty_success": empty_success,
                "failed":failed,"events":total_fresh,
                "finished_at_utc":pd.Timestamp.now(tz="UTC").isoformat(),
            }
            atomic_json(state,state_file)
            completed = sum(len(set(processed.get(part["label"],[]))) for part in months)
            print(f"[{completed}/{total}] {msg}",flush=True)
        print(f"FINISHED ATTEMPT: {attempted} | SUCCESS: {successful} | FAILED: {failed} | EVENTS: {total_fresh}",flush=True)
        print(f"Saved {events_file}",flush=True)
        if failed:
            print("NOTE: Failed symbols stay pending and will be retried. Inspect state.json errors.",flush=True)
    return run_id


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project",type=Path,default=Path(__file__).resolve().parent)
    p.add_argument("--start",default=START_DEFAULT)
    p.add_argument("--end",default=END_DEFAULT)
    p.add_argument("--symbols-file",type=Path, help="Optional JSON or newline text list of exact symbols")
    p.add_argument("--limit",type=int,default=0,help="0=all, else prefix/pilot")
    p.add_argument("--expect-symbols",type=int,default=290,help="Safety check; set 0 to disable")
    p.add_argument("--batch-size",type=int,default=290,help="Maximum pending symbol/month units in one call")
    p.add_argument("--run",action="store_true",help="Start processing; default only prints run info")
    args=p.parse_args()
    if args.batch_size<1 or args.limit<0:
        p.error("batch-size must be >=1 and limit >=0")
    run(args.project.resolve(),start=args.start,end=args.end,limit=args.limit,
        expect_symbols=args.expect_symbols,batch_size=args.batch_size,
        symbols_path=args.symbols_file,execute=args.run)


if __name__ == "__main__":
    main()
