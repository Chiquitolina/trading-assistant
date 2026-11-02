"""HLL | auditoría de join de Huella PRE vs. mecanismos POST existentes.

Etapa 1: sólo lectura de fuentes. NO recalcula huellas, mecanismos, entradas
ni ranking. Los únicos archivos que escribe son reportes bajo --output.

Uso desde /home/multiservices-1m:
    python3 hll_fingerprint_mechanism_join_audit_v1.py --plan
    python3 hll_fingerprint_mechanism_join_audit_v1.py --run
    python3 hll_fingerprint_mechanism_join_audit_v1.py --run --output reports/.../audit_v1
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

VERSION = "HLL_FINGERPRINT_MECHANISM_JOIN_AUDIT_V1"
DEFAULT_OUTPUT = "reports/horizontal_level_fingerprint_mechanism_join_v1/audit_v1"

SOURCE_FILES = {
    "episode_map": "reports/horizontal_level_structural_overlap_audit_v1/full290_v1/attempt_episode_map.csv",
    "fingerprints": "reports/horizontal_level_structural_similarity_v1/first_attempts_290_v1/fingerprints_first_pre.csv",
    "normalizers": "reports/horizontal_level_structural_similarity_v1/first_attempts_290_v1/normalizers.json",
    "pre_cohort": "reports/horizontal_level_structure_quality_v282/runs/sep290_pre_all_frozen_v282/pre_cohort.csv",
    "entry_events": "reports/horizontal_level_full290_v301/entries_FIRST_PER_LEVEL/entry_events.csv",
    "entry_coverage": "reports/horizontal_level_full290_v301/entries_FIRST_PER_LEVEL/attempt_route_coverage.csv",
    "open_discrepancies": "reports/horizontal_level_full290_v301/entries_FIRST_PER_LEVEL/entry_open_discrepancies.csv",
    "continuation": "reports/horizontal_level_continuation_v29/runs/sep290_full_v301/continuation_events.csv",
    "mechanisms": "reports/horizontal_level_mechanisms_v292/runs/sep290_full_v301/mechanism_attempts.csv",
}
MANIFEST_FILES = {
    "similarity": "reports/horizontal_level_structural_similarity_v1/first_attempts_290_v1/manifest.json",
    "episodes": "reports/horizontal_level_structural_overlap_audit_v1/full290_v1/manifest.json",
    "pre": "reports/horizontal_level_structure_quality_v282/runs/sep290_pre_all_frozen_v282/manifest.json",
    "entries": "reports/horizontal_level_full290_v301/entries_FIRST_PER_LEVEL/manifest.json",
    "continuation": "reports/horizontal_level_continuation_v29/runs/sep290_full_v301/manifest.json",
    "mechanisms": "reports/horizontal_level_mechanisms_v292/runs/sep290_full_v301/manifest.json",
}
ENTRY_ROUTES = ("FLIP_1M", "V29_CONTINUATION_30M", "V29_RECLAIM_30M")
BRANCHES = ("BREAKOUT_CONTINUATION", "RECLAIM_CONTINUATION")
ENTRY_STATUSES = ("NO_SIGNAL", "ENTRY_VERIFIED", "SIGNAL_OPEN_MISSING")


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def read_rows(path: Path):
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"CSV sin encabezados: {path}")
        yield from reader


def write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def stable_ts(v):
    """Compare only equivalent textual UTC-millisecond timestamps (no coercion into float)."""
    return str(v or "").strip()


def assess(root: Path):
    """Return all reports in memory; never changes inputs or dashboard state."""
    root = Path(root)
    paths = {k: root / v for k, v in SOURCE_FILES.items()}
    man_paths = {k: root / v for k, v in MANIFEST_FILES.items()}
    missing = [str(p) for p in (*paths.values(), *man_paths.values()) if not p.is_file()]
    if missing:
        raise FileNotFoundError("Fuentes requeridas ausentes:\n" + "\n".join(missing))

    mismatches = []
    errors = Counter()
    stats = Counter()
    source_sha = {k: digest(p) for k, p in paths.items()}
    manifests = {k: json.loads(p.read_text(encoding="utf-8")) for k, p in man_paths.items()}

    def issue(kind, source, attempt_id="", detail="", expected="", actual=""):
        errors[kind] += 1
        mismatches.append({"kind": kind, "source": source, "attempt_id": attempt_id,
                           "detail": detail, "expected": expected, "actual": actual})

    def verify_hash(field, claimed, actual):
        if not claimed or claimed != actual:
            issue("HASH_OR_MISSING_CLAIM", field, detail="Hash SHA-256 de procedencia",
                  expected=str(claimed or "(ausente)"), actual=str(actual))

    sim, eps, pre, ent, cont, mech = (manifests[x] for x in
                                       ("similarity", "episodes", "pre", "entries", "continuation", "mechanisms"))
    verify_hash("similarity.fingerprints_sha256", sim.get("fingerprints_sha256"), source_sha["fingerprints"])
    verify_hash("similarity.normalizers_sha256", sim.get("normalizers_sha256"), source_sha["normalizers"])
    verify_hash("similarity.source_map_sha256", sim.get("source_map_sha256"), source_sha["episode_map"])
    verify_hash("episodes.outputs_sha256.attempt_episode_map.csv",
                eps.get("outputs_sha256", {}).get("attempt_episode_map.csv"), source_sha["episode_map"])
    upstream = {
        "episodes.source_sha256.cohort": eps.get("source_sha256", {}).get("cohort"),
        "entries.source.pre_sha256": ent.get("source", {}).get("pre_sha256"),
        "continuation.source_sha256": cont.get("source_sha256"),
        "mechanisms.source_prehash": mech.get("source_prehash"),
    }
    for name, claimed in upstream.items():
        verify_hash(name, claimed, source_sha["pre_cohort"])
    if sim.get("index_cuts") != "EPISODE_FIRST_ONLY":
        issue("WRONG_INDEX_SCOPE", "similarity", expected="EPISODE_FIRST_ONLY", actual=str(sim.get("index_cuts")))
    if ent.get("scope") != "FIRST_PER_LEVEL":
        issue("WRONG_ENTRY_SCOPE", "entries", expected="FIRST_PER_LEVEL", actual=str(ent.get("scope")))
    for name, val in (("episodes", eps.get("attempts")), ("pre", pre.get("original_attempts")),
                      ("entries", ent.get("source", {}).get("n_attempts")),
                      ("continuation", cont.get("attempts")), ("mechanisms", mech.get("attempts"))):
        if val != eps.get("attempts"):
            issue("UNIVERSE_MISMATCH", name, expected=str(eps.get("attempts")), actual=str(val))
    for name, val in (("episodes", eps.get("symbols")), ("pre", pre.get("symbols")),
                      ("entries", ent.get("source", {}).get("n_symbols")),
                      ("continuation", cont.get("symbols")), ("mechanisms", mech.get("symbols"))):
        if val != eps.get("symbols"):
            issue("SYMBOL_UNIVERSE_MISMATCH", name, expected=str(eps.get("symbols")), actual=str(val))

    # Canonical identity is the exact CROSS_UP attempt. A heuristic episode is
    # NEVER a key for joining POST to the selected cut.
    base = {}
    for r in read_rows(paths["episode_map"]):
        stats["episode_map_rows"] += 1
        aid = r.get("attempt_id", "")
        if not aid or aid in base:
            issue("DUPLICATE_OR_EMPTY_ATTEMPT_ID", "episode_map", aid)
            continue
        base[aid] = {k: str(r.get(k, "") or "") for k in
                     ("attempt_id", "symbol", "level_id", "pre_closed_ts", "breakout_closed_ts",
                      "episode_id", "episode_first_attempt_id", "is_first_in_episode")}

    def check_identity(r, source, fields=("symbol", "level_id", "pre_closed_ts", "breakout_closed_ts")):
        aid = str(r.get("attempt_id", "") or "")
        canonical = base.get(aid)
        if canonical is None:
            issue("NOT_IN_EPISODE_MAP", source, aid)
            return None
        for key in fields:
            if key in r and r[key] not in (None, "") and stable_ts(r[key]) != stable_ts(canonical[key]):
                issue("IDENTITY_MISMATCH", source, aid, detail=key,
                      expected=canonical[key], actual=r[key])
        return canonical

    # Causally defined fingerprint PRE index; covers one cut per episode.
    fingerprint_rows = {}
    episodes_seen = set()
    for r in read_rows(paths["fingerprints"]):
        stats["fingerprint_rows"] += 1
        aid = str(r.get("attempt_id", ""))
        original = check_identity(r, "fingerprints", ("symbol", "level_id", "pre_closed_ts"))
        if aid in fingerprint_rows:
            issue("DUPLICATE_FINGERPRINT", "fingerprints", aid)
            continue
        if original:
            if r.get("episode_id") != original.get("episode_id"):
                issue("EPISODE_ID_MISMATCH", "fingerprints", aid,
                      expected=original.get("episode_id"), actual=r.get("episode_id"))
            if original.get("episode_first_attempt_id") != aid:
                issue("INDEX_NOT_EPISODE_FIRST", "fingerprints", aid,
                      expected=original.get("episode_first_attempt_id"), actual=aid)
            if r.get("episode_id") in episodes_seen:
                issue("DUPLICATE_EPISODE_IN_INDEX", "fingerprints", aid, actual=r.get("episode_id"))
            episodes_seen.add(r.get("episode_id"))
        fingerprint_rows[aid] = r

    if len(base) != eps.get("attempts"):
        issue("ROW_COUNT_MISMATCH", "episode_map", expected=str(eps.get("attempts")), actual=str(len(base)))
    if len(fingerprint_rows) != sim.get("episodes"):
        issue("ROW_COUNT_MISMATCH", "fingerprints", expected=str(sim.get("episodes")),
              actual=str(len(fingerprint_rows)))

    # Separate original state, POST confirmed events, and verified execution.
    mechanisms = {}
    for r in read_rows(paths["mechanisms"]):
        stats["mechanism_rows"] += 1
        aid = r.get("attempt_id", "")
        check_identity(r, "mechanisms")
        if aid in mechanisms:
            issue("DUPLICATE_MECHANISM_ATTEMPT", "mechanisms", aid)
        else:
            mechanisms[aid] = {k: r.get(k, "") for k in
                               ("v26_status", "flip_confirmed_closed_ts", "post_outcome")}
    if stats["mechanism_rows"] != mech.get("attempts"):
        issue("ROW_COUNT_MISMATCH", "mechanisms", expected=str(mech.get("attempts")),
              actual=str(stats["mechanism_rows"]))

    # V2.9 is one event per attempt AND branch; branch names are not entry routes.
    continuation = defaultdict(dict)
    for r in read_rows(paths["continuation"]):
        stats["continuation_rows"] += 1
        aid, branch = r.get("attempt_id", ""), r.get("branch", "")
        # V2.9 calls symbol 'origin_symbol'.
        ident = dict(r)
        if "origin_symbol" in r:
            ident["symbol"] = r["origin_symbol"]
        check_identity(ident, "continuation", ("symbol", "level_id"))
        if branch not in BRANCHES:
            issue("UNKNOWN_BRANCH", "continuation", aid, actual=branch)
        if branch in continuation[aid]:
            issue("DUPLICATE_ATTEMPT_BRANCH", "continuation", aid, actual=branch)
        else:
            continuation[aid][branch] = {k: r.get(k, "") for k in
                                         ("status", "confirmed_ts", "signal_id", "next_open_ts", "next_open")}
    if stats["continuation_rows"] != cont.get("total_rows"):
        issue("ROW_COUNT_MISMATCH", "continuation", expected=str(cont.get("total_rows")),
              actual=str(stats["continuation_rows"]))
    for aid in base:
        if aid not in mechanisms:
            issue("MISSING_MECHANISM", "mechanisms", aid)
        for branch in BRANCHES:
            if branch not in continuation.get(aid, {}):
                issue("MISSING_CONTINUATION_BRANCH", "continuation", aid, actual=branch)

    # The FIRST_PER_LEVEL cohort has 3 route status rows per covered attempt.
    coverage = defaultdict(dict)
    for r in read_rows(paths["entry_coverage"]):
        stats["entry_coverage_rows"] += 1
        aid, route = r.get("attempt_id", ""), r.get("route", "")
        check_identity(r, "entry_coverage", ("symbol", "level_id", "breakout_closed_ts"))
        if route not in ENTRY_ROUTES:
            issue("UNKNOWN_ENTRY_ROUTE", "entry_coverage", aid, actual=route)
        if r.get("status") not in ENTRY_STATUSES:
            issue("UNKNOWN_ENTRY_STATUS", "entry_coverage", aid, actual=r.get("status"))
        if route in coverage[aid]:
            issue("DUPLICATE_COVERAGE_ROUTE", "entry_coverage", aid, actual=route)
        else:
            coverage[aid][route] = {k: r.get(k, "") for k in
                                    ("status", "entry_ts", "entry_price", "signal_ts")}

    # Coverage's 30m reference OPEN is intentionally NOT the final executable
    # 1m OPEN in entry_events.csv. Read the frozen reconciliation audit instead
    # of wrongly treating all nine known source discrepancies as invalid joins.
    open_audit_by_key = {}
    for r in read_rows(paths["open_discrepancies"]):
        stats["open_audit_rows"] += 1
        key = (r.get("attempt_id", ""), r.get("route", ""))
        if key in open_audit_by_key:
            issue("DUPLICATE_OPEN_AUDIT_KEY", "open_discrepancies", key[0], actual=key[1])
        else:
            open_audit_by_key[key] = r
    if stats["open_audit_rows"] != ent.get("entry_open_discrepancies"):
        issue("OPEN_AUDIT_COUNT_MISMATCH", "open_discrepancies",
              expected=str(ent.get("entry_open_discrepancies")),
              actual=str(stats["open_audit_rows"]))

    entries = {}
    for r in read_rows(paths["entry_events"]):
        stats["entry_event_rows"] += 1
        aid, route = r.get("attempt_id", ""), r.get("route", "")
        check_identity(r, "entry_events")
        key = aid, route
        if key in entries:
            issue("DUPLICATE_ENTRY_ROUTE", "entry_events", aid, actual=route)
        else:
            entries[key] = {k: r.get(k, "") for k in
                            ("status", "entry_ts", "entry_price", "signal_ts", "entry_id")}
        if r.get("status") != "ENTRY_VERIFIED":
            issue("NONVERIFIED_IN_ENTRY_EVENTS", "entry_events", aid, actual=r.get("status"))
    if stats["entry_event_rows"] != ent.get("entries"):
        issue("ROW_COUNT_MISMATCH", "entry_events", expected=str(ent.get("entries")),
              actual=str(stats["entry_event_rows"]))
    observed_price_differences = []
    for aid, routes in coverage.items():
        if set(routes) != set(ENTRY_ROUTES):
            issue("INCOMPLETE_COVERAGE_ROUTES", "entry_coverage", aid,
                  expected=",".join(ENTRY_ROUTES), actual=",".join(sorted(routes)))
        if aid not in fingerprint_rows:
            issue("COVERAGE_NOT_EPISODE_FIRST", "entry_coverage", aid)
        for route, e in routes.items():
            detail = entries.get((aid, route))
            if e["status"] == "ENTRY_VERIFIED" and detail is None:
                issue("VERIFIED_WITHOUT_ENTRY_EVENT", "entry_coverage", aid, actual=route)
            if e["status"] != "ENTRY_VERIFIED" and detail is not None:
                issue("ENTRY_EVENT_WITHOUT_VERIFIED_COVERAGE", "entry_events", aid, actual=route)
            if detail is not None:
                for field in ("entry_ts", "signal_ts"):
                    if e.get(field) not in (None, "") and str(e[field]) != str(detail[field]):
                        issue("ENTRY_COVERAGE_DETAIL_MISMATCH", "entry_events", aid,
                              detail=f"{route}.{field}", expected=e[field], actual=detail[field])
                try:
                    ref_open, exe_open = float(e["entry_price"]), float(detail["entry_price"])
                    if not all(math.isfinite(v) and v > 0 for v in (ref_open, exe_open)):
                        raise ValueError('invalid price')
                    # Frozen reconcile_verified_opens() exact-source tolerance.
                    changed = abs(exe_open-ref_open) > max(1e-10, abs(ref_open)*1e-7)
                except (ValueError, TypeError):
                    changed = False
                    issue("INVALID_VERIFIED_ENTRY_PRICE", "entry_events", aid, actual=route)
                a = open_audit_by_key.get((aid, route))
                if changed:
                    observed_price_differences.append({"attempt_id": aid, "symbol": base.get(aid, {}).get("symbol", ""),
                        "route": route, "signal_reference_open_30m": e["entry_price"],
                        "execution_open_1m": detail["entry_price"], "reconciliation_entry_id": detail["entry_id"]})
                if changed != (a is not None):
                    issue("OPEN_AUDIT_MEMBERSHIP_MISMATCH", "open_discrepancies", aid,
                          actual=route, detail="Cobertura 30m vs ejecución 1m")
                if a is not None:
                    for field, value in (("entry_id", detail["entry_id"]),
                                          ("entry_ts", detail["entry_ts"])):
                        if str(a.get(field, "")) != str(value):
                            issue("OPEN_AUDIT_IDENTITY_MISMATCH", "open_discrepancies", aid,
                                  detail=f"{route}.{field}", expected=str(value), actual=str(a.get(field, "")))
                    try:
                        if not math.isclose(float(a["signal_reference_open_30m"]), ref_open, rel_tol=1e-10, abs_tol=1e-12) or not math.isclose(float(a["execution_open_1m"]), exe_open, rel_tol=1e-10, abs_tol=1e-12):
                            issue("OPEN_AUDIT_PRICE_MISMATCH", "open_discrepancies", aid, actual=route)
                    except (ValueError, TypeError, KeyError):
                        issue("OPEN_AUDIT_PRICE_UNREADABLE", "open_discrepancies", aid, actual=route)
    for aid, route in entries:
        if aid not in coverage or route not in coverage[aid]:
            issue("ENTRY_WITHOUT_COVERAGE_ROUTE", "entry_events", aid, actual=route)

    for (aid, route) in open_audit_by_key:
        if (aid, route) not in entries:
            issue("OPEN_AUDIT_WITHOUT_VERIFIED_ENTRY", "open_discrepancies", aid, actual=route)

    # Audit each indexed PRE candidate without changing its fingerprint or ranking.
    joined = []
    groups = Counter()
    per_route_status = Counter()
    route_pairs = Counter()
    status_mismatches = {r["attempt_id"] for r in mismatches if r["attempt_id"]}
    for aid, fp in fingerprint_rows.items():
        canonical = base.get(aid, {})
        original = mechanisms.get(aid, {})
        events = continuation.get(aid, {})
        routes = coverage.get(aid)
        if routes is None:
            group = "NOT_COVERED"
            statuses = {r: "NOT_COVERED" for r in ENTRY_ROUTES}
        else:
            statuses = {r: routes.get(r, {}).get("status", "MISSING_ROUTE") for r in ENTRY_ROUTES}
            if "ENTRY_VERIFIED" in statuses.values():
                group = "WITH_VERIFIED_ENTRY"
            elif "SIGNAL_OPEN_MISSING" in statuses.values():
                group = "SIGNAL_OPEN_MISSING_NO_ENTRY"
            elif all(s == "NO_SIGNAL" for s in statuses.values()):
                group = "COVERED_NO_SIGNAL"
            else:
                group = "OTHER_COVERED_STATUS"
            for route, status in statuses.items():
                per_route_status[f"{route}|{status}"] += 1
        groups[group] += 1
        verified = [r for r in ENTRY_ROUTES if statuses[r] == "ENTRY_VERIFIED"]
        route_pairs[len(verified)] += 1
        e1 = events.get(BRANCHES[0], {})
        e2 = events.get(BRANCHES[1], {})
        joined.append({
            "attempt_id": aid, "symbol": fp.get("symbol", ""), "episode_id": fp.get("episode_id", ""),
            "level_id": fp.get("level_id", ""), "pre_closed_ts": fp.get("pre_closed_ts", ""),
            "breakout_closed_ts": canonical.get("breakout_closed_ts", ""),
            "fingerprint_status": fp.get("status", ""),
            "identity_audit": "ISSUE" if aid in status_mismatches else "MATCHED",
            "v26_status": original.get("v26_status", "NOT_AVAILABLE"),
            "flip_confirmed_closed_ts": original.get("flip_confirmed_closed_ts", ""),
            "continuation_status": e1.get("status", "NOT_AVAILABLE"),
            "continuation_confirmed_ts": e1.get("confirmed_ts", ""),
            "reclaim_status": e2.get("status", "NOT_AVAILABLE"),
            "reclaim_confirmed_ts": e2.get("confirmed_ts", ""),
            "entry_cohort": "FIRST_PER_LEVEL", "entry_coverage_group": group,
            "flip_entry_status": statuses[ENTRY_ROUTES[0]],
            "continuation_entry_status": statuses[ENTRY_ROUTES[1]],
            "reclaim_entry_status": statuses[ENTRY_ROUTES[2]],
            "verified_entry_routes": ";".join(verified),
            "verified_entry_count": len(verified),
            "post_is_retrospective": "true",
        })

    summary = {
        "version": VERSION,
        "mode": "OFFLINE_READ_ONLY_SOURCE_AUDIT",
        "index_scope": "EPISODE_FIRST_ONLY", "entry_scope": "FIRST_PER_LEVEL",
        "input_counts": dict(sorted(stats.items())),
        "base_distinct_attempts": len(base), "indexed_distinct_attempts": len(fingerprint_rows),
        "entry_coverage_distinct_attempts": len(coverage), "entry_event_distinct_routes": len(entries),
        "observed_open_source_differences": len(observed_price_differences),
        "entries_distinct_attempts": len({aid for aid, _ in entries}),
        "index_entry_groups": dict(sorted(groups.items())),
        "index_entry_route_statuses": dict(sorted(per_route_status.items())),
        "index_verified_routes_per_attempt": {str(k): v for k, v in sorted(route_pairs.items())},
        "mismatch_counts": dict(sorted(errors.items())), "mismatch_total": len(mismatches),
        "is_compatible": not mismatches,
        "warning": "NOT_COVERED = fuera de FIRST_PER_LEVEL, NO equivale a NO_SIGNAL; mecanismos V2.6/V2.9 disponibles por intento. No informa edge, PnL ni recomendación.",
    }
    inventory = {
        "version": VERSION,
        "source_files": {k: {"relative_path": SOURCE_FILES[k], "sha256": v,
                              "bytes": paths[k].stat().st_size} for k, v in source_sha.items()},
        "manifest_files": {k: {"relative_path": MANIFEST_FILES[k], "sha256": digest(man_paths[k])}
                           for k in man_paths},
        "manifest_versions": {k: v.get("version", "") for k, v in manifests.items()},
        "provenance_checks": upstream,
    }
    return joined, mismatches, summary, inventory, observed_price_differences


def run(root: Path, output: Path):
    joined, mismatches, summary, inventory, open_differences = assess(root)
    output.mkdir(parents=True, exist_ok=True)
    write_csv(output / "join_audit.csv", joined, list(joined[0]) if joined else [
        "attempt_id", "episode_id", "identity_audit", "entry_coverage_group"])
    write_csv(output / "mismatches.csv", mismatches,
              ["kind", "source", "attempt_id", "detail", "expected", "actual"])
    write_csv(output / "open_source_differences.csv", open_differences,
              ["attempt_id", "symbol", "route", "signal_reference_open_30m", "execution_open_1m", "reconciliation_entry_id"])
    write_json(output / "coverage_summary.json", summary)
    write_json(output / "source_inventory.json", inventory)
    output_hashes = {name: digest(output / name) for name in
                     ("join_audit.csv", "mismatches.csv", "open_source_differences.csv", "coverage_summary.json", "source_inventory.json")}
    write_json(output / "manifest.json", {
        "version": VERSION, "created_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "OFFLINE_NO_LIVE", "audit_only": True,
        "status": "DONE" if summary["is_compatible"] else "AUDIT_FAILED",
        "output_sha256": output_hashes, "source_index_sha256": inventory["source_files"]["fingerprints"]["sha256"],
        "rows": len(joined), "mismatch_total": summary["mismatch_total"],
        "ranking_or_fingerprint_mutated": False,
    })
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true", help="Listar rutas, no procesar ni escribir")
    mode.add_argument("--run", action="store_true", help="Ejecutar auditoría y escribir sólo reportes")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, default=Path(DEFAULT_OUTPUT))
    args = parser.parse_args(argv)
    root = args.root.resolve()
    paths = {**SOURCE_FILES, **{f"manifest_{k}": v for k, v in MANIFEST_FILES.items()}}
    if args.plan:
        print("HLL_JOIN_PLAN", json.dumps({"version": VERSION, "root": str(root),
            "mode": "OFFLINE_NO_LIVE", "output": str(args.output),
            "sources": {k: {"present": (root / p).is_file(), "path": p} for k, p in paths.items()}},
            ensure_ascii=False))
        return 0
    target = args.output if args.output.is_absolute() else root / args.output
    try:
        summary = run(root, target)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print("HLL_JOIN_ERROR", str(exc), file=sys.stderr)
        return 3
    print("HLL_JOIN_FINAL", json.dumps({"compatible": summary["is_compatible"],
        "mismatch_total": summary["mismatch_total"], "groups": summary["index_entry_groups"],
        "output": str(target)}, ensure_ascii=False))
    return 0 if summary["is_compatible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
