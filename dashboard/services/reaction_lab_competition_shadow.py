"""Read-only, fail-closed isolated outcomes for ALL causal Runner candidates.

The frozen portfolio's 1-slot decisions NEVER change. A shadow simulation means
'what this individual order would do with an available slot'; it does not create
an alternate portfolio. Non-eligible candidates are explicitly labeled.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import shutil
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from dashboard.services import reaction_lab_historical as hist
from dashboard.services import reaction_lab_runner_optimizer as v2
from dashboard.services.reaction_lab_portfolio import source_digest
from dashboard.services.reaction_lab_competition import _sha

ENGINE = "candidate-competition-shadow-v1"
MINUTE = 60_000
CLOSED = {"TP", "SL", "AMBIGUOUS_BOTH_AS_SL", "TIMEOUT"}
SHADOW_FIELDS = (
    "strategy", "event_key", "symbol", "side", "ready_ms", "actual_decision",
    "actual_reason", "eligible_under_frozen_rules", "shadow_class",
    "shadow_status", "shadow_net_pct", "shadow_gross_pct", "shadow_cost_pct",
    "shadow_fill_ts", "shadow_fill_price", "shadow_entry_liquidity",
    "shadow_exit_ts", "shadow_exit_price", "shadow_exit_liquidity",
    "shadow_duration_min", "shadow_mfe_raw_pct", "shadow_mae_raw_pct",
    "shadow_mfe_30m_raw_pct", "shadow_mae_30m_raw_pct", "shadow_bars_observed",
    "shadow_observability", "selected_trade_parity", "room_1h", "departure",
    "volume_rank", "return_rank",
)
PATH_FIELDS = ("event_key", "timestamp_ms", "open", "high", "low", "close")
REQUIRED_LEDGER_FILES = (
    "competition_manifest.json", "competition_decisions.csv",
    "slot_episodes.csv", "ready_groups.csv",
)


def _csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _write(path, columns, records):
    with Path(path).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def _optional_numeric(outcome, key):
    value = outcome.get(key)
    return "" if value is None else value


def _assert_close(a, b, label, *, rel=1e-8, abs_=1e-8):
    if not math.isclose(float(a), float(b), abs_tol=abs_, rel_tol=rel):
        raise ValueError(f"Selected-order parity failure: {label}: {a} != {b}")


def verify_selected(outcome, decision, trade):
    """Require the immutable portfolio's submitted orders to match 1m simulator."""
    key = f"{decision['strategy']} :: {decision['event_key']}"
    if outcome["status"] != decision["reason"]:
        raise ValueError(f"Selected-order parity failure: {key}: {outcome['status']} vs {decision['reason']}")
    if outcome["status"] == "NO_FILL":
        if trade is not None:
            raise ValueError(f"Selected-order parity failure: NO_FILL has trade {key}")
        return
    if trade is None:
        raise ValueError(f"Selected-order parity failure: missing trade {key}")
    if trade.get("status") != outcome["status"]:
        raise ValueError(f"Selected-order parity failure: wrong trade status {key}")
    for field, tfield in (("fill_ts", "fill_ts"), ("exit_ts", "exit_ts")):
        if outcome.get(field) is not None and int(outcome[field]) != int(trade[tfield]):
            raise ValueError(f"Selected-order parity failure: {field} {key}")
    for field, tfield in (("entry_liquidity", "entry_liquidity"),
                          ("exit_liquidity", "exit_liquidity")):
        if outcome.get(field) is not None and outcome[field] != trade[tfield]:
            raise ValueError(f"Selected-order parity failure: {field} {key}")
    for field, tfield in (("fill_price", "fill_price"), ("pnl", "net_pct"),
                          ("gross_pct", "gross_pct"), ("cost_pct", "cost_pct")):
        if outcome.get(field) is not None:
            _assert_close(outcome[field], trade[tfield], f"{field} {key}")
    if outcome.get("gross_pct") is not None and outcome.get("fill_price") is not None:
        entry = float(outcome["fill_price"])
        direction = 1 if decision["side"] == "LONG" else -1
        expected_exit = entry * (1 + direction * float(outcome["gross_pct"]) / 100)
        _assert_close(expected_exit, trade["exit_price"], f"exit_price {key}", abs_=1e-10)


def excursion(record, outcome):
    """Raw 1m envelope ONLY, not a claim about intrabar sequence/executable MFE."""
    if outcome.get("fill_ts") is None:
        return {"mfe": "", "mae": "", "mfe30": "", "mae30": "", "bars": 0}
    fill = int(outcome["fill_ts"])
    exit_ = outcome.get("exit_ts")
    if exit_ is None:
        # Unresolved after fill: no fabricated final PnL, but report visible range.
        end = min(record["bars"][-1][0], fill + (v2.HORIZON - 1) * MINUTE)
    else:
        end = int(exit_)
    price = float(outcome["fill_price"])
    side = record["side"]
    all_hi = []; all_lo = []; first_hi = []; first_lo = []
    for ts, op, high, low, close in record["bars"]:
        if not fill <= ts <= end:
            continue
        fav = (100 * (high / price - 1) if side == "LONG" else
               100 * (1 - low / price))
        adv = (100 * (1 - low / price) if side == "LONG" else
               100 * (high / price - 1))
        all_hi.append(max(0, fav)); all_lo.append(max(0, adv))
        if ts < fill + 30 * MINUTE:
            first_hi.append(max(0, fav)); first_lo.append(max(0, adv))
    return {"mfe": max(all_hi, default=""), "mae": max(all_lo, default=""),
            "mfe30": max(first_hi, default=""), "mae30": max(first_lo, default=""),
            "bars": len(all_hi)}


def isolated_rows(*, decisions, trade_index, records, profiles, costs):
    """Pure deterministic phase. Every valid decision gets an isolated outcome.

    This includes FILTER_REJECTED events for descriptive research, tagged as
    OUTSIDE_FROZEN_FILTERS; they are not eligible alternative slot choices.
    """
    index = {r["key"]: r for r in records}
    counts = Counter(); results = []
    seen = set()
    for row in decisions:
        strategy, key = row["strategy"], row["event_key"]
        if strategy not in profiles or key not in index:
            raise ValueError(f"Missing strategy/profile/causal record: {strategy} {key}")
        if (strategy, key) in seen:
            raise ValueError(f"Duplicate strategy/event decision: {strategy} {key}")
        seen.add((strategy, key))
        r = index[key]
        params = profiles[strategy]
        eligible = bool(v2._eligible(r, params))
        decision = row["decision"]
        if decision == "FILTER_REJECTED" and eligible:
            raise ValueError(f"Frozen eligibility inconsistency: filtered eligible {strategy} {key}")
        if decision != "FILTER_REJECTED" and not eligible:
            raise ValueError(f"Frozen eligibility inconsistency: admitted ineligible {strategy} {key}")
        if decision == "FILTER_REJECTED":
            cls = "OUTSIDE_FROZEN_FILTERS"
        elif decision == "FILTER_REJECTED_EXTREME_STRENGTH":
            cls = "GUARD_VETO_HYPOTHETICAL"
        elif decision == "SKIPPED_SLOT_BUSY":
            cls = "ELIGIBLE_BLOCKED_SLOT"
        elif decision == "ORDER_SUBMITTED":
            cls = "SELECTED_PARITY_CHECKED"
        else:
            cls = "OTHER_DECISION_HYPOTHETICAL"
        outcome = v2.simulate(r, params, costs=costs)
        trade = trade_index.get((strategy, key))
        if decision == "ORDER_SUBMITTED":
            verify_selected(outcome, row, trade)
            counts["selected_parity_verified"] += 1
        ext = excursion(r, outcome)
        status = outcome["status"]
        fill_ts = outcome.get("fill_ts")
        exit_ts = outcome.get("exit_ts")
        exit_price = ""
        if status in CLOSED:
            direction = 1 if r["side"] == "LONG" else -1
            exit_price = float(outcome["fill_price"]) * (1 + direction * float(outcome["gross_pct"]) / 100)
        results.append({
            "strategy": strategy, "event_key": key, "symbol": r["symbol"],
            "side": r["side"], "ready_ms": int(r["ready_ms"]),
            "actual_decision": decision, "actual_reason": row["reason"],
            "eligible_under_frozen_rules": int(eligible), "shadow_class": cls,
            "shadow_status": status, "shadow_net_pct": _optional_numeric(outcome, "pnl"),
            "shadow_gross_pct": _optional_numeric(outcome, "gross_pct"),
            "shadow_cost_pct": _optional_numeric(outcome, "cost_pct"),
            "shadow_fill_ts": _optional_numeric(outcome, "fill_ts"),
            "shadow_fill_price": _optional_numeric(outcome, "fill_price"),
            "shadow_entry_liquidity": _optional_numeric(outcome, "entry_liquidity"),
            "shadow_exit_ts": _optional_numeric(outcome, "exit_ts"),
            "shadow_exit_price": exit_price,
            "shadow_exit_liquidity": _optional_numeric(outcome, "exit_liquidity"),
            "shadow_duration_min": (round((exit_ts - fill_ts + MINUTE) / MINUTE, 4)
                                    if fill_ts is not None and exit_ts is not None else ""),
            "shadow_mfe_raw_pct": ext["mfe"], "shadow_mae_raw_pct": ext["mae"],
            "shadow_mfe_30m_raw_pct": ext["mfe30"], "shadow_mae_30m_raw_pct": ext["mae30"],
            "shadow_bars_observed": ext["bars"],
            "shadow_observability": ("UNRESOLVED_INTRABAR_OR_PATH" if outcome["pnl"] is None
                                     else "RESOLVED_OHLC_ESTIMATED"),
            "selected_trade_parity": ("EXACT" if decision == "ORDER_SUBMITTED" else "N/A"),
            "room_1h": float(r["values"][1]), "departure": float(r["values"][0]),
            "volume_rank": float(r["values"][2]), "return_rank": float(r["values"][3]),
        })
        counts[cls] += 1
        counts[f"status_{status}"] += 1
    if len(seen) != len(decisions):
        raise ValueError("Missing decisions in shadow expansion")
    return results, dict(counts)


def export_shadow(run_dir, ledger_dir, events, factors, cache_dir, output_dir):
    """Generate new immutable export with Fase1 reference and isolated paths."""
    run_dir, ledger_dir = Path(run_dir), Path(ledger_dir)
    events, factors, cache_dir, output_dir = map(Path, (events, factors, cache_dir, output_dir))
    if output_dir.exists():
        raise FileExistsError(f"Create-only: output already exists: {output_dir}")
    for name in REQUIRED_LEDGER_FILES:
        if not (ledger_dir / name).is_file():
            raise FileNotFoundError(f"Missing phase-1 ledger: {name}")
    original = json.loads((ledger_dir / "competition_manifest.json").read_text(encoding="utf-8"))
    expected = original["source_hashes"]
    supplied = {
        "decisions.csv": _sha(run_dir / "decisions.csv"),
        "trades.csv": _sha(run_dir / "trades.csv"),
        "events": _sha(events), "factors": _sha(factors),
    }
    for k, value in supplied.items():
        if value != expected.get(k):
            raise ValueError(f"Frozen dataset hash mismatch: {k}")
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    provenance = manifest.get("provenance", {})
    delay = provenance.get("assumed_execution_delay_ms", 0)
    if not isinstance(delay, int) or delay < 0:
        raise ValueError("Invalid historic execution-delay provenance")
    if provenance.get("post_boundary_guard_ms") != hist.POST_BOUNDARY_GUARD_MS:
        raise ValueError("Historical candle-boundary timing policy mismatch")
    candidates, summary = hist.load_replay(events)
    factors_by_key = hist.load_factors(factors)
    prepared, issues = hist.prepare_candidates(candidates, factors_by_key,
                                                assumed_execution_delay_ms=delay)
    if not prepared:
        raise ValueError("No valid causal candidate records")
    if len(issues) != int(original["coverage"]["historical_events_total"]) - len(prepared):
        raise ValueError("Source factor-issue count differs from ledger")
    needed = defaultdict(set)
    for candidate in prepared:
        first = candidate["retest_ts"] + MINUTE
        needed[candidate["symbol"]].update(first + i * MINUTE for i in range(240))
    cache, info = hist.load_candles_from_cache(cache_dir, set(needed), needed_timestamps=needed)
    records, path_issues = hist.attach_paths(prepared, cache, length=240)
    if path_issues:
        raise ValueError(f"Missing 1m paths for {len(path_issues)} causal candidates; no partial shadow output")
    if len(records) != int(manifest["source_records"]):
        raise ValueError(f"Record count mismatch: {len(records)} vs {manifest['source_records']}")
    digest = source_digest(records)
    if digest != manifest["source_digest_sha256"]:
        raise ValueError("Original portfolio 1m candle-path digest mismatch; refusing shadow comparison")
    decisions = _csv(ledger_dir / "competition_decisions.csv")
    indexed_trades = {(t["strategy"], t["event_key"]): t
                      for t in _csv(run_dir / "trades.csv")}
    profiles = manifest["profiles"]
    costs = v2._validated_costs(manifest["cost_assumptions"])
    shadow, stats = isolated_rows(decisions=decisions, trade_index=indexed_trades,
                                   records=records, profiles=profiles, costs=costs)
    if len(shadow) != len(decisions):
        raise ValueError("Not all 1-slot decisions were tracked")
    if stats.get("selected_parity_verified", 0) != sum(
            row["decision"] == "ORDER_SUBMITTED" for row in decisions):
        raise ValueError("Selected parity count mismatch")
    output_dir.mkdir(parents=True, exist_ok=False)
    for name in REQUIRED_LEDGER_FILES:
        shutil.copyfile(ledger_dir / name, output_dir / name)
    _write(output_dir / "shadow_outcomes.csv", SHADOW_FIELDS, shadow)
    # Indexed SQLite: dashboard reads only the chosen candidates, never millions
    # of 1m candles into RAM. Each event has a full 240m isolated path.
    path = output_dir / "shadow_paths_1m.sqlite3"
    con = sqlite3.connect(path)
    try:
        con.execute("""CREATE TABLE paths (
            event_key TEXT NOT NULL, timestamp_ms INTEGER NOT NULL,
            open REAL NOT NULL, high REAL NOT NULL,
            low REAL NOT NULL, close REAL NOT NULL,
            PRIMARY KEY(event_key, timestamp_ms)
        ) WITHOUT ROWID""")
        for r in sorted(records, key=lambda t: t["key"]):
            con.executemany(
                "INSERT INTO paths (event_key,timestamp_ms,open,high,low,close) VALUES (?,?,?,?,?,?)",
                ((r["key"], int(ts), float(op), float(hi), float(lo), float(close))
                 for ts, op, hi, lo, close in r["bars"]))
        con.commit()
    finally:
        con.close()
    report = {
        "engine": ENGINE,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "cohort": manifest.get("cohort"),
        "source_portfolio_manifest_sha256": _sha(run_dir / "manifest.json"),
        "source_competition_manifest_sha256": _sha(ledger_dir / "competition_manifest.json"),
        "source_cache_dir": str(cache_dir),
        "source_digest_sha256": digest,
        "historical_events": len(candidates),
        "valid_causal_events": len(records),
        "invalid_factor_events_excluded": len(issues),
        "shadow_rows": len(shadow),
        "strategy_count": len(profiles),
        "selected_orders_parity_verified": stats["selected_parity_verified"],
        "counts": stats,
        "path_1m_sqlite": path.name,
        "method": "v2.simulate(record, frozen_parameters, frozen_costs), isolated per candidate",
        "restrictions": [
            "Counterfactual per event only; no additional capital or free slot assumed in historical portfolio",
            "Filtered and guard-vetoed signals are hypothetical outside admitted policy",
            "NO_FILL implies zero net trade, not a guaranteed negative/positive outcome",
            "No observed live fills, no funding, no liquidation or intra-minute sequence",
            "MFE/MAE are OHLC envelopes; exit candle may contain post-exit moves",
            "Do not rank policies in same discovery period using future shadow outcomes",
        ],
    }
    (output_dir / "shadow_manifest.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report
