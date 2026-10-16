#!/usr/bin/env python3
"""Phase 3B - causal historical research factors for Continuous V1 REACTIONs.

Reads Phase3A replay_events.csv and canonical project source without importing
Streamlit, reconstructs 1h Room and canonical 4h cross-sectional percentile
ranks from *closed* Binance Futures OHLCV. Writes isolated research CSV/JSON.

This is NOT a LIVE parity certificate and never writes production stores.
"""
from __future__ import annotations

import argparse
import ast
import bisect
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from statistics import median
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd

HOUR = 3_600_000
FOUR_H = 4 * HOUR
BASELINE = 42
ROOM_TAIL = 168
REPLAY_FIELDS = ('event_key', 'symbol', 'side', 'retest_ts', 'known_ts',
                 'first_replay_observed_ms', 'causal_json', 'source_hash')
FACTOR_FIELDS = ('event_key', 'room_1h_pct', 'volume_rank_pct_4h',
                 'return_rank_pct_4h', 'room_available_ms', 'ranks_available_ms',
                 'room_asof_ms', 'ranks_asof_ms', 'room_method', 'ranks_method')
EXTRA_FIELDS = ('symbol', 'retest_ts', 'source_1h', 'source_4h',
                'room_reference_price', 'opposing_swing_price',
                'opposing_swing_confirmed_ts', 'opposing_swing_actionable_ts',
                'market_valid_symbols', 'market_universe_symbols',
                'market_coverage_pct', 'factor_issue')
TIMEFRAMES = {'1h': HOUR, '4h': FOUR_H}
PROVENANCE = 'BINANCE_FUTURES_NATIVE_KLINES_QUOTE_VOLUME'


def sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump_json_atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False), encoding='utf-8')
    os.replace(tmp, path)


def csv_atomic(path, fields, rows):
    import csv
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w', newline='', encoding='utf-8') as handle:
        w = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def load_canonical_source(project):
    """Compile unmodified *source AST* of official research math and Room helper."""
    project = Path(project)
    regime = project / 'dashboard/analytics/candidate_v1_market_regime.py'
    app = project / 'dashboard/app.py'
    swing = project / 'engine/live/research/swing_detector.py'
    if not all(p.is_file() for p in (regime, app, swing)):
        raise FileNotFoundError(f'Canonical math files missing: {[str(p) for p in (regime,app,swing) if not p.is_file()]}')
    # Import SwingDetector via source path: avoid importing running app / external deps.
    import importlib.util
    spec = importlib.util.spec_from_file_location('_phase3b_canonical_swing', swing)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    room_tree = ast.parse(app.read_text(encoding='utf-8-sig'))
    room_name = '_reaction_driver_swing_distances'
    room_def = next((n for n in room_tree.body if isinstance(n, ast.FunctionDef) and n.name == room_name), None)
    if room_def is None:
        raise RuntimeError('Missing canonical Room helper in dashboard/app.py')
    room_def.decorator_list = []
    room_ast = ast.fix_missing_locations(ast.Module(body=[room_def], type_ignores=[]))
    room_scope = {'pd': pd, 'np': np}
    exec(compile(room_ast, str(app), 'exec'), room_scope)
    regime_tree = ast.parse(regime.read_text(encoding='utf-8-sig'))
    helper_names = ('_extract_quote_volume', '_percentile_ranks',
                    '_universe_signature', '_symbol_metrics_at', '_build_boundary_snapshot')
    assignments = []
    functions = []
    for node in regime_tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in ('FOUR_H_MS', 'STORE_COLUMNS') for t in node.targets):
            assignments.append(node)
        if isinstance(node, ast.FunctionDef) and node.name in helper_names:
            node.decorator_list = []
            functions.append(node)
    if len(functions) != len(helper_names) or len(assignments) != 2:
        raise RuntimeError('Canonical 4h regime math source missing required definitions')
    regime_ast = ast.fix_missing_locations(ast.Module(body=assignments+functions, type_ignores=[]))
    import typing
    scope = {'pd': pd, 'np': np, 'bisect': bisect, 'median': median,
             'Mapping': typing.Mapping, 'Sequence': typing.Sequence}
    exec(compile(regime_ast, str(regime), 'exec'), scope)
    return {
        'room': room_scope[room_name], 'SwingDetector': mod.SwingDetector,
        'build_snapshot': scope['_build_boundary_snapshot'],
        'universe_signature': scope['_universe_signature'],
        'hashes': {str(p.relative_to(project)): sha_file(p) for p in (regime, app, swing)},
        'math_ast_sha256': hashlib.sha256(ast.dump(regime_ast,include_attributes=False).encode()).hexdigest(),
        'room_ast_sha256': hashlib.sha256(ast.dump(room_ast,include_attributes=False).encode()).hexdigest(),
    }


def closed_boundary(known, interval):
    """Candle OPEN whose CLOSE is known at known_ms."""
    return (int(known)//interval - 1)*interval


def price_from_payload(payload):
    # Exact precedence used by reaction_lab_context.context_for_event.
    v = payload.get('retest_close') or payload.get('retest_price')
    try:
        n = float(v)
        return n if math.isfinite(n) and n > 0 else None
    except (ValueError, TypeError):
        return None


def contiguous_candles(rows, interval):
    ts = [int(c['timestamp']) for c in rows]
    return len(ts) == len(set(ts)) and all(b-a == interval for a,b in zip(ts,ts[1:]))


def build_room(event, candles, canonical):
    """Room 1h identical to LIVE research helper, truncated to last 168 completed 1h bars."""
    known = int(event['known_ts'])
    bound = closed_boundary(known, HOUR)
    bars = [c for c in candles if int(c['timestamp']) + HOUR <= known]
    if not bars or int(bars[-1]['timestamp']) != bound:
        return None, 'MISSING_LAST_CLOSED_1H', {}
    bars = bars[-ROOM_TAIL:]
    if len(bars) < 11 or not contiguous_candles(bars, HOUR):
        return None, 'MISSING_OR_GAPPED_1H_ROOM_HISTORY', {}
    price = price_from_payload(event['payload'])
    if price is None:
        return None, 'MISSING_RETEST_PRICE', {}
    pts = canonical['SwingDetector'](left_bars=5, right_bars=5,
                                     min_prominence_pct=0.0).detect_all(bars)
    info = canonical['room'](pts, known, HOUR, price, event['side'])
    room = info.get('opposing_room_pct')
    if room is None or not math.isfinite(float(room)) or float(room) < 0:
        return None, 'NO_FAVORABLE_CONFIRMED_OPPOSING_SWING_1H', {'reference_price': price}
    actionable = info.get('opposing_swing_actionable_timestamp')
    if actionable is None or int(actionable) > known:
        raise RuntimeError('1h Room future confirmed swing entered past research decision')
    return float(room), '', {
        'reference_price': price,
        'opposing_swing_price': float(info['opposing_swing_price']),
        'opposing_swing_confirmed_ts': int(info['opposing_swing_confirmed_timestamp']),
        'opposing_swing_actionable_ts': int(actionable),
        'asof_ms': bound + HOUR,
        'available_ms': known, # retest price is known only at completed 1m candle
    }


def rank_snapshots(events, universe, candles_4h, canonical, minimum_coverage):
    """One whole-market canonical 4h snapshot per completed boundary (NOT per selected signal)."""
    boundaries = sorted({closed_boundary(int(e['known_ts']), FOUR_H) for e in events.values()})
    output = {}
    diagnostics = []
    signature = canonical['universe_signature'](universe)
    for boundary in boundaries:
        histories = {}
        gapped = []
        for sym in universe:
            rows = [c for c in candles_4h.get(sym,()) if int(c['timestamp']) <= boundary]
            idx = len(rows) - 1
            if idx < BASELINE or int(rows[idx]['timestamp']) != boundary:
                continue
            required = rows[idx-BASELINE:idx+1]
            if not contiguous_candles(required, FOUR_H):
                gapped.append(sym)
                continue
            histories[sym] = (rows, {int(c['timestamp']):i for i,c in enumerate(rows)})
        # If any symbol *appears to have 43 bars* but has gaps, do not silently
        # compute a misleading cross-sectional percentile; block boundary.
        if gapped:
            output[boundary] = ({}, 'NONCONTIGUOUS_CANONICAL_4H_HISTORY')
            diagnostics.append({'boundary_open_ms': boundary, 'valid_symbols': 0,
                                'coverage_pct': 0.0, 'issue': 'NONCONTIGUOUS_CANONICAL_4H_HISTORY',
                                'affected': gapped[:20]})
            continue
        frame = canonical['build_snapshot'](boundary, histories, universe, {},
                                            BASELINE, 1, signature)
        valid = len(frame)
        coverage = 100*valid/len(universe)
        issue = '' if coverage >= 100*minimum_coverage else 'LOW_4H_UNIVERSE_COVERAGE'
        rows = {} if issue else {str(r['symbol']):r for r in frame.to_dict('records')}
        output[boundary] = (rows, issue)
        diagnostics.append({'boundary_open_ms': boundary,
                            'boundary_close_ms': boundary + FOUR_H,
                            'valid_symbols': valid, 'universe_total': len(universe),
                            'coverage_pct': round(coverage,4), 'issue': issue,
                            'affected': [],
                            'missing_symbols_sample': [s for s in universe if s not in set(frame['symbol'])][:25]})
    return output, diagnostics


def factors_for_events(events, rooms, ranks, boundary_diagnostics):
    diag_map = {int(x['boundary_open_ms']):x for x in boundary_diagnostics}
    output=[]
    for key,e in sorted(events.items(), key=lambda kv: (int(kv[1]['known_ts']),kv[0])):
        known = int(e['known_ts'])
        bound = closed_boundary(known, FOUR_H)
        ranking, r_issue = ranks.get(bound, ({}, 'MISSING_4H_SNAPSHOT'))
        row = ranking.get(e['symbol']) if ranking else None
        room, room_issue, detail = rooms.get(key,(None,'MISSING_1H_ROOM',{}))
        volume = row.get('volume_rank_pct_4h') if row else None
        ret = row.get('return_rank_pct_4h') if row else None
        issues = [x for x in (room_issue, r_issue, 'TARGET_4H_RANK_MISSING' if row is None and not r_issue else '') if x]
        valid = (not issues and all(math.isfinite(float(v)) for v in (room,volume,ret)))
        report = {
            'event_key':key,
            'room_1h_pct':room if valid else '',
            'volume_rank_pct_4h':volume if valid else '',
            'return_rank_pct_4h':ret if valid else '',
            'room_available_ms':detail.get('available_ms','') if valid else '',
            'ranks_available_ms':bound+FOUR_H if valid else '',
            'room_asof_ms':detail.get('asof_ms','') if valid else '',
            'ranks_asof_ms':bound+FOUR_H if valid else '',
            'room_method':'ASOF_CONFIRMED_SWINGS_1H' if valid else '',
            'ranks_method':'ASOF_CLOSED_4H_CROSS_SECTION' if valid else '',
            'symbol':e['symbol'], 'retest_ts':e['retest_ts'],
            'source_1h':PROVENANCE, 'source_4h':PROVENANCE,
            'room_reference_price':detail.get('reference_price',''),
            'opposing_swing_price':detail.get('opposing_swing_price',''),
            'opposing_swing_confirmed_ts':detail.get('opposing_swing_confirmed_ts',''),
            'opposing_swing_actionable_ts':detail.get('opposing_swing_actionable_ts',''),
            'market_valid_symbols':diag_map.get(bound,{}).get('valid_symbols',''),
            'market_universe_symbols':diag_map.get(bound,{}).get('universe_total',''),
            'market_coverage_pct':diag_map.get(bound,{}).get('coverage_pct',''),
            'factor_issue':';'.join(issues),
        }
        output.append(report)
    return output


def _api_klines(sym, tf, start, end, delay, retries=4):
    """Quote asset volume is Binance native row[7], NEVER estimated from close*volume."""
    interval = TIMEFRAMES[tf]
    pos = start
    candles=[]
    while pos < end:
        params = urllib.parse.urlencode({'symbol':sym, 'interval':tf,
                                         'startTime':pos, 'endTime':end-1,
                                         'limit':1000})
        url = f'https://fapi.binance.com/fapi/v1/klines?{params}'
        data = None
        for attempt in range(retries):
            try:
                req=urllib.request.Request(url,headers={'User-Agent':'runner-historical-context/3B'})
                with urllib.request.urlopen(req,timeout=35) as response:
                    data=json.loads(response.read())
                if not isinstance(data,list):
                    raise RuntimeError(f'Unexpected Binance payload {sym} {tf}')
                break
            except (urllib.error.HTTPError,urllib.error.URLError,TimeoutError,ValueError) as exc:
                if isinstance(exc,urllib.error.HTTPError) and exc.code in (418,429):
                    raise RuntimeError(f'Binance rate limit HTTP {exc.code}; stop and retry later') from exc
                if isinstance(exc,urllib.error.HTTPError) and 400 <= exc.code < 500:
                    raise RuntimeError(f'Binance rejected {sym} {tf} HTTP {exc.code}; unavailable historical market') from exc
                if attempt==retries-1:raise RuntimeError(f'Binance error {sym} {tf}: {exc}') from exc
                time.sleep(min(12,2**attempt))
        if not data:break
        for r in data:
            ts=int(r[0])
            if start<=ts<end:
                item={'timestamp':ts,'open':float(r[1]),'high':float(r[2]),
                      'low':float(r[3]),'close':float(r[4]),
                      'volume':float(r[5]),'quoteVolume':float(r[7])}
                if (ts % interval or any(not math.isfinite(v) for v in item.values())
                    or min(item['open'],item['high'],item['low'],item['close'])<=0
                    or item['volume']<0 or item['quoteVolume']<0
                    or item['low']>min(item['open'],item['close'])
                    or item['high']<max(item['open'],item['close'])):
                    raise ValueError(f'Invalid OHLC/volume {sym} {tf} {ts}')
                candles.append(item)
        next_pos = int(data[-1][0])+interval
        if next_pos<=pos:raise RuntimeError('Binance pagination stalled')
        pos=next_pos
        if delay:time.sleep(delay)
        if len(data)<1000:break
    dedup={}
    for item in candles:
        t=item['timestamp']
        if t in dedup and dedup[t]!=item:
            raise ValueError(f'Conflicting Binance candle {sym} {tf} {t}')
        dedup[t]=item
    return [dedup[t] for t in sorted(dedup)]


def download_cached(sym, tf, start, end, cache_dir, *, offline=False, delay=0.10):
    """Own caches; never touches Phase3A or production data."""
    stamp=f'{sym}|{tf}|{start}|{end}|{PROVENANCE}|v1'
    cache_dir=Path(cache_dir)
    path=cache_dir / (hashlib.sha256(stamp.encode()).hexdigest()[:22]+'.json')
    if path.exists():
        data=json.loads(path.read_text(encoding='utf-8'))
        if (data.get('symbol'),data.get('timeframe'),data.get('start_ms'),data.get('end_ms'),data.get('origin')) != (sym,tf,start,end,PROVENANCE):
            raise ValueError(f'Historical context cache mismatch: {path}')
        if not isinstance(data.get('candles'),list):
            raise ValueError(f'Invalid context cache: {path}')
        candles=data['candles']
        for c in candles:
            if not start<=int(c['timestamp'])<end or 'quoteVolume' not in c:
                raise ValueError(f'Corrupt candle cache: {path}')
        return candles, True, path
    if offline:
        raise FileNotFoundError(f'Offline context cache absent {sym} {tf} {path}')
    rows=_api_klines(sym,tf,start,end,delay)
    payload={'origin':PROVENANCE,'symbol':sym,'timeframe':tf,
             'start_ms':start,'end_ms':end,'retrieved_utc':datetime.now(timezone.utc).isoformat(),
             'candles':rows}
    dump_json_atomic(path,payload)
    return rows, False, path


def from_csv(path):
    import csv
    with Path(path).open('r', newline='',encoding='utf-8-sig') as f:
        rows=list(csv.DictReader(f))
    if not rows:raise ValueError('Replay CSV has no events')
    if not set(REPLAY_FIELDS).issubset(rows[0]):
        raise ValueError('Replay CSV missing required canonical fields')
    seen={}
    for r in rows:
        key=r['event_key']; known=int(r['known_ts']); retest=int(r['retest_ts']); virtual=int(r['first_replay_observed_ms'])
        if r['side'] not in ('LONG','SHORT') or key!=f"{r['symbol']}|{r['side']}|{retest}" or known!=retest+60_000 or virtual<known:
            raise ValueError(f'Invalid replay event provenance/times: {key}')
        if r.get('observation_type')!='HISTORICAL_REPLAY_UNVERIFIED':
            raise ValueError('Only HISTORICAL_REPLAY_UNVERIFIED events supported')
        r['payload']=json.loads(r['causal_json'])
        if not isinstance(r['payload'],dict):raise ValueError('Invalid replay causal_json')
        if key in seen:raise ValueError(f'Duplicate Event ID: {key}')
        seen[key]=r
    return seen


def verify_replay_summary(replay_path, events):
    """Do not enrich unknown/partial structural source; no parity claim implied."""
    path = Path(replay_path).parent / 'summary.json'
    if not path.is_file():
        raise FileNotFoundError(f'Phase3A summary.json required alongside replay_events.csv: {path}')
    summary = json.loads(path.read_text(encoding='utf-8'))
    if summary.get('mode') != 'research':
        raise ValueError('Phase3B first cohort must be Phase3A mode=research')
    if summary.get('symbols_failed') != 0 or summary.get('data_gaps') != 0:
        raise ValueError('Phase3A incomplete: symbols_failed or data_gaps nonzero')
    if summary.get('symbols_ok') != summary.get('symbols_total'):
        raise ValueError('Phase3A structural source has incomplete symbol coverage')
    if int(summary.get('replay_events',-1)) != len(events):
        raise ValueError('Phase3A replay_events count differs from attached replay CSV')
    canonical = str(summary.get('canonical_hash') or '')
    if not canonical or any(str(e.get('source_hash')) != canonical for e in events.values()):
        raise ValueError('Replay event scanner fingerprint mismatches Phase3A summary')
    return summary


def universe_from_project(project, explicit=None):
    if explicit:
        p=Path(explicit)
        if not p.is_file():raise FileNotFoundError(p)
        syms=tuple(sorted({s.strip().upper() for s in p.read_text().splitlines() if s.strip()}))
    else:
        # Same initial current-universe source as Continuous V1 scanner.
        import csv
        p=Path(project)/'volume_exhaustion_events.csv'
        if not p.is_file():raise FileNotFoundError(f'Missing scanner universe {p}')
        with p.open(encoding='utf-8-sig',newline='') as f:
            rows=csv.DictReader(f)
            if 'symbol' not in (rows.fieldnames or []):raise ValueError('symbol column missing')
            syms=tuple(sorted({str(r.get('symbol') or '').strip().upper() for r in rows if r.get('symbol')}))
    if not syms:raise ValueError('Market universe empty')
    return syms


def run(project, replay, output, cache, universe, *, min_coverage=.95, delay=.10, offline=False):
    project=Path(project).resolve(); replay=Path(replay).resolve(); output=Path(output).resolve(); cache=Path(cache).resolve()
    if output==cache or output==project or cache==project:raise ValueError('Output/cache must be isolated')
    if output.exists() and any(output.iterdir()):
        raise RuntimeError('Output directory already contains files. Choose a new --output to preserve provenance.')
    events=from_csv(replay)
    source_replay_summary=verify_replay_summary(replay,events)
    sources=load_canonical_source(project)
    ordered=list(events.values())
    known=[int(e['known_ts']) for e in ordered]
    start_known=min(known); end_known=max(known)
    first_4h=closed_boundary(start_known, FOUR_H); last_4h=closed_boundary(end_known, FOUR_H)
    one_start=((start_known-(ROOM_TAIL+6)*HOUR)//HOUR)*HOUR
    one_end=((end_known//HOUR)+2)*HOUR
    four_start=first_4h-(BASELINE+3)*FOUR_H
    four_end=last_4h+FOUR_H
    # Fail fast on final outputs. Cache directory is separate and may be resumed.
    output.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    market={}; rooms={}; errors=[]; sources_by_symbol={}
    print(f'Events={len(events)} target_symbols={len(set(e["symbol"] for e in ordered))} market_universe={len(universe)}',flush=True)
    for i,sym in enumerate(universe,1):
        try:
            bars,cached,path=download_cached(sym,'4h',four_start,four_end,cache,offline=offline,delay=delay)
            market[sym]=bars
            sources_by_symbol[sym]={'candles_4h':len(bars),'cache_sha256':sha_file(path)}
            if i%25==0 or i==len(universe):
                print(f'4h market progress {i}/{len(universe)} (cache={cached})',flush=True)
        except Exception as exc:
            # Individual missing contracts are explicit coverage gaps, not zeros or arbitrary ranks.
            errors.append({'symbol':sym,'timeframe':'4h','error':str(exc)})
            print(f'4h FAILED {i}/{len(universe)} {sym}: {exc}',flush=True)
            if 'rate limit' in str(exc).lower():raise
            market[sym]=[]
    for i,sym in enumerate(sorted(set(e['symbol'] for e in ordered)),1):
        try:
            bars,cached,path=download_cached(sym,'1h',one_start,one_end,cache,offline=offline,delay=delay)
            sources_by_symbol.setdefault(sym,{})['candles_1h']=len(bars)
            sources_by_symbol[sym]['cache_sha256_1h']=sha_file(path)
            for e in ordered:
                if e['symbol']==sym:
                    rooms[e['event_key']]=build_room(e,bars,sources)
            print(f'1h room {i}/{len(set(e["symbol"] for e in ordered))} {sym}: bars={len(bars)} cache={cached}',flush=True)
        except Exception as exc:
            errors.append({'symbol':sym,'timeframe':'1h','error':str(exc)})
            print(f'1h FAILED {sym}: {exc}',flush=True)
            if 'rate limit' in str(exc).lower():raise
            for e in ordered:
                if e['symbol']==sym:
                    rooms[e['event_key']]=(None,'1H_DOWNLOAD_ERROR',{})
    ranks, boundary_diagnostics=rank_snapshots(events,universe,market,sources,min_coverage)
    rows=factors_for_events(events,rooms,ranks,boundary_diagnostics)
    csv_atomic(output/'historical_factors.csv',FACTOR_FIELDS+EXTRA_FIELDS,rows)
    # Persist full cross-sectional ranks for audit/parity, not only event-level values.
    market_fields = ('boundary_open_ms','boundary_close_ms','symbol',
                     'return_pct_4h','quote_volume_4h','median_quote_volume_4h',
                     'relative_volume_4h','return_rank_pct_4h','volume_rank_pct_4h',
                     'market_breadth_4h','valid_universe_size','configured_universe_size','coverage_pct')
    market_rows=[]
    for boundary,(by_symbol,issue) in sorted(ranks.items()):
        if issue:continue
        for sym,record in sorted(by_symbol.items()):
            market_rows.append({'boundary_open_ms':boundary,
                                'boundary_close_ms':boundary+FOUR_H,
                                'symbol':sym,**record})
    csv_atomic(output/'market_snapshots_4h.csv',market_fields,market_rows)
    csv_atomic(output/'download_errors.csv',('symbol','timeframe','error'),errors)
    complete=sum(not r['factor_issue'] for r in rows)
    summary={
        'phase':'3B','mode':'HISTORICAL_CONTEXT_RESEARCH_UNVERIFIED',
        'replay_file':str(replay),'replay_sha256':sha_file(replay),
        'replay_summary_sha256':sha_file(replay.parent/'summary.json'),
        'replay_canonical_scanner_hash':source_replay_summary['canonical_hash'],
        'replay_step_minutes':source_replay_summary['step_minutes'],
        'source_hashes':sources['hashes'],
        'market_math_ast_sha256':sources['math_ast_sha256'],
        'room_math_ast_sha256':sources['room_ast_sha256'],
        'universe_symbols':len(universe),'universe_sha256':hashlib.sha256('\n'.join(universe).encode()).hexdigest(),
        'universe_source':'CURRENT_SCANNER_UNIVERSE_NOT_POINT_IN_TIME',
        'replay_events':len(events),'complete_factor_events':complete,
        'missing_factor_events':len(rows)-complete,
        'factor_issue_counts':dict(Counter(issue for row in rows for issue in row['factor_issue'].split(';') if issue)),
        'data_download_errors':len(errors),'context_boundary_diagnostics':boundary_diagnostics,
        'minimum_coverage_fraction':min_coverage,
        'independent_numeric_parity_verified':False,
        'structural_parity_verified':False,
        'historical_validation_ready':False,
        'research_only':True,
        'note':'Causal closed candles and canonical math, but not verified against archived LIVE same-boundary snapshots. Historical universe selection bias remains. Never treat as LIVE_OBSERVED.',
    }
    dump_json_atomic(output/'summary.json',summary)
    dump_json_atomic(output/'universe.json',{'symbols':universe,'sha256':summary['universe_sha256']})
    dump_json_atomic(output/'download_provenance.json', sources_by_symbol)
    # Text manifest can be passed back as --universe-file in all subsequent runs.
    (output/'universe.txt').write_text('\n'.join(universe)+'\n',encoding='utf-8')
    print(json.dumps({'replay_events':summary['replay_events'],'factor_complete':complete,
                      'factor_missing':summary['missing_factor_events'],
                      'factor_issue_counts':summary['factor_issue_counts'],
                      'download_errors':len(errors),'independent_numeric_parity_verified':False,
                      'output':str(output)},indent=2),flush=True)
    return summary


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--project',required=True,type=Path)
    ap.add_argument('--replay',required=True,type=Path,help='Phase3A replay_events.csv')
    ap.add_argument('--output',required=True,type=Path)
    ap.add_argument('--cache-dir',required=True,type=Path)
    ap.add_argument('--universe-file',type=Path,help='Optional FROZEN full-market universe; never five-symbol subset')
    ap.add_argument('--min-coverage',type=float,default=.95)
    ap.add_argument('--api-delay-seconds',type=float,default=.10)
    ap.add_argument('--offline',action='store_true')
    args=ap.parse_args(argv)
    if not .95<=args.min_coverage<=1:
        ap.error('--min-coverage must be 0.95..1.0')
    if args.api_delay_seconds<0:
        ap.error('--api-delay-seconds must be >= 0')
    universe=universe_from_project(args.project,args.universe_file)
    if len(universe) < 100:
        ap.error('4h ranking requires the FULL scanner market universe (>=100 symbols); do not supply the 5-symbol replay smoke list')
    run(args.project,args.replay,args.output,args.cache_dir,universe,
        min_coverage=args.min_coverage,delay=args.api_delay_seconds,offline=args.offline)
    return 0


if __name__=='__main__':
    sys.exit(main())
