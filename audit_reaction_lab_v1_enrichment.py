#!/usr/bin/env python3
"""READ-ONLY numeric parity: new Continuous V1 4h sidecar vs original V1 module.

Requires Redis 4h histories to still contain the historical 42-bar baseline.
Does not write any SQLite, Redis, legacy pickles or other project files.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys

FOUR_H_MS=14_400_000


def equal(frozen,reference,tol=0.000055):
    if frozen is None and reference is None:return True
    if frozen is None or reference is None:return False
    try:
        a,b=float(frozen),float(reference)
    except (TypeError,ValueError):
        return str(frozen)==str(reference)
    return math.isfinite(a) and math.isfinite(b) and math.isclose(a,b,abs_tol=tol,rel_tol=1e-10)


def compare_values(row,reference):
    fields=('return_pct_4h','btc_return_pct_4h','market_breadth_4h',
        'relative_volume_4h','return_rank_pct_4h','volume_rank_pct_4h',
        'sector_return_pct_4h','sector_breadth_4h','sector_relative_volume_4h',
        'sector_return_rank_pct_4h','sector_strength_vs_btc_4h',
        'symbol_strength_vs_sector_4h','sector_coverage_pct','symbol_strength_vs_btc_4h',
        'quote_volume_4h','median_quote_volume_4h','positive_symbols',
        'valid_universe_size','configured_universe_size','coverage_pct')
    return [(f,row.get(f),reference.get(f),equal(row.get(f),reference.get(f)))
            for f in fields]


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--project',type=Path,default=Path.cwd())
    ap.add_argument('--db',type=Path,required=True)
    ap.add_argument('--limit',type=int,default=12)
    args=ap.parse_args(argv)
    project=args.project.resolve(); sys.path.insert(0,str(project))
    from reaction_lab_v1_regime_adapter import canonical_module,regime_snapshot,event_boundary
    from config.strategies.v1 import SYMBOLS
    from config.market_sectors import MIN_SECTOR_SYMBOLS
    from engine.live.data.market_sector_catalog import MarketSectorCatalog
    from dotenv import load_dotenv
    import redis
    if not args.db.is_file():
        print('NO DB:',args.db);return 3
    with closing(sqlite3.connect(f'file:{args.db.resolve().as_posix()}?mode=ro',uri=True)) as con:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='v1_regime_events'").fetchone():
            print('NO SIDE-CAR: v1_regime_events has not been created');return 3
        rows=con.execute('''SELECT r.event_key,r.known_ts,r.boundary_ts,r.status,
                  r.metrics_json,c.context_json
                  FROM v1_regime_events r INNER JOIN contexts c USING (event_key)
                  ORDER BY r.known_ts DESC LIMIT ?''',(max(1,args.limit),)).fetchall()
    if not rows:
        print('NO V1 REGIME EVENTS available');return 3
    universe=tuple(sorted(set(str(s).upper() for s in SYMBOLS)))
    catalog=MarketSectorCatalog(required_symbols=universe)
    sectors={str(s).upper():str(sector) for sector,group in catalog.symbols_by_primary_sector().items() for s in group}
    canonical=canonical_module(project)
    load_dotenv(project/'.env')
    client=redis.Redis(host=os.getenv('REDIS_HOST','127.0.0.1'),port=int(os.getenv('REDIS_PORT','6379')),
                       db=int(os.getenv('REDIS_DB','0')),socket_timeout=15)
    client.ping()
    print('AUDITOR V1 SIDECAR: sólo lectura; universo',len(universe),'eventos',len(rows),flush=True)
    references={};comparison=diffs=missing=sector_pass=0
    for key,known,boundary,status,raw,core_json in rows:
        if boundary != event_boundary(known):
            diffs+=1;print('BAD BOUNDARY',key,boundary,known);continue
        if boundary not in references:
            references[boundary]=regime_snapshot(client,canonical,universe,sectors,boundary,MIN_SECTOR_SYMBOLS)
            print('BOUNDARY',boundary,'valid_symbols',len(references[boundary]),flush=True)
        frozen=json.loads(raw)
        symbol=json.loads(core_json).get('symbol') or key.split('|')[0]
        original=references[boundary].get(str(symbol).upper())
        if original is None:
            missing+=1;print('NO_REFERENCE',key);continue
        print('\nEVENT:',key,'status:',status)
        for field,actual,reference,passed in compare_values(frozen,original):
            comparison+=1
            if passed:
                print('  PASS',field,'frozen=',actual,'original=',reference)
                if field.startswith('sector_'):sector_pass+=1
            else:
                diffs+=1
                print('  DIFF',field,'frozen=',actual,'original=',reference)
        expected_sector=original.get('primary_sector')
        if str(frozen.get('primary_sector')) != str(expected_sector):
            diffs+=1
            print('  DIFF primary_sector:',frozen.get('primary_sector'),expected_sector)
        if status=='READY' and frozen.get('sector_context_status')!='READY':
            diffs+=1;print('  DIFF sector status is inconsistent')
        expected_hash=hashlib.sha256((project/'dashboard/analytics/candidate_v1_market_regime.py').read_bytes()).hexdigest()[:24]
        if frozen.get('v1_regime_module_hash') != expected_hash:
            diffs+=1;print('  DIFF canonical source hash has changed')
    summary={'events':len(rows),'boundaries':len(references),'comparisons':comparison,
             'differences':diffs,'no_reference':missing,'sector_checks_pass':sector_pass}
    print('\nRESUMEN',json.dumps(summary,ensure_ascii=False))
    if diffs or missing or comparison==0:
        print('AUDIT FAIL / INCONCLUSIVE: do not activate PM2');return 2
    print('AUDIT PASS: sidecar V1 parity in sampled events (not a full-production guarantee)')
    return 0


if __name__=='__main__':raise SystemExit(main())
