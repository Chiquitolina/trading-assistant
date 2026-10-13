#!/usr/bin/env python3
"""Read-only parity audit for frozen Reaction Lab context versus canonical app.py V2 Market Flow.

Never runs Streamlit top-level code, writes SQLite, or changes any scanner/PM2 process.
Does NOT establish V1 market_regime parity or exact HTF Room parity with V1/V2.
"""
from __future__ import annotations

import argparse
import ast
from collections import defaultdict
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import sys


def ro_connection(path: Path):
    return sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)


def source_functions(app_path: Path, service, symbols):
    import numpy as np
    import pandas as pd

    source = app_path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source, filename=str(app_path))
    names = (
        "_candidate_v2_normalize_candle_frame",
        "_candidate_v2_build_transition_rows_for_timeframe",
        "_candidate_v1_add_regime_labels",
        "_candidate_v2_transition_direction",
        "_candidate_v2_transition_divergence",
    )
    defs = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    missing = sorted(set(names) - set(defs))
    if missing:
        raise RuntimeError(f"Faltan funciones originales en app.py: {missing}")
    nodes = []
    for name in names:
        node = defs[name]
        node.decorator_list = []
        nodes.append(node)
    scope = {
        "pd": pd, "np": np,
        "CANDIDATE_V1_MARKET_SYMBOLS": list(symbols),
        "geometry_scanner_data_service": service,
    }
    compiled = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(compiled, str(app_path), "exec"), scope)
    return scope


def values_equal(a, b, tolerance=0.000055):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return math.isclose(float(a), float(b), rel_tol=1e-10, abs_tol=tolerance)
    except (ValueError, TypeError):
        return str(a) == str(b)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--db", type=Path, required=True, help="SQLite piloto causal_context (SOLO LECTURA)")
    parser.add_argument("--limit", type=int, default=2)
    args = parser.parse_args()
    project = args.project.resolve()
    sys.path.insert(0, str(project))

    from config.strategies.v1 import SYMBOLS
    from dashboard.services.geometry_scanner_data_service import GeometryScannerDataService
    import redis
    import os
    try:
        from dotenv import load_dotenv
        load_dotenv(project / ".env")
    except ImportError:
        pass
    symbols = sorted(set(str(s).upper() for s in SYMBOLS))
    if not args.db.is_file():
        raise FileNotFoundError(f"No existe la DB piloto: {args.db}")
    with closing(ro_connection(args.db)) as con:
        rows = con.execute(
            "SELECT event_key, known_ts, status, context_json, source_hash "
            "FROM contexts ORDER BY known_ts DESC LIMIT ?",
            (args.limit,),
        ).fetchall()
        frozen = {
            (str(tf), int(boundary)): json.loads(payload)
            for tf, boundary, payload in con.execute(
                "SELECT timeframe, boundary_ts, snapshot_json FROM breadth"
            )
        }

    if not rows:
        print("SIN DATOS: base piloto vacía")
        return 3
    client = redis.Redis(
        host=os.getenv("REDIS_HOST", "127.0.0.1"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        db=int(os.getenv("REDIS_DB", "0")),
        socket_timeout=10,
    )
    client.ping()
    service = GeometryScannerDataService(redis_client=client)
    f = source_functions(project / "dashboard/app.py", service, symbols)

    # Limit each canonical call to only relevant time boundaries, shared between events.
    boundaries = defaultdict(set)
    contexts = []
    for event_key, known, status, context_json, source_hash in rows:
        ctx = json.loads(context_json)
        contexts.append((event_key, int(known), status, ctx, source_hash))
        for tf, interval in (("1h", 3_600_000), ("4h", 14_400_000)):
            boundary = (int(known) // interval - 1) * interval
            boundaries[tf].update((boundary, boundary - interval))

    print("AUDITORÍA SOLO LECTURA — Candidate V2 Market Flow")
    print("DB:", args.db)
    print("UNIVERSO:", len(symbols), "símbolos")
    print("EVENTOS:", len(contexts))
    print("Ninguna función escribe en SQLite ni ejecuta Streamlit.")

    canonical = {}
    for tf in ("1h", "4h"):
        print(f"Calculando referencia original app.py {tf} para {len(boundaries[tf])} boundaries...")
        frame = f["_candidate_v2_build_transition_rows_for_timeframe"](tf, boundaries[tf])
        if frame is None or frame.empty:
            print("SIN REFERENCIA", tf)
            continue
        for record in frame.to_dict("records"):
            canonical[(tf, int(record["boundary_timestamp"]))] = record

    # Independently compare symbol-vs-BTC 1h/4h returns using the canonical
    # V2 candle normalizer (without rounding intermediate source prices).
    price_cache = {}
    def original_price_return(symbol, timeframe, boundary, interval):
        key = (symbol, timeframe, boundary)
        if key in price_cache:
            return price_cache[key]
        candles = service.get_closed_candles(symbol=symbol, timeframe=timeframe, limit=500)
        frame = f["_candidate_v2_normalize_candle_frame"](candles)
        prices = dict(zip(frame["timestamp"].astype(int), frame["close"].astype(float)))
        current = prices.get(boundary)
        previous = prices.get(boundary - interval)
        value = (current / previous - 1.0) * 100.0 if current is not None and previous not in (None, 0) else None
        price_cache[key] = value
        return value

    tested = 0
    differences = 0
    unavailable = 0

    def cmp(label, actual, reference, tolerance=0.000055):
        nonlocal tested, differences, unavailable
        if reference is None:
            unavailable += 1
            print(f"  NO_COMPARABLE {label}: frozen={actual!r}; referencia ausente")
            return
        tested += 1
        ok = values_equal(actual, reference, tolerance)
        if not ok:
            differences += 1
        print(f"  {'PASS' if ok else 'DIFF'} {label}: frozen={actual!r} original={reference!r}")

    for key, known, status, ctx, source_hash in contexts:
        print(f"\nEVENT: {key} | {status} | known_ts={known} | helper_hash={source_hash}")
        side = str(ctx.get("signal") or ctx.get("side") or "").upper()
        for tf, interval in (("1h", 3_600_000), ("4h", 14_400_000)):
            boundary = (known // interval - 1) * interval
            print(f"  [{tf}] boundary={boundary}")
            current = canonical.get((tf, boundary))
            previous = canonical.get((tf, boundary - interval))
            if not current:
                unavailable += 1
                print("  NO_COMPARABLE: referencia original no devolvió esta boundary")
                continue
            mappings = (
                (f"market_breadth_{tf}", "market_breadth_pct"),
                (f"market_coverage_{tf}", "coverage_pct"),
                (f"positive_symbols_{tf}", "positive_symbols"),
                (f"market_median_return_pct_{tf}", "market_median_return_pct"),
                (f"btc_return_pct_{tf}", "btc_return_pct"),
            )
            for source_field, original_field in mappings:
                cmp(source_field, ctx.get(source_field), current.get(original_field))
            symbol = str(ctx.get("symbol") or key.split("|", 1)[0]).upper()
            symbol_return = original_price_return(symbol, tf, boundary, interval)
            btc_return = original_price_return("BTCUSDT", tf, boundary, interval)
            cmp(f"return_pct_{tf} [precio original]", ctx.get(f"return_pct_{tf}"), symbol_return, tolerance=1e-9)
            if symbol_return is not None and btc_return is not None:
                residual = symbol_return - btc_return
                cmp(f"symbol_strength_vs_btc_{tf} [precio original]",
                    ctx.get(f"symbol_strength_vs_btc_{tf}"), residual, tolerance=1e-9)
                adjusted = residual * (1 if side == "LONG" else -1)
                cmp(f"side_adjusted_strength_vs_btc_{tf} [precio original]",
                    ctx.get(f"side_adjusted_strength_vs_btc_{tf}"), adjusted, tolerance=1e-9)
            else:
                unavailable += 1
                print(f"  NO_COMPARABLE: velas de precio {symbol} / BTCUSDT {tf} incompletas")
            cached = frozen.get((tf, boundary))
            if cached:
                cmp(f"frozen_breadth_snapshot_{tf}_valid_symbols", cached.get("valid_symbols"), current.get("valid_symbols"), tolerance=0)
                cmp(f"frozen_breadth_snapshot_{tf}_universe_count", cached.get("universe_count"), len(symbols), tolerance=0)
            if previous:
                if current.get("market_breadth_pct") is not None and previous.get("market_breadth_pct") is not None:
                    delta = current["market_breadth_pct"] - previous["market_breadth_pct"]
                    # Both source values are rounded to four decimals; derived delta can shift by 1e-4.
                    cmp(f"breadth_delta_{tf}", ctx.get(f"breadth_delta_{tf}"), delta, tolerance=0.00012)
                    direction = f["_candidate_v2_transition_direction"](__import__("pandas").Series([delta])).iloc[0]
                    cmp(f"breadth_direction_{tf}", ctx.get(f"breadth_direction_{tf}"), direction, tolerance=0)
                else:
                    unavailable += 1
                    print(f"  NO_COMPARABLE: breadth previo {tf} sin datos")
        # This validates label formula from V1, not numerical data-generation parity with market_regime.py.
        import pandas as pd
        prepared = pd.DataFrame([{
            "side":side,
            "market_breadth_4h":ctx.get("market_breadth_4h"),
            "btc_return_pct_4h":ctx.get("btc_return_pct_4h"),
            "symbol_strength_vs_btc_4h":ctx.get("symbol_strength_vs_btc_4h"),
            "sector_strength_vs_btc_4h":ctx.get("sector_strength_vs_btc_4h"),
        }])
        labeled = f["_candidate_v1_add_regime_labels"](prepared).iloc[0]
        cmp("Market alignment [fórmula V1]", ctx.get("Market alignment"), labeled["Market alignment"], tolerance=0)
        cmp("side_adjusted_strength_vs_btc_4h [fórmula V1]", ctx.get("side_adjusted_strength_vs_btc_4h"), labeled["side_adjusted_strength_vs_btc_4h"], tolerance=1e-9)

    print("\nRESUMEN", json.dumps({
        "comparisons":tested,
        "differences":differences,
        "not_comparable":unavailable,
        "scope":"app.py V2 Market Flow 1h/4h y etiquetas V1; NO V1 market-regime ni Room HTF",
    }, ensure_ascii=False))
    if differences:
        print("AUDIT FAIL: existen diferencias; NO activar PM2.")
        return 2
    if unavailable:
        print("AUDIT INCOMPLETE: alguna referencia faltante; NO tomar como paridad completa.")
        return 3
    print("AUDIT PASS (ALCANCE LIMITADO): ahora auditar módulo original market_regime y HTF Room.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
