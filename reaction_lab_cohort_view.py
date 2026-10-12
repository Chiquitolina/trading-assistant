"""Read-only, source-aware research cohorts for Reaction Lab Continuous V1.

This module NEVER mutates candidate CSVs, scanner/paths SQLite databases, or
existing Candidate snapshots/portfolios. A REACTION belongs to Continuous V1
only when its causal known_ts >= the first successful scanner cycle start (or
an explicitly frozen manifest cutoff) AND it exists in the scanner journal.
"""
from __future__ import annotations

import csv
import json
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

LEGACY_FILES = {
    "v1_raw": "reports/candidate_v1_analysis/legacy_v1_raw_monitor.csv",
    "v1_short_frozen": "candidate_v1_frozen_monitor.csv",
    "v1_long_frozen": "candidate_v1_long_frozen_monitor.csv",
    "v2": "candidate_v2_monitor.csv",
}
SCANNER_REL = "reports/reaction_lab/scanner_events_v1.sqlite3"
PATH_REL = "reports/reaction_lab/auto_paths_v1.sqlite3"
MANIFEST_REL = "reports/reaction_lab/continuous_v1_cohort.json"
CHOICES = ("Continuous V1", "Legacy", "Todos")


def _readonly(db_path):
    return sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True, timeout=8)


def _read_legacy(root, cutoff_ms):
    """Read historical records; never count post-cutover monitor writes as Legacy."""
    output = {}
    archive_root = root / "reports/reaction_lab"
    archives = sorted(p for p in archive_root.glob("legacy_archive_*") if p.is_dir()) if archive_root.exists() else []
    frozen_archive = archives[-1] if archives else None
    sources = {}
    for profile, relative in LEGACY_FILES.items():
        live = root / relative
        archived = frozen_archive / live.name if frozen_archive is not None else None
        path = archived if archived is not None and archived.is_file() else live
        sources[profile] = str(path.relative_to(root)) if path.is_file() else None
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            for raw in csv.DictReader(stream):
                if str(raw.get("status") or "REACTION").strip().upper() != "REACTION":
                    continue
                symbol = str(raw.get("symbol") or "").strip().upper()
                side = str(raw.get("signal") or raw.get("side") or "").strip().upper()
                try:
                    retest = int(float(raw.get("retest_timestamp")))
                except (TypeError, ValueError, OverflowError):
                    continue
                if not symbol or side not in {"LONG", "SHORT"} or retest <= 0:
                    continue
                known = retest + 60_000
                if cutoff_ms is not None and known >= cutoff_ms:
                    continue
                key = f"{symbol}|{side}|{retest}"
                recorded = str(raw.get("candidate_v2_event_key") or raw.get("candidate_v1_event_key") or "").strip()
                if recorded and recorded != key:
                    continue
                item = output.setdefault(key, {
                    "event_key": key, "symbol": symbol, "signal": side,
                    "retest_timestamp": retest, "known_ts": known,
                    "observation_type": "LEGACY_MONITOR", "first_seen_ms": None,
                    "sources": set(), "cohort": "Legacy",
                })
                item["sources"].add(profile)
    return output, sources


def _read_scanner(root):
    """Read all persisted signals with their *original* first-seen classification."""
    path = root / SCANNER_REL
    if not path.is_file():
        return None, {}, None, ["Scanner SQLite todavía no existe."]
    errors = []
    events = {}
    try:
        with closing(_readonly(path)) as con:
            first = con.execute("SELECT MIN(started_ms) FROM cycles WHERE error IS NULL").fetchone()[0]
            records = con.execute("""
                SELECT event_key, symbol, side, retest_ts, known_ts,
                       first_seen_ms, observation_type
                FROM events
            """)
            for key, symbol, side, retest, known, first_seen, typ in records:
                key = str(key)
                expected = f"{str(symbol).upper()}|{str(side).upper()}|{int(retest)}"
                if key != expected:
                    errors.append("Identidad inválida en el scanner: " + key)
                    continue
                events[key] = {
                    "event_key": key, "symbol": str(symbol).upper(),
                    "signal": str(side).upper(), "retest_timestamp": int(retest),
                    "known_ts": int(known), "first_seen_ms": int(first_seen),
                    "observation_type": str(typ), "sources": {"continuous_scan"},
                }
    except (sqlite3.Error, OSError, ValueError) as ex:
        return None, {}, None, [f"No se pudo leer el scanner: {ex}"]

    manifest = root / MANIFEST_REL
    cutoff = int(first) if first is not None else None
    cutoff_source = "FIRST_SUCCESSFUL_SCANNER_CYCLE"
    if manifest.is_file():
        try:
            manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
            saved = int(manifest_data["start_ms"])
            if saved > 0 and first is not None and saved >= int(first):
                cutoff = saved
                cutoff_source = "FROZEN_MANIFEST"
            else:
                errors.append("El manifiesto de cohorte tiene un corte anterior al primer ciclo; se usa el primer ciclo.")
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            errors.append("Manifiesto inválido; se usa el primer ciclo exitoso.")
    return cutoff, events, cutoff_source, errors


def _read_path_states(root):
    path = root / PATH_REL
    if not path.is_file():
        return {}, None
    try:
        with closing(_readonly(path)) as con:
            return {
                str(key): (str(state), int(count))
                for key, state, count in con.execute("SELECT event_key,state,count FROM events")
            }, None
    except (sqlite3.Error, OSError, ValueError) as ex:
        return {}, f"No se pudo leer el estado de paths: {ex}"


def load_reaction_cohort(project, mode="Continuous V1"):
    """Return reproducible cohorts and counts without writing any files.

    Startup/recovered scanner events with known_ts BEFORE the cohort cutoff stay
    in Legacy. Recovered events AFTER cutoff are included, with origin visible.
    The same event key is never counted twice in Todos.
    """
    if mode not in CHOICES:
        raise ValueError("Modo desconocido: " + str(mode))
    root = Path(project).resolve()
    cutoff, scanner, origin, errors = _read_scanner(root)
    historical, sources = _read_legacy(root, cutoff) if mode != "Continuous V1" else ({}, {})
    old_scan = {k: v for k, v in scanner.items() if cutoff is None or v["known_ts"] < cutoff}
    new_scan = {k: v for k, v in scanner.items() if cutoff is not None and v["known_ts"] >= cutoff}
    legacy = dict(historical)
    for key, row in old_scan.items():
        if key in legacy:
            legacy[key]["sources"].add("startup_recovered")
            if row["observation_type"] == "RECOVERED_WINDOW":
                legacy[key]["observation_type"] = "LEGACY_MONITOR + STARTUP_RECOVERED"
        else:
            legacy[key] = {**row, "sources": {"startup_recovered"}, "cohort": "Legacy"}
    continuous = {k: {**v, "cohort": "Continuous V1"} for k, v in new_scan.items()}

    if mode == "Continuous V1":
        shown = continuous
    elif mode == "Legacy":
        shown = legacy
    else:
        shown = dict(legacy)
        for key, row in continuous.items():
            if key in shown:
                errors.append("Evento duplicado a través del corte: " + key)
            else:
                shown[key] = row

    paths, path_error = _read_path_states(root)
    if path_error:
        errors.append(path_error)
    rows = []
    for row in shown.values():
        path_status, bars = paths.get(row["event_key"], ("NO_PATH", 0))
        rows.append({
            **row,
            "sources": ", ".join(sorted(row["sources"])),
            "path_state": path_status,
            "path_bars": bars,
        })
    rows.sort(key=lambda x: (x["retest_timestamp"], x["event_key"]), reverse=True)
    return {
        "mode": mode, "cutoff_ms": cutoff, "cutoff_source": origin,
        "cutoff_utc": datetime.fromtimestamp(cutoff / 1000, tz=timezone.utc).isoformat() if cutoff else None,
        "rows": rows, "shown": len(rows),
        "continuous_count": len(continuous),
        "continuous_live_count": sum(r["observation_type"] == "LIVE_OBSERVED" for r in continuous.values()),
        "continuous_recovered_count": sum(r["observation_type"] == "RECOVERED_WINDOW" for r in continuous.values()),
        "scanner_startup_recovered_count": len(old_scan),
        "legacy_monitor_count": len(historical),
        "scanner_total": len(scanner),
        "legacy_sources": sources,
        "legacy_frozen": any(
            "legacy_archive_" in path for path in sources.values() if path
        ),
        "errors": errors,
    }
