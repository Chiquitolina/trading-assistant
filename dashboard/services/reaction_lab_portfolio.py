"""Reaction Lab research portfolio engine, isolated from live execution.

Uses V2 runner optimizer as the single source of fill/TP/SL/cost assumptions.
Represents chronological single-slot order reservation, isolated margin,
compounded *realized* equity, and 1m mark-to-market estimates. Never places
exchange orders or writes scanner/collector SQLite files.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import sqlite3
import struct
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from dashboard.services import reaction_lab_runner_optimizer as v2

MINUTE = v2.MINUTE
ENGINE_VERSION = "runner-portfolio-v1.0"

# Fixed comparison. Challenger #220 was selected after inspecting research data,
# so ALL earlier outcomes are exploratory, not out-of-sample proof.
PROFILES = {
    "Runner V1 Frozen": dict(v2.BASELINE),
    "Runner Challenger 220": {
        "departure": 1.5, "room_1h": 1.0,
        "volume_rank": 40.0, "return_rank": 40.0,
        "limit": 0.0, "ttl": 30, "tp": 2.0, "sl": 2.0,
    },
}

TERMINAL = {"TP", "SL", "AMBIGUOUS_BOTH_AS_SL", "TIMEOUT"}
UNRESOLVED = {"AMBIGUOUS_FILL_BAR", "CENSORED_AFTER_FILL", "UNOBSERVABLE_WINDOW"}


@dataclass(frozen=True)
class PortfolioSettings:
    initial_equity: float = 200.0
    margin_fraction: float = 0.80
    leverage: float = 3.0
    # Single pending-or-open slot, not only a single filled trade.
    max_slots: int = 1

    def validate(self):
        if (not math.isfinite(self.initial_equity) or self.initial_equity <= 0 or
                not math.isfinite(self.margin_fraction) or not 0 < self.margin_fraction <= 1 or
                not math.isfinite(self.leverage) or not 0 < self.leverage <= 20 or
                self.max_slots != 1):
            raise ValueError("Portfolio: equity>0, margin (0,1], leverage (0,20], slots=1")
        return self


def validate_profiles(profiles):
    if not profiles:
        raise ValueError("No profiles specified")
    if dict(profiles.get("Runner V1 Frozen", {})) != v2.BASELINE:
        raise ValueError("Frozen baseline modified")
    if dict(profiles.get("Runner Challenger 220", {})) != PROFILES["Runner Challenger 220"]:
        raise ValueError("Frozen challenger #220 modified")
    return profiles


def source_digest(records):
    """Full OHLC path and causal feature fingerprint, stable over read order."""
    digest = hashlib.sha256()
    for r in sorted(records, key=lambda x: x["key"]):
        header = {"key": r["key"], "ready_ms": r["ready_ms"],
                  "retest_ts": r["retest_ts"], "side": r["side"],
                  "values": list(r["values"]), "reference": r["reference"],
                  "bars": len(r["bars"])}
        digest.update(json.dumps(header, sort_keys=True, separators=(",", ":")).encode())
        digest.update(b"\n")
        for bar in r["bars"]:
            digest.update(struct.pack("<qdddd", int(bar[0]), *(float(x) for x in bar[1:])))
    return digest.hexdigest()


def _event(row, kind, ts, **extra):
    return {"strategy": row["strategy"], "event_key": row["event_key"],
            "kind": kind, "timestamp_ms": int(ts), **extra}


def _compute_mark_curve(accepted_trades, initial_equity, strategy, start_ms=None):
    """One-minute close marks; intraminute extrema are NOT known in sequence."""
    curve = []
    initial_ts = (start_ms if start_ms is not None else
                  min((t["ready_ms"] for t in accepted_trades), default=0))
    curve.append({"strategy": strategy, "timestamp_ms": initial_ts,
                  "wallet_usdt": initial_equity, "marked_equity_usdt": initial_equity,
                  "open_positions": 0, "mark_price": None})
    for t in accepted_trades:
        before = t["wallet_before_usdt"]
        # Pending orders reserve the slot, but do not change equity.
        curve.append({"strategy": strategy, "timestamp_ms": t["ready_ms"],
                      "wallet_usdt": before, "marked_equity_usdt": before,
                      "open_positions": 0, "mark_price": None})
        if t["status"] not in TERMINAL:
            # When execution is uncertain, leave the approximate equity flat,
            # but flag audit as INCOMPLETE. Do not fabricate trade PnL.
            continue
        fill_ts = t["fill_ts"]
        exit_ts = t["exit_ts"]
        entry = t["fill_price"]
        direction = 1.0 if t["side"] == "LONG" else -1.0
        entry_charge = t["entry_cost_usdt"]
        for bar in t["bars"]:
            ts = int(bar[0])
            if ts < fill_ts or ts > exit_ts:
                continue
            if ts == exit_ts:
                curve.append({"strategy": strategy, "timestamp_ms": ts + MINUTE,
                              "wallet_usdt": t["wallet_after_usdt"],
                              "marked_equity_usdt": t["wallet_after_usdt"],
                              "open_positions": 0, "mark_price": float(t["exit_price"])})
                break
            mark = float(bar[4])
            unreal = t["notional_usdt"] * direction * (mark / entry - 1)
            curve.append({"strategy": strategy, "timestamp_ms": ts + MINUTE,
                          "wallet_usdt": before,
                          "marked_equity_usdt": before + unreal - entry_charge,
                          "open_positions": 1, "mark_price": mark})
    # Multiple points at the same instant are sequential transitions, with the
    # last event winning at that timestamp. No future bars affect earlier marks.
    curve.sort(key=lambda c: c["timestamp_ms"])
    merged = {}
    for c in curve:
        merged[c["timestamp_ms"]] = c
    points = list(merged.values())
    high = initial_equity
    worst = 0.0
    for p in points:
        high = max(high, p["marked_equity_usdt"])
        dd = 100.0 * (p["marked_equity_usdt"] / high - 1.0) if high else 0
        p["drawdown_pct"] = dd
        worst = min(worst, dd)
    return points, worst


def simulate_strategy(records, name, params, settings=None, costs=None):
    """Run one deterministic one-slot portfolio, preserving every decision.

    Order priorities: ready_ms ascending; if equal, larger *known* Room 1h
    first; if still tied event key ascending. An outstanding limit ORDER reserves
    the only slot until expiry. Open trades reserve it through exit bar close.
    Later signals cannot wait and enter after slot frees: they're skipped.
    """
    settings = (settings or PortfolioSettings()).validate()
    costs = v2._validated_costs(costs)
    rows = sorted(records, key=lambda r: (
        int(r["ready_ms"]), -float(r["values"][1]), r["key"]))
    wallet = settings.initial_equity
    busy_until = -(10**30)
    decisions, events, trades, accepted, traces = [], [], [], [], []
    counts = Counter()
    booked_costs = 0.0
    underdetermined = False
    insolvency_risk = False
    frozen_uncertainty_reason = None

    for r in rows:
        key, ready, side = r["key"], int(r["ready_ms"]), r["side"]
        row = {"strategy": name, "event_key": key, "symbol": r["symbol"],
               "side": side, "retest_ts": int(r["retest_ts"]), "ready_ms": ready,
               "scanner_first_seen_ms": r.get("first_seen_ms"),
               "context_status": r.get("context_status"),
               "regime_status": r.get("regime_status"),
               "capture_method": r.get("capture_method"),
               "departure": float(r["values"][0]), "room_1h": float(r["values"][1]),
               "volume_rank": float(r["values"][2]), "return_rank": float(r["values"][3])}
        if frozen_uncertainty_reason is not None:
            decisions.append({**row, "decision": "BLOCKED_UNRESOLVED_STATE",
                              "reason": frozen_uncertainty_reason})
            counts["BLOCKED_UNRESOLVED_STATE"] += 1
            continue
        if not v2._eligible(r, params):
            decisions.append({**row, "decision": "FILTER_REJECTED", "reason": "thresholds"})
            counts["FILTER_REJECTED"] += 1
            continue
        if ready < busy_until:
            decisions.append({**row, "decision": "SKIPPED_SLOT_BUSY",
                              "reason": f"slot_busy_through_{busy_until}"})
            counts["SKIPPED_SLOT_BUSY"] += 1
            continue
        if wallet <= 0:
            decisions.append({**row, "decision": "SKIPPED_NO_EQUITY", "reason": "insufficient_wallet"})
            counts["SKIPPED_NO_EQUITY"] += 1
            continue

        outcome = v2.simulate(r, params, costs=costs)
        state = outcome["status"]
        counts[state] += 1
        decisions.append({**row, "decision": "ORDER_SUBMITTED", "reason": state})
        events.append(_event(row, "ORDER_PLACED", ready,
                             limit_price=r["reference"] *
                             (1-params["limit"]/100 if side == "LONG" else 1+params["limit"]/100),
                             ttl_min=params["ttl"], status=state))
        last_window = ready + int(params["ttl"]) * MINUTE
        limit_price = r["reference"] * (1-params["limit"]/100 if side == "LONG" else
                                        1+params["limit"]/100)
        if state in ("NO_FILL", "UNOBSERVABLE_WINDOW"):
            # Persist order watch candles for reproducible no-fill audit.
            first_full = ((ready + MINUTE - 1) // MINUTE) * MINUTE
            for bar in r["bars"]:
                ts, op, high, low, close = bar
                if first_full <= ts and ts + MINUTE <= last_window:
                    traces.append({**row, "stage": "ORDER_WAIT", "timestamp_ms": int(ts),
                                   "open": float(op), "high": float(high), "low": float(low),
                                   "close": float(close), "limit_price": limit_price,
                                   "tp_price": None, "sl_price": None,
                                   "favorable_pct": None, "adverse_pct": None})
        if state == "NO_FILL":
            busy_until = last_window
            events.append(_event(row, "ORDER_EXPIRED", last_window, status=state))
            continue
        if state == "UNOBSERVABLE_WINDOW":
            underdetermined = True
            frozen_uncertainty_reason = f"{key}: order window not observable"
            # Blocking a full order window is conservative, not an inferred fill.
            busy_until = last_window
            events.append(_event(row, "UNRESOLVED_WINDOW", last_window, status=state))
            continue

        fill_ts = outcome.get("fill_ts")
        if fill_ts is None:
            underdetermined = True
            frozen_uncertainty_reason = f"{key}: fill time not observable"
            busy_until = last_window
            events.append(_event(row, "UNRESOLVED_FILL", last_window, status=state))
            continue
        fill_ts = int(fill_ts)
        entry = float(outcome["fill_price"])
        margin = wallet * settings.margin_fraction
        notional = margin * settings.leverage
        ent_liq = outcome["entry_liquidity"]
        entry_fee_pct = costs["maker_fee_pct"] if ent_liq == "MAKER" else costs["taker_fee_pct"]
        entry_slip_pct = 0.0 if ent_liq == "MAKER" else costs["taker_slippage_bps"] / 100.0
        entry_cost = notional * (entry_fee_pct + entry_slip_pct) / 100.0
        events.append(_event(row, "ORDER_FILLED", fill_ts,
                             fill_price=entry, liquidity=ent_liq,
                             notional_usdt=notional, margin_usdt=margin,
                             entry_cost_usdt=entry_cost))
        stop_price = entry * (1-params["sl"]/100 if side == "LONG" else
                              1+params["sl"]/100)
        target_price = entry * (1+params["tp"]/100 if side == "LONG" else
                                1-params["tp"]/100)
        # Full independent OHLC audit from placement until exit, including
        # first fill bar. Each value derives only from persisted 1m OHLC.
        trace_end = outcome.get("exit_ts")
        if trace_end is None:
            trace_end = min(int(r["bars"][-1][0]), fill_ts + v2.HORIZON*MINUTE)
        first_full = ((ready + MINUTE - 1) // MINUTE) * MINUTE
        mfe, mae = 0., 0.
        for bar in r["bars"]:
            ts, op, high, low, close = bar
            if not first_full <= ts <= trace_end:
                continue
            stage = "ORDER_WAIT" if ts < fill_ts else ("EXIT_BAR" if ts == trace_end else "IN_POSITION")
            favorable = adverse = None
            if ts >= fill_ts:
                favorable = 100 * (float(high)/entry-1) if side == "LONG" else 100 * (1-float(low)/entry)
                adverse = 100 * (1-float(low)/entry) if side == "LONG" else 100 * (float(high)/entry-1)
                mfe = max(mfe, favorable)
                mae = max(mae, adverse)
            traces.append({**row, "stage": stage, "timestamp_ms": int(ts),
                           "open": float(op), "high": float(high), "low": float(low),
                           "close": float(close), "limit_price": limit_price,
                           "tp_price": target_price, "sl_price": stop_price,
                           "favorable_pct": favorable, "adverse_pct": adverse,
                           "running_mfe_pct": mfe if ts >= fill_ts else None,
                           "running_mae_pct": mae if ts >= fill_ts else None})
        t = {**row, "status": state, "fill_ts": fill_ts,
             "fill_price": entry, "entry_liquidity": ent_liq,
             "margin_usdt": margin, "notional_usdt": notional,
             "wallet_before_usdt": wallet, "entry_cost_usdt": entry_cost,
             "bars": r["bars"]}
        if state not in TERMINAL:
            underdetermined = True
            frozen_uncertainty_reason = f"{key}: filled trade outcome uncertain"
            # Do NOT treat ambiguity as a winning, losing, or flat trade.
            # Slot might be occupied through horizon; reserve conservatively.
            busy_until = fill_ts + v2.HORIZON * MINUTE
            t.update(exit_ts=None, net_pnl_usdt=None, wallet_after_usdt=None)
            trades.append(t)
            accepted.append(t)
            events.append(_event(row, "UNRESOLVED_POSITION", fill_ts,
                                 status=state, reason="requires finer market data"))
            # A complete equity after this ambiguous trade cannot be certified.
            continue

        exit_ts = int(outcome["exit_ts"])
        net_pct = float(outcome["pnl"])
        gross_pct = float(outcome["gross_pct"])
        cost_pct = float(outcome["cost_pct"])
        exit_liq = outcome["exit_liquidity"]
        exit_fee_pct = costs["maker_fee_pct"] if exit_liq == "MAKER" else costs["taker_fee_pct"]
        exit_slip_pct = 0.0 if exit_liq == "MAKER" else costs["taker_slippage_bps"] / 100.0
        # Exact same convention as V2: additive bps as per-notional costs.
        assert abs(cost_pct - (entry_fee_pct + entry_slip_pct + exit_fee_pct + exit_slip_pct)) < 1e-8
        gross_amount = notional * gross_pct / 100.0
        net_amount = notional * net_pct / 100.0
        exit_cost = notional * (exit_fee_pct + exit_slip_pct) / 100.0
        next_wallet = wallet + net_amount
        implied_exit = entry * (1 + gross_pct / 100 if side == "LONG" else 1 - gross_pct / 100)
        if next_wallet <= 0:
            insolvency_risk = True
        # Isolated leverage 3 liquidation is NOT modeled. Flag if bar extremes
        # breach maintenance-free naive 100/leverage adverse threshold.
        visited = [b for b in r["bars"] if fill_ts <= int(b[0]) <= exit_ts]
        worst_adverse = max((
            (1-float(b[3])/entry)*100 if side == "LONG" else
            (float(b[2])/entry-1)*100 for b in visited), default=0.0)
        liquidation_risk = worst_adverse >= 100.0 / settings.leverage
        insolvency_risk = insolvency_risk or liquidation_risk
        if insolvency_risk:
            frozen_uncertainty_reason = f"{key}: liquidation/insolvency risk not modeled"
        booked_costs += entry_cost + exit_cost
        t.update(exit_ts=exit_ts, exit_price=implied_exit,
                 exit_liquidity=exit_liq, net_pct=net_pct, gross_pct=gross_pct,
                 cost_pct=cost_pct, gross_pnl_usdt=gross_amount,
                 net_pnl_usdt=net_amount, exit_cost_usdt=exit_cost,
                 wallet_after_usdt=next_wallet,
                 adverse_extreme_pct=worst_adverse,
                 liquidation_risk_unmodeled=liquidation_risk)
        accepted.append(t)
        trades.append(t)
        events.append(_event(row, "POSITION_EXIT", exit_ts + MINUTE,
                             status=state, exit_price=implied_exit,
                             net_pnl_usdt=net_amount, wallet_after_usdt=next_wallet))
        wallet = next_wallet
        busy_until = exit_ts + MINUTE

    first_ready = min((int(r["ready_ms"]) for r in rows), default=0)
    equity, max_dd = _compute_mark_curve(accepted, settings.initial_equity, name, first_ready)
    complete = [t for t in trades if t["status"] in TERMINAL]
    positive = [t["net_pnl_usdt"] for t in complete if t["net_pnl_usdt"] > 0]
    negative = [-t["net_pnl_usdt"] for t in complete if t["net_pnl_usdt"] < 0]
    summary = {"strategy": name, "engine_version": ENGINE_VERSION,
               "equity_initial_usdt": settings.initial_equity,
               "equity_final_realized_usdt": (None if underdetermined or insolvency_risk else wallet),
               "last_known_wallet_usdt": wallet,
               "pnl_realized_usdt": (None if underdetermined or insolvency_risk else wallet - settings.initial_equity),
               "return_realized_pct": (None if underdetermined or insolvency_risk else
                                        (wallet / settings.initial_equity - 1) * 100),
               "max_marked_drawdown_pct": max_dd,
               "total_source_events": len(rows),
               "filter_eligible": sum(v2._eligible(r, params) for r in rows),
               "orders_placed": sum(d["decision"] == "ORDER_SUBMITTED" for d in decisions),
               "resolved_trades": len(complete),
               "win_rate_pct": 100*len(positive)/len(complete) if complete else None,
               "profit_factor": (sum(positive)/sum(negative) if negative else None),
               "total_fees_and_slippage_usdt": booked_costs,
               "skipped_slot_busy": counts["SKIPPED_SLOT_BUSY"],
               "unresolved": sum(counts[x] for x in UNRESOLVED),
               "frozen_uncertainty_reason": frozen_uncertainty_reason,
               "outcome_counts": dict(counts),
               "audit_status": ("INCOMPLETE_UNRESOLVED_OR_LIQUIDATION" if
                                (underdetermined or insolvency_risk) else
                                "COMPLETE_1M_ESTIMATED"),
               "caveat": "Historical simulated equity, NOT exchange-executed; 1m OHLC fill assumption; no funding or exchange liquidation."}
    # Remove volatile candle arrays from exported trades; retain them only while
    # building 1m equity curves. The original read-only Auto Paths own the bars.
    public_trades = [{k: v for k, v in t.items() if k != "bars"} for t in trades]
    return {"summary": summary, "decisions": decisions, "events": events,
            "trades": public_trades, "equity": equity, "path_traces": traces,
            "settings": asdict(settings), "parameters": dict(params), "costs": costs}


def run_comparison(records, *, settings=None, costs=None, profiles=None):
    settings = (settings or PortfolioSettings()).validate()
    profiles = validate_profiles(profiles or PROFILES)
    return {name: simulate_strategy(records, name, params, settings, costs)
            for name, params in profiles.items()}


def _flat_csv(path, records):
    if not records:
        path.write_text("\n", encoding="utf-8")
        return
    fields = sorted({k for row in records for k in row})
    with path.open("w", encoding="utf-8", newline="") as f:
        out = csv.DictWriter(f, fieldnames=fields)
        out.writeheader()
        for row in records:
            out.writerow({k: json.dumps(v, sort_keys=True) if isinstance(v, (dict, list))
                          else v for k, v in row.items()})


def export_comparison(comparison, records, destination, *, provenance=None,
                      cohort="LIVE_OBSERVED_RETROSPECTIVE_RESEARCH"):
    """Create NEW isolated run folder; never overwrite, never touch inputs."""
    dest = Path(destination)
    dest.mkdir(parents=True, exist_ok=False)
    provenance = provenance or {}
    digest = source_digest(records)
    manifest = {"engine_version": ENGINE_VERSION,
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "source_digest_sha256": digest,
                "cohort": str(cohort),
                "source_records": len(records), "profiles": {k: v["parameters"] for k, v in comparison.items()},
                "portfolio_settings": next(iter(comparison.values()))["settings"],
                "cost_assumptions": next(iter(comparison.values()))["costs"],
                "provenance": provenance,
                "immutability_note": "Run folder create-only; source SQLite untouched"}
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (dest / "summary.json").write_text(json.dumps({k: v["summary"] for k, v in comparison.items()},
                                                  indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    records_by_name = [(category, [row for result in comparison.values() for row in result[category]])
                       for category in ("decisions", "events", "trades", "equity", "path_traces")]
    for category, rows in records_by_name:
        _flat_csv(dest / f"{category}.csv", rows)
    con = sqlite3.connect(dest / "portfolio_ledger_v1.sqlite3")
    try:
        for category, rows in records_by_name:
            # JSON row makes the ledger schema stable across parameter versions.
            con.execute(f'CREATE TABLE "{category}" (id INTEGER PRIMARY KEY, strategy TEXT, event_key TEXT, timestamp_ms INTEGER, payload_json TEXT NOT NULL)')
            con.executemany(f'INSERT INTO "{category}"(strategy,event_key,timestamp_ms,payload_json) VALUES (?,?,?,?)', (
                (row.get("strategy"), row.get("event_key"),
                 row.get("timestamp_ms", row.get("ready_ms", row.get("fill_ts"))),
                 json.dumps(row, sort_keys=True, ensure_ascii=False)) for row in rows))
        con.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value_json TEXT NOT NULL)')
        con.execute('INSERT INTO metadata VALUES (?,?)', ("manifest", json.dumps(manifest, sort_keys=True)))
        con.execute('INSERT INTO metadata VALUES (?,?)', ("summary", json.dumps({k:v["summary"] for k,v in comparison.items()})))
        con.commit()
    finally:
        con.close()
    return dest
