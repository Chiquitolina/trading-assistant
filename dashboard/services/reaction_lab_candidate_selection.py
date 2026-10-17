"""Candidate Selection Research v1: immutable causal feature snapshots and descriptive tie-breaks.

Never enters an order, recomputes production decisions, selects on future outcomes,
or claims that isolated shadow returns are an achievable portfolio result.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ENGINE = "candidate-selection-research-v1"
POLICIES = (
    "BASELINE_ROOM",
    "DIRECTIONAL_STRENGTH_VS_BTC_4H_PROXY",
    "STRENGTH_THEN_ROOM",
    "ROOM_THEN_STRENGTH",
    "DIRECTIONAL_RETURN_RANK_4H",
    "VOLUME_RANK_4H",
    "RELATIVE_VOLUME_4H",
)
ADMITTED = {"ORDER_SUBMITTED", "SKIPPED_SLOT_BUSY"}
TERMINAL = {"TP", "SL", "TIMEOUT", "AMBIGUOUS_BOTH_AS_SL"}
FACTOR_COLUMNS = (
    "event_key", "symbol", "side", "ready_ms",
    "room_1h_pct", "departure_pct", "return_rank_pct_4h", "volume_rank_pct_4h",
    "directional_return_rank_4h", "room_asof_ms", "room_available_ms",
    "ranks_asof_ms", "ranks_available_ms", "causal_verified_for_decision",
    "symbol_return_closed_4h_pct", "btc_return_closed_4h_pct",
    "directional_strength_vs_btc_4h_proxy_pct", "relative_volume_4h",
    "market_breadth_4h", "feature_snapshot_close_ms", "strength_status",
)
CANDIDATE_COLUMNS = (
    "strategy", "group_id", "event_key", "symbol", "side", "ready_ms",
    "decision", "shadow_class", "shadow_status", "shadow_net_pct",
    "shadow_fill_ts", "room_1h_pct", "departure_pct",
    "return_rank_pct_4h", "volume_rank_pct_4h", "directional_return_rank_4h",
    "directional_strength_vs_btc_4h_proxy_pct", "relative_volume_4h",
    "market_breadth_4h", "strength_status",
)
GROUP_COLUMNS = (
    "strategy", "group_id", "ready_ms", "eligible_count", "competition_type",
    "baseline_selected_event_key", "eligible_event_keys", "has_complete_strength",
)
POLICY_COLUMNS = (
    "strategy", "group_id", "ready_ms", "policy", "candidate_count",
    "baseline_event_key", "choice_event_key", "decision_status", "changed_from_baseline",
    "baseline_shadow_status", "choice_shadow_status", "baseline_shadow_net_pct",
    "choice_shadow_net_pct", "isolated_delta_net_pct_both_filled",
)


def _csv(path):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames:
            raise ValueError(f"CSV header missing: {path}")
        return list(reader)


def _write(path, columns, rows):
    with Path(path).open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _num(value, label, *, nullable=False):
    if nullable and value in ("", None):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid numeric {label}: {value!r}") from exc
    if not math.isfinite(result):
        raise ValueError(f"Nonfinite {label}")
    return result


def _millis(value, label):
    n = _num(value, label)
    if not n.is_integer():
        raise ValueError(f"Noninteger ms at {label}: {value}")
    return int(n)


def _index_unique(rows, keyfn, label):
    idx = {}
    for r in rows:
        key = keyfn(r)
        if key in idx:
            raise ValueError(f"Duplicate {label}: {key}")
        idx[key] = r
    return idx


def _market_index(paths):
    index, sources = {}, {}
    for path in paths:
        path = Path(path)
        sources[str(path.resolve())] = _sha(path)
        for row in _csv(path):
            b = _millis(row["boundary_close_ms"], "boundary_close_ms")
            if b <= 0:
                raise ValueError("Invalid 4h boundary")
            key = (b, row["symbol"].strip().upper())
            attrs = {
                "return_pct_4h": row.get("return_pct_4h", ""),
                "relative_volume_4h": row.get("relative_volume_4h", ""),
                "market_breadth_4h": row.get("market_breadth_4h", ""),
            }
            if key in index and index[key] != attrs:
                raise ValueError(f"Conflicting market snapshots: {key}")
            index[key] = attrs
    return index, sources


def build_factor_snapshots(factors, decisions, market):
    """Freeze only as-of known fields; strength is a PROXY of closed 4h returns.

    Does NOT claim parity with Candidate V2's canonical Strength. Missing BTC
    or a missing closed boundary leads to MISSING, never an imputed zero.
    """
    decision_index = defaultdict(list)
    for d in decisions:
        decision_index[d["event_key"]].append(d)
    snapshots = []
    for f in factors:
        key = f["event_key"]
        if key not in decision_index:  # structurally excluded from frozen portfolio
            continue
        ds = decision_index[key]
        ready = {_millis(d["ready_ms"], "ready_ms") for d in ds}
        if len(ready) != 1:
            raise ValueError(f"Inconsistent decision clock for {key}")
        ready_ms = ready.pop()
        symbols = {d["symbol"] for d in ds}
        sides = {d["side"] for d in ds}
        if len(symbols) != 1 or len(sides) != 1 or symbols.pop() != f["symbol"]:
            raise ValueError(f"Decision/factor identity mismatch for {key}")
        side = sides.pop()
        if side not in ("LONG", "SHORT"):
            raise ValueError(f"Bad side for {key}")
        room = _num(f["room_1h_pct"], f"room {key}")
        volrank = _num(f["volume_rank_pct_4h"], f"volume {key}")
        retrank = _num(f["return_rank_pct_4h"], f"return {key}")
        room_asof = _millis(f["room_asof_ms"], "room_asof_ms")
        room_avail = _millis(f["room_available_ms"], "room_available_ms")
        rank_asof = _millis(f["ranks_asof_ms"], "ranks_asof_ms")
        rank_avail = _millis(f["ranks_available_ms"], "ranks_available_ms")
        if not (0 < room_asof <= room_avail <= ready_ms and
                0 < rank_asof <= rank_avail <= ready_ms):
            raise ValueError(f"Lookahead or stale timing: {key}")
        d = ds[0]
        for other in ds[1:]:
            for field in ("room_1h", "departure", "return_rank", "volume_rank"):
                if not math.isclose(_num(d[field], field), _num(other[field], field), abs_tol=1e-8):
                    raise ValueError(f"Factor differs across frozen strategies: {key} {field}")
        if not math.isclose(_num(d["room_1h"], "room"), room, abs_tol=1e-8):
            raise ValueError(f"Room mismatch with original decision: {key}")
        if not math.isclose(_num(d["return_rank"], "return"), retrank, abs_tol=1e-8):
            raise ValueError(f"Return rank mismatch: {key}")
        if not math.isclose(_num(d["volume_rank"], "volume"), volrank, abs_tol=1e-8):
            raise ValueError(f"Volume rank mismatch: {key}")
        sym_return = btc_return = dir_strength = relative_volume = breadth = ""
        status = "NO_CLOSED_4H_SNAPSHOT_PROVIDED" if not market else "MISSING_CLOSED_4H_SYMBOL_OR_BTC"
        if market:
            sym_row = market.get((rank_asof, f["symbol"]))
            btc_row = market.get((rank_asof, "BTCUSDT"))
            if sym_row and btc_row:
                sym_return = _num(sym_row.get("return_pct_4h"), f"symbol 4h {key}")
                btc_return = _num(btc_row.get("return_pct_4h"), f"BTC 4h {key}")
                dir_strength = round((sym_return - btc_return) * (1 if side == "LONG" else -1), 12)
                relative_volume = _num(sym_row.get("relative_volume_4h"), "rel vol", nullable=True)
                breadth = _num(sym_row.get("market_breadth_4h"), "breadth", nullable=True)
                status = "CLOSED_4H_RELATIVE_RETURN_PROXY_NOT_CANONICAL_STRENGTH"
        snapshots.append({
            "event_key": key, "symbol": f["symbol"], "side": side, "ready_ms": ready_ms,
            "room_1h_pct": room, "departure_pct": _num(d["departure"], "departure"),
            "return_rank_pct_4h": retrank, "volume_rank_pct_4h": volrank,
            "directional_return_rank_4h": retrank if side == "LONG" else 100 - retrank,
            "room_asof_ms": room_asof, "room_available_ms": room_avail,
            "ranks_asof_ms": rank_asof, "ranks_available_ms": rank_avail,
            "causal_verified_for_decision": "INPUT_TIMESTAMPS_ONLY",
            "symbol_return_closed_4h_pct": sym_return,
            "btc_return_closed_4h_pct": btc_return,
            "directional_strength_vs_btc_4h_proxy_pct": dir_strength,
            "relative_volume_4h": relative_volume,
            "market_breadth_4h": breadth, "feature_snapshot_close_ms": rank_asof,
            "strength_status": status,
        })
    if len(snapshots) != len(decision_index):
        raise ValueError("Some portfolio decisions lacked complete historical factors")
    return snapshots


def _feature(r, policy):
    if policy == "BASELINE_ROOM":
        return (-float(r["room_1h_pct"]), r["event_key"])
    if policy == "DIRECTIONAL_STRENGTH_VS_BTC_4H_PROXY":
        return (-float(r["directional_strength_vs_btc_4h_proxy_pct"]), r["event_key"])
    if policy == "STRENGTH_THEN_ROOM":
        return (-float(r["directional_strength_vs_btc_4h_proxy_pct"]),
                -float(r["room_1h_pct"]), r["event_key"])
    if policy == "ROOM_THEN_STRENGTH":
        return (-float(r["room_1h_pct"]),
                -float(r["directional_strength_vs_btc_4h_proxy_pct"]), r["event_key"])
    if policy == "DIRECTIONAL_RETURN_RANK_4H":
        return (-float(r["directional_return_rank_4h"]), r["event_key"])
    if policy == "VOLUME_RANK_4H":
        return (-float(r["volume_rank_pct_4h"]), r["event_key"])
    if policy == "RELATIVE_VOLUME_4H":
        return (-float(r["relative_volume_4h"]), r["event_key"])
    raise ValueError(f"Unknown policy: {policy}")


def _available(group_rows, policy):
    if policy in ("DIRECTIONAL_STRENGTH_VS_BTC_4H_PROXY", "STRENGTH_THEN_ROOM", "ROOM_THEN_STRENGTH"):
        return all(r["directional_strength_vs_btc_4h_proxy_pct"] not in ("", None) for r in group_rows)
    if policy == "RELATIVE_VOLUME_4H":
        return all(r["relative_volume_4h"] not in ("", None) for r in group_rows)
    return True


def _status_outcome(row):
    return row["shadow_status"] if row else ""


def build_selection(factors, decisions, groups, shadows, market):
    snapshots = build_factor_snapshots(factors, decisions, market)
    snapshot_idx = _index_unique(snapshots, lambda r: r["event_key"], "snapshot")
    shadow_idx = _index_unique(shadows, lambda r: (r["strategy"], r["event_key"]), "shadow")
    decision_idx = _index_unique(decisions, lambda r: (r["strategy"], r["event_key"]), "decision")
    if set(shadow_idx) != set(decision_idx):
        raise ValueError("Frozen decisions and shadow outcomes do not cover same keys")
    joined = []
    for d in decisions:
        s = snapshot_idx[d["event_key"]]
        o = shadow_idx[(d["strategy"], d["event_key"])]
        if o["actual_decision"] != d["decision"]:
            raise ValueError(f"Frozen vs shadow decision mismatch: {d['event_key']}")
        joined.append({
            "strategy": d["strategy"], "group_id": d["group_id"],
            "event_key": d["event_key"], "symbol": d["symbol"], "side": d["side"],
            "ready_ms": s["ready_ms"], "decision": d["decision"],
            "shadow_class": o["shadow_class"], "shadow_status": o["shadow_status"],
            "shadow_net_pct": o["shadow_net_pct"], "shadow_fill_ts": o["shadow_fill_ts"],
            **{k: s[k] for k in (
                "room_1h_pct", "departure_pct", "return_rank_pct_4h",
                "volume_rank_pct_4h", "directional_return_rank_4h",
                "directional_strength_vs_btc_4h_proxy_pct", "relative_volume_4h",
                "market_breadth_4h", "strength_status",
            )},
        })
    admitted_groups = defaultdict(list)
    for row in joined:
        if row["decision"] in ADMITTED:
            admitted_groups[(row["strategy"], row["group_id"])].append(row)
    groups_out, policies_out = [], []
    checked = 0
    for g in groups:
        if not str(g["competition_type"]).startswith("SIMULTANEOUS"):
            continue
        key = (g["strategy"], g["group_id"])
        participants = admitted_groups.get(key, [])
        if len(participants) != int(g["eligible_count"]) or len(participants) < 2:
            raise ValueError(f"Simultaneous eligible-group mismatch: {key}")
        baseline = g["selected_event_key"]
        free = g["competition_type"] == "SIMULTANEOUS_FREE_SLOT"
        if free:
            actual = [p["event_key"] for p in participants if p["decision"] == "ORDER_SUBMITTED"]
            if len(actual) != 1 or baseline != actual[0]:
                raise ValueError(f"Frozen selected event missing for free slot: {key}")
            chosen_room = sorted(participants, key=lambda r: _feature(r, "BASELINE_ROOM"))[0]["event_key"]
            if chosen_room != baseline:
                raise ValueError(f"BASELINE_ROOM parity failure: {key}: {chosen_room} != {baseline}")
            checked += 1
        else:
            if baseline or any(p["decision"] == "ORDER_SUBMITTED" for p in participants):
                raise ValueError(f"Preoccupied slot cannot select a new candidate: {key}")
        groups_out.append({
            "strategy": g["strategy"], "group_id": g["group_id"],
            "ready_ms": int(g["ready_ms"]), "eligible_count": len(participants),
            "competition_type": g["competition_type"],
            "baseline_selected_event_key": baseline,
            "eligible_event_keys": json.dumps([p["event_key"] for p in participants]),
            "has_complete_strength": int(_available(participants, "STRENGTH_THEN_ROOM")),
        })
        if not free:
            continue
        baseline_row = next(p for p in participants if p["event_key"] == baseline)
        for policy in POLICIES:
            if not _available(participants, policy):
                status, winner = "SKIPPED_MISSING_CAUSAL_FEATURE", None
            else:
                status = "DESCRIPTIVE_TIE_BREAK_ONLY_NOT_PORTFOLIO"
                winner = sorted(participants, key=lambda r: _feature(r, policy))[0]
            outcome_pair = (baseline_row, winner)
            both_filled = bool(winner and all(
                r["shadow_status"] in TERMINAL and r["shadow_fill_ts"] not in (None, "")
                and r["shadow_net_pct"] not in (None, "") for r in outcome_pair
            ))
            policies_out.append({
                "strategy": g["strategy"], "group_id": g["group_id"],
                "ready_ms": int(g["ready_ms"]), "policy": policy,
                "candidate_count": len(participants), "baseline_event_key": baseline,
                "choice_event_key": winner["event_key"] if winner else "",
                "decision_status": status,
                "changed_from_baseline": int(winner["event_key"] != baseline) if winner else "",
                "baseline_shadow_status": baseline_row["shadow_status"],
                "choice_shadow_status": _status_outcome(winner),
                "baseline_shadow_net_pct": baseline_row["shadow_net_pct"],
                "choice_shadow_net_pct": winner["shadow_net_pct"] if winner else "",
                "isolated_delta_net_pct_both_filled": (
                    round(float(winner["shadow_net_pct"]) - float(baseline_row["shadow_net_pct"]), 10)
                    if both_filled else ""
                ),
            })
    return snapshots, joined, groups_out, policies_out, checked


def export_selection(shadow_dir, factors_file, output_dir, market_paths=()):
    shadow_dir, factors_file, output_dir = map(Path, (shadow_dir, factors_file, output_dir))
    market_paths = tuple(Path(p) for p in market_paths)
    if output_dir.exists():
        raise FileExistsError(f"Selection research export is create-only: {output_dir}")
    compfile = shadow_dir / "competition_manifest.json"
    shadowfile = shadow_dir / "shadow_manifest.json"
    comp = json.loads(compfile.read_text(encoding="utf-8"))
    shadow = json.loads(shadowfile.read_text(encoding="utf-8"))
    if shadow["source_competition_manifest_sha256"] != _sha(compfile):
        raise ValueError("Shadow/Competition manifest hash mismatch")
    if comp["source_hashes"].get("factors") != _sha(factors_file):
        raise ValueError("Historic factor file differs from original frozen source")
    for name in ("competition_decisions.csv", "ready_groups.csv", "shadow_outcomes.csv"):
        if not (shadow_dir / name).is_file():
            raise FileNotFoundError(shadow_dir / name)
    factors = _csv(factors_file)
    if len(_index_unique(factors, lambda r: r["event_key"], "source factor")) != len(factors):
        raise ValueError("Duplicate historical factors")
    decisions = _csv(shadow_dir / "competition_decisions.csv")
    groups = _csv(shadow_dir / "ready_groups.csv")
    shadows = _csv(shadow_dir / "shadow_outcomes.csv")
    if len(shadows) != int(shadow["shadow_rows"]):
        raise ValueError("Shadow rows count mismatch")
    market, source_market_hashes = _market_index(market_paths)
    results = build_selection(factors, decisions, groups, shadows, market)
    snapshots, joined, group_rows, policy_rows, baseline_checked = results
    output_dir.mkdir(parents=True, exist_ok=False)
    _write(output_dir / "candidate_factor_snapshots.csv", FACTOR_COLUMNS, snapshots)
    _write(output_dir / "selection_candidates.csv", CANDIDATE_COLUMNS, joined)
    _write(output_dir / "selection_groups.csv", GROUP_COLUMNS, group_rows)
    _write(output_dir / "selection_policy_choices.csv", POLICY_COLUMNS, policy_rows)
    summary = {
        "engine": ENGINE, "created_utc": datetime.now(timezone.utc).isoformat(),
        "source_shadow_dir": str(shadow_dir.resolve()),
        "source_competition_manifest_sha256": _sha(compfile),
        "source_shadow_manifest_sha256": _sha(shadowfile),
        "source_factors_sha256": _sha(factors_file),
        "source_market_snapshot_sha256": source_market_hashes,
        "feature_method": "CLOSED_4H_RELATIVE_RETURN_PROXY_NOT_CANONICAL_STRENGTH",
        "feature_status": dict(Counter(r["strength_status"] for r in snapshots)),
        "source_valid_events": len(snapshots), "strategy_candidate_rows": len(joined),
        "simultaneous_groups": len(group_rows), "simultaneous_free_slot_groups_checked": baseline_checked,
        "policy_rows": len(policy_rows),
        "baseline_room_parity_verified": True,
        "source_cohort": shadow.get("cohort"),
        "notices": [
            "No frozen portfolio, strategy, order, or live process is modified.",
            "All policy choices use only event-time causal features; outcomes are joined AFTER selection.",
            "Strength is a side-adjusted (symbol minus BTC) single CLOSED 4h candle-return PROXY, NOT certified parity with canonical Candidate V2 Strength.",
            "Policy comparisons are descriptive isolated event comparisons, not portfolio counterfactuals.",
            "Only eligible simultaneous groups with slot originally free can be ranked.",
            "Non-admitted, blocked by preexisting slot, and missing-feature cases are never treated as executable picks.",
            "Temporal and market context remain HISTORICAL_RESEARCH_UNVERIFIED_OR_PARTIAL.",
        ],
    }
    (output_dir / "selection_manifest.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return summary
