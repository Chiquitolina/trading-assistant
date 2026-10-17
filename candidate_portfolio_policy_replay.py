#!/usr/bin/env python3
"""Historical 1-slot Portfolio Policy Replay. Read-only inputs; isolated outputs."""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from dashboard.services import reaction_lab_historical as hist
from dashboard.services import reaction_lab_portfolio as frozen
from dashboard.services.reaction_lab_policy_replay import (
    POLICIES, STRATEGY_TO_PARAMS, csv_rows, load_snapshots, run_policy_set, sha256,
    ensure_frozen_source,
)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--events", type=Path, required=True)
    p.add_argument("--summary", type=Path, required=True)
    p.add_argument("--factors", type=Path, required=True)
    p.add_argument("--cache-dir", type=Path, required=True)
    p.add_argument("--reference-run", type=Path, required=True)
    p.add_argument("--selection-dir", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--strategies", choices=("challenger", "guard", "both"), default="both")
    p.add_argument("--policies", default=",".join(POLICIES))
    p.add_argument("--assumed-execution-delay-ms", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    return p


def write_csv(path, rows):
    if not rows:
        return
    keys = sorted(set().union(*(set(row) for row in rows)))
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for row in rows:
            w.writerow({k: json.dumps(v, sort_keys=True) if isinstance(v,(dict,list)) else v
                        for k,v in row.items()})


def main(argv=None):
    args = parser().parse_args(argv)
    policies = [p.strip() for p in args.policies.split(",") if p.strip()]
    if not policies or len(set(policies))!=len(policies) or any(p not in POLICIES for p in policies):
        raise ValueError(f"Invalid --policies; valid={POLICIES}")
    if "BASELINE_ROOM" not in policies:
        policies.insert(0,"BASELINE_ROOM")
    if args.assumed_execution_delay_ms < 0:
        raise ValueError("Execution delay must be nonnegative")
    if args.output.exists():
        raise FileExistsError(f"Create-only output already exists: {args.output}")
    # Reject outdated source before loading potentially gigabytes of 1m history.
    ensure_frozen_source()

    ref_manifest = json.loads((args.reference_run/"manifest.json").read_text(encoding="utf-8"))
    # Check scenario inputs match exactly those of the saved frozen reference.
    expected_sha = ref_manifest.get("provenance", {})
    for flag,path,source_name in (("historical_replay_events_sha256", args.events,"events"),
                                   ("historical_replay_summary_sha256", args.summary,"summary"),
                                   ("historical_factors_sha256", args.factors,"factors")):
        if expected_sha.get(flag) != sha256(path):
            raise ValueError(f"Reference historical {source_name} SHA mismatch")
    if int(expected_sha.get("assumed_execution_delay_ms", -1)) != args.assumed_execution_delay_ms:
        raise ValueError("Reference execution delay differs")
    summary_input = json.loads(args.summary.read_text(encoding="utf-8"))
    if summary_input.get("symbols_failed") or summary_input.get("data_gaps"):
        raise ValueError("Replay with gaps or failed symbols not supported")
    if summary_input.get("step_minutes") != 1:
        raise ValueError("Require 1m decision checkpoints")

    events,summary = hist.load_replay(args.events,args.summary)
    factors = hist.load_factors(args.factors)
    candidates, issues = hist.prepare_candidates(events, factors,
                   assumed_execution_delay_ms=args.assumed_execution_delay_ms)
    if not candidates:
        raise ValueError("No complete causal candidates")
    # Partials allowed only if EXACT same cohort as saved; never silently add events.
    needed = defaultdict(set)
    for r in candidates:
        first = r["retest_ts"]+hist.MINUTE
        needed[r["symbol"]].update(first+i*hist.MINUTE for i in range(240))
    candles,_ = hist.load_candles_from_cache(args.cache_dir, set(needed), needed_timestamps=needed)
    records, path_issues = hist.attach_paths(candidates, candles, length=240)
    if path_issues:
        raise ValueError(f"{len(path_issues)} incomplete OHLC paths. No results written.")
    if not records:
        raise ValueError("No complete 1m historical paths")

    snap_path = args.selection_dir/"candidate_factor_snapshots.csv"
    selection_manifest = args.selection_dir/"selection_manifest.json"
    snapshots = load_snapshots(snap_path, records,
                               manifest_path=selection_manifest, factors_path=args.factors)
    if len(events) != len(records)+len(issues):
        raise ValueError("Original event universe count mismatch")

    names = (["Runner Challenger 220"] if args.strategies=="challenger" else
             [frozen.EXPERIMENTAL_NAME] if args.strategies=="guard" else
             list(STRATEGY_TO_PARAMS))
    settings = frozen.PortfolioSettings(**ref_manifest["portfolio_settings"]).validate()
    costs = ref_manifest["cost_assumptions"]
    results,parity,divergences = run_policy_set(
        records, snapshots, reference_dir=args.reference_run,
        strategies=names, policies=policies, settings=settings, costs=costs)
    grouped_summary, rows_dec, rows_tr, rows_eq, rows_ev = [],[],[],[],[]
    for (strategy,policy),result in results.items():
        s = dict(result["summary"])
        s.update(policy=policy, reference_strategy=strategy,
                 research_status="HISTORICAL_RESEARCH_UNVERIFIED_OR_PARTIAL")
        grouped_summary.append(s)
        for dst,typ in ((rows_dec,"decisions"),(rows_tr,"trades"),
                        (rows_eq,"equity"),(rows_ev,"events")):
            dst.extend({"policy":policy, **row} for row in result[typ])
    # Reconstruct actual per-policy chosen orders in each simultaneous eligible
    # group. This is not a shadow-label "best candidate" view.
    competitions = []
    for strategy in names:
        params, guard = STRATEGY_TO_PARAMS[strategy]
        eligible = defaultdict(list)
        for r in records:
            from dashboard.services import reaction_lab_runner_optimizer as v2
            if v2._eligible(r, params) and not (guard and frozen.extreme_strength_short_guard(r)):
                eligible[int(r["ready_ms"])].append(r["key"])
        for ready, keys in sorted(eligible.items()):
            if len(keys)<2:
                continue
            for policy in policies:
                # Baseline always included, all requested variants fully simulated.
                outcome = results[(strategy,policy)]
                picks = [d["event_key"] for d in outcome["decisions"]
                         if int(d["ready_ms"])==ready and d["decision"]=="ORDER_SUBMITTED"]
                if len(picks)>1 or any(key not in keys for key in picks):
                    raise ValueError("Invalid multi-order competition: slot invariant broken")
                competitions.append({"strategy":strategy,"policy":policy,"ready_ms":ready,
                    "eligible_count":len(keys),"eligible_event_keys":";".join(sorted(keys)),
                    "order_submitted_event_key":picks[0] if picks else "",
                    "slot_was_available_under_policy":bool(picks)})

    report = {"base_parity_verified":True,
              "reference":str(args.reference_run),
              "source_events":len(events), "complete_records":len(records),
              "excluded_missing_factors":len(issues),
              "strategies":names,"policies":policies,
              "divergent_decisions":len(divergences),
              "simultaneous_group_policy_rows":len(competitions),
              "results":{f"{r['strategy']} | {r['policy']}":{
                  "equity_final_realized_usdt":r["equity_final_realized_usdt"],
                  "return_realized_pct":r["return_realized_pct"],
                  "max_marked_drawdown_pct":r["max_marked_drawdown_pct"],
                  "resolved_trades":r["resolved_trades"],
                  "orders_placed":r["orders_placed"]} for r in grouped_summary}}
    print(json.dumps(report, indent=2, ensure_ascii=False),flush=True)
    if args.dry_run:
        print("DRY RUN. No file created.")
        return report
    args.output.mkdir(parents=True,exist_ok=False)
    hashes = {key: sha256(value) for key,value in (("events",args.events),("summary",args.summary),
                ("factors",args.factors),("selection_snapshot",snap_path),
                ("selection_manifest",selection_manifest),
                ("original_portfolio_manifest",args.reference_run/"manifest.json"))}
    manifest = {"engine":"candidate-portfolio-policy-replay-v1",
                "created_utc":datetime.now(timezone.utc).isoformat(),
                "research_status":"HISTORICAL_RESEARCH_UNVERIFIED_OR_PARTIAL",
                "source_hashes":hashes,"source_digest_sha256":frozen.source_digest(records),
                "reference_parity":parity,"policies":policies,"strategies":names,
                "description":"One-slot full chronological resimulation. Same freezes, costs, 1m OHLC simulator; only ties among same-ready eligible candidates re-ordered.",
                "notices":["Research only: not LIVE execution, no funding/liquidations/intra-minute path",
                  "Strength = side-adjusted CLOSED 4h proxy, not canonical V2 Strength parity",
                  "Policies were explored on same historical dates: results not independent validation",
                  "No shadow outcome label used to decide; portfolio slot and wallet recomputed",
                  "V2 replay and independent 4h/1h parity not yet certified for full research window"]}
    (args.output/"manifest.json").write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    (args.output/"summary.json").write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    write_csv(args.output/"strategy_policy_summary.csv",grouped_summary)
    write_csv(args.output/"policy_decisions.csv",rows_dec)
    write_csv(args.output/"policy_trades.csv",rows_tr)
    write_csv(args.output/"policy_equity.csv",rows_eq)
    write_csv(args.output/"policy_events.csv",rows_ev)
    write_csv(args.output/"policy_divergences.csv",divergences)
    write_csv(args.output/"policy_competitions.csv",competitions)
    print("Saved NEW POLICY research run:",args.output,flush=True)
    return report


if __name__ == "__main__":
    try:
        main()
    except (FileNotFoundError,FileExistsError,ValueError,KeyError) as e:
        raise SystemExit(f"ERROR (fail-closed): {e}")
