#!/usr/bin/env python3
"""Offline TP +1..+5 / SL -1..-5 matrix over frozen Horizontal First Touch labels.

Read-only source DBs; no Redis, Binance calls, order placement, or changes to LIVE.
Each event requires 180 verified contiguous 1m bars from its next 1m OPEN.
The exact same eligible cohort is evaluated across ALL 25 TP/SL combinations.
Uses published Runner Discovery path/auto/recovery provenance functions.

The recorded side is ORIGINAL_OPEN; INVERSE tests the opposite side as a
research comparator, NOT an independently detected entry. These are idealized
structural entries; historical RECOVERED_WINDOW is not contemporaneously live.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys
import time
import zlib
import struct

import reaction_lab_runner_discovery_all_touches_v1 as paths_tool
import reaction_lab_first_touch_paths_v2 as simulator

VERSION = "universal-runner-horizontal-tp-sl-v1.0"
STATES = ("REACTION", "TOUCH_INDECISIVE", "TOUCH_FAILED")
GROUPS = ("PERSISTENT_LEVEL_NEAR_PRE", "NO_PERSISTENT_LEVEL_NEAR_PRE")
PERSPECTIVES = ("ORIGINAL_OPEN", "INVERSE")
GRID = tuple((tp, sl) for tp in range(1, 6) for sl in range(1, 6))
HORIZON = 180
MINUTE_MS = 60_000
BASE = Path("reports/reaction_lab")
OUTPUT_ROOT = BASE / "universal_runner_horizontal_tp_sl_matrix_v1" / "runs"

EVENT_FIELDS = (
    "touch_key", "symbol", "side", "status", "group_pre", "observation_type",
    "retest_ts", "known_ts", "path_quality", "path_source", "path_count",
    "entry_open", "first_seen_ms", "entry_observable_at_next_open"
)
OUTCOME_FIELDS = (
    "touch_key", "symbol", "side", "status", "group_pre", "observation_type",
    "perspective", "tp_pct", "sl_pct", "horizon_min", "outcome",
    "exit_minute", "gross_return_pct", "net_assumed_pct", "path_source"
)
STAT_FIELDS = (
    "status", "side", "group_pre", "perspective", "horizon_min", "tp_pct", "sl_pct",
    "N_labeled", "N_evaluable", "N_missing_path", "coverage_pct",
    "N_tp_first", "N_sl_first", "N_sl_ambiguous", "N_time_exit",
    "tp_first_pct", "mean_gross_pct", "mean_net_assumed_pct", "positive_net_pct"
)
COMPARE_FIELDS = (
    "status", "side", "perspective", "horizon_min", "tp_pct", "sl_pct",
    "N_near", "N_far", "coverage_near_pct", "coverage_far_pct",
    "tp_near_pct", "tp_far_pct", "delta_tp_pp",
    "mean_net_near_pct", "mean_net_far_pct", "delta_mean_net_pp"
)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as inp:
        for chunk in iter(lambda: inp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def frozen_labels(run_dir):
    run_dir = Path(run_dir)
    audit_path = run_dir / "audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    if audit.get("no_outcomes_used_for_labels") is not True:
        raise ValueError("PRE labels are not certified outcome-blind")
    if audit.get("source_mode") != "OFFLINE_FROZEN":
        raise ValueError("Expected OFFLINE_FROZEN horizontal history")
    for filename in ("event_labels.csv", "candidate_levels.csv"):
        expected = audit.get("sha256", {}).get(filename)
        if not expected or digest(run_dir / filename) != expected:
            raise ValueError(f"Frozen label source SHA mismatch: {filename}")
    with (run_dir / "event_labels.csv").open(newline="", encoding="utf-8-sig") as inp:
        labels = list(csv.DictReader(inp))
    keys = set()
    for row in labels:
        key = row.get("touch_key")
        if not key or key in keys:
            raise ValueError("Duplicate or missing touch_key in frozen labels")
        keys.add(key)
        if row.get("status") not in STATES or row.get("side") not in ("LONG", "SHORT"):
            raise ValueError("Unexpected status or side in frozen labels")
        if int(row["known_ts"]) != int(row["retest_ts"]) + MINUTE_MS:
            raise ValueError("Noncausal frozen known_ts")
        if row.get("group_pre") not in GROUPS and row.get("group_pre") != "UNKNOWN_HISTORY":
            raise ValueError("Unknown frozen horizontal classification")
    return labels, audit


def existing_1pct_results(db, known_keys):
    """Compare newly simulated 1/1/180 outcomes with frozen V2 outcomes."""
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'outcomes' not in tables:
        return {}
    paths_tool.check_columns(db, 'outcomes', (
        'touch_key', 'horizon', 'perspective', 'tp', 'sl', 'outcome'))
    baseline = {}
    for key, perspective, outcome in db.execute(
        "SELECT touch_key,perspective,outcome FROM outcomes "
        "WHERE horizon=180 AND tp=1.0 AND sl=1.0 "
        "AND perspective IN ('ORIGINAL_OPEN','INVERSE')"
    ):
        if key in known_keys:
            baseline[(key, perspective)] = outcome
    return baseline


def auto_index_from_db(auto_db, identities):
    idx = defaultdict(list)
    if auto_db is None:
        return idx
    paths_tool.check_columns(auto_db, "events", (
        "event_key", "symbol", "side", "retest_ts", "entry_ts", "count", "path_blob"))
    for key, symbol, side, retest, entry, count in auto_db.execute(
        "SELECT event_key,symbol,side,retest_ts,entry_ts,count FROM events"
    ):
        token = (str(symbol).upper(), str(side).upper(), int(retest))
        if token in identities:
            idx[token].append((key, entry, count))
    return idx


def load_bars(row, first_db, auto_db, auto_idx, historical_idx, historic_conns):
    """Equivalent provenance precedence to Runner Discovery, subset-optimized.

    Returns (validated bars, provenance, quality, first_seen_ms). No source
    conflicts are silently converted into valid trading outcomes.
    """
    key = row["touch_key"]
    record = first_db.execute(
        "SELECT symbol,side,status,retest_ts,entry_ts,first_seen_ms,state,count,path_blob "
        "FROM paths WHERE touch_key=?", (key,)
    ).fetchone()
    if record is None:
        return [], "NO_PATH_ROW", "NO_PATH", None
    symbol, side, status, retest, entry, seen, state, count, blob = record
    label_identity = (row["symbol"], row["side"], row["status"], int(row["retest_ts"]))
    if (symbol, side, status, int(retest)) != label_identity or int(entry) != int(row["known_ts"]):
        return [], "IDENTITY_MISMATCH", "BAD_IDENTITY", seen
    try:
        ft_bars = paths_tool.decode_checked(blob, count, entry)
    except (ValueError, TypeError, zlib.error, struct.error):
        return [], "FIRST_TOUCH_V2", "BAD_FT_PATH", seen
    token = (symbol.upper(), side.upper(), int(retest))
    bars, provenance = paths_tool.match_auto(
        ft_bars, auto_idx.get(token, []), auto_db, entry
    )
    unsafe_markers = ("BAD_", "MISMATCH", "CONFLICT", "AMBIGUOUS")
    historical = historical_idx.get(key)
    if historical and len(bars) < 360 and not any(tag in provenance for tag in unsafe_markers):
        db_path, hs, hd, original_status, hist_retest, hist_entry = historical
        if (symbol, side, status, int(retest), int(entry)) != (
            hs, hd, original_status, hist_retest, hist_entry
        ):
            return [], "HISTORICAL_IDENTITY_MISMATCH", "BAD_IDENTITY", seen
        db = historic_conns.get(db_path)
        if db is None:
            db = paths_tool.ro_connect(db_path)
            historic_conns[db_path] = db
        recovered_row = db.execute(
            "SELECT path_blob FROM recovered_paths WHERE touch_key=?", (key,)
        ).fetchone()
        if recovered_row is None:
            return [], "HISTORICAL_RECORD_MISSING", "BAD_HISTORICAL", seen
        try:
            bars = paths_tool.verify_historical_overlay(ft_bars if len(ft_bars) >= len(bars) else bars, recovered_row[0], entry)
        except (ValueError, TypeError, zlib.error, struct.error):
            return [], "HISTORICAL_OHLC_CONFLICT", "SOURCE_REVIEW_REQUIRED", seen
        provenance = "HISTORICAL_BINANCE_V11"
    if any(tag in provenance for tag in unsafe_markers):
        return [], provenance, "SOURCE_REVIEW_REQUIRED", seen
    if len(bars) < HORIZON:
        return bars, provenance, "INSUFFICIENT_180M", seen
    # All paths are validated in decode_checked, including historical overlay.
    # Recheck specifically the 180m timestamp prefix to fail closed.
    if any(bar[0] != int(entry) + i * MINUTE_MS for i, bar in enumerate(bars[:HORIZON])):
        return [], provenance, "BAD_180M_CONTIGUITY", seen
    return bars[:HORIZON], provenance, "FULL_180M", seen


def simulate_180(bars, side, tp, sl):
    if len(bars) != HORIZON:
        raise ValueError("180 bars required")
    return simulator.simulate(bars, side, float(tp), float(sl))


def bucket_keys(row, perspective, tp, sl):
    # Keep each event counted once in each requested aggregation level.
    status, side, group = row["status"], row["side"], row["group_pre"]
    return (
        (status, side, group, perspective, tp, sl),
        (status, "ALL_SIDES", group, perspective, tp, sl),
        ("ALL_STATUSES", "ALL_SIDES", group, perspective, tp, sl),
    )


def stats_record(key, data, labeled_count, cost_pp):
    status, side, group, perspective, tp, sl = key
    n = data["N_evaluable"]
    tps = data["N_tp_first"]
    return {
        "status": status, "side": side, "group_pre": group, "perspective": perspective,
        "horizon_min": HORIZON, "tp_pct": tp, "sl_pct": sl,
        "N_labeled": labeled_count, "N_evaluable": n,
        "N_missing_path": labeled_count - n,
        "coverage_pct": round(100 * n / labeled_count, 3) if labeled_count else None,
        "N_tp_first": tps, "N_sl_first": data["N_sl_first"],
        "N_sl_ambiguous": data["N_sl_ambiguous"], "N_time_exit": data["N_time_exit"],
        "tp_first_pct": round(100 * tps / n, 3) if n else None,
        "mean_gross_pct": round(data["sum_gross"] / n, 5) if n else None,
        "mean_net_assumed_pct": round(data["sum_gross"] / n - cost_pp, 5) if n else None,
        "positive_net_pct": round(100 * data["N_positive_net"] / n, 3) if n else None,
    }


def compare_records(stats_rows):
    idx = {(r["status"], r["side"], r["group_pre"], r["perspective"], r["tp_pct"], r["sl_pct"]): r
           for r in stats_rows}
    records = []
    for status in (*STATES, "ALL_STATUSES"):
        for side in (("ALL_SIDES",) if status == "ALL_STATUSES" else ("ALL_SIDES", "LONG", "SHORT")):
            for perspective in PERSPECTIVES:
                for tp, sl in GRID:
                    a = idx.get((status, side, GROUPS[0], perspective, tp, sl))
                    b = idx.get((status, side, GROUPS[1], perspective, tp, sl))
                    if not a or not b:
                        continue
                    at, bt = a["tp_first_pct"], b["tp_first_pct"]
                    an, bn = a["mean_net_assumed_pct"], b["mean_net_assumed_pct"]
                    records.append({
                        "status": status, "side": side, "perspective": perspective,
                        "horizon_min": HORIZON, "tp_pct": tp, "sl_pct": sl,
                        "N_near": a["N_evaluable"], "N_far": b["N_evaluable"],
                        "coverage_near_pct": a["coverage_pct"],
                        "coverage_far_pct": b["coverage_pct"],
                        "tp_near_pct": at, "tp_far_pct": bt,
                        "delta_tp_pp": round(at - bt, 3) if at is not None and bt is not None else None,
                        "mean_net_near_pct": an, "mean_net_far_pct": bn,
                        "delta_mean_net_pp": round(an - bn, 5) if an is not None and bn is not None else None,
                    })
    return records


def write_rows(path, rows, columns):
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run_matrix(project, run_dir, *, output_dir=None, cost_pp=0.20, no_recovery=False):
    project = Path(project).resolve()
    run_dir = Path(run_dir).resolve()
    if not math.isfinite(cost_pp) or cost_pp < 0:
        raise ValueError("Invalid nonnegative cost")
    labels, audit = frozen_labels(run_dir)
    base = project / BASE
    ft_db = base / "first_touch_paths_v2.sqlite3"
    auto_path = base / "auto_paths_v1.sqlite3"
    first = paths_tool.ro_connect(ft_db)
    auto = paths_tool.ro_connect(auto_path) if auto_path.is_file() else None
    historic_conns = {}
    try:
        paths_tool.check_columns(first, "paths", (
            "touch_key", "symbol", "side", "status", "retest_ts", "entry_ts",
            "first_seen_ms", "count", "path_blob"
        ))
        known_keys = {r["touch_key"] for r in labels}
        baseline = existing_1pct_results(first, known_keys)
        parity_compared = 0
        identity_set = {(r["symbol"].upper(), r["side"].upper(), int(r["retest_ts"])) for r in labels}
        auto_idx = auto_index_from_db(auto, identity_set)
        recovery_root = base / "first_touch_historical_recovery_v11"
        hist_idx, batches = ({}, 0) if no_recovery else paths_tool.recovery_index(recovery_root)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        output_dir = Path(output_dir).resolve() if output_dir else (project / OUTPUT_ROOT / stamp)
        output_dir.mkdir(parents=True, exist_ok=False)
        labels_per = Counter()
        for row in labels:
            if row["group_pre"] in GROUPS:
                for status, side, group in (
                    (row["status"], row["side"], row["group_pre"]),
                    (row["status"], "ALL_SIDES", row["group_pre"]),
                    ("ALL_STATUSES", "ALL_SIDES", row["group_pre"]),
                ):
                    labels_per[(status, side, group)] += 1
        aggregates = defaultdict(Counter)
        quality_counts = Counter()
        source_counts = Counter()
        outcomes_written = 0
        full_count = 0
        with (output_dir / "event_coverage.csv").open("w", newline="", encoding="utf-8") as coverage_fp, \
             (output_dir / "event_outcomes_180m.csv").open("w", newline="", encoding="utf-8") as outcomes_fp:
            cw = csv.DictWriter(coverage_fp, fieldnames=EVENT_FIELDS)
            ow = csv.DictWriter(outcomes_fp, fieldnames=OUTCOME_FIELDS)
            cw.writeheader(); ow.writeheader()
            for i, row in enumerate(labels, 1):
                bars, source, quality, first_seen = load_bars(
                    row, first, auto, auto_idx, hist_idx, historic_conns
                )
                quality_counts[quality] += 1
                source_counts[source] += 1
                eligible = quality == "FULL_180M" and row["group_pre"] in GROUPS
                if eligible:
                    full_count += 1
                observed_next_open = (
                    first_seen is not None
                    and row.get("observation_type") == "LIVE_OBSERVED"
                    and int(first_seen) <= int(row["known_ts"])
                )
                cw.writerow({
                    **{k: row.get(k) for k in EVENT_FIELDS},
                    "path_quality": quality, "path_source": source, "path_count": len(bars),
                    "entry_open": bars[0][1] if bars else None,
                    "first_seen_ms": first_seen,
                    "entry_observable_at_next_open": "YES_PROXY" if observed_next_open else "NO",
                })
                if not eligible:
                    continue
                for perspective in PERSPECTIVES:
                    trade_side = row["side"] if perspective == "ORIGINAL_OPEN" else (
                        "SHORT" if row["side"] == "LONG" else "LONG"
                    )
                    for tp, sl in GRID:
                        result, minute, gross = simulate_180(bars, trade_side, tp, sl)
                        if tp == 1 and sl == 1 and (row["touch_key"], perspective) in baseline:
                            original_outcome = baseline[(row["touch_key"], perspective)]
                            if result != original_outcome:
                                raise ValueError(
                                    f"1/1/180 parity mismatch for {row['touch_key']} "
                                    f"{perspective}: old={original_outcome}, new={result}"
                                )
                            parity_compared += 1
                        net = gross - cost_pp
                        ow.writerow({
                            "touch_key": row["touch_key"], "symbol": row["symbol"],
                            "side": row["side"], "status": row["status"],
                            "group_pre": row["group_pre"],
                            "observation_type": row["observation_type"],
                            "perspective": perspective, "tp_pct": tp, "sl_pct": sl,
                            "horizon_min": HORIZON, "outcome": result,
                            "exit_minute": minute, "gross_return_pct": round(gross, 6),
                            "net_assumed_pct": round(net, 6), "path_source": source,
                        })
                        outcomes_written += 1
                        for key in bucket_keys(row, perspective, tp, sl):
                            b = aggregates[key]
                            b["N_evaluable"] += 1
                            b["sum_gross"] += gross
                            b["N_positive_net"] += net > 0
                            b["N_" + result.lower()] += 1
                if i % 250 == 0:
                    print("UNIVERSAL_MATRIX_PROGRESS " + json.dumps({
                        "labels_seen": i, "labels_total": len(labels),
                        "complete_180": full_count
                    }), flush=True)
        # Make all 25 cells visible, even for groups with zero eligible paths.
        result_rows = []
        for (status, side, group), labeled in sorted(labels_per.items()):
            for perspective in PERSPECTIVES:
                for tp, sl in GRID:
                    key = (status, side, group, perspective, tp, sl)
                    result_rows.append(stats_record(key, aggregates[key], labeled, cost_pp))
        by_side = [r for r in result_rows if r["side"] in ("LONG", "SHORT")]
        by_status = [r for r in result_rows if r["side"] == "ALL_SIDES" and r["status"] in STATES]
        all_statuses = [r for r in result_rows if r["status"] == "ALL_STATUSES"]
        comparison = compare_records(result_rows)
        write_rows(output_dir / "matrix_by_status_side.csv", by_side, STAT_FIELDS)
        write_rows(output_dir / "matrix_by_status.csv", by_status, STAT_FIELDS)
        write_rows(output_dir / "matrix_all_statuses.csv", all_statuses, STAT_FIELDS)
        write_rows(output_dir / "comparison_near_vs_far_180m.csv", comparison, COMPARE_FIELDS)
        manifest = {
            "schema": VERSION, "created_utc": datetime.now(timezone.utc).isoformat(),
            "label_run": str(run_dir),
            "frozen_sha256": {
                "event_labels.csv": audit["sha256"]["event_labels.csv"],
                "candidate_levels.csv": audit["sha256"]["candidate_levels.csv"],
            },
            "settings": {"tp_pct": list(range(1, 6)), "sl_pct": list(range(1, 6)),
                         "horizon_min": HORIZON, "entry": "NEXT_1M_OPEN_AFTER_RETEST_CLOSE",
                         "perspectives": list(PERSPECTIVES), "same_candle_both": "SL_AMBIGUOUS_CONSERVATIVE",
                         "roundtrip_cost_pp_assumption": cost_pp,
                         "require_full_contiguous_180m": True},
            "events_labeled": len(labels), "events_full_180m_near_far": full_count,
            "events_unknown_horizontal": sum(r["group_pre"] == "UNKNOWN_HISTORY" for r in labels),
            "quality_counts": dict(quality_counts), "source_counts": dict(source_counts),
            "historical_recovery_batches_verified": batches,
            "historical_index_entries": len(hist_idx),
            "outcome_rows": outcomes_written, "comparisons": len(comparison),
            "existing_tp1_sl1_180_parity_compared": parity_compared,
            "existing_tp1_sl1_180_parity_mismatches": 0,
            "notes": [
                "Descriptive in-sample 5x5 grid, NOT 25 independent trading strategies.",
                "All 25 cells use same complete-path cohort within each group; absent paths excluded, not losses.",
                "The initial status and near/far PRE label never depend on future outcomes.",
                "Historical RECOVERED_WINDOW is a retrospective hypothetical next-open entry, not an observed fill.",
                "Cost is a fixed illustrative percentage-point deduction, not exchange-execution fees.",
                "No slippage, funding, position overlap, portfolio sizing, or out-of-sample validation.",
                "The full 180m path is required even when a barrier might have hit earlier, for comparable denominators."
            ],
            "output_dir": str(output_dir),
        }
        filenames = (
            "event_coverage.csv", "event_outcomes_180m.csv", "matrix_by_status_side.csv",
            "matrix_by_status.csv", "matrix_all_statuses.csv", "comparison_near_vs_far_180m.csv"
        )
        manifest["files_sha256"] = {f: digest(output_dir / f) for f in filenames}
        (output_dir / "audit.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8"
        )
        return manifest
    finally:
        first.close()
        if auto is not None: auto.close()
        for db in historic_conns.values(): db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--run-dir", type=Path, required=True, help="Existing immutable PRE label run")
    parser.add_argument("--cost-pp", type=float, default=0.20,
                        help="Illustrative roundtrip cost, percentage points (default 0.20)")
    parser.add_argument("--no-recovery", action="store_true", help="Disable historical recovered batches")
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    result = run_matrix(args.project, args.run_dir, cost_pp=args.cost_pp,
                        no_recovery=args.no_recovery, output_dir=args.output_dir)
    print("UNIVERSAL_MATRIX_RESULT " + json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
