"""Read-only, causal, reproducible search of Reaction Lab LIVE runner configurations.

No database writes, no network, no exchange orders, no PM2 interaction.
Discovery / temporal holdout are both exploratory (not future forward validation).
"""
from __future__ import annotations

import itertools
import hashlib
import os
import tempfile
import json
import math
import random
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

MINUTE = 60_000
ARG = timezone(timedelta(hours=-3))
LIVE_START_MS = int(datetime(2026, 10, 15, 12, 25, tzinfo=ARG).timestamp() * 1000)
HORIZON = 180
# Research assumptions in percentage points of notional (never exchange fees).
DEFAULT_COSTS = {
    "maker_fee_pct": 0.02,
    "taker_fee_pct": 0.05,
    "taker_slippage_bps": 2.0,
}
MANIFEST_FILENAME = "runner_opt_holdout_freeze_v2.json"
MANIFEST_VERSION = 2

# DO NOT tune this dictionary using search results: frozen reference profile.
BASELINE = {
    "departure": 1.5,
    "room_1h": 2.0,
    "volume_rank": 60.0,
    "return_rank": 60.0,
    "limit": 2.0,
    "ttl": 15,
    "tp": 2.0,
    "sl": 1.0,
}
KEYS = tuple(BASELINE)
SETTINGS = (
    ("departure", "Departure mínimo (%)", "0.5; 1; 1.5; 2; 2.5", True),
    ("room_1h", "Room opuesto 1h mínimo (%)", "0.5; 1; 2; 3", True),
    ("volume_rank", "Volume Rank 4h mínimo", "40; 60; 80", True),
    ("return_rank", "Return Rank 4h mínimo", "40; 60; 80", True),
    ("limit", "LIMIT retroceso (%)", "0; 0.5; 1; 1.5; 2; 2.5", True),
    ("ttl", "TTL orden (min)", "5; 10; 15; 20; 30", True),
    ("tp", "Take Profit (%)", "1.5; 2; 2.5; 3; 4", True),
    ("sl", "Stop Loss (%)", "0.5; 0.75; 1; 1.5; 2", True),
)


def _read(db: Path, sql: str, args=()):
    if not db.is_file():
        raise FileNotFoundError(str(db))
    # immutable=1 would ignore live WAL updates; mode=ro sees committed WAL data.
    with sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True, timeout=10) as con:
        return con.execute(sql, args).fetchall()


def _json(raw):
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (ValueError, TypeError, OverflowError):
        return None


def load_live(project: str | Path, cutoff_ms: int = LIVE_START_MS):
    """Join frozen event IDs by key, never join latest Redis live values.

    Only COMPLETE, contiguous >=180+max TTL paths enter the *common* mature
    research population. Incomplete/missing sources are reported, not failures.
    """
    from reaction_lab_auto_paths import decode_path

    base = Path(project).resolve() / "reports" / "reaction_lab"
    source_rows = _read(
        base / "scanner_events_v1.sqlite3",
        """SELECT event_key,symbol,side,retest_ts,known_ts,first_seen_ms,causal_json
           FROM events WHERE observation_type='LIVE_OBSERVED'
           AND retest_ts>=? AND first_seen_ms>=?""",
        (cutoff_ms, cutoff_ms),
    )
    if not source_rows:
        return [], {"live": 0, "complete_factors": 0, "complete_paths": 0, "usable": 0}

    ids = {r[0] for r in source_rows}
    ctx = {r[0]: r[1:] for r in _read(
        base / "causal_context_v1.sqlite3",
        "SELECT event_key,known_ts,captured_at_ms,status,context_json FROM contexts",
    ) if r[0] in ids}
    regime = {r[0]: r[1:] for r in _read(
        base / "causal_context_v1.sqlite3",
        """SELECT event_key,known_ts,boundary_ts,captured_at_ms,status,
                  capture_method,metrics_json FROM v1_regime_events""",
    ) if r[0] in ids}
    paths = {r[0]: r[1:] for r in _read(
        base / "auto_paths_v1.sqlite3",
        "SELECT event_key,state,entry_ts,retest_ts,count,path_blob FROM events",
    ) if r[0] in ids}

    quality = Counter(live=len(source_rows), ctx=len(ctx), regime=len(regime), paths=len(paths))
    records = []
    for key, symbol, side, retest, known, seen, raw in source_rows:
        if side not in ("LONG", "SHORT") or key not in ctx or key not in regime:
            continue
        c_known, c_cap, c_status, c_json = ctx[key]
        r_known, r_bound, r_cap, r_status, method, r_json = regime[key]
        if (known is None or c_known != known or r_known != known
                or r_bound is None or r_bound > known or any(
                    v is None for v in (seen, c_cap, r_cap))):
            quality["temporal_invalid"] += 1
            continue
        causal, context, ranks = _json(raw), _json(c_json), _json(r_json)
        fields = (
            _number(causal.get("max_departure_pct")),
            _number(context.get("opposing_room_1h_pct")),
            _number(ranks.get("volume_rank_pct_4h")),
            _number(ranks.get("return_rank_pct_4h")),
        )
        if any(v is None for v in fields):
            quality["factors_missing"] += 1
            continue
        quality["complete_factors"] += 1
        path = paths.get(key)
        if not path:
            quality["path_missing"] += 1
            continue
        state, entry_ts, path_retest, count, blob = path
        if state != "COMPLETE":
            quality["path_not_complete"] += 1
            continue
        try:
            bars = decode_path(blob)
            if (path_retest != retest or entry_ts != retest + MINUTE
                    or len(bars) < HORIZON + 30 or
                    any(int(bar[0]) != entry_ts + i * MINUTE
                        for i, bar in enumerate(bars)) or
                    any(len(b) != 5 or not all(
                        math.isfinite(float(v)) for v in b[1:]
                    ) or float(b[1]) <= 0 or float(b[3]) <= 0 or
                        float(b[2]) < float(b[3]) for b in bars)):
                quality["path_invalid"] += 1
                continue
        except Exception:
            quality["path_invalid"] += 1
            continue
        quality["complete_paths"] += 1
        ready = max(int(seen), int(c_cap), int(r_cap))
        # If context was written beyond persisted 1m bars, it is unobservable.
        if ready >= int(bars[-1][0]) + MINUTE:
            quality["ready_after_path"] += 1
            continue
        records.append({
            "key": key, "symbol": symbol, "side": side,
            "retest_ts": int(retest), "first_seen_ms": int(seen),
            "ready_ms": ready, "values": fields,
            "context_status": c_status, "regime_status": r_status,
            "capture_method": method,
            "bars": tuple(tuple(b) for b in bars),
            "reference": float(bars[0][1]),
        })
    records.sort(key=lambda r: (r["retest_ts"], r["key"]))
    quality["usable"] = len(records)
    quality["complete_paths"] = quality["complete_paths"]
    return records, dict(quality)


def parameter_grid(editor_rows):
    """Parse data_editor table, cap individual value counts, no unsafe eval."""
    provided = {str(row["Clave"]): row for row in editor_rows}
    out = {}
    for key, _, _, _ in SETTINGS:
        row = provided.get(key)
        if not row:
            raise ValueError(f"Falta fila de parámetro {key}")
        if not bool(row["Optimizar"]):
            out[key] = (BASELINE[key],)
            continue
        text = str(row["Valores a probar"])
        try:
            raw = [float(x.strip()) for x in text.replace(",", ";").split(";") if x.strip()]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Valores inválidos para {key}: {text}") from exc
        vals = sorted(set(raw))
        if not vals or len(vals) > 15 or any(not math.isfinite(x) for x in vals):
            raise ValueError(f"{key}: indicar entre 1 y 15 valores numéricos finitos")
        if key == "ttl":
            if any(int(x) != x or x < 1 or x > 60 for x in vals):
                raise ValueError("TTL debe expresarse en minutos enteros entre 1 y 60")
            vals = [int(x) for x in vals]
        elif key in ("volume_rank", "return_rank"):
            if any(x < 0 or x > 100 for x in vals):
                raise ValueError(f"{key}: el rango debe estar entre 0 y 100")
        elif key in ("limit", "departure", "room_1h"):
            if any(x < 0 or x > 15 for x in vals):
                raise ValueError(f"{key}: valores entre 0 y 15%")
        else:
            if any(x <= 0 or x > 15 for x in vals):
                raise ValueError(f"{key}: valores mayores a 0 y hasta 15%")
        out[key] = tuple(vals)
    return out


def grid_size(grid):
    return math.prod(len(grid[k]) for k in KEYS)


def _index_to_config(index, grid):
    params = {}
    for key in reversed(KEYS):
        choices = grid[key]
        index, offset = divmod(index, len(choices))
        params[key] = choices[offset]
    return {k: params[k] for k in KEYS}


def sample_configs(grid, budget=500, seed=20261016):
    """Fully deterministic random grid, exact enumeration for small grids."""
    total = grid_size(grid)
    budget = max(1, min(int(budget), 10000))
    if total <= budget:
        all_configs = [dict(zip(KEYS, values)) for values in itertools.product(
            *(grid[k] for k in KEYS))]
    else:
        ids = sorted(random.Random(int(seed)).sample(range(total), budget))
        all_configs = [_index_to_config(i, grid) for i in ids]
    # Add exact frozen baseline even if it is outside the exploratory grid.
    found = {tuple(c[k] for k in KEYS) for c in all_configs}
    frozen_key = tuple(BASELINE[k] for k in KEYS)
    if frozen_key not in found:
        all_configs.insert(0, dict(BASELINE))
    return all_configs, total


def _eligible(record, params):
    return all(v >= params[k] for v, k in zip(
        record["values"], ("departure", "room_1h", "volume_rank", "return_rank")
    ))


def _validated_costs(costs=None):
    result = dict(DEFAULT_COSTS)
    if costs is not None:
        if not isinstance(costs, dict):
            raise ValueError("Costs must be a dictionary")
        unknown = set(costs) - set(DEFAULT_COSTS)
        if unknown:
            raise ValueError(f"Unknown cost inputs: {sorted(unknown)}")
        result.update(costs)
    for key, value in result.items():
        numeric = _number(value)
        upper = 1.0 if key.endswith("fee_pct") else 100.0
        if numeric is None or not 0 <= numeric <= upper:
            raise ValueError(f"{key} debe estar entre 0 y {upper}")
        result[key] = numeric
    return result


def _round_trip_cost(costs, entry_type, exit_type):
    """Return notional percentage cost, fees and taker-only slip assumptions."""
    entry = costs["maker_fee_pct"] if entry_type == "MAKER" else costs["taker_fee_pct"]
    exit_ = costs["maker_fee_pct"] if exit_type == "MAKER" else costs["taker_fee_pct"]
    slip = costs["taker_slippage_bps"] / 100.0
    return (entry + exit_ +
            (slip if entry_type == "TAKER" else 0.0) +
            (slip if exit_type == "TAKER" else 0.0))


def simulate(record, params, horizon=HORIZON, costs=None):
    """1m bar approximation. Use information actually persisted before entry.

    No intraminute sequencing: entry is allowed only on wholly post-ready bars.
    Entry crossing at open is treated as marketable/taker, otherwise a passive
    maker fill at the limit; both assumptions require external fill modeling.
    Non-marketable fill and favorable target in its minute => ambiguous.
    Subsequent TP+SL same minute => conservative stop-first result.
    Stop gaps exit at the worse OPEN, not the unreachable stop price.
    """
    costs = _validated_costs(costs)
    bars, side = record["bars"], record["side"]
    ref, ready = record["reference"], record["ready_ms"]
    first = ((ready + MINUTE - 1) // MINUTE) * MINUTE
    expiry = ready + int(params["ttl"]) * MINUTE
    limit = ref * (1 - params["limit"] / 100 if side == "LONG" else
                   1 + params["limit"] / 100)
    fill = None
    eligible_bars = 0
    for i, (ts, o, h, l, close) in enumerate(bars):
        if ts < first or ts + MINUTE > expiry:
            continue
        eligible_bars += 1
        marketable = o <= limit if side == "LONG" else o >= limit
        touched = l <= limit if side == "LONG" else h >= limit
        if marketable or touched:
            # Marketable limit crosses existing liquidity, modeled as taker.
            fill = (i, float(o) if marketable else limit,
                    "TAKER" if marketable else "MAKER")
            break
    if fill is None:
        last_full_ts = ((expiry - MINUTE) // MINUTE) * MINUTE
        if not eligible_bars or bars[-1][0] < last_full_ts:
            return {"status": "UNOBSERVABLE_WINDOW", "pnl": None}
        return {"status": "NO_FILL", "pnl": 0.0}
    idx, price, entry_type = fill
    target = price * (1 + params["tp"] / 100 if side == "LONG" else
                      1 - params["tp"] / 100)
    stop = price * (1 - params["sl"] / 100 if side == "LONG" else
                    1 + params["sl"] / 100)
    extras = {"fill_ts": int(bars[idx][0]), "fill_price": price,
              "entry_liquidity": entry_type}
    for j in range(idx, min(len(bars), idx + horizon)):
        ts, o, h, l, close = bars[j]
        hit_tp = h >= target if side == "LONG" else l <= target
        hit_sl = l <= stop if side == "LONG" else h >= stop
        if hit_tp and j == idx and entry_type == "MAKER":
            return {"status": "AMBIGUOUS_FILL_BAR", "pnl": None, **extras}
        if hit_sl:
            # A gap through the stop must not assume ideal stop-price execution.
            exit_price = min(float(o), stop) if side == "LONG" else max(float(o), stop)
            gross = ((exit_price / price - 1) * 100 if side == "LONG" else
                     (1 - exit_price / price) * 100)
            cost = _round_trip_cost(costs, entry_type, "TAKER")
            return {"status": "AMBIGUOUS_BOTH_AS_SL" if hit_tp else "SL",
                    "pnl": gross - cost, "gross_pct": gross,
                    "cost_pct": cost, "exit_liquidity": "TAKER",
                    "exit_ts": int(ts), **extras}
        if hit_tp:
            # Passive resting take-profit assumed maker; liquidity not guaranteed.
            gross = float(params["tp"])
            cost = _round_trip_cost(costs, entry_type, "MAKER")
            return {"status": "TP", "pnl": gross - cost,
                    "gross_pct": gross, "cost_pct": cost,
                    "exit_liquidity": "MAKER", "exit_ts": int(ts), **extras}
    if len(bars) < idx + horizon:
        return {"status": "CENSORED_AFTER_FILL", "pnl": None, **extras}
    last_bar = bars[idx + horizon - 1]
    last = float(last_bar[4])
    gross = ((last / price - 1) * 100 if side == "LONG" else
             (1 - last / price) * 100)
    cost = _round_trip_cost(costs, entry_type, "TAKER")
    return {"status": "TIMEOUT", "pnl": gross - cost,
            "gross_pct": gross, "cost_pct": cost,
            "exit_liquidity": "TAKER", "exit_ts": int(last_bar[0]), **extras}


def evaluate(records, params, *, min_fills=20, min_signals=30,
             costs=None, trade_cache=None):
    """Unbiased event universe; censoring is explicit, not classified as loss."""
    costs = _validated_costs(costs)
    outcome = Counter()
    pnl = []
    eligible = 0
    for record in records:
        if not _eligible(record, params):
            continue
        eligible += 1
        cache_key = (record["key"], params["limit"], params["ttl"],
                     params["tp"], params["sl"], tuple(costs.values()))
        result = trade_cache.get(cache_key) if trade_cache is not None else None
        if result is None:
            result = simulate(record, params, costs=costs)
            if trade_cache is not None:
                trade_cache[cache_key] = result
        outcome[result["status"]] += 1
        if result["pnl"] is not None and result["status"] != "NO_FILL":
            pnl.append(result["pnl"])
    fills = sum(outcome[k] for k in (
        "TP", "SL", "TIMEOUT", "AMBIGUOUS_FILL_BAR",
        "AMBIGUOUS_BOTH_AS_SL", "CENSORED_AFTER_FILL"))
    comparable = sum(outcome[k] for k in (
        "NO_FILL", "TP", "SL", "TIMEOUT", "AMBIGUOUS_BOTH_AS_SL"))
    wins = sum(x > 0 for x in pnl)
    gains = sum(x for x in pnl if x > 0)
    losses = -sum(x for x in pnl if x < 0)
    expectancy = sum(pnl) / comparable if comparable else None
    reliable = bool(eligible >= min_signals and len(pnl) >= min_fills and
                    comparable >= max(eligible * .8, 1))
    return {
        "señales": eligible, "evaluables": comparable, "fills": fills,
        "sin_fill": outcome["NO_FILL"], "tp_count": outcome["TP"],
        "sl_count": outcome["SL"] + outcome["AMBIGUOUS_BOTH_AS_SL"],
        "timeout": outcome["TIMEOUT"],
        "ambiguas": outcome["AMBIGUOUS_FILL_BAR"] + outcome["AMBIGUOUS_BOTH_AS_SL"],
        "censuradas": outcome["CENSORED_AFTER_FILL"] + outcome["UNOBSERVABLE_WINDOW"],
        "resueltas": len(pnl),
        "fill_pct": round(100 * fills / eligible, 2) if eligible else None,
        "wr_pct": round(100 * wins / len(pnl), 2) if pnl else None,
        "pf": round(gains / losses, 3) if losses else None,
        "pnl_medio_fill_pct": round(sum(pnl) / len(pnl), 5) if pnl else None,
        "expectativa_por_señal_pct": round(expectancy, 5) if expectancy is not None else None,
        "expectativa_por_universo_pct": round(sum(pnl) / len(records), 5) if records else None,
        "pnl_acumulado_pct": round(sum(pnl), 5),
        "muestra_suficiente": reliable,
    }


def _manifest_signature(keys):
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()


def _create_manifest(records, path, train_share):
    if not 0.5 <= train_share <= 0.9:
        raise ValueError("train_share entre 0.5 y 0.9")
    if len(records) < 3:
        raise ValueError("Se necesitan al menos tres REACTIONs maduras para el split")
    sorted_records = sorted(records, key=lambda r: (r["retest_ts"], r["key"]))
    cut = max(1, min(len(sorted_records)-1, int(len(sorted_records) * train_share)))
    train_ids = [r["key"] for r in sorted_records[:cut]]
    holdout_ids = [r["key"] for r in sorted_records[cut:]]
    return {
        "version": MANIFEST_VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "cohort": "LIVE_OBSERVED", "start_ms": LIVE_START_MS,
        "source": "reaction_lab_scanner_plus_causal_context_plus_complete_auto_paths",
        "train_share_requested": train_share,
        "train_ids": train_ids,
        "holdout_ids": holdout_ids,
        "train_sha256": _manifest_signature(train_ids),
        "holdout_sha256": _manifest_signature(holdout_ids),
        "cut_retest_ts_ms": sorted_records[cut]["retest_ts"],
    }


def load_or_freeze_cohort(records, path, *, train_share=0.70):
    """Create-once, immutable manifest of event membership across reruns.

    New live events are OUTSIDE the frozen experiment. Existing keys disappearing
    fail closed instead of quietly shrinking train or validation populations.
    Original SQLite DBs are never touched.
    """
    path = Path(path)
    if path.is_file():
        manifest = json.loads(path.read_text(encoding="utf-8"))
    else:
        manifest = _create_manifest(records, path, train_share)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomically publish only if absent: a crash never leaves half a JSON.
        staging = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=".runner_freeze_", suffix=".tmp", delete=False,
            ) as out:
                staging = Path(out.name)
                json.dump(manifest, out, ensure_ascii=False, indent=2)
                out.write("\n")
                out.flush()
                os.fsync(out.fileno())
            try:
                os.link(staging, path)  # atomic create-only, never replace
            except FileExistsError:
                manifest = json.loads(path.read_text(encoding="utf-8"))
        finally:
            if staging is not None:
                staging.unlink(missing_ok=True)
    if manifest.get("version") != MANIFEST_VERSION:
        raise ValueError("Manifest version incompatible: no se modifica automáticamente")
    train_keys = manifest["train_ids"]
    holdout_keys = manifest["holdout_ids"]
    if (len(train_keys) != len(set(train_keys)) or
            len(holdout_keys) != len(set(holdout_keys)) or
            set(train_keys) & set(holdout_keys) or
            manifest.get("train_sha256") != _manifest_signature(train_keys) or
            manifest.get("holdout_sha256") != _manifest_signature(holdout_keys)):
        raise ValueError("Manifest corrupto o de membresía alterada: no se continúa")
    by_id = {r["key"]: r for r in records}
    missing = (set(train_keys) | set(holdout_keys)) - set(by_id)
    if missing:
        raise ValueError(f"Faltan {len(missing)} IDs congelados en las SQLite; "
                         "no se reduce silenciosamente la cohorte")
    train = [by_id[k] for k in train_keys]
    holdout = [by_id[k] for k in holdout_keys]
    return train, holdout, manifest


def run_search(records, grid, *, budget=500, seed=20261016,
               min_fills=20, min_signals=30, train_share=0.70,
               costs=None, frozen=None):
    """Search ONLY in frozen train cohort; holdout unseen in ranking."""
    costs = _validated_costs(costs)
    if frozen is None:
        # For unit tests/in-memory use only; Streamlit uses persisted manifest.
        cut = max(1, min(len(records), int(len(records) * train_share)))
        train, holdout = records[:cut], records[cut:]
        manifest = None
    else:
        train, holdout, manifest = frozen
    configs, total = sample_configs(grid, budget=budget, seed=seed)
    ranking = []
    cache = {}
    for index, params in enumerate(configs, 1):
        perf = evaluate(train, params, min_fills=min_fills,
                        min_signals=min_signals, costs=costs, trade_cache=cache)
        ranking.append({"config_id": index, "baseline": params == BASELINE,
                        **params, **{f"train_{k}": v for k, v in perf.items()}})
    ranking.sort(key=lambda row: (
        bool(row["train_muestra_suficiente"]),
        row["train_expectativa_por_señal_pct"] if row["train_expectativa_por_señal_pct"] is not None else -1e10,
        row["train_resueltas"], -row["config_id"]), reverse=True)
    return {
        "ranking": ranking, "total_combinations": total,
        "tested": len(configs), "train_n": len(train),
        "holdout_n": len(holdout),
        "holdout_start_ms": holdout[0]["retest_ts"] if holdout else None,
        "parameters": dict(BASELINE), "seed": seed,
        "costs": costs, "manifest": manifest,
    }


def evaluate_holdout(records, selected, *, min_fills=20,
                     min_signals=30, costs=None, frozen=None):
    """Evaluate only explicitly selected configurations on frozen holdout."""
    costs = _validated_costs(costs)
    if frozen is None:
        raise ValueError("Holdout requires explicit frozen event IDs")
    _, holdout, _ = frozen
    result = []
    for row in selected:
        params = {k: row[k] for k in KEYS}
        perf = evaluate(holdout, params, min_fills=min_fills,
                        min_signals=min_signals, costs=costs)
        result.append({"config_id": row["config_id"],
                       "baseline": row["baseline"],
                       **params, **perf})
    return result


def inspect_trades(records, params, *, costs=None):
    out = []
    for record in records:
        if not _eligible(record, params):
            continue
        trade = simulate(record, params, costs=costs)
        out.append({"event_key": record["key"], "symbol": record["symbol"],
                    "side": record["side"], "retest_utc": datetime.fromtimestamp(
                        record["retest_ts"]/1000, timezone.utc).isoformat(),
                    "ready_utc": datetime.fromtimestamp(
                        record["ready_ms"]/1000, timezone.utc).isoformat(),
                    "context": record["context_status"],
                    "regime": record["regime_status"],
                    "departure": record["values"][0], "room_1h": record["values"][1],
                    "volume_rank": record["values"][2], "return_rank": record["values"][3],
                    "result": trade["status"], "net_pct": trade["pnl"],
                    "fill_price": trade.get("fill_price"),
                    "entry_liquidity": trade.get("entry_liquidity"),
                    "exit_liquidity": trade.get("exit_liquidity"),
                    "estimated_cost_pct": trade.get("cost_pct"),
                    "fill_utc": datetime.fromtimestamp(trade["fill_ts"]/1000, timezone.utc).isoformat()
                    if trade.get("fill_ts") is not None else None})
    return out
