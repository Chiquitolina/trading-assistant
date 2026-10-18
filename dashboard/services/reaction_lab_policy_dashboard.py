"""Read-only helpers for Portfolio Policy Research UI (phase 4.1).

No trading strategy, historical runner, selection engine or data file is mutated.
All portfolio data are independently simulated (not sum of shadow candidates).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

import pandas as pd

ENGINE = "candidate-portfolio-policy-replay-v1"
POLICY_BASE = "BASELINE_ROOM"
REQUIRED = (
    "manifest.json", "summary.json", "strategy_policy_summary.csv",
    "policy_competitions.csv", "policy_divergences.csv",
    "policy_decisions.csv", "policy_trades.csv", "policy_equity.csv",
)
POLICY_LABELS = {
    "BASELINE_ROOM": "Room original",
    "DIRECTIONAL_STRENGTH_VS_BTC_4H_PROXY": "Strength vs BTC 4h (proxy)",
    "STRENGTH_THEN_ROOM": "Strength → Room",
    "ROOM_THEN_STRENGTH": "Room → Strength",
    "DIRECTIONAL_RETURN_RANK_4H": "Return Rank direccional",
    "VOLUME_RANK_4H": "Volume Rank 4h",
    "RELATIVE_VOLUME_4H": "Volumen relativo 4h",
}


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def validate_policy_run(root, selection_root):
    """Fail closed on wrong cohort and absent baseline/reference parity.

    The manifest hashes historical *inputs*; exported CSVs are NOT signed. This
    view also verifies their internal strategy/policy linkage, but not authenticity.
    """
    root, selection_root = Path(root), Path(selection_root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("engine") != ENGINE:
        raise ValueError("El motor de policy run no corresponde a la fase 4")
    hashes = manifest.get("source_hashes", {})
    for field, filename in (("selection_manifest", "selection_manifest.json"),
                            ("selection_snapshot", "candidate_factor_snapshots.csv")):
        if not (selection_root / filename).is_file():
            raise ValueError(f"Selection export sin {filename}")
        if hashes.get(field) != sha256(selection_root / filename):
            raise ValueError(f"Policy run y Selection export no coinciden: {field}")
    strategies = manifest.get("strategies", [])
    policies = manifest.get("policies", [])
    if not strategies or not policies or POLICY_BASE not in policies:
        raise ValueError("Faltan estrategias/políticas o baseline en el manifest")
    for strategy in strategies:
        parity = manifest.get("reference_parity", {}).get(strategy, {})
        if not all(int(parity.get(k, 0)) > 0 for k in
                   ("verified_decisions", "verified_trades", "verified_equity_points")):
            raise ValueError(f"Falta paridad con run congelado: {strategy}")
        if parity.get("reference_source_digest_sha256") != manifest.get("source_digest_sha256"):
            raise ValueError(f"Source digest de baseline no coincide: {strategy}")
    for filename in REQUIRED:
        if not (root / filename).is_file():
            raise ValueError(f"Falta reporte del Policy Replay: {filename}")
    summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
    if not summary.get("base_parity_verified"):
        raise ValueError("Policy Replay no tiene baseline parity")
    if set(summary.get("strategies", [])) != set(strategies) or set(summary.get("policies", [])) != set(policies):
        raise ValueError("Policy summary y manifest discrepan")
    return manifest


def find_policy_runs(selection_root, *, custom=None):
    """Locate create-only policy exports and accept only hash-linked runs."""
    selection_root = Path(selection_root)
    if custom is None:
        custom = os.environ.get("CANDIDATE_POLICY_REPLAY_DIR", "")
    folders = []
    if custom and custom.strip():
        folders.append(Path(custom.strip()))
    parent = selection_root.parent
    for pat in ("runner_policy_*", "candidate_policy_*", "policy_replay_*"):
        folders.extend(p for p in parent.glob(pat) if p.is_dir())
    unique, valid = set(), []
    for folder in folders:
        try:
            path = folder.resolve()
            if path in unique:
                continue
            unique.add(path)
            manifest = validate_policy_run(path, selection_root)
            valid.append((path, manifest))
        except (FileNotFoundError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
    return sorted(valid, key=lambda it: (it[0] / "manifest.json").stat().st_mtime_ns, reverse=True)


def csv_columns(path):
    return pd.read_csv(path, nrows=0).columns.tolist()


def load_core(root, manifest):
    """Only small tables (summary, competitions, divergence)."""
    root = Path(root)
    summary = pd.read_csv(root / "strategy_policy_summary.csv", keep_default_na=False)
    competitions = pd.read_csv(root / "policy_competitions.csv", keep_default_na=False)
    divergence = pd.read_csv(root / "policy_divergences.csv", keep_default_na=False)
    needed_summary = {"strategy", "policy", "equity_final_realized_usdt", "return_realized_pct",
                      "max_marked_drawdown_pct", "profit_factor", "resolved_trades", "orders_placed"}
    needed_comp = {"strategy", "policy", "ready_ms", "eligible_event_keys", "order_submitted_event_key",
                   "slot_was_available_under_policy"}
    needed_diff = {"strategy", "policy", "event_key", "ready_ms", "baseline_decision", "policy_decision",
                   "baseline_reason", "policy_reason"}
    for name, table, expected in (("summary", summary, needed_summary),
                                  ("competitions", competitions, needed_comp),
                                  ("divergences", divergence, needed_diff)):
        missing = expected - set(table.columns)
        if missing:
            raise ValueError(f"Columnas faltantes en {name}: {sorted(missing)}")
    strategies, policies = set(manifest["strategies"]), set(manifest["policies"])
    expected_pairs = {(s, p) for s in strategies for p in policies}
    observed_pairs = set(zip(summary["strategy"], summary["policy"]))
    if observed_pairs != expected_pairs or len(summary) != len(expected_pairs):
        raise ValueError("Cobertura incompleta/duplicada del resumen de carteras")
    for name, table in (("competitions", competitions), ("divergences", divergence)):
        if not set(table["strategy"]).issubset(strategies) or not set(table["policy"]).issubset(policies):
            raise ValueError(f"{name} contiene combinaciones ajenas al run")
    if len(competitions) and competitions.duplicated(["strategy", "policy", "ready_ms"]).any():
        raise ValueError("Grupos de competición duplicados")
    for table in (competitions, divergence):
        table["ready_ms"] = pd.to_numeric(table["ready_ms"], errors="raise").astype("int64")
    if len(divergence) and divergence.duplicated(["strategy", "policy", "event_key"]).any():
        raise ValueError("Divergencias duplicadas")
    for col in ("equity_final_realized_usdt", "return_realized_pct", "max_marked_drawdown_pct",
                "profit_factor", "resolved_trades", "orders_placed", "win_rate_pct"):
        if col in summary:
            summary[col] = pd.to_numeric(summary[col], errors="coerce")
    return summary, competitions, divergence


def _bool(x):
    return str(x).strip().lower() in ("true", "1", "yes")


def competition_comparison(competitions, strategy, policy):
    """Two portfolio decisions at the same ready_ms, not shadow hindsight."""
    keep = competitions[competitions["strategy"] == strategy]
    base = keep[keep["policy"] == POLICY_BASE].copy()
    alt = keep[keep["policy"] == policy].copy()
    if base.empty or alt.empty:
        return pd.DataFrame()
    pairs = base.merge(alt, on=["strategy", "ready_ms"], suffixes=("_baseline", "_policy"),
                       how="outer", validate="one_to_one", indicator=True)
    if not pairs["_merge"].eq("both").all():
        raise ValueError("Competencias baseline y alternativa no tienen el mismo universo")
    if not pairs["eligible_event_keys_baseline"].eq(pairs["eligible_event_keys_policy"]).all():
        raise ValueError("Candidatas elegibles distintas en el mismo instante")
    pairs["changed_pick"] = (
        pairs["order_submitted_event_key_baseline"] != pairs["order_submitted_event_key_policy"]
    )
    pairs["baseline_slot_free"] = pairs["slot_was_available_under_policy_baseline"].map(_bool)
    pairs["policy_slot_free"] = pairs["slot_was_available_under_policy_policy"].map(_bool)
    return pairs.sort_values("ready_ms").reset_index(drop=True)


def divergence_after(divergences, strategy, policy, ready_ms):
    """Observed divergences at/after a competition (not causal attribution)."""
    x = divergences[(divergences["strategy"] == strategy) &
                    (divergences["policy"] == policy)].sort_values(["ready_ms", "event_key"]).copy()
    if x.empty:
        return x, False
    had_prior = (x["ready_ms"] < int(ready_ms)).any()
    y = x[x["ready_ms"] >= int(ready_ms)].copy()
    y["change_type"] = y.apply(
        lambda r: "Decisión distinta" if r["baseline_decision"] != r["policy_decision"]
        else "Misma decisión / reserva o resultado distinto", axis=1)
    return y, bool(had_prior)


def load_trade_detail(root, strategy, policies, candidate_keys):
    """Only trades for requested candidate keys, streaming CSV for large runs."""
    requested = set(candidate_keys)
    if not requested:
        return pd.DataFrame()
    chunks = []
    for part in pd.read_csv(Path(root) / "policy_trades.csv", chunksize=80000, keep_default_na=False):
        selected = part[(part["strategy"] == strategy) & part["policy"].isin(policies) &
                        part["event_key"].isin(requested)]
        if not selected.empty:
            chunks.append(selected)
    return pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()


def load_decisions_window(root, strategy, policies, start_ms, end_ms):
    """Time-local decision audit, without loading the full 290-symbol ledger."""
    results = []
    path = Path(root) / "policy_decisions.csv"
    for part in pd.read_csv(path, chunksize=80000, keep_default_na=False):
        mask = ((part["strategy"] == strategy) & part["policy"].isin(policies) &
                pd.to_numeric(part["ready_ms"], errors="coerce").between(start_ms, end_ms))
        if mask.any():
            results.append(part.loc[mask])
    return pd.concat(results, ignore_index=True) if results else pd.DataFrame()


def equity_curve(root, strategy, policies, *, bucket_minutes=60):
    """Stream and downsample marked equity/wallet per time bucket.

    Preserve each bucket's first, last, low and high marked equity so short
    drawdown spikes remain visible. Summary metrics always use exact engine CSV.
    """
    if bucket_minutes < 1:
        raise ValueError("bucket_minutes >= 1")
    usecols = ["strategy", "policy", "timestamp_ms", "wallet_usdt", "marked_equity_usdt"]
    all_cols = set(csv_columns(Path(root) / "policy_equity.csv"))
    if not set(usecols).issubset(all_cols):
        raise ValueError("Equity CSV incompleto")
    buckets = {}
    width = bucket_minutes * 60000
    for part in pd.read_csv(Path(root) / "policy_equity.csv", usecols=usecols,
                            chunksize=100000, keep_default_na=False):
        part = part[(part["strategy"] == strategy) & part["policy"].isin(policies)].copy()
        if part.empty:
            continue
        for col in ("timestamp_ms", "wallet_usdt", "marked_equity_usdt"):
            part[col] = pd.to_numeric(part[col], errors="raise")
        if not all(math.isfinite(float(v)) for v in part["marked_equity_usdt"].iloc[:100]):
            raise ValueError("Curva con equity no finita")
        part["bucket"] = part["timestamp_ms"] // width
        for (policy, bucket), group in part.groupby(["policy", "bucket"], sort=False):
            # Keep an ordered set of points with extrema from each input chunk.
            group = group.sort_values("timestamp_ms")
            special = group.loc[list((group.index[0], group.index[-1],
                                     group["marked_equity_usdt"].idxmin(),
                                     group["marked_equity_usdt"].idxmax()))]
            entry = buckets.setdefault((policy, int(bucket)), {})
            for _, row in special.iterrows():
                entry[int(row["timestamp_ms"])] = (float(row["marked_equity_usdt"]),
                                                    float(row["wallet_usdt"]))
            # Each bucket across file chunks can have up to 4 points per chunk;
            # reduce to global first/last/min/max before the next chunk.
            if len(entry) > 4:
                first, last = min(entry), max(entry)
                low = min(entry, key=lambda t: entry[t][0])
                high = max(entry, key=lambda t: entry[t][0])
                buckets[(policy, int(bucket))] = {t: entry[t] for t in (first, last, low, high)}
    rows = []
    for (policy, _), data in buckets.items():
        for ms, (marked, wallet) in data.items():
            rows.append({"policy":policy, "timestamp_ms":ms,
                         "marked_equity_usdt":marked, "wallet_usdt":wallet})
    if not rows:
        return pd.DataFrame(columns=["policy", "timestamp_ms", "marked_equity_usdt", "wallet_usdt"])
    frame = pd.DataFrame(rows).drop_duplicates(["policy", "timestamp_ms"])
    return frame.sort_values(["policy", "timestamp_ms"]).reset_index(drop=True)
