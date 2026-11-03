"""HLL | POST historical mechanism overlay, exact Attempt ID. OFFLINE ONLY.

Builds read-only outputs for Structural Discovery PRE similarity inspector.
No detector, ranker, fingerprint, source CSV, or LIVE code is modified.

Only after a successful frozen HLL join audit. This adapter deliberately
indexes ALL historical attempts (not merely episode-first fingerprints), since
an arbitrary selected #N attempt may be the user's reference.

Command:
    python3 hll_similarity_mechanism_adapter_v1.py --plan
    python3 -u hll_similarity_mechanism_adapter_v1.py --run
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import hll_fingerprint_mechanism_join_audit_v1 as audit

VERSION = "HLL_SIMILARITY_MECHANISM_ADAPTER_V1"
DEFAULT_AUDIT = audit.DEFAULT_OUTPUT
DEFAULT_OUTPUT = "reports/horizontal_level_fingerprint_mechanism_join_v1/overlay_v1"
ROUTE_TO_BRANCH = {
    "FLIP_1M": None,
    "V29_CONTINUATION_30M": "BREAKOUT_CONTINUATION",
    "V29_RECLAIM_30M": "RECLAIM_CONTINUATION",
}
ROUTES = tuple(ROUTE_TO_BRANCH)
COLUMNS = (
    "attempt_id", "symbol", "level_id", "episode_id", "pre_closed_ts", "breakout_closed_ts",
    "route", "mechanism_source", "mechanism_status", "confirmed_closed_ts", "signal_id",
    "entry_cohort", "entry_status", "entry_id", "signal_ts", "entry_ts",
    "entry_open_signal_reference", "signal_reference_timeframe", "entry_open_execution_1m", "open_source_discrepancy",
    "audit_match", "post_is_retrospective",
)


def _rows(path: Path):
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rdr = csv.DictReader(f)
        if not rdr.fieldnames:
            raise ValueError(f"CSV sin encabezados: {path}")
        yield from rdr


def _require(cond, message):
    if not cond:
        raise ValueError(message)


def _verify_audit(root: Path, audit_dir: Path):
    """Fail closed on a failed, inconsistent or stale audit."""
    manifest_file = audit_dir / "manifest.json"
    summary_file = audit_dir / "coverage_summary.json"
    inventory_file = audit_dir / "source_inventory.json"
    for path in (manifest_file, summary_file, inventory_file):
        _require(path.is_file(), f"Falta archivo auditor: {path}")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    summary = json.loads(summary_file.read_text(encoding="utf-8"))
    inventory = json.loads(inventory_file.read_text(encoding="utf-8"))
    _require(manifest.get("status") == "DONE" and manifest.get("mismatch_total") == 0,
             "Auditor sin estado DONE y cero discrepancias")
    _require(summary.get("is_compatible") is True and summary.get("mismatch_total") == 0,
             "coverage_summary incompatible")
    _require(summary.get("index_scope") == "EPISODE_FIRST_ONLY" and
             summary.get("entry_scope") == "FIRST_PER_LEVEL", "Cambió el alcance de las cohortes")
    for name, expected_sha in manifest.get("output_sha256", {}).items():
        file = audit_dir / name
        _require(file.is_file() and audit.digest(file) == expected_sha,
                 f"Reporte de auditoría modificado: {name}")
    checked_source = {}
    for key, data in inventory.get("source_files", {}).items():
        path = root / data["relative_path"]
        _require(path.is_file(), f"No existe fuente congelada: {path}")
        actual = audit.digest(path)
        _require(actual == data["sha256"], f"Fuente distinta de la auditoría: {key}")
        checked_source[key] = actual
    _require(set(audit.SOURCE_FILES).issubset(checked_source), "Inventario de auditoría incompleto")
    for key, data in inventory.get("manifest_files", {}).items():
        path = root / data["relative_path"]
        _require(path.is_file() and audit.digest(path) == data["sha256"],
                 f"Manifiesto fuente distinto de la auditoría: {key}")
    _require(manifest.get("source_index_sha256") == checked_source["fingerprints"],
             "Fingerprint index distinto de auditoría")
    return summary, manifest, checked_source


def _get_data(root: Path):
    sources = {key: root / rel for key, rel in audit.SOURCE_FILES.items()}
    base = {}
    for r in _rows(sources["episode_map"]):
        aid = r.get("attempt_id", "")
        _require(aid and aid not in base, f"attempt_id base duplicado o vacío: {aid}")
        base[aid] = {key: r.get(key, "") for key in (
            "attempt_id", "symbol", "level_id", "episode_id", "pre_closed_ts", "breakout_closed_ts"
        )}

    def identity(r, label, symbol_key="symbol", timestamps=True):
        aid = r.get("attempt_id", "")
        _require(aid in base, f"{label}: intento no encontrado {aid}")
        original = base[aid]
        for field, key in (("symbol", symbol_key), ("level_id", "level_id")):
            if key in r and r[key] not in (None, ""):
                _require(r[key] == original[field], f"{label}: identidad distinta {aid}.{field}")
        if timestamps:
            for field in ("pre_closed_ts", "breakout_closed_ts"):
                if field in r and r[field] not in (None, ""):
                    _require(audit.stable_ts(r[field]) == audit.stable_ts(original[field]),
                             f"{label}: fecha distinta {aid}.{field}")
        return aid

    v26 = {}
    for r in _rows(sources["mechanisms"]):
        aid = identity(r, "mechanisms")
        _require(aid not in v26, f"mechanism intento duplicado {aid}")
        v26[aid] = {"status": r.get("v26_status", ""),
                    "confirmed_ts": r.get("flip_confirmed_closed_ts", "")}

    continuation = {}
    for r in _rows(sources["continuation"]):
        aid = identity(r, "continuation", symbol_key="origin_symbol", timestamps=False)
        branch = r.get("branch", "")
        _require(branch in audit.BRANCHES, f"Rama desconocida {branch}")
        key = (aid, branch)
        _require(key not in continuation, f"Continuación duplicada {key}")
        continuation[key] = {"status": r.get("status", ""),
                             "confirmed_ts": r.get("confirmed_ts", ""),
                             "signal_id": r.get("signal_id", "")}

    coverage = {}
    for r in _rows(sources["entry_coverage"]):
        aid = identity(r, "coverage")
        route = r.get("route", "")
        _require(route in ROUTES, f"Ruta coverage desconocida: {route}")
        key = (aid, route)
        _require(key not in coverage, f"Cobertura duplicada: {key}")
        coverage[key] = {"status": r.get("status", ""), "entry_price": r.get("entry_price", ""),
                         "entry_ts": r.get("entry_ts", ""), "signal_ts": r.get("signal_ts", "")}

    entries = {}
    for r in _rows(sources["entry_events"]):
        aid = identity(r, "entries")
        route = r.get("route", "")
        _require(route in ROUTES, f"Ruta entry desconocida: {route}")
        key = (aid, route)
        _require(key not in entries, f"Entrada duplicada: {key}")
        _require(r.get("status") == "ENTRY_VERIFIED", f"Entrada no verificada: {key}")
        entries[key] = {"entry_id": r.get("entry_id", ""), "entry_ts": r.get("entry_ts", ""),
                        "signal_ts": r.get("signal_ts", ""), "entry_price": r.get("entry_price", "")}
    return base, v26, continuation, coverage, entries


def _load_discrepancies(audit_dir: Path):
    result = {}
    for r in _rows(audit_dir / "open_source_differences.csv"):
        key = (r.get("attempt_id", ""), r.get("route", ""))
        _require(key not in result, f"Discrepancia OPEN duplicada {key}")
        result[key] = r
    return result


def _route_rows(base, v26, continuation, coverage, entries, differences):
    """Yield three independent route rows for EVERY historical attempt."""
    for aid, original in base.items():
        _require(aid in v26, f"Falta estado V26 {aid}")
        for route, branch in ROUTE_TO_BRANCH.items():
            key = aid, route
            if branch is None:
                mech = v26[aid]
                mech_source = "V26_FLIP"
            else:
                _require((aid, branch) in continuation, f"Falta mecanismo {aid}/{branch}")
                mech = continuation[(aid, branch)]
                mech_source = branch
            covered = coverage.get(key)
            entry = entries.get(key)
            if covered is None:
                status = "NOT_COVERED"
                _require(entry is None, f"Entrada sin cobertura: {key}")
            else:
                status = covered["status"]
                _require(status in audit.ENTRY_STATUSES, f"Estado cobertura desconocido: {key} {status}")
                _require((status == "ENTRY_VERIFIED") == (entry is not None),
                         f"Inconsistencia entrada-cobertura {key}")
            if entry:
                _require(bool(entry["entry_id"]) and bool(entry["entry_ts"]),
                         f"Entrada verificada incompleta {key}")
                for f in ("signal_ts", "entry_ts"):
                    _require(audit.stable_ts(covered[f]) == audit.stable_ts(entry[f]),
                             f"Timestamp discrepante {key}.{f}")
            difference = differences.get(key)
            _require((difference is not None) <= bool(entry),
                     f"Discrepancia OPEN sin entrada {key}")
            yield {
                **original,
                "route": route,
                "mechanism_source": mech_source,
                "mechanism_status": mech["status"],
                "confirmed_closed_ts": audit.stable_ts(mech.get("confirmed_ts", "")),
                "signal_id": mech.get("signal_id", ""),
                "entry_cohort": "FIRST_PER_LEVEL",
                "entry_status": status,
                "entry_id": entry["entry_id"] if entry else "",
                "signal_ts": audit.stable_ts(entry["signal_ts"]) if entry else
                    (audit.stable_ts(covered["signal_ts"]) if covered else ""),
                "entry_ts": audit.stable_ts(entry["entry_ts"]) if entry else
                    (audit.stable_ts(covered["entry_ts"]) if covered else ""),
                "entry_open_signal_reference": covered["entry_price"] if covered else "",
                "signal_reference_timeframe": "1m" if route == "FLIP_1M" else "30m",
                "entry_open_execution_1m": entry["entry_price"] if entry else "",
                "open_source_discrepancy": "true" if difference else "false",
                "audit_match": "MATCHED",
                "post_is_retrospective": "true",
            }


def load_attempt(db_file: Path, attempt_id: str):
    """Read only exact Attempt ID lookup for future Streamlit panel."""
    path = Path(db_file).resolve()
    _require(path.is_file(), f"No existe base de overlay: {path}")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM overlay WHERE attempt_id = ? ORDER BY route", (str(attempt_id),))]
    finally:
        conn.close()


def run(root: Path, audit_dir: Path, output: Path):
    root = Path(root).resolve()
    audit_dir = audit_dir if audit_dir.is_absolute() else root / audit_dir
    output = output if output.is_absolute() else root / output
    _require(not output.resolve().is_relative_to(root / "dashboard"), "Salida no puede escribirse en dashboard")
    summary, audit_manifest, source_sha = _verify_audit(root, audit_dir)
    base, v26, continuation, coverage, entries = _get_data(root)
    differences = _load_discrepancies(audit_dir)
    _require(len(base) == summary["base_distinct_attempts"], "Cambió universo de intentos")
    _require(len(entries) == summary["entry_event_distinct_routes"], "Cambió universo de entradas")
    _require(len(coverage) == summary["input_counts"]["entry_coverage_rows"], "Cambió cobertura")
    _require(len(differences) == summary["observed_open_source_differences"], "Cambió auditoría de OPEN")
    _require(len(v26) == len(base) and len(continuation) == 2 * len(base),
             "Mecanismos incompletos")
    _require(len(coverage) % len(ROUTES) == 0, "Cobertura parcial")
    _require(all(sum((aid, route) in coverage for route in ROUTES) == len(ROUTES)
                 for aid, _ in coverage), "Rutas de cobertura incompletas")

    output.mkdir(parents=True, exist_ok=True)
    pending_csv = None
    pending_db = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".overlay-", suffix=".csv", dir=output, delete=False) as f:
            pending_csv = Path(f.name)
        with tempfile.NamedTemporaryFile(prefix=".overlay-", suffix=".sqlite3", dir=output, delete=False) as f:
            pending_db = Path(f.name)
        conn = sqlite3.connect(pending_db)
        try:
            conn.execute("CREATE TABLE overlay (" + ", ".join(f'"{c}" TEXT NOT NULL' for c in COLUMNS) + ")")
            conn.execute("CREATE UNIQUE INDEX idx_attempt_route ON overlay(attempt_id, route)")
            placeholders = ",".join("?" for _ in COLUMNS)
            insert = "INSERT INTO overlay VALUES (" + placeholders + ")"
            counts = Counter()
            unique = set()
            diffseen = set()
            with pending_csv.open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=COLUMNS)
                writer.writeheader()
                batch = []
                for item in _route_rows(base, v26, continuation, coverage, entries, differences):
                    writer.writerow(item)
                    batch.append(tuple(str(item.get(c, "")) for c in COLUMNS))
                    counts[(item["route"], item["entry_status"])] += 1
                    unique.add(item["attempt_id"])
                    if item["open_source_discrepancy"] == "true":
                        diffseen.add((item["attempt_id"], item["route"]))
                    if len(batch) >= 2000:
                        conn.executemany(insert, batch)
                        batch.clear()
                if batch:
                    conn.executemany(insert, batch)
            _require(len(unique) == len(base), "Cobertura de intentos incompleta")
            _require(sum(counts.values()) == len(base) * len(ROUTES), "Cardinalidad incorrecta")
            _require(diffseen == set(differences), "Discrepancias de OPEN no preservadas")
            _require(sum(n for (r, s), n in counts.items() if s == "ENTRY_VERIFIED") == len(entries),
                     "No se conservaron todas las entradas")
            conn.commit()
        finally:
            conn.close()
        os.replace(pending_csv, output / "candidate_mechanism_overlay.csv")
        os.replace(pending_db, output / "candidate_mechanism_overlay.sqlite3")
        manifest = {
            "version": VERSION,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "mode": "OFFLINE_NO_LIVE",
            "status": "DONE",
            "scope": "ALL_ATTEMPTS_BY_EXACT_ATTEMPT_ID",
            "entry_scope": "FIRST_PER_LEVEL_ONLY",
            "ranker_changed": False,
            "reads_visual_labels": False,
            "pre_features_changed": False,
            "selected_reference_may_be_episode_attempt_N": True,
            "records": sum(counts.values()),
            "attempts": len(base),
            "routes_per_attempt": len(ROUTES),
            "entries": len(entries),
            "source_open_discrepancies": len(differences),
            "route_status_counts": {f"{r}|{s}": n for (r, s), n in sorted(counts.items())},
            "source_fingerprints_sha256": source_sha["fingerprints"],
            "source_episode_map_sha256": source_sha["episode_map"],
            "source_audit_manifest_sha256": audit.digest(audit_dir / "manifest.json"),
            "output_csv_sha256": audit.digest(output / "candidate_mechanism_overlay.csv"),
            "output_db_sha256": audit.digest(output / "candidate_mechanism_overlay.sqlite3"),
            "warning": "Solo desenlaces históricos. NOT_COVERED no significa NO_SIGNAL. Eventos confirmados no implican OPEN verificado. No PnL."
        }
        (output / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return manifest
    finally:
        for pending in (pending_csv, pending_db):
            if pending is not None:
                pending.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--run", action="store_true")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--audit", type=Path, default=Path(DEFAULT_AUDIT))
    parser.add_argument("--output", type=Path, default=Path(DEFAULT_OUTPUT))
    args = parser.parse_args(argv)
    root = args.root.resolve()
    audit_dir = args.audit if args.audit.is_absolute() else root / args.audit
    output = args.output if args.output.is_absolute() else root / args.output
    if args.plan:
        print("HLL_OVERLAY_PLAN", json.dumps({
            "version": VERSION,
            "root": str(root), "audit_ready": (audit_dir / "manifest.json").is_file(),
            "sources_present": {k: (root / p).is_file() for k, p in audit.SOURCE_FILES.items()},
            "output": str(output), "mode": "OFFLINE_NO_LIVE"
        }, ensure_ascii=False))
        return 0
    try:
        result = run(root, audit_dir, output)
    except (OSError, ValueError, KeyError, sqlite3.Error, json.JSONDecodeError) as exc:
        print("HLL_OVERLAY_ERROR", str(exc), file=sys.stderr)
        return 3
    print("HLL_OVERLAY_FINAL", json.dumps({k: result[k] for k in
          ("status", "attempts", "records", "entries", "source_open_discrepancies", "route_status_counts")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
