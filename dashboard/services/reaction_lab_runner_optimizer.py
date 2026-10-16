"""Read-only, causal, reproducible search of Reaction Lab LIVE runner configurations.

No database writes, no network, no exchange orders, no PM2 interaction.
Discovery / temporal holdout are both exploratory (not future forward validation).
"""
from __future__ import annotations

import itertools
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
FEE_PCT = 0.07

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


def simulate(record, params, horizon=HORIZON, fee_pct=FEE_PCT):
    """Conservative causal fill from first COMPLETE minute after every input exists.

    Entry price is limit for intrabar touch, first candle OPEN if already
    marketable at the start. Ambiguous TP in intrabar fill minute is excluded.
    Both TP/SL on a later candle => worst-case SL, explicitly tagged.
    """
    bars, side = record["bars"], record["side"]
    ref = record["reference"]
    ready = record["ready_ms"]
    first = ((ready + MINUTE - 1) // MINUTE) * MINUTE
    expiry = ready + int(params["ttl"]) * MINUTE
    # TTL from all-four-factors ready time, not retest time.
    limit = ref * (1 - params["limit"]/100 if side == "LONG" else
                   1 + params["limit"]/100)
    found = None
    eligible_bars = 0
    for i, bar in enumerate(bars):
        ts, o, h, l, close = bar
        if ts < first or ts + MINUTE > expiry:
            continue
        eligible_bars += 1
        marketable = o <= limit if side == "LONG" else o >= limit
        touched = l <= limit if side == "LONG" else h >= limit
        if marketable or touched:
            found = i, (float(o) if marketable else limit), marketable
            break
    if found is None:
        last_full_ts = ((expiry - MINUTE)//MINUTE)*MINUTE
        if not eligible_bars or bars[-1][0] < last_full_ts:
            return {"status": "UNOBSERVABLE_WINDOW", "pnl": None}
        return {"status": "NO_FILL", "pnl": 0.0}
    idx, price, marketable = found
    target = price * (1 + params["tp"]/100 if side == "LONG" else
                      1 - params["tp"]/100)
    stop = price * (1 - params["sl"]/100 if side == "LONG" else
                    1 + params["sl"]/100)
    for j in range(idx, min(len(bars), idx + horizon)):
        ts, o, h, l, close = bars[j]
        hit_tp = h >= target if side == "LONG" else l <= target
        hit_sl = l <= stop if side == "LONG" else h >= stop
        if hit_tp and hit_sl and j == idx and not marketable:
            return {"status": "AMBIGUOUS_FILL_BAR", "pnl": None, "fill_ts": int(ts)}
        if hit_sl and hit_tp:
            return {"status": "AMBIGUOUS_BOTH_AS_SL", "pnl": -float(params["sl"]) - fee_pct,
                    "fill_ts": int(bars[idx][0]), "exit_ts": int(ts)}
        if hit_sl:
            return {"status": "SL", "pnl": -float(params["sl"]) - fee_pct,
                    "fill_ts": int(bars[idx][0]), "exit_ts": int(ts)}
        if hit_tp:
            if j == idx and not marketable:
                return {"status": "AMBIGUOUS_FILL_BAR", "pnl": None, "fill_ts": int(ts)}
            return {"status": "TP", "pnl": float(params["tp"]) - fee_pct,
                    "fill_ts": int(bars[idx][0]), "exit_ts": int(ts)}
    if len(bars) < idx + horizon:
        return {"status": "CENSORED_AFTER_FILL", "pnl": None,
                "fill_ts": int(bars[idx][0])}
    last = float(bars[idx+horizon-1][4])
    gross = ((last/price - 1)*100 if side == "LONG" else
             (1 - last/price)*100)
    return {"status": "TIMEOUT", "pnl": gross-fee_pct,
            "fill_ts": int(bars[idx][0]), "exit_ts": int(bars[idx+horizon-1][0])}


def evaluate(records, params, *, min_fills=5, trade_cache=None):
    """All matured events under one fixed source cohort, never runner-selected.

    Net per candidate signal includes NO_FILL=0. Censored/ambiguous results
    excluded from expectancy; report their count, do not call them losses.
    """
    outcome = Counter()
    pnl = []
    eligible = 0
    for record in records:
        if not _eligible(record, params):
            continue
        eligible += 1
        cache_key = (record["key"], params["limit"], params["ttl"],
                     params["tp"], params["sl"])
        result = trade_cache.get(cache_key) if trade_cache is not None else None
        if result is None:
            result = simulate(record, params)
            if trade_cache is not None:
                trade_cache[cache_key] = result
        outcome[result["status"]] += 1
        if result["pnl"] is not None and result["status"] != "NO_FILL":
            pnl.append(result["pnl"])
    fills = sum(outcome[k] for k in (
        "TP", "SL", "TIMEOUT", "AMBIGUOUS_FILL_BAR",
        "AMBIGUOUS_BOTH_AS_SL", "CENSORED_AFTER_FILL"))
    # Comparable across search configs: only fully resolved or no-fill signals.
    comparable = outcome["NO_FILL"] + outcome["TP"] + outcome["SL"] + outcome["TIMEOUT"] + outcome["AMBIGUOUS_BOTH_AS_SL"]
    wins = sum(x > 0 for x in pnl)
    gains = sum(x for x in pnl if x > 0)
    losses = -sum(x for x in pnl if x < 0)
    expectancy = sum(pnl)/comparable if comparable else None
    reliable = bool(len(pnl) >= min_fills and comparable >= min_fills and
                    comparable >= max(eligible * 0.8, 1))
    return {
        "señales": eligible, "evaluables": comparable, "fills": fills,
        "sin_fill": outcome["NO_FILL"], "tp": outcome["TP"],
        "sl": outcome["SL"] + outcome["AMBIGUOUS_BOTH_AS_SL"],
        "timeout": outcome["TIMEOUT"],
        "ambiguas": outcome["AMBIGUOUS_FILL_BAR"] + outcome["AMBIGUOUS_BOTH_AS_SL"],
        "censuradas": outcome["CENSORED_AFTER_FILL"] + outcome["UNOBSERVABLE_WINDOW"],
        "resueltas": len(pnl),
        "fill_pct": round(100 * fills / eligible, 2) if eligible else None,
        "wr_pct": round(100 * wins / len(pnl), 2) if pnl else None,
        "pf": round(gains / losses, 3) if losses else None,
        "pnl_medio_fill_pct": round(sum(pnl)/len(pnl), 5) if pnl else None,
        "expectativa_por_señal_pct": round(expectancy, 5) if expectancy is not None else None,
        "pnl_acumulado_pct": round(sum(pnl), 5),
        "muestra_suficiente": reliable,
    }


def run_search(records, grid, *, budget=500, seed=20261016,
               min_fills=5, train_share=0.70):
    if not 0.5 <= train_share <= 0.9:
        raise ValueError("train_share debe estar entre 0.5 y 0.9")
    configs, total = sample_configs(grid, budget=budget, seed=seed)
    cut = max(1, min(len(records), int(len(records) * train_share)))
    train = records[:cut]
    validation = records[cut:]
    rows = []
    cache = {}
    for params in configs:
        perf = evaluate(train, params, min_fills=min_fills, trade_cache=cache)
        rows.append({"config_id": len(rows) + 1,
                     "baseline": params == BASELINE, **params,
                     **{f"train_{k}": v for k,v in perf.items()}})
    # Validation is deliberately not consulted to rank configurations.
    rows.sort(key=lambda row: (
        bool(row["train_muestra_suficiente"]),
        row["train_expectativa_por_señal_pct"] if row["train_expectativa_por_señal_pct"] is not None else -1e10,
        row["train_resueltas"], -row["config_id"]), reverse=True)
    return {
        "ranking": rows,
        "total_combinations": total,
        "tested": len(configs),
        "train_n": len(train), "holdout_n": len(validation),
        "holdout_start_ms": validation[0]["retest_ts"] if validation else None,
        "parameters": dict(BASELINE), "seed": seed,
    }


def evaluate_holdout(records, selected, *, train_share=0.70, min_fills=5):
    cut = max(1, min(len(records), int(len(records)*train_share)))
    result = []
    for row in selected:
        params = {k: row[k] for k in KEYS}
        result.append({"config_id": row["config_id"], "baseline": row["baseline"],
                       **params, **evaluate(records[cut:], params, min_fills=min_fills)})
    return result


def inspect_trades(records, params):
    out = []
    for record in records:
        if not _eligible(record, params):
            continue
        trade = simulate(record, params)
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
                    "fill_utc": datetime.fromtimestamp(trade["fill_ts"]/1000, timezone.utc).isoformat()
                    if trade.get("fill_ts") is not None else None})
    return out
