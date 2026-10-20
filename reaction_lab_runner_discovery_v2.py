#!/usr/bin/env python3
"""Runner Discovery V2 — read-only Continuous REACTION research.

Research outputs are descriptive, never actual Binance fills. Two entry bases:
STRUCTURAL: retest close, next 1m bar onward (historical reference only).
OBSERVED: LIVE_OBSERVED only; next archived 1m OPEN at/after both known_ts
          and first_seen_ms. Zero latency, ideal OHLC fills, no costs.
Outputs are atomic CSV + JSON in a separate reports folder; input DBs untouched.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import zlib
from collections import Counter
from statistics import median
from typing import Iterable

MINUTE_MS = 60_000
HORIZONS = (15, 30, 60, 120, 180, 360)
TARGETS = (2, 3, 5)
SL_PCT = 1.0
SCHEMA_VERSION = "runner-discovery-v2.2-dna"

# Labels only: never infer that missing values indicate weak market behavior.
FEATURE_LABELS = {
    "max_departure_pct": "Departure (%)",
    "retest_distance_pct": "Distancia retest (%)",
    "relative_volume": "Volumen retest relativo",
    "nearest_opposing_room_pct": "Room HTF (%)",
    "opposing_room_1h_pct": "Room 1h (%)",
    "side_adjusted_strength_vs_btc_4h": "Fuerza vs BTC 4h (%)",
    "market_breadth_1h": "Breadth 1h (%)",
    "breadth_delta_1h": "Cambio breadth 1h",
    "market_breadth_4h": "Breadth 4h (%)",
    "rsi14_15m": "RSI 15m",
    "rel_volume_15m": "Volumen relativo 15m",
}

FIELDS = [
    "event_key", "symbol", "side", "retest_ts", "known_ts", "first_seen_ms",
    "observation_type", "date_utc", "minute_group", "path_state", "path_count",
    "runner_catalog", "quality", "retest_close", "entry_structural_ts", "entry_structural_open",
    "entry_observed_ts", "entry_observed_open", "observed_delay_min", "observed_bars", "observed_quality",
    "mfe_360_pct", "mae_360_pct", "mfe_180_pct", "mae_180_pct",
    *[f"time_to_run_{tp}pct" for tp in TARGETS],
    *[f"obs_time_to_run_{tp}pct" for tp in TARGETS],
    *[f"struct_tp{tp}_result" for tp in TARGETS],
    *[f"struct_tp{tp}_min" for tp in TARGETS],
    *[f"obs_tp{tp}_result" for tp in TARGETS],
    *[f"obs_tp{tp}_min" for tp in TARGETS],
    "context_quality", "data_error", *FEATURE_LABELS,
]


def as_number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def read_json(value):
    if not value:
        return {}
    data = json.loads(value)
    return data if isinstance(data, dict) else {}


def validate_path(bars, *, retest_ts, entry_ts, count):
    """Validate physical 1m open timestamps, OHLC values and 360-bar maturity."""
    if len(bars) != 360 or int(count) != 360 or int(entry_ts) != int(retest_ts) + MINUTE_MS:
        return "BAD_LENGTH_OR_ENTRY"
    for i, candle in enumerate(bars):
        if len(candle) < 5:
            return "BAD_BAR_SHAPE"
        stamp, o, high, low, c = candle[:5]
        if int(stamp) != int(entry_ts) + i * MINUTE_MS:
            return "GAPPED_OR_SHIFTED"
        values = (o, high, low, c)
        if not all(as_number(x) is not None and float(x) > 0 for x in values):
            return "BAD_PRICE"
        if float(high) < max(float(o), float(c), float(low)) or float(low) > min(float(o), float(c), float(high)):
            return "BAD_OHLC"
    return "COMPLETE_VALID"


def first_touch(bars, *, side, price, target_pct, stop_pct=SL_PCT):
    """One earliest touch across up to 360 closed bars. NO_HIT has no minute.

    Intrabar ordering is unknown: simultaneous TP/SL remains AMBIGUOUS.
    Bar open is the hypothetical entry; no stop fill/slippage is assumed.
    """
    direction = str(side).upper()
    if direction not in ("LONG", "SHORT") or not price or price <= 0:
        return "INVALID", None
    for i, (_, o, high, low, close) in enumerate(bars, 1):
        if direction == "LONG":
            hit_tp = high >= price * (1 + target_pct / 100)
            hit_sl = low <= price * (1 - stop_pct / 100)
        else:
            hit_tp = low <= price * (1 - target_pct / 100)
            hit_sl = high >= price * (1 + stop_pct / 100)
        if hit_tp and hit_sl:
            return "AMBIGUOUS", i
        if hit_tp:
            return "TP_FIRST", i
        if hit_sl:
            return "SL_FIRST", i
    return "NO_HIT", None


def first_bar_observable(bars, *, known_ts, first_seen_ms, observation_type):
    """Never interpret RECOVERED_WINDOW as a contemporaneously observed signal."""
    if observation_type != "LIVE_OBSERVED":
        return None, "RECOVERED_NOT_LIVE"
    if known_ts is None or first_seen_ms is None:
        return None, "NO_OBSERVATION_TIME"
    ready = max(int(known_ts), int(first_seen_ms))
    # An OPEN exactly at the observation boundary is an idealized proxy only.
    for i, candle in enumerate(bars):
        if int(candle[0]) >= ready:
            return i, "OBSERVED_PROXY"
    return None, "OBSERVED_TOO_LATE"


def favorable_adverse(bar, baseline, side):
    high, low = float(bar[2]), float(bar[3])
    if side == "LONG":
        return max(0, 100 * (high / baseline - 1)), max(0, 100 * (1 - low / baseline))
    return max(0, 100 * (1 - low / baseline)), max(0, 100 * (high / baseline - 1))


def path_metrics(bars, price, side):
    mfe = mae = 0.0
    mfe180 = mae180 = 0.0
    first_hits = {t: None for t in TARGETS}
    for i, bar in enumerate(bars, 1):
        f, a = favorable_adverse(bar, price, side)
        mfe = max(mfe, f)
        mae = max(mae, a)
        if i <= 180:
            mfe180 = max(mfe180, f)
            mae180 = max(mae180, a)
        for t in TARGETS:
            if first_hits[t] is None and f >= t:
                first_hits[t] = i
    return mfe, mae, mfe180, mae180, first_hits


def first_favorable_hits(bars, *, side, price, targets=TARGETS):
    """First favorable high/low hit from a fixed entry; ignores SL entirely.

    Null = target not observed in available candles. One-based bar numbers.
    Unlike first_touch(), an earlier adverse move does not end the search.
    """
    hits = {int(tp): None for tp in targets}
    if str(side).upper() not in ("LONG", "SHORT") or as_number(price) is None or price <= 0:
        return hits
    remaining = set(hits)
    for i, bar in enumerate(bars, 1):
        favorable, _ = favorable_adverse(bar, float(price), str(side).upper())
        for tp in tuple(remaining):
            if favorable >= tp:
                hits[tp] = i
                remaining.remove(tp)
        if not remaining:
            break
    return hits


def readonly_uri(path):
    return path.resolve().as_uri() + "?mode=ro"


def _open_input(project):
    base = project / "reports" / "reaction_lab"
    dbs = {
        "sc": base / "scanner_events_v1.sqlite3",
        "ap": base / "auto_paths_v1.sqlite3",
        "rn": base / "runners_v1.sqlite3",
        "ctx": base / "causal_context_v1.sqlite3",
    }
    for required in ("sc", "ap"):
        if not dbs[required].is_file():
            raise FileNotFoundError(f"No existe la base requerida: {dbs[required]}")
    con = sqlite3.connect(readonly_uri(dbs["sc"]), uri=True, timeout=15)
    try:
        con.execute("PRAGMA busy_timeout=15000")
        con.execute("ATTACH DATABASE ? AS ap", (readonly_uri(dbs["ap"]),))
        for name in ("rn", "ctx"):
            if dbs[name].is_file():
                con.execute(f"ATTACH DATABASE ? AS {name}", (readonly_uri(dbs[name]),))
        existing = {row[1] for row in con.execute("PRAGMA database_list")}
        con.execute("PRAGMA query_only=ON")
        return con, existing
    except Exception:
        con.close()
        raise


def _event_sql(existing):
    runner = "CASE WHEN r.event_key IS NULL THEN 0 ELSE 1 END" if "rn" in existing else "0"
    runner_join = "LEFT JOIN rn.runners r ON r.event_key=s.event_key" if "rn" in existing else ""
    context = "c.known_ts,c.asof_complete,c.status,c.context_json" if "ctx" in existing else "NULL,NULL,NULL,NULL"
    context_join = "LEFT JOIN ctx.contexts c ON c.event_key=s.event_key" if "ctx" in existing else ""
    return f"""SELECT s.event_key,s.symbol,s.side,s.retest_ts,s.known_ts,
      s.first_seen_ms,s.observation_type,s.causal_json,
      p.entry_ts,p.state,p.count,p.path_blob,{runner},
      {context}
      FROM main.events s
      LEFT JOIN ap.events p ON p.event_key=s.event_key
      {runner_join} {context_join}
      ORDER BY s.retest_ts,s.event_key"""


def generate_rows(project):
    """Generator of small flat rows; outputs include ineligible events as QA, not losses."""
    from reaction_lab_auto_paths import decode_path
    con, existing = _open_input(Path(project))
    try:
        for record in con.execute(_event_sql(existing)):
            (key, symbol, side, retest_ts, known_ts, first_seen_ms, origin, causal,
             entry_ts, state, count, blob, runner, ctx_known, ctx_asof, ctx_status, ctx_json) = record
            row = {k: None for k in FIELDS}
            row.update({
                "event_key": key, "symbol": symbol, "side": side,
                "retest_ts": retest_ts, "known_ts": known_ts, "first_seen_ms": first_seen_ms,
                "observation_type": origin,
                "date_utc": datetime.fromtimestamp(int(retest_ts)/1000, timezone.utc).date().isoformat(),
                "minute_group": int(retest_ts) // MINUTE_MS,
                "path_state": state or "MISSING", "path_count": count or 0,
                "runner_catalog": runner, "quality": "NO_COMPLETE_PATH",
                "context_quality": "MISSING",
            })
            try:
                causal_data = read_json(causal)
                ref = as_number(causal_data.get("retest_close"))
                row["retest_close"] = ref
                for name in ("max_departure_pct", "retest_distance_pct", "relative_volume"):
                    row[name] = as_number(causal_data.get(name))
                if (ctx_known is not None and ctx_asof == 1 and
                        int(ctx_known) == int(known_ts)):
                    try:
                        context_data = read_json(ctx_json)
                        for name in FEATURE_LABELS:
                            if name not in ("max_departure_pct", "retest_distance_pct", "relative_volume"):
                                row[name] = as_number(context_data.get(name))
                        row["context_quality"] = str(ctx_status or "ASOF_FROZEN")
                    except (TypeError, ValueError):
                        row["context_quality"] = "CORRUPT_CONTEXT"
                elif ctx_known is not None:
                    row["context_quality"] = "NOT_VERIFIED_ASOF"
                if state != "COMPLETE" or not blob:
                    yield row
                    continue
                if ref is None or ref <= 0:
                    row["quality"] = "MISSING_RETEST_CLOSE"
                    yield row
                    continue
                bars = decode_path(blob)
                integrity = validate_path(bars, retest_ts=retest_ts, entry_ts=entry_ts, count=count)
                row["quality"] = integrity
                if integrity != "COMPLETE_VALID":
                    yield row
                    continue
                row["entry_structural_ts"] = int(bars[0][0])
                row["entry_structural_open"] = bars[0][1]
                (row["mfe_360_pct"], row["mae_360_pct"], row["mfe_180_pct"],
                    row["mae_180_pct"], first_hits) = path_metrics(bars, ref, side)
                for tp in TARGETS:
                    row[f"time_to_run_{tp}pct"] = first_hits[tp]
                    result, minute = first_touch(bars, side=side, price=ref, target_pct=tp)
                    row[f"struct_tp{tp}_result"] = result
                    row[f"struct_tp{tp}_min"] = minute
                idx, observed_quality = first_bar_observable(
                    bars, known_ts=known_ts, first_seen_ms=first_seen_ms,
                    observation_type=origin)
                row["observed_quality"] = observed_quality
                if idx is not None:
                    row["entry_observed_ts"] = int(bars[idx][0])
                    row["entry_observed_open"] = float(bars[idx][1])
                    row["observed_delay_min"] = int(idx)
                    observed_first_hits = first_favorable_hits(
                        bars[idx:], side=side, price=float(bars[idx][1]))
                    for tp in TARGETS:
                        row[f"obs_time_to_run_{tp}pct"] = observed_first_hits[tp]
                        result, minute = first_touch(
                            bars[idx:], side=side, price=float(bars[idx][1]),
                            target_pct=tp)
                        row[f"obs_tp{tp}_result"] = result
                        row[f"obs_tp{tp}_min"] = minute
                    row["observed_bars"] = 360 - idx
                yield row
            except (TypeError, ValueError, OverflowError, UnicodeError, KeyError, IndexError,
                    sqlite3.Error, json.JSONDecodeError, zlib.error, struct.error, MemoryError) as exc:
                row["quality"] = "DECODE_OR_DATA_ERROR"
                row["observed_quality"] = "NOT_EVALUABLE"
                row["data_error"] = type(exc).__name__
                yield row
    finally:
        con.close()


def project_results_dir(project):
    return Path(project) / "reports" / "reaction_lab" / "runner_discovery_v2"


def _atomic_csv(path, rows):
    fd, temp = tempfile.mkstemp(prefix=".runner_v2_", suffix=".csv", dir=path.parent)
    count = Counter()
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as out:
            writer = csv.DictWriter(out, fieldnames=FIELDS, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
                count["scanner_events"] += 1
                count[f"path_{row['quality']}"] += 1
                count[f"origin_{row['observation_type']}"] += 1
                if row.get("observed_quality") == "OBSERVED_PROXY":
                    count["observed_proxy"] += 1
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return count


def build_dataset(project):
    """Rebuild on explicit request; never change source databases or PM2 processes."""
    project = Path(project).resolve()
    folder = project_results_dir(project)
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / "runner_discovery_v2_events.csv"
    counts = _atomic_csv(output, generate_rows(project))
    manifest = {
        "version": SCHEMA_VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "files": {"events": output.name},
        "settings": {"stop_pct": SL_PCT, "targets_pct": TARGETS, "horizons_min": HORIZONS,
                     "barrier_touch": ">=", "same_bar": "AMBIGUOUS"},
        "counts": dict(counts),
        "notes": [
            "All scanner REACTIONs included; missing/invalid paths are NOT losses.",
            "Structural uses retest_close (not executable).",
            "Observed proxy only LIVE_OBSERVED, first archived 1m open at/after known and first seen.",
            "Costs, latency, funding, portfolio slots and intra-1m fill order not simulated.",
            "DNA runner hit means favorable target touched regardless of SL: never a TP-first trade label.",
            "Outcomes are descriptive; factor availability is as-of verified where archived.",
        ],
    }
    target = folder / "manifest.json"
    fd, tmp = tempfile.mkstemp(prefix=".manifest_", suffix=".json", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return manifest


def outcome_at_horizon(result, minute, horizon, available=None):
    """No hindsight: a first touch after selected horizon is a timeout only if covered."""
    try:
        horizon = int(horizon)
        if available is not None and int(available) < horizon:
            return "INSUFFICIENT"
        if result is None or result == "" or result == "INVALID":
            return "INSUFFICIENT"
        if result == "NO_HIT" or minute is None or minute == "":
            return "NO_HIT"
        if int(float(minute)) > horizon:
            return "NO_HIT"
        return str(result)
    except (TypeError, ValueError):
        return "INSUFFICIENT"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project", default=".")
    args = p.parse_args(argv)
    manifest = build_dataset(args.project)
    print(json.dumps(manifest, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
