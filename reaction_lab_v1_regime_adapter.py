#!/usr/bin/env python3
"""Canonical V1 4h regime snapshots for every observed Continuous Base REACTION.

Uses _build_boundary_snapshot from the *existing* V1 market-regime module.
Keeps the immutable causal contexts table unchanged; data is stored separately,
keyed by event_key, with evidence of the 4h closed boundary and implementation.
Reads Redis 4h history; does not edit legacy pickle or start a new worker.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import time

FOUR_H_MS = 14_400_000
DEFAULT_BASELINE = 42

SCHEMA = '''
CREATE TABLE IF NOT EXISTS v1_regime_snapshots (
 boundary_ts INTEGER PRIMARY KEY,
 computed_at_ms INTEGER NOT NULL,
 universe_signature TEXT NOT NULL,
 sector_catalog_hash TEXT NOT NULL,
 module_hash TEXT NOT NULL,
 valid_symbols INTEGER NOT NULL,
 rows_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v1_regime_events (
 event_key TEXT PRIMARY KEY,
 known_ts INTEGER NOT NULL,
 boundary_ts INTEGER NOT NULL,
 captured_at_ms INTEGER NOT NULL,
 status TEXT NOT NULL,
 metrics_json TEXT NOT NULL,
 capture_method TEXT NOT NULL,
 FOREIGN KEY(boundary_ts) REFERENCES v1_regime_snapshots(boundary_ts)
);
CREATE INDEX IF NOT EXISTS v1_regime_events_boundary ON v1_regime_events(boundary_ts);
CREATE TABLE IF NOT EXISTS v1_regime_attempts (
 event_key TEXT PRIMARY KEY,
 attempts INTEGER NOT NULL,
 last_attempt_ms INTEGER NOT NULL,
 next_retry_ms INTEGER NOT NULL,
 reason TEXT NOT NULL
);
'''


def event_boundary(known_ts: int) -> int:
    """Only the latest fully closed 4h candle is visible to the signal."""
    return (int(known_ts) // FOUR_H_MS - 1) * FOUR_H_MS


def catalog_hash(universe, sector_map, min_sector):
    payload = {'universe': sorted(set(universe)),
               'sector_map': {k: sector_map.get(k) for k in sorted(set(universe))},
               'min_sector': int(min_sector)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]


def source_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:24]


def canonical_module(project):
    # Load the actual file through its path, avoiding unrelated installed packages.
    import importlib.util
    path=Path(project)/'dashboard/analytics/candidate_v1_market_regime.py'
    if not path.is_file():
        raise FileNotFoundError(f'Canonical V1 market-regime module missing: {path}')
    spec=importlib.util.spec_from_file_location('_reaction_lab_canonical_v1_regime',path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'Cannot load V1 canonical module: {path}')
    canonical=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(canonical)
    for name in ('_parse_histories','_build_boundary_snapshot','_universe_signature'):
        if not callable(getattr(canonical, name, None)):
            raise RuntimeError(f'Missing V1 canonical function {name}')
    return canonical


def check_contiguous_reference(histories, boundary, universe, baseline=42):
    """Validate the 43 completed 4h timestamps for each available V1 symbol.

    The canonical V1 function itself checks index count, not continuity.  Avoid
    persisting an apparently valid ranking if Redis has a 4h historical gap.
    """
    broken=[]
    for symbol in universe:
        candles,index=histories.get(symbol, ([],{}))
        idx=index.get(boundary)
        if idx is None or idx < baseline:continue
        expected=list(range(boundary-baseline*FOUR_H_MS,boundary+FOUR_H_MS,FOUR_H_MS))
        actual=[int(c['timestamp']) for c in candles[idx-baseline:idx+1]]
        if actual!=expected:broken.append(symbol)
    if broken:
        raise RuntimeError(f'Non-contiguous 4h V1 histories for {len(broken)} symbols; first={broken[:5]}')


def regime_snapshot(client, canonical, universe, sector_map, boundary, min_sector):
    """Return all 290 canonical rows at a boundary (never future candles)."""
    from engine.live.data.redis_market_data_protocol import history_key
    pipe=client.pipeline(transaction=False)
    for symbol in universe:
        pipe.lrange(history_key(symbol,'4h'), -150, -1)
    raw=pipe.execute()
    decoded=[[v.decode('utf-8') if isinstance(v,bytes) else v for v in (history or [])]
             for history in raw]
    histories=canonical._parse_histories(universe,decoded)
    check_contiguous_reference(histories,boundary,universe,DEFAULT_BASELINE)
    result=canonical._build_boundary_snapshot(
        int(boundary),histories,universe,sector_map,
        DEFAULT_BASELINE,int(min_sector),canonical._universe_signature(universe))
    if result.empty:
        return {}
    return {str(row['symbol']).upper():row for row in result.to_dict('records')}


def required_rank_available(row):
    """A canonical event row cannot be marked ready with missing rank inputs."""
    for key in ('return_pct_4h','relative_volume_4h','return_rank_pct_4h',
                'volume_rank_pct_4h','market_breadth_4h'):
        x=row.get(key)
        if x is None:
            return False
        try:
            if not math.isfinite(float(x)):
                return False
        except (TypeError,ValueError):
            return False
    return True


def sector_available(row):
    sector=row.get('primary_sector')
    if not sector or sector=='Other':return False
    for key in ('sector_return_pct_4h','sector_breadth_4h','sector_relative_volume_4h',
                'sector_return_rank_pct_4h','sector_strength_vs_btc_4h',
                'symbol_strength_vs_sector_4h','sector_coverage_pct'):
        try:
            if not math.isfinite(float(row.get(key))):return False
        except (ValueError,TypeError):return False
    return True


def enrich_once(con,client,project,universe,sector_map,limit=50):
    """Append-only enrichment of existing Continuous contexts; never alter originals.

    Pending events with unavailable historical bars are NOT declared complete.
    An already stored 4h boundary is reused for every event on that boundary.
    """
    from config.market_sectors import MIN_SECTOR_SYMBOLS
    from reaction_lab_context import safe_json, now_ms
    canonical=canonical_module(project)
    module_file=Path(project)/'dashboard/analytics/candidate_v1_market_regime.py'
    module_fingerprint=source_hash(module_file)
    universe=tuple(sorted(set(str(s).upper() for s in universe)))
    sectors={str(k).upper():str(v) for k,v in sector_map.items()}
    signature=canonical._universe_signature(universe)
    sector_fingerprint=catalog_hash(universe,sectors,MIN_SECTOR_SYMBOLS)
    con.executescript(SCHEMA)
    now=now_ms()
    events=con.execute('''SELECT c.event_key,c.known_ts,c.context_json
            FROM contexts c LEFT JOIN v1_regime_events r ON r.event_key=c.event_key
              LEFT JOIN v1_regime_attempts a ON a.event_key=c.event_key
            WHERE r.event_key IS NULL AND COALESCE(a.next_retry_ms,0)<=?
            ORDER BY c.known_ts DESC LIMIT ?''',
            (now,max(0,int(limit)))).fetchall()
    summary={'checked':len(events),'enriched':0,'ready':0,'sector_unavailable':0,
             'missing_history':0,'missing_boundaries':0,'new_snapshots':0,'stored_snapshots':0}
    in_memory={}
    def defer(event_key,reason):
        prior=con.execute('SELECT attempts FROM v1_regime_attempts WHERE event_key=?',
                          (event_key,)).fetchone()
        attempts=(int(prior[0]) if prior else 0)+1
        delay_ms=min(6*3600_000,5*60_000*(2**min(attempts-1,7)))
        with con:
            con.execute('''INSERT INTO v1_regime_attempts VALUES(?,?,?,?,?)
                 ON CONFLICT(event_key) DO UPDATE SET
                 attempts=excluded.attempts,last_attempt_ms=excluded.last_attempt_ms,
                 next_retry_ms=excluded.next_retry_ms,reason=excluded.reason''',
                 (event_key,attempts,now_ms(),now_ms()+delay_ms,reason))

    for event_key,known,raw_context in events:
        boundary=event_boundary(known)
        if boundary + FOUR_H_MS>int(known):
            raise RuntimeError('Causality error: V1 4h candle is not closed')
        ctx=json.loads(raw_context)
        symbol=str(ctx.get('symbol') or event_key.split('|')[0]).upper()
        if boundary not in in_memory:
            cached=con.execute('''SELECT universe_signature,sector_catalog_hash,module_hash,rows_json
                FROM v1_regime_snapshots WHERE boundary_ts=?''',(boundary,)).fetchone()
            if cached is not None:
                if (cached[0],cached[1],cached[2])!=(signature,sector_fingerprint,module_fingerprint):
                    raise RuntimeError('V1 sector catalog or code changed for cached boundary; '
                                       'versioned migration required, frozen evidence untouched')
                in_memory[boundary]=json.loads(cached[3]);summary['stored_snapshots']+=1
            else:
                rows=regime_snapshot(client,canonical,universe,sectors,boundary,MIN_SECTOR_SYMBOLS)
                if not rows:
                    summary['missing_boundaries']+=1
                    in_memory[boundary]={}
                else:
                    in_memory[boundary]=rows
                    with con:
                        con.execute('''INSERT OR IGNORE INTO v1_regime_snapshots
                          VALUES (?,?,?,?,?,?,?)''',
                          (boundary,now_ms(),signature,sector_fingerprint,module_fingerprint,
                           len(rows),safe_json(rows)))
                    summary['new_snapshots']+=1
        row=in_memory[boundary].get(symbol)
        if row is None or not required_rank_available(row):
            summary['missing_history']+=1
            defer(event_key,'V1_REF_UNAVAILABLE_AT_BOUNDARY')
            continue
        status='READY' if sector_available(row) else 'SECTOR_UNAVAILABLE'
        metrics={**row,'sector_context_status':'READY' if status=='READY' else 'SECTOR_UNAVAILABLE',
                 'sector_context_available':status=='READY',
                 'v1_regime_status':status,'v1_regime_module_hash':module_fingerprint,
                 'v1_regime_sector_catalog_hash':sector_fingerprint,
                 'v1_regime_capture_method':'RECOMPUTED_ASOF_CLOSED_4H_REDIS',
                 'v1_regime_4h_boundary_ts':boundary}
        with con:
            con.execute('''INSERT OR IGNORE INTO v1_regime_events
              VALUES (?,?,?,?,?,?,?)''',
              (event_key,int(known),boundary,now_ms(),status,safe_json(metrics),
               'RECOMPUTED_ASOF_CLOSED_4H_REDIS'))
            con.execute('DELETE FROM v1_regime_attempts WHERE event_key=?',(event_key,))
        summary['enriched']+=1
        summary['ready']+=status=='READY'
        summary['sector_unavailable']+=status!='READY'
    return summary
