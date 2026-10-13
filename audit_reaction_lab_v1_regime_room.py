#!/usr/bin/env python3
"""READ-ONLY audit: frozen Continuous V1 vs original Candidate V1 4h regime
and original Candidate V2 HTF Room causality validation.

Does not start Streamlit, write to Redis, modify SQLite, or write legacy pickle.
The 4h source is imported from the existing VPS Python module (not a rewritten
implementation); its pure _build_boundary_snapshot is called directly.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import closing
import importlib.util
import json
import math
import os
from pathlib import Path
import sqlite3
import sys

FOUR_H_MS = 14_400_000


def read_only(path):
    return sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)


def value_equal(actual, expected, tol=0.000055):
    if actual is None and expected is None:
        return True
    if actual is None or expected is None:
        return False
    try:
        a, e = float(actual), float(expected)
        return math.isfinite(a) and math.isfinite(e) and math.isclose(a, e, rel_tol=1e-10, abs_tol=tol)
    except (ValueError, TypeError):
        return str(actual) == str(expected)


def extract_dashboard_function(app_path, name):
    import numpy as np
    import pandas as pd
    source = app_path.read_text(encoding='utf-8-sig')
    node = next((n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == name), None)
    if node is None:
        raise RuntimeError(f"No se encontró {name} en {app_path}")
    node.decorator_list = []
    compiled = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    env = {'pd': pd, 'np': np}
    exec(compile(compiled, str(app_path), 'exec'), env)
    return env[name]


def load_v1_module(path):
    if not path.is_file():
        raise FileNotFoundError(f"Falta el módulo original: {path}. El texto pegado no reemplaza al .py de tu VPS")
    spec = importlib.util.spec_from_file_location('_parity_canonical_v1_regime', path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"No se puede importar módulo original: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ('_build_boundary_snapshot', '_parse_histories', '_universe_signature'):
        if not callable(getattr(module, name, None)):
            raise RuntimeError(f"La versión VPS no tiene la función original {name}")
    return module


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path.cwd())
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=12)
    parser.add_argument('--max-history', type=int, default=500)
    args = parser.parse_args(argv)
    project = args.project.resolve()
    sys.path.insert(0, str(project))
    module_file = project / 'dashboard/analytics/candidate_v1_market_regime.py'
    import pandas as pd
    from config.strategies.v1 import SYMBOLS
    from config.market_sectors import MIN_SECTOR_SYMBOLS
    from engine.live.data.market_sector_catalog import MarketSectorCatalog
    from engine.live.data.redis_market_data_protocol import history_key

    canonical = load_v1_module(module_file)
    room_validator = extract_dashboard_function(project/'dashboard/app.py', '_candidate_v2_room_causality_components')
    source_room = extract_dashboard_function(project/'dashboard/app.py', '_reaction_driver_swing_distances')
    from engine.live.research.swing_detector import SwingDetector
    if not args.db.is_file():
        raise FileNotFoundError(f"Base piloto inexistente: {args.db}")
    with closing(read_only(args.db)) as con:
        rows = con.execute('SELECT event_key,known_ts,status,context_json,candles_json FROM contexts ORDER BY known_ts DESC LIMIT ?', (max(1,args.limit),)).fetchall()
    if not rows:
        print('SIN EVENTOS para auditar; no se hicieron cambios'); return 3
    events = [(str(key), int(known), status, json.loads(raw), json.loads(candles)) for key,known,status,raw,candles in rows]
    universe = tuple(sorted({str(s).upper() for s in SYMBOLS}))
    sector_map = {}
    catalog = MarketSectorCatalog(required_symbols=universe)
    for sector, members in catalog.symbols_by_primary_sector().items():
        for symbol in members:
            sector_map[str(symbol).upper()] = str(sector)
    min_sector = int(MIN_SECTOR_SYMBOLS)

    print('AUDITORÍA SOLO LECTURA — V1 regime + V2 HTF Room')
    print('PROYECTO:', project)
    print('DB:', args.db.resolve())
    print('MÓDULO CANÓNICO:', module_file)
    print('UNIVERSO:', len(universe), 'SÍMBOLOS, min_sector=',min_sector)
    print('EVENTOS:', len(events), 'INTERVALOS 4H:', len({(known//FOUR_H_MS-1)*FOUR_H_MS for _,known,_,_,_ in events}))
    print('CAVEAT: el volumen del worker actual es volumen BASE; V1 usa volumen QUOTE, mediana 42')
    print('NO se modifica SQLite, Redis ni archivos legacy.\n')

    import redis
    from dotenv import load_dotenv
    load_dotenv(project/'.env')
    client = redis.Redis(host=os.getenv('REDIS_HOST','127.0.0.1'), port=int(os.getenv('REDIS_PORT','6379')),
                         db=int(os.getenv('REDIS_DB','0')),socket_timeout=15)
    client.ping()
    pipe = client.pipeline(transaction=False)
    size = max(48,min(500,int(args.max_history)))
    for symbol in universe:
        pipe.lrange(history_key(symbol,'4h'), -size, -1)
    histories = canonical._parse_histories(universe, pipe.execute())
    signature = canonical._universe_signature(universe)
    boundaries = sorted({(known//FOUR_H_MS-1)*FOUR_H_MS for _,known,_,_,_ in events})
    references = {}
    for boundary in boundaries:
        frame = canonical._build_boundary_snapshot(boundary, histories, universe, sector_map,
                                                   42, min_sector, signature)
        references[boundary] = ({} if frame.empty else {
            str(d['symbol']).upper():d for d in frame.to_dict('records')
        })
        print('ORIGINAL 4H',boundary,':',len(references[boundary]),'símbolos con 42 velas y volumen QUOTE válidos')

    comparisons = differences = no_reference = optional_missing = room_pass = room_fail = room_no_room = 0
    room_replay_pass = room_replay_fail = 0
    optional_fields = (
        'relative_volume_4h','return_rank_pct_4h','volume_rank_pct_4h',
        'sector_return_pct_4h','sector_breadth_4h','sector_relative_volume_4h',
        'sector_return_rank_pct_4h','sector_strength_vs_btc_4h',
        'symbol_strength_vs_sector_4h','sector_coverage_pct',
    )
    comparisons_fields = (
        ('return_pct_4h','return_pct_4h'),
        ('btc_return_pct_4h','btc_return_pct_4h'),
        ('market_breadth_4h','market_breadth_4h'),
        ('market_coverage_4h','coverage_pct'),
        ('positive_symbols_4h','positive_symbols'),
        ('symbol_strength_vs_btc_4h','symbol_strength_vs_btc_4h'),
    )
    for key,known,state,ctx,archived_candles in events:
        symbol=str(ctx.get('symbol') or key.split('|',1)[0]).upper()
        boundary=(known//FOUR_H_MS-1)*FOUR_H_MS
        orig=references[boundary].get(symbol)
        print(f'\nEVENT {key} status={state} known_ts={known} boundary={boundary}')
        if orig is None:
            no_reference += 1
            print('  NO_COMPARABLE V1: no hay 42 velas 4h con volumen QUOTE válido para este símbolo/boundary')
        else:
            for frozen,source in comparisons_fields:
                actual=ctx.get(frozen); expected=orig.get(source)
                if expected is None or pd.isna(expected):
                    no_reference+=1;print(f'  NO_COMPARABLE {frozen}: original={expected!r}');continue
                comparisons+=1
                tol = 0.00011 if frozen == 'symbol_strength_vs_btc_4h' else 0.000055
                ok=value_equal(actual, expected, tol=tol)
                if not ok: differences+=1
                print(f"  {'PASS' if ok else 'DIFF'} {frozen}: guardado={actual!r} V1={expected!r}")
            for name in optional_fields:
                expected=orig.get(name)
                if expected is None or pd.isna(expected):
                    print(f'  N/A V1 {name}: referencia sin valor (p.ej. sector Other o sin coverage)')
                elif ctx.get(name) is None:
                    optional_missing+=1
                    print(f'  PENDIENTE de persistir {name}: V1={expected!r}')
                else:
                    comparisons+=1
                    ok=value_equal(ctx.get(name),expected,0.000055)
                    if not ok:differences+=1
                    print(f"  {'PASS' if ok else 'DIFF'} {name}: frozen={ctx.get(name)!r} V1={expected!r}")
        # Independently replay the original dashboard 5x5 detector over the immutable
        # per-event candle archive. Compare all three HTFs, not just the minimum.
        side = str(ctx.get('signal') or key.split('|')[1]).upper()
        reaction_price = ctx.get('retest_close') or ctx.get('retest_price')
        for tf,tf_ms in (('30m',1_800_000),('1h',3_600_000),('4h',14_400_000)):
            bars = archived_candles.get(tf) or []
            if not bars or reaction_price is None:
                no_reference+=1
                print(f'  ROOM NO_COMPARABLE {tf}: candles o reaction_price ausentes')
                continue
            if any(int(b['timestamp'])+tf_ms > known for b in bars):
                room_replay_fail+=1
                print(f'  ROOM FAIL {tf}: ARCHIVO contiene vela futura respecto a known_ts')
                continue
            detected = SwingDetector(left_bars=5,right_bars=5,min_prominence_pct=0.0).detect_all(bars)
            d = source_room(detected,known,tf_ms,float(reaction_price),side)
            original_val = d.get('opposing_room_pct')
            if pd.isna(original_val): original_val=None
            stored = ctx.get(f'opposing_room_{tf}_pct')
            if value_equal(stored,original_val,1e-8):
                room_replay_pass+=1
                print(f'  ROOM REPLAY PASS {tf}: stored={stored!r} original={original_val!r}')
            else:
                room_replay_fail+=1
                print(f'  ROOM REPLAY DIFF {tf}: stored={stored!r} original={original_val!r}')
        # Compare original room causality implementation to stored price/timestamps.
        work=dict(ctx)
        work['side']=ctx.get('signal') or key.split('|')[1]
        work['candidate_v1_reaction_known_ts']=known
        work['retest_close']=ctx.get('retest_close') or ctx.get('retest_price')
        ref=room_validator(pd.DataFrame([work]))
        if ref.empty:
            room_fail+=1;print('  ROOM ERROR: original validator returned no rows')
        else:
            status=str(ref.iloc[0]['room_causal_status'])
            print('  ROOM', status,'recomputed=',ref.iloc[0]['room_recomputed_pct'],
                  'stored=',ctx.get('nearest_opposing_room_pct'),
                  'lag_min=',ref.iloc[0]['room_causal_lag_min'])
            if status == 'PASS':room_pass+=1
            elif status == 'NO_ROOM':room_no_room+=1
            else:room_fail+=1
        # Independent nearest minimum among 30m/1h/4h as a second internal consistency check.
        candidates=[(float(ctx[f'opposing_room_{tf}_pct']),tf) for tf in ('30m','1h','4h')
                    if ctx.get(f'opposing_room_{tf}_pct') is not None]
        if candidates:
            minimum=min(candidates)
            if not value_equal(ctx.get('nearest_opposing_room_pct'),minimum[0],1e-8):
                differences+=1
                print('  DIFF nearest Room vs min(30m,1h,4h)', minimum)
    print('\nRESUMEN',json.dumps({
        'events':len(events), 'regime_compared':comparisons,'regime_differences':differences,
        'no_comparable':no_reference,'sector_or_rank_fields_pending':optional_missing,
        'room_pass':room_pass,'room_fail':room_fail,'room_no_room':room_no_room,
        'room_replay_pass':room_replay_pass,'room_replay_fail':room_replay_fail,
        '4h_boundaries_checked':len(boundaries),
    },ensure_ascii=False))
    if differences or room_fail or room_replay_fail:
        print('AUDIT DIFF / ROOM FAIL: NO activar el servicio aún.');return 2
    if no_reference:
        print('AUDIT INCONCLUSO: faltaron referencias originales; revisar retención / 42 velas.');return 3
    if optional_missing:
        print('AUDIT CORE PASS, SECTOR PENDIENTE: NO confundir con paridad completa.');return 0
    print('AUDIT PASS (alcance eventos seleccionados): revisar variedad de horarios / SHORT antes de PM2.');return 0

if __name__=='__main__':
    raise SystemExit(main())
