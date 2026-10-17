"""Read-only, deterministic competition ledger from a frozen Runner Portfolio export.

Phase 1: reconstruct the one-slot admission decisions, their exact busy owner,
and simultaneous contests. No OHLC/shadow simulation, no new selection policy,
no exchange calls, no writes to original reports or scanner databases.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ENGINE = "candidate-competition-ledger-phase1-v1"
GUARD_REJECT = "FILTER_REJECTED_EXTREME_STRENGTH"
NON_ADMITTED = {"FILTER_REJECTED", GUARD_REJECT, "SKIPPED_NO_EQUITY", "BLOCKED_UNRESOLVED_STATE"}
TERMINAL = {"TP", "SL", "TIMEOUT", "AMBIGUOUS_BOTH_AS_SL"}
DEFAULT_TTL = {
    "Runner V1 Frozen": 15,
    "Runner Challenger 220": 30,
    "Runner Challenger 220 · Extreme Strength Guard": 30,
}
MINUTE = 60_000
BUSY_PATTERN = re.compile(r"^slot_busy_through_(\d+)$")
DECISION_COLUMNS = (
    "strategy", "event_key", "symbol", "side", "ready_ms", "room_1h",
    "departure", "return_rank", "volume_rank", "decision", "reason",
    "admitted_after_filters", "group_id", "simultaneous_eligible", "simultaneous_rank",
    "competition_type", "owner_at_decision", "owner_until_ms", "owner_stage",
    "submitted_outcome", "is_selected_at_timestamp",
)
EPISODE_COLUMNS = (
    "strategy", "owner_event_key", "owner_symbol", "owner_side", "ready_ms",
    "reserved_until_ms", "reservation_minutes", "order_outcome", "fill_ts",
    "exit_ts", "blocked_count", "blocked_event_keys",
)
GROUP_COLUMNS = (
    "strategy", "group_id", "ready_ms", "events_total", "eligible_count",
    "filtered_count", "guard_veto_count", "selected_event_key", "competition_type",
    "owner_at_group_start", "owner_until_group_start_ms", "eligible_event_keys",
)


def _read_csv(path: Path):
    if not path.is_file():
        raise FileNotFoundError(f"Required CSV missing: {path}")
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames or not {"strategy", "event_key"}.issubset(reader.fieldnames):
            raise ValueError(f"Unrecognized CSV schema: {path}")
        return list(reader)


def _sha(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _integer(row, column):
    try:
        val = int(row[column])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {column} at {row.get('event_key')}") from exc
    return val


def _number(row, column):
    try:
        val = float(row[column])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {column} at {row.get('event_key')}") from exc
    if not math.isfinite(val):
        raise ValueError(f"Nonfinite {column} at {row.get('event_key')}")
    return val


def _write_csv(path: Path, columns, rows):
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _settings(run_dir: Path, strategies):
    """Use the run's original per-profile TTL when available; otherwise
    explicit audited baseline fallback, verified by all busy-through markers.
    """
    path = run_dir / "manifest.json"
    if path.exists():
        manifest = json.loads(path.read_text(encoding="utf-8"))
        profiles = manifest.get("profiles", {})
        if not isinstance(profiles, dict):
            raise ValueError("Unexpected profiles manifest")
        ttl = {}
        for strategy in strategies:
            raw = profiles.get(strategy, {}).get("ttl")
            if not isinstance(raw, (int, float)) or not float(raw).is_integer() or raw <= 0:
                raise ValueError(f"Missing/invalid TTL in manifest for {strategy}")
            ttl[strategy] = int(raw)
        return ttl, "RUN_MANIFEST"
    if not set(strategies).issubset(DEFAULT_TTL):
        raise ValueError("Unknown strategy and no run manifest: cannot infer TTL")
    return {s: DEFAULT_TTL[s] for s in strategies}, "AUDITED_FROZEN_PROFILE_FALLBACK"


def construct(run_dir: Path, *, events_path: Path | None = None, factors_path: Path | None = None):
    """Return data tables. No IO mutations. Raise on any attribution discrepancy."""
    run_dir = Path(run_dir)
    decision_file, trade_file = run_dir / "decisions.csv", run_dir / "trades.csv"
    decisions, trades = _read_csv(decision_file), _read_csv(trade_file)
    by_strategy = defaultdict(list)
    for d in decisions:
        # Every event must have a stable row identity and timing.
        _integer(d, "ready_ms")
        _number(d, "room_1h")
        if not d["event_key"]:
            raise ValueError("Empty event_key")
        by_strategy[d["strategy"]].append(d)
    if not by_strategy:
        raise ValueError("Empty decision ledger")
    ttl_map, ttl_source = _settings(run_dir, by_strategy)

    trade_index = {}
    for t in trades:
        key = (t["strategy"], t["event_key"])
        if key in trade_index:
            raise ValueError(f"Duplicate trade: {key}")
        trade_index[key] = t

    enriched, episodes, groups, summary = [], [], [], {}
    seen_trades = set()
    for strategy, strategy_decisions in sorted(by_strategy.items()):
        rows = sorted(strategy_decisions, key=lambda d: (
            _integer(d, "ready_ms"), -_number(d, "room_1h"), d["event_key"]))
        keys = [r["event_key"] for r in rows]
        if len(set(keys)) != len(keys):
            raise ValueError(f"Duplicate decision keys for {strategy}")
        per_ready = defaultdict(list)
        for r in rows:
            per_ready[_integer(r, "ready_ms")].append(r)
        owner = None
        owner_until = -(10**30)
        episode = None
        row_output = {}
        busy_verified = 0
        simultaneous_groups = 0
        simultaneous_free_groups = 0
        for ready, at_time in sorted(per_ready.items()):
            group_id = f"{strategy}|{ready}"
            eligible = [r for r in at_time if r["decision"] not in NON_ADMITTED]
            group_owner = owner if ready < owner_until else None
            group_until = owner_until if group_owner else None
            started_free = group_owner is None
            selected = [r for r in at_time if r["decision"] == "ORDER_SUBMITTED"]
            if len(selected) > 1:
                raise ValueError(f"Multiple orders at same timestamp: {group_id}")
            if len(eligible) >= 2:
                simultaneous_groups += 1
                if started_free:
                    simultaneous_free_groups += 1
                group_type = "SIMULTANEOUS_FREE_SLOT" if started_free else "SIMULTANEOUS_PREOCCUPIED"
            elif len(eligible) == 1:
                group_type = "SINGLE_ELIGIBLE_FREE_SLOT" if started_free else "SINGLE_ELIGIBLE_PREOCCUPIED"
            else:
                group_type = "NO_ADMITTED_CANDIDATES"
            groups.append({
                "strategy": strategy, "group_id": group_id, "ready_ms": ready,
                "events_total": len(at_time), "eligible_count": len(eligible),
                "filtered_count": sum(r["decision"] == "FILTER_REJECTED" for r in at_time),
                "guard_veto_count": sum(r["decision"] == GUARD_REJECT for r in at_time),
                "selected_event_key": selected[0]["event_key"] if selected else "",
                "competition_type": group_type,
                "owner_at_group_start": group_owner["event_key"] if group_owner else "",
                "owner_until_group_start_ms": group_until or "",
                "eligible_event_keys": json.dumps([r["event_key"] for r in eligible]),
            })
            for r in at_time:
                key, decision = r["event_key"], r["decision"]
                active_owner = owner if ready < owner_until else None
                active_until = owner_until if active_owner else None
                owner_stage = ""
                if active_owner is not None:
                    if episode is None:
                        raise ValueError(f"Owner without episode at {key}")
                    fill_str = episode.get("fill_ts")
                    owner_stage = ("OPEN_POSITION" if fill_str and ready >= int(fill_str)
                                   else "PENDING_ORDER")
                rank = next((i for i, e in enumerate(eligible, 1) if e["event_key"] == key), None)
                extra = {
                    "admitted_after_filters": int(decision not in NON_ADMITTED),
                    "group_id": group_id,
                    "simultaneous_eligible": len(eligible),
                    "simultaneous_rank": rank if rank is not None else "",
                    "competition_type": group_type,
                    "owner_at_decision": active_owner["event_key"] if active_owner else "",
                    "owner_until_ms": active_until if active_until is not None else "",
                    "owner_stage": owner_stage,
                    "submitted_outcome": r["reason"] if decision == "ORDER_SUBMITTED" else "",
                    "is_selected_at_timestamp": int(decision == "ORDER_SUBMITTED"),
                }
                enriched.append({**r, **extra})
                row_output[key] = enriched[-1]
                if decision == "ORDER_SUBMITTED":
                    if active_owner is not None:
                        raise ValueError(f"Order admitted into occupied slot: {strategy}: {key}")
                    outcome = r["reason"]
                    trade = trade_index.get((strategy, key))
                    if outcome == "NO_FILL":
                        if trade is not None:
                            raise ValueError(f"NO_FILL has trade row: {key}")
                        reserved_until = ready + ttl_map[strategy] * MINUTE
                    elif outcome in TERMINAL:
                        if not trade or trade.get("status") != outcome:
                            raise ValueError(f"Trade outcome mismatch: {strategy}: {key}")
                        fill = _integer(trade, "fill_ts")
                        exit_ts = _integer(trade, "exit_ts")
                        if fill < ready - 60_000 or exit_ts < fill:
                            raise ValueError(f"Invalid trade chronology: {key}")
                        reserved_until = exit_ts + MINUTE
                        seen_trades.add((strategy, key))
                    else:
                        raise ValueError(f"Unresolved outcome {outcome} at {key}; cannot assert slot owner")
                    owner, owner_until = r, reserved_until
                    episode = {
                        "strategy": strategy, "owner_event_key": key,
                        "owner_symbol": r["symbol"], "owner_side": r["side"],
                        "ready_ms": ready, "reserved_until_ms": reserved_until,
                        "reservation_minutes": round((reserved_until - ready) / MINUTE, 8),
                        "order_outcome": outcome,
                        "fill_ts": trade["fill_ts"] if trade is not None else "",
                        "exit_ts": trade["exit_ts"] if trade is not None else "",
                        "blocked_count": 0, "blocked_event_keys": [],
                    }
                    episodes.append(episode)
                elif decision == "SKIPPED_SLOT_BUSY":
                    if active_owner is None:
                        raise ValueError(f"Busy with no owner: {strategy}: {key}")
                    m = BUSY_PATTERN.fullmatch(r["reason"] or "")
                    if not m or int(m.group(1)) != active_until:
                        raise ValueError(f"Busy-through mismatch: {strategy}: {key}")
                    if episode is None or episode["owner_event_key"] != active_owner["event_key"]:
                        raise ValueError(f"Wrong episode owner: {strategy}: {key}")
                    episode["blocked_count"] += 1
                    episode["blocked_event_keys"].append(key)
                    busy_verified += 1
                elif decision not in NON_ADMITTED:
                    raise ValueError(f"Unexpected decision {decision} at {key}")

        counts = Counter(d["decision"] for d in rows)
        summary[strategy] = {
            "source_decisions": len(rows), "orders_submitted": counts["ORDER_SUBMITTED"],
            "skipped_slot_busy": counts["SKIPPED_SLOT_BUSY"],
            "slot_busy_owner_verified": busy_verified,
            "guard_veto": counts[GUARD_REJECT],
            "simultaneous_eligible_groups": simultaneous_groups,
            "simultaneous_free_slot_groups": simultaneous_free_groups,
            "slot_episodes": sum(e["strategy"] == strategy for e in episodes),
            "events_excluded_from_portfolio": None,
        }

    if set(trade_index) != seen_trades:
        raise ValueError(f"Unmatched trades: {sorted(set(trade_index) - seen_trades)[:5]}")

    summary_file = run_dir / "summary.json"
    if summary_file.exists():
        exported = json.loads(summary_file.read_text(encoding="utf-8"))
        for strategy, stats in summary.items():
            expected = exported.get(strategy)
            if not isinstance(expected, dict):
                raise ValueError(f"Missing summary for {strategy}")
            for metric, source_field in (
                ("source_decisions", "total_source_events"),
                ("orders_submitted", "orders_placed"),
                ("skipped_slot_busy", "skipped_slot_busy"),
                ("guard_veto", "experimental_guard_rejected"),
            ):
                if source_field in expected and stats[metric] != int(expected[source_field]):
                    raise ValueError(f"Summary mismatch for {strategy}: {metric}")

    coverage = {}
    if events_path is not None:
        ep = Path(events_path)
        if not ep.is_file():
            raise FileNotFoundError(ep)
        with ep.open(encoding="utf-8-sig", newline="") as handle:
            raw_events = list(csv.DictReader(handle))
        raw_keys = [r.get("event_key") for r in raw_events]
        if not raw_keys or len(raw_keys) != len(set(raw_keys)):
            raise ValueError("Invalid event source: empty or duplicates")
        for strategy, stats in summary.items():
            source_keys = {r["event_key"] for r in by_strategy[strategy]}
            if not source_keys.issubset(raw_keys):
                raise ValueError(f"Portfolio events absent from historical replay: {strategy}")
            stats["events_excluded_from_portfolio"] = len(set(raw_keys) - source_keys)
        coverage["historical_events_total"] = len(raw_keys)
    if factors_path is not None:
        fp = Path(factors_path)
        if not fp.is_file():
            raise FileNotFoundError(fp)
        with fp.open(encoding="utf-8-sig", newline="") as handle:
            factor_rows = list(csv.DictReader(handle))
        fkeys = [r.get("event_key") for r in factor_rows]
        if len(fkeys) != len(set(fkeys)):
            raise ValueError("Duplicate factor keys")
        coverage["factor_rows_total"] = len(fkeys)

    for ep in episodes:
        ep["blocked_event_keys"] = json.dumps(ep["blocked_event_keys"])
    return {
        "decisions": enriched, "slot_episodes": episodes,
        "ready_groups": groups, "strategies": summary,
        "coverage": coverage, "ttl_source": ttl_source, "ttl_minutes": ttl_map,
        "source_hashes": {
            "decisions.csv": _sha(decision_file),
            "trades.csv": _sha(trade_file),
            **({"events": _sha(Path(events_path))} if events_path is not None else {}),
            **({"factors": _sha(Path(factors_path))} if factors_path is not None else {}),
        },
    }


def export_readonly(run_dir: Path, output_dir: Path, *, events_path=None, factors_path=None):
    """Build create-only research artifacts; do not modify the original run."""
    results = construct(run_dir, events_path=events_path, factors_path=factors_path)
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError(f"Output exists. Never overwrite: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=False)
    _write_csv(output_dir / "competition_decisions.csv", DECISION_COLUMNS, results["decisions"])
    _write_csv(output_dir / "slot_episodes.csv", EPISODE_COLUMNS, results["slot_episodes"])
    _write_csv(output_dir / "ready_groups.csv", GROUP_COLUMNS, results["ready_groups"])
    manifest = {
        "engine": ENGINE,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source_run_dir": str(Path(run_dir).resolve()),
        "cohort": "HISTORICAL_RESEARCH_UNVERIFIED_OR_PARTIAL",
        "future_outcomes_used_to_select": False,
        "shadow_outcomes_computed": False,
        "new_portfolio_policies_tested": False,
        "assumptions": [
            "Slot released at exit candle close (exit_ts + 60000ms)",
            "NO_FILL reserves slot for frozen strategy TTL",
            "Uses existing frozen portfolio decisions; does not propose counterfactual selection",
            "Reservation owner reconstruction verified against persisted slot_busy_through markers",
        ],
        **{k: results[k] for k in ("ttl_source", "ttl_minutes", "source_hashes", "strategies", "coverage")},
    }
    (output_dir / "competition_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest
