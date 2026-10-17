"""Read-only one-slot counterfactual selection policies.

Never reads shadow labels to make decisions. Reuses the immutable historical
records and exact frozen fill/fee/slot algorithm through an audited code fork.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

from dashboard.services import reaction_lab_portfolio as frozen
from dashboard.services import reaction_lab_portfolio_policy_fork as fork
from dashboard.services import reaction_lab_runner_optimizer as v2

FROZEN_SHA256 = "e0f12ae3fe3d21c52e6fa5551271e25f781e28bc73d03d9b4ef37ba47136caa1"
POLICIES = (
    "BASELINE_ROOM", "DIRECTIONAL_STRENGTH_VS_BTC_4H_PROXY",
    "STRENGTH_THEN_ROOM", "ROOM_THEN_STRENGTH", "DIRECTIONAL_RETURN_RANK_4H",
    "VOLUME_RANK_4H", "RELATIVE_VOLUME_4H",
)
STRATEGY_TO_PARAMS = {
    "Runner Challenger 220": (frozen.PROFILES["Runner Challenger 220"], False),
    frozen.EXPERIMENTAL_NAME: (frozen.PROFILES["Runner Challenger 220"], True),
}
FEATURE_REQUIRED = {
    "BASELINE_ROOM": ("room_1h_pct",),
    "DIRECTIONAL_STRENGTH_VS_BTC_4H_PROXY": ("directional_strength_vs_btc_4h_proxy_pct",),
    "STRENGTH_THEN_ROOM": ("directional_strength_vs_btc_4h_proxy_pct", "room_1h_pct"),
    "ROOM_THEN_STRENGTH": ("room_1h_pct", "directional_strength_vs_btc_4h_proxy_pct"),
    "DIRECTIONAL_RETURN_RANK_4H": ("directional_return_rank_4h",),
    "VOLUME_RANK_4H": ("volume_rank_pct_4h",),
    "RELATIVE_VOLUME_4H": ("relative_volume_4h",),
}


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1_048_576), b""):
            h.update(block)
    return h.hexdigest()


def ensure_frozen_source():
    source = Path(frozen.__file__)
    actual = sha256(source)
    if actual != FROZEN_SHA256:
        raise ValueError(f"Frozen portfolio source changed: SHA={actual}; expected {FROZEN_SHA256}. "
                         "Do not run fork until its unchanged portions are reaudited.")
    if frozen.PROFILES != fork.PROFILES or frozen.EXPERIMENTAL_GUARD_ID != fork.EXPERIMENTAL_GUARD_ID:
        raise ValueError("Frozen parameters/guard mismatch with isolated fork")


def csv_rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        if not rd.fieldnames:
            raise ValueError(f"CSV missing header: {path}")
        return list(rd)


def _finite(value, field):
    try:
        n = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Missing/non-numeric {field}: {value!r}") from exc
    if not math.isfinite(n):
        raise ValueError(f"Nonfinite {field}")
    return n


def _ms(value, field):
    n = _finite(value, field)
    if n != int(n):
        raise ValueError(f"Not an integer timestamp for {field}")
    return int(n)


def load_snapshots(path, records, *, manifest_path=None, factors_path=None):
    if manifest_path is not None:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        if factors_path is None:
            raise ValueError("--factors required for Selection snapshot provenance")
        expected = manifest.get("source_factors_sha256")
        if expected != sha256(factors_path):
            raise ValueError("Selection manifest does not match historical factors")
        if manifest.get("feature_method") != "CLOSED_4H_RELATIVE_RETURN_PROXY_NOT_CANONICAL_STRENGTH":
            raise ValueError("Unknown selection feature methodology")
    idx = {}
    for row in csv_rows(path):
        key = row.get("event_key", "")
        if not key or key in idx:
            raise ValueError(f"Invalid/duplicate snapshot {key}")
        idx[key] = row
    records_idx = {r["key"]: r for r in records}
    if set(idx) != set(records_idx):
        raise ValueError(f"Selection snapshots vs simulator events differ: "
                         f"missing={len(set(records_idx)-set(idx))}, extra={len(set(idx)-set(records_idx))}")
    for key, row in idx.items():
        r = records_idx[key]
        if row["symbol"] != r["symbol"] or row["side"] != r["side"]:
            raise ValueError(f"Snapshot event mismatch: {key}")
        ready = _ms(row["ready_ms"], "ready_ms")
        if ready != int(r["ready_ms"]):
            raise ValueError(f"Snapshot ready_ms mismatch: {key}")
        for name, actual in (("room_1h_pct", r["values"][1]),
                             ("departure_pct", r["values"][0]),
                             ("volume_rank_pct_4h", r["values"][2]),
                             ("return_rank_pct_4h", r["values"][3])):
            if not math.isclose(_finite(row[name], name), float(actual), rel_tol=0, abs_tol=1e-8):
                raise ValueError(f"Snapshot original factor mismatch: {key} {name}")
        for name in ("room_asof_ms", "room_available_ms", "ranks_asof_ms",
                     "ranks_available_ms"):
            if not 0 < _ms(row[name], name) <= ready:
                raise ValueError(f"Future/missing causal feature {key}: {name}")
        if not _ms(row["room_asof_ms"],"room_asof_ms") <= _ms(row["room_available_ms"],"room_available_ms"):
            raise ValueError("Room availability before as-of")
        if not _ms(row["ranks_asof_ms"],"ranks_asof_ms") <= _ms(row["ranks_available_ms"],"ranks_available_ms"):
            raise ValueError("Rank availability before as-of")
        close = row.get("feature_snapshot_close_ms", "")
        if close:
            if _ms(close, "feature_snapshot_close_ms") > ready:
                raise ValueError(f"Future strength close for {key}")
        if row.get("strength_status") == "CLOSED_4H_RELATIVE_RETURN_PROXY_NOT_CANONICAL_STRENGTH":
            if _ms(close,"feature_snapshot_close_ms") != _ms(row["ranks_asof_ms"],"ranks_asof_ms"):
                raise ValueError("Proxy strength is from a different 4h boundary")
            sym_ret = _finite(row["symbol_return_closed_4h_pct"], "symbol return")
            btc_ret = _finite(row["btc_return_closed_4h_pct"], "btc return")
            expected = (sym_ret-btc_ret) * (1 if r["side"]=="LONG" else -1)
            if not math.isclose(_finite(row["directional_strength_vs_btc_4h_proxy_pct"], "strength proxy"),
                                expected, abs_tol=1e-8, rel_tol=0):
                raise ValueError("Direction-adjusted Strength proxy mismatch")
        ret = _finite(row["return_rank_pct_4h"], "return rank")
        if not math.isclose(_finite(row["directional_return_rank_4h"], "dir ret rank"),
                            ret if r["side"]=="LONG" else 100-ret, abs_tol=1e-8):
            raise ValueError("Directional return rank mismatch")
    return idx


def _features_key(row, policy, key):
    result = []
    for name in FEATURE_REQUIRED[policy]:
        if policy in ("DIRECTIONAL_STRENGTH_VS_BTC_4H_PROXY", "STRENGTH_THEN_ROOM", "ROOM_THEN_STRENGTH") and name == "directional_strength_vs_btc_4h_proxy_pct":
            if row.get("strength_status") != "CLOSED_4H_RELATIVE_RETURN_PROXY_NOT_CANONICAL_STRENGTH":
                raise ValueError(f"Unavailable closed-4h strength for competitive candidate {key}")
        result.append(-_finite(row.get(name), f"{key}:{name}"))
    return tuple(result)+(key,)


def policy_rank_key(records, snapshots, params, guard, policy):
    """Ranks only same-ready eligible candidates; no ranking using future outcomes."""
    if policy not in POLICIES:
        raise ValueError(f"Unknown selection policy: {policy}")
    by_ready = defaultdict(list)
    for r in records:
        if v2._eligible(r, params) and not (guard and frozen.extreme_strength_short_guard(r)):
            by_ready[int(r["ready_ms"])].append(r)
    ranks = {}
    competing = 0
    for ts, candidates in by_ready.items():
        if len(candidates) < 2:
            continue
        competing += 1
        ranked = sorted(candidates, key=lambda r: _features_key(snapshots[r["key"]], policy, r["key"]))
        for i, r in enumerate(ranked):
            ranks[r["key"]] = i
    def keyfunc(record):
        if record["key"] in ranks:
            return (0, ranks[record["key"]], record["key"])
        # Non-competing candidates sort as original Room baseline; no changes
        # to their temporal execution or to admission rules.
        return (1, -float(record["values"][1]), record["key"])
    return keyfunc, competing


def _compare_data(expected, actual, name, *, fields):
    def index(rows):
        idx = {}
        for row in rows:
            key = str(row.get("event_key"))
            if key in idx:
                raise ValueError(f"Duplicate {name} key: {key}")
            idx[key] = row
        return idx
    e, a = index(expected), index(actual)
    if set(e) != set(a):
        raise ValueError(f"{name} event_key mismatch missing={len(set(e)-set(a))} extra={len(set(a)-set(e))}")
    for key in e:
        for field in fields:
            x, y = e[key].get(field), a[key].get(field)
            if x in (None, "") and y in (None, ""):
                continue
            try:
                if math.isfinite(float(x)) and math.isfinite(float(y)):
                    if not math.isclose(float(x), float(y), abs_tol=1e-8, rel_tol=1e-12):
                        raise ValueError(f"{name} parity failed {key} {field}: {x} != {y}")
                    continue
            except (TypeError, ValueError):
                pass
            if str(x) != str(y):
                raise ValueError(f"{name} parity failed {key} {field}: {x} != {y}")


def validate_source_portfolio(records, reference_run, base_outcomes, *, strategy):
    """Fail if historic records/replayed baseline drift from frozen saved run."""
    folder = Path(reference_run)
    manifest = json.loads((folder/"manifest.json").read_text(encoding="utf-8"))
    digest = frozen.source_digest(records)
    if manifest.get("source_digest_sha256") != digest:
        raise ValueError("Source digest differs from saved reference portfolio")
    if manifest.get("cohort") != "HISTORICAL_RESEARCH_UNVERIFIED_OR_PARTIAL":
        raise ValueError("Reference cohort is not expected historical research")
    if manifest.get("portfolio_settings") != base_outcomes["settings"]:
        raise ValueError("Reference margin/equity/leverage mismatch")
    if manifest.get("cost_assumptions") != base_outcomes["costs"]:
        raise ValueError("Reference transaction cost mismatch")
    profile = manifest.get("profiles", {}).get(strategy)
    if profile != base_outcomes["parameters"]:
        raise ValueError("Reference frozen parameters mismatch")
    saved_summary = json.loads((folder/"summary.json").read_text(encoding="utf-8"))[strategy]
    for col in ("equity_final_realized_usdt", "return_realized_pct", "max_marked_drawdown_pct",
                "resolved_trades", "orders_placed", "skipped_slot_busy", "win_rate_pct", "profit_factor"):
        a,b=saved_summary.get(col), base_outcomes["summary"].get(col)
        if a is None and b is None: continue
        if isinstance(a, (int,float)) and isinstance(b, (int,float)) and math.isclose(a,b,abs_tol=1e-8,rel_tol=1e-12):
            continue
        if a != b:
            raise ValueError(f"Baseline summary parity mismatch {strategy} {col}: {a} != {b}")
    saved_decisions = [r for r in csv_rows(folder/"decisions.csv") if r["strategy"]==strategy]
    _compare_data(saved_decisions,base_outcomes["decisions"],"decisions",fields=("ready_ms","decision","reason", "room_1h", "departure", "volume_rank", "return_rank"))
    saved_trades = [r for r in csv_rows(folder/"trades.csv") if r["strategy"]==strategy]
    _compare_data(saved_trades,base_outcomes["trades"],"trades",fields=("status","fill_ts","fill_price","entry_liquidity",
                                "exit_ts","exit_price","exit_liquidity","net_pct","net_pnl_usdt","wallet_after_usdt"))
    saved_equity = [r for r in csv_rows(folder/"equity.csv") if r["strategy"]==strategy]
    actual_equity = base_outcomes["equity"]
    if len(saved_equity) != len(actual_equity):
        raise ValueError(f"Baseline equity curve length mismatch for {strategy}")
    for i, (x, y) in enumerate(zip(saved_equity,actual_equity)):
        for col in ("timestamp_ms", "wallet_usdt", "marked_equity_usdt",
                    "drawdown_pct", "open_positions"):
            a, b = x.get(col), y.get(col)
            if a in (None, "") and b in (None, ""):
                continue
            if not math.isclose(_finite(a,f"saved {col}"), _finite(b,f"actual {col}"),
                                abs_tol=1e-8, rel_tol=1e-12):
                raise ValueError(f"Baseline equity curve mismatch {strategy} point={i} {col}")
    return {"reference_source_digest_sha256":digest,"verified_decisions":len(saved_decisions),
            "verified_trades":len(saved_trades),"verified_equity_points":len(saved_equity)}


def run_policy_set(records, snapshots, *, reference_dir, strategies,
                   policies, settings=None, costs=None):
    ensure_frozen_source()
    settings = (settings or frozen.PortfolioSettings()).validate()
    # Reproduce exact cost dictionary as frozen engine before sending to fork.
    costs = v2._validated_costs(costs)
    results, validations, divergences = {}, {}, []
    for strategy in strategies:
        if strategy not in STRATEGY_TO_PARAMS:
            raise ValueError(f"Unsupported frozen reference strategy: {strategy}")
        params, guard = STRATEGY_TO_PARAMS[strategy]
        orig = frozen.simulate_strategy(records, strategy, params, settings=settings,
                                        costs=costs, experimental_guard=guard)
        forked = fork.simulate_strategy(records, strategy, params, settings=settings,
                                       costs=costs, experimental_guard=guard)
        # Source fork parity, then reference stored-run parity.
        if orig != forked:
            raise ValueError(f"Fork changes frozen behavior for {strategy}")
        validations[strategy] = validate_source_portfolio(records, reference_dir, orig, strategy=strategy)
        # Thousands of research paths can generate millions of trace dictionaries.
        # Trace arrays do not influence later decisions; discard after parity.
        orig["path_traces"] = []
        del forked
        base_decisions = {r["event_key"]:r for r in orig["decisions"]}
        results[(strategy,"BASELINE_ROOM")] = orig
        for policy in policies:
            if policy == "BASELINE_ROOM":
                continue
            sorter, group_count = policy_rank_key(records, snapshots, params, guard, policy)
            alt = fork.simulate_strategy(records, strategy, params, settings=settings, costs=costs,
                                         experimental_guard=guard, selection_sort_key=sorter)
            # Only the selection order changes; never tune or read shadow labels.
            # Preserve compact decision/trade/equity outputs, not heavyweight OHLC traces.
            alt["path_traces"] = []
            results[(strategy,policy)] = alt
            chosen = {r["event_key"]:r for r in alt["decisions"]}
            if set(chosen)!=set(base_decisions):
                raise ValueError("Policy altered source event universe")
            for key in sorted(chosen):
                before, after = base_decisions[key], chosen[key]
                if before["decision"] != after["decision"] or before["reason"] != after["reason"]:
                    divergences.append({"strategy":strategy,"policy":policy,
                        "event_key":key,"ready_ms":before["ready_ms"],
                        "baseline_decision":before["decision"],"policy_decision":after["decision"],
                        "baseline_reason":before["reason"],"policy_reason":after["reason"]})
    return results,validations,divergences
