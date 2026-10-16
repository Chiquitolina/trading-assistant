#!/usr/bin/env python3
"""Read-only historical reconstruction of Continuous V1 REACTIONs.

Uses the real Continuous V1 AST-compiled scanner. The only state the replay
owns is its isolated HTTP download cache and output folder. No Redis writes,
no calls to the live scanner loop, no PM2 interaction, no production DB writes.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ONE_MIN = 60_000
FIFTEEN_MIN = 15 * ONE_MIN
ARG = timezone(timedelta(hours=-3))
API_ROOT = "https://fapi.binance.com"
COMPARE_NUMERIC = (
    'pivot_timestamp', 'actionable_timestamp', 'departure_timestamp',
    'retest_timestamp', 'swing_price', 'entry_price', 'retest_close',
    'max_departure_pct', 'retest_price', 'retest_distance_pct',
)
OUTPUT_FIELDS = ('event_key', 'symbol', 'side', 'retest_ts', 'known_ts',
                 'status', 'observation_type', 'source_hash', 'causal_json')


def iso_to_ms(value):
    """Date/time must be TZ-aware; do not silently treat ARG timestamps as UTC."""
    value = value.replace('Z', '+00:00')
    when = datetime.fromisoformat(value)
    if when.tzinfo is None:
        raise ValueError('Supply a timezone offset, e.g. 2026-10-14T12:40:00-03:00')
    return int(when.timestamp() * 1000)


def format_arg(ms):
    return datetime.fromtimestamp(ms / 1000, ARG).isoformat(timespec='seconds')


def checkpoints(start_ms, end_ms, step_min):
    """Scan enough past the requested end to see the last CLOSED 1m candle."""
    step = int(step_min) * ONE_MIN
    t = ((start_ms + step - 1) // step) * step
    stop = ((end_ms + step - 1) // step) * step
    while t <= stop:
        yield t
        t += step


def csv_write(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic snapshot: a crash must not leave a truncated CSV for --resume.
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


def cache_name(symbol, timeframe, start_ms, end_ms):
    label = f'{symbol}:{timeframe}:{start_ms}:{end_ms}:futures_klines_v1'
    return hashlib.sha256(label.encode()).hexdigest()[:18] + '.json'


def _request_klines(symbol, timeframe, start_ms, end_ms, retries=4):
    params = urllib.parse.urlencode({
        'symbol': symbol, 'interval': timeframe,
        'startTime': start_ms, 'endTime': end_ms,
        'limit': 1000,
    })
    url = f'{API_ROOT}/fapi/v1/klines?{params}'
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'continuous-v1-replay/1.0'})
            with urllib.request.urlopen(req, timeout=35) as response:
                data = json.loads(response.read())
            if not isinstance(data, list):
                raise RuntimeError(f'Binance returned non-list for {symbol} {timeframe}: {str(data)[:150]}')
            return data
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            # On explicit rate-limits, stop rather than hammering API.
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (418, 429):
                raise RuntimeError(f'Binance rate limited ({exc.code}); stop and resume later') from exc
            if attempt == retries - 1:
                raise RuntimeError(f'Binance download failed: {symbol} {timeframe}: {exc}') from exc
            time.sleep(min(12, 2 ** attempt))
    raise AssertionError('unreachable')


def fetch_klines(symbol, timeframe, start_ms, end_ms, cache_dir, delay=0.09):
    """Fetch native futures candles, store atomically, reject duplicate/conflicting bars."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / cache_name(symbol, timeframe, start_ms, end_ms)
    if cache_file.exists():
        payload = json.loads(cache_file.read_text())
        if payload.get('symbol') != symbol or payload.get('timeframe') != timeframe or payload.get('start_ms') != start_ms or payload.get('end_ms') != end_ms:
            raise RuntimeError(f'Cache provenance mismatch: {cache_file}')
        return payload['candles'], True
    tf_ms = ONE_MIN if timeframe == '1m' else FIFTEEN_MIN
    cursor = (start_ms // tf_ms) * tf_ms
    found = {}
    while cursor < end_ms:
        data = _request_klines(symbol, timeframe, cursor, end_ms - 1)
        if not data:
            break
        last_ts = None
        for row in data:
            ts = int(row[0])
            if not start_ms <= ts < end_ms:
                continue
            val = {
                'timestamp': ts,
                'open': float(row[1]), 'high': float(row[2]),
                'low': float(row[3]), 'close': float(row[4]),
                'volume': float(row[5]),
            }
            if not all(isinstance(val[k], (float, int)) and __import__('math').isfinite(val[k]) for k in ('open','high','low','close','volume')):
                raise RuntimeError(f'Non-finite OHLC {symbol} {timeframe} {ts}')
            if val['low'] > min(val['open'],val['close']) or val['high'] < max(val['open'],val['close']) or val['high'] < val['low']:
                raise RuntimeError(f'Invalid OHLC {symbol} {timeframe} {ts}')
            if ts in found and found[ts] != val:
                raise RuntimeError(f'Conflicting duplicate candle {symbol} {timeframe} {ts}')
            found[ts] = val
            last_ts = ts
        # Some Binance responses include earlier bars than our cutoff, so use last raw timestamp.
        raw_last = int(data[-1][0])
        if raw_last < cursor:
            raise RuntimeError(f'Binance cursor did not advance {symbol} {timeframe}')
        cursor = raw_last + tf_ms
        if delay:
            time.sleep(delay)
        if len(data) < 1000:
            break
    candles = sorted(found.values(), key=lambda c: c['timestamp'])
    payload = {'symbol':symbol, 'timeframe':timeframe, 'start_ms':start_ms,
               'end_ms':end_ms, 'origin':'binance_futures_native_klines', 'candles':candles}
    tmp = cache_file.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(payload, separators=(',', ':'), allow_nan=False))
    os.replace(tmp, cache_file)
    return candles, False



def progress_signature(*, mode, start_ms, end_ms, step_minutes, project, journal, cache_dir, symbols, fingerprint, collision_policy="native"):
    """Freeze replay identity so --resume cannot silently mix incompatible runs."""
    payload = {
        'replay_progress_version': 2,
        'mode': mode,
        'start_ms': start_ms, 'end_ms': end_ms,
        'step_minutes': step_minutes,
        'collision_policy': collision_policy,
        'project': str(project), 'journal': str(journal),
        'cache_dir': str(cache_dir),
        'symbols': list(symbols),
        'canonical_hash': fingerprint,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def resume_or_initialize(output, *, resume, signature, symbols, start_ms, end_ms):
    """Load only completed symbols and their saved rows; never infer success from caches.

    Writes to output happen in the order: event CSV, diagnostic CSVs, progress.
    If interrupted between these writes, any extra event rows not marked complete
    are intentionally discarded and that symbol is scanned again.
    """
    manifest = output / 'progress.json'
    events_file = output / 'replay_events.csv'
    gaps_file = output / 'history_gaps.csv'
    if not resume:
        if manifest.exists() or events_file.exists():
            raise RuntimeError(
                f'Output directory already contains a replay: {output}. '
                'Use --resume for the SAME inputs, or choose a NEW --output directory.'
            )
        return set(), {}, []
    if not manifest.is_file() or not events_file.is_file() or not gaps_file.is_file():
        raise RuntimeError('Cannot --resume: progress.json, replay_events.csv and history_gaps.csv are required')
    progress = json.loads(manifest.read_text(encoding='utf-8'))
    if progress.get('signature') != signature or progress.get('version') != 2:
        raise RuntimeError('Cannot --resume: inputs, symbol universe, or canonical hash changed; choose a new output')
    completed_list = progress.get('completed')
    if not isinstance(completed_list, list) or len(completed_list) != len(set(completed_list)):
        raise RuntimeError('Cannot --resume: invalid completed list')
    completed = set(completed_list)
    if not completed.issubset(set(symbols)):
        raise RuntimeError('Cannot --resume: completed symbol not in current universe')
    collected = {}
    with events_file.open(encoding='utf-8', newline='') as f:
        for r in csv.DictReader(f):
            # These could be from the last symbol persisted before a crash
            # but before its progress.json was updated. Rescan them.
            if r['symbol'] not in completed:
                continue
            key = r['event_key']
            ts = int(r['retest_ts'])
            if not start_ms <= ts < end_ms or key != f"{r['symbol']}|{r['side']}|{ts}":
                raise RuntimeError('Cannot --resume: corrupt saved Event ID')
            if key in collected:
                raise RuntimeError('Cannot --resume: duplicate saved Event ID')
            collected[key] = {
                'event_key': key, 'symbol': r['symbol'], 'side': r['side'],
                'retest_ts': ts, 'known_ts': int(r['known_ts']),
                'first_replay_observed_ms': int(r['first_replay_observed_ms']),
                'payload': json.loads(r['causal_json']),
            }
    with gaps_file.open(encoding='utf-8', newline='') as f:
        saved_gaps = [r for r in csv.DictReader(f) if r['symbol'] in completed]
    print(f'RESUME: {len(completed)} completed symbols, {len(collected)} saved events', flush=True)
    return completed, collected, saved_gaps


def write_progress(path, signature, completed):
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps({
        'version': 2, 'signature': signature, 'completed': sorted(completed),
    }, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def contiguous_diagnostics(candles, tf_ms, start_ms, end_ms):
    times = [c['timestamp'] for c in candles if start_ms <= c['timestamp'] < end_ms]
    gaps = [(a, b, round((b-a)/tf_ms)-1)
            for a,b in zip(times,times[1:]) if b != a + tf_ms]
    return gaps


def load_journal(db_path, start_ms, end_ms, symbols):
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    allowed = set(symbols)
    con = sqlite3.connect(f'file:{db_path.resolve()}?mode=ro', uri=True, timeout=10)
    try:
        rows = con.execute('''SELECT event_key,symbol,side,retest_ts,known_ts,
                           observation_type,source_hash,causal_json
                           FROM events WHERE retest_ts >= ? AND retest_ts < ?''',
                           (start_ms, end_ms)).fetchall()
    finally:
        con.close()
    return {r[0]:dict(zip(('event_key','symbol','side','retest_ts','known_ts',
                           'observation_type','source_hash','causal_json'),r))
            for r in rows if r[1] in allowed}


def compare_events(replayed, existing, fingerprint):
    rows = []
    counts = {'MATCHED':0, 'REPLAY_ONLY':0, 'JOURNAL_ONLY':0, 'NUMERIC_DIFFERENCES':0}
    for key in sorted(set(replayed) | set(existing)):
        a = replayed.get(key)
        b = existing.get(key)
        status = 'MATCHED' if a and b else ('REPLAY_ONLY' if a else 'JOURNAL_ONLY')
        counts[status] += 1
        diff = []
        source_match = None
        if a and b:
            source_match = str(b['source_hash']) == str(fingerprint)
            try:
                payload = json.loads(b['causal_json'])
            except (ValueError, TypeError):
                payload = {}
                diff.append('invalid_journal_json')
            event = a['payload']
            for field in COMPARE_NUMERIC:
                v1, v2 = event.get(field), payload.get(field)
                if v1 is None and v2 is None:
                    continue
                try:
                    left = float(v1)
                    right = float(v2)
                    tol = 1e-8 * max(1,abs(left),abs(right))
                    if not __import__('math').isclose(left,right, rel_tol=1e-8, abs_tol=tol):
                        diff.append(field)
                except (ValueError,TypeError,OverflowError):
                    if v1 != v2:
                        diff.append(field)
            if diff:
                counts['NUMERIC_DIFFERENCES'] += 1
        rows.append(dict(event_key=key,comparison=status,
                         retest_ts=a['retest_ts'] if a else b['retest_ts'],
                         symbol=a['symbol'] if a else b['symbol'],
                         side=a['side'] if a else b['side'],
                         source_hash_equal=source_match,
                         numeric_differences=';'.join(diff)))
    counts['FULL_ID_PARITY'] = counts['REPLAY_ONLY']==0 and counts['JOURNAL_ONLY']==0
    counts['FULL_NUMERIC_PARITY'] = counts['FULL_ID_PARITY'] and counts['NUMERIC_DIFFERENCES']==0
    return rows, counts


def run_symbol(sym, scan, normalize_reactions, ticks, candles_1m, candles_15m,
               start_ms, end_ms, step_min, collision_policy="native"):
    """Feed only CLOSED candles as of each virtual clock tick, capped at live 400."""
    import numpy as np
    import pandas as pd
    def mk_df(candles):
        return pd.DataFrame(candles, columns=('timestamp','open','high','low','close','volume'))
    df1 = mk_df(candles_1m)
    df15 = mk_df(candles_15m)
    t1 = np.array(df1['timestamp'], dtype=np.int64) if len(df1) else np.array([],dtype=np.int64)
    t15 = np.array(df15['timestamp'],dtype=np.int64) if len(df15) else np.array([],dtype=np.int64)
    known = {}
    # Each window is exactly the live scanner's capped 400 closed candles.
    for tick in ticks:
        e1 = int(np.searchsorted(t1, tick - ONE_MIN, side='right'))
        e15 = int(np.searchsorted(t15, tick - FIFTEEN_MIN, side='right'))
        if e1 == 0 or e15 == 0:
            continue
        snap1 = df1.iloc[max(0,e1-400):e1]
        snap15 = df15.iloc[max(0,e15-400):e15]
        if len(snap1) < 40 or len(snap15) < 7:
            continue
        def loader(symbol, timeframe, limit):
            if symbol != sym:
                raise RuntimeError('Replay may only read the active symbol')
            if timeframe == '1m':
                return snap1.tail(min(400,int(limit)))
            if timeframe == '15m':
                return snap15.tail(min(400,int(limit)))
            raise RuntimeError(f'Unsupported timeframe {timeframe}')
        scan.__globals__['load_volume_exhaustion_research_candles'] = loader
        frame = scan(symbols=(sym,), swing_timeframes=('15m',),
                     swing_detector_items=(('15m','3x3'),),
                     min_swing_prominence_pct=0.0, retest_tolerance_pct=0.10,
                     min_departure_pct=0.20, max_age_minutes=360,
                     max_retest_age_minutes=4320)
        if collision_policy not in ('native', 'latest-pivot'):
            raise ValueError(f'Unknown collision policy: {collision_policy}')
        # Empty frames are perfectly normal when a checkpoint has no reactions.
        # Keep the policy valid, and only sort when candidate rows exist.
        if collision_policy == 'latest-pivot' and frame is not None and not frame.empty:
            # EXPERIMENT: only change which candidate wins when multiple
            # confirmed swings share the same symbol|side|retest_ts.
            # Do not modify detection rules, candle selection, or production.
            required = {'retest_timestamp', 'pivot_timestamp'}
            if not required.issubset(frame.columns):
                raise RuntimeError('Canonical scanner omitted retest/pivot timestamps')
            frame = frame.sort_values(
                ['retest_timestamp', 'pivot_timestamp'],
                ascending=[False, False], kind='mergesort', na_position='last'
            )
        for key, payload in normalize_reactions(frame, tick).items():
            ts = int(payload['retest_timestamp'])
            if not start_ms <= ts < end_ms:
                continue
            if key not in known:
                known[key] = {'event_key':key, 'symbol':sym,
                              'side':payload['signal'], 'retest_ts':ts,
                              'known_ts':ts+ONE_MIN, 'payload':payload,
                              'first_replay_observed_ms':tick}
    return known


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--project', type=Path, default=Path('/home/multiservices-1m'))
    ap.add_argument('--start', required=True, help='ISO date with offset, inclusive retest open')
    ap.add_argument('--end', required=True, help='ISO date with offset, exclusive retest open')
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--mode', choices=('parity','gap'), required=True)
    ap.add_argument('--step-minutes', type=int, default=15,
                    help='15 default; optional 1 for highest temporal fidelity')
    ap.add_argument('--collision-policy', choices=('native', 'latest-pivot'),
                    default='native',
                    help='Experimental retest collision tie-break (replay ONLY)')
    ap.add_argument('--symbols-file', type=Path,
                    help='Optional subset of symbols; otherwise scanner canonical universe')
    ap.add_argument('--max-symbols', type=int, default=0,
                    help='For smoke tests ONLY; zero means all symbols')
    ap.add_argument('--api-delay-seconds', type=float, default=0.09)
    ap.add_argument('--cache-dir', type=Path,
                    help='Downloader-owned cache; default OUTPUT/download_cache')
    ap.add_argument('--journal', type=Path,
                    help='Original events SQLite (read-only)')
    ap.add_argument('--parity-summary', type=Path,
                    help='Required for gap mode: successful full-universe parity summary.json')
    ap.add_argument('--resume', action='store_true',
                    help='Resume validated per-symbol checkpoint from this exact run')
    ap.add_argument('--allow-incomplete', action='store_true',
                    help='Do not abort on incomplete native 1m/15m history (report flagged)')
    args = ap.parse_args(argv)
    start_ms, end_ms = iso_to_ms(args.start), iso_to_ms(args.end)
    if start_ms >= end_ms:
        ap.error('start must precede end')
    if args.step_minutes < 1 or args.step_minutes > 30:
        ap.error('step-minutes must be 1..30; 400 bar buffer minus max 360m age')
    project = args.project.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    cache_dir = (args.cache_dir or output/'download_cache').resolve()
    sys.path.insert(0, str(project))
    import reaction_lab_continuous_scanner as live
    symbols = (tuple(sorted({x.strip().upper() for x in args.symbols_file.read_text().splitlines() if x.strip()}))
               if args.symbols_file else live.get_symbols(project))
    if args.max_symbols:
        symbols = symbols[:args.max_symbols]
    scan, fingerprint = live.compile_canonical_scanner(project/'dashboard/app.py', lambda **kw:None)
    journal = (args.journal or project/'reports/reaction_lab/scanner_events_v1.sqlite3').resolve()
    existing = load_journal(journal, start_ms, end_ms, symbols)
    if args.mode == 'gap':
        if not args.parity_summary or not args.parity_summary.is_file():
            ap.error('gap mode requires --parity-summary from a completed parity run')
        gate = json.loads(args.parity_summary.read_text())
        trusted = (gate.get('mode') == 'parity' and
                   gate.get('canonical_hash') == fingerprint and
                   gate.get('step_minutes') == args.step_minutes and
                   gate.get('collision_policy') == args.collision_policy and
                   gate.get('symbols_failed') == 0 and
                   gate.get('data_gaps') == 0 and
                   gate.get('symbols_total') == len(symbols) and
                   gate.get('journal_hash_equal') is True and
                   gate.get('journal_events',0)>0 and
                   gate.get('metrics',{}).get('FULL_NUMERIC_PARITY') is True)
        if not trusted:
            ap.error('Parity gate failed. Do not proceed with historical gap reconstruction')
    ticks = list(checkpoints(start_ms, end_ms, args.step_minutes))
    if not ticks:
        ap.error('No replay checkpoints')
    # Include 400 bars prior to earliest checkpoint; align starts on candle boundaries.
    one_start = ((start_ms - 430*ONE_MIN)//ONE_MIN)*ONE_MIN
    fifteen_start = ((start_ms - 410*FIFTEEN_MIN)//FIFTEEN_MIN)*FIFTEEN_MIN
    fetch_end = ticks[-1] + FIFTEEN_MIN
    print(f'MODE={args.mode} collision_policy={args.collision_policy} symbols={len(symbols)} checkpoints/symbol={len(ticks)}')
    print(f'WINDOW={format_arg(start_ms)} to {format_arg(end_ms)} fingerprint={fingerprint}')
    print(f'PRODUCTION JOURNAL: read-only {journal}',flush=True)
    signature = progress_signature(
        mode=args.mode, start_ms=start_ms, end_ms=end_ms,
        step_minutes=args.step_minutes, project=project, journal=journal,
        cache_dir=cache_dir, symbols=symbols, fingerprint=fingerprint,
        collision_policy=args.collision_policy,
    )
    checkpoint_file = output / 'progress.json'
    completed, collected, gaps_report = resume_or_initialize(
        output, resume=args.resume, signature=signature, symbols=symbols,
        start_ms=start_ms, end_ms=end_ms,
    )
    errors = []
    # Save results incrementally so a long batch is not lost upon interruption.
    for idx, sym in enumerate(symbols, 1):
        if sym in completed:
            print(f'[{idx}/{len(symbols)}] SKIP completed {sym}',flush=True)
            continue
        try:
            b1, cached1 = fetch_klines(sym,'1m',one_start,fetch_end,cache_dir, args.api_delay_seconds)
            b15,cached15= fetch_klines(sym,'15m',fifteen_start,fetch_end,cache_dir,args.api_delay_seconds)
            gaps1 = contiguous_diagnostics(b1,ONE_MIN,start_ms,end_ms)
            gaps15 = contiguous_diagnostics(b15,FIFTEEN_MIN,start_ms,end_ms)
            if not b1 or not b15:
                raise RuntimeError(f'Empty OHLC history: 1m={len(b1)} 15m={len(b15)}')
            if gaps1 or gaps15:
                gaps_report.append({'symbol':sym,'one_min_gap_count':len(gaps1),
                                    'fifteen_min_gap_count':len(gaps15),
                                    'one_min_first_gap':str(gaps1[:2]),
                                    'fifteen_min_first_gap':str(gaps15[:2])})
                if not args.allow_incomplete:
                    raise RuntimeError(f'Historical gaps: 1m={len(gaps1)} 15m={len(gaps15)}')
            found = run_symbol(sym,scan,live.normalize_reactions,ticks,b1,b15,
                               start_ms,end_ms,args.step_minutes,args.collision_policy)
            collected.update(found)
            completed.add(sym)
            print(f'[{idx}/{len(symbols)}] {sym}: events={len(found)} 1m={len(b1)} 15m={len(b15)} cache={cached1}/{cached15}',flush=True)
        except Exception as exc:
            errors.append({'symbol':sym,'error':str(exc)})
            print(f'[{idx}/{len(symbols)}] FAILED {sym}: {exc}',flush=True)
        finally:
            replay_rows=[{'event_key':v['event_key'],'symbol':v['symbol'],'side':v['side'],
                          'retest_ts':v['retest_ts'],'known_ts':v['known_ts'],
                          'first_replay_observed_ms':v['first_replay_observed_ms'],
                          'observation_type':'HISTORICAL_REPLAY_UNVERIFIED',
                          'source_hash':fingerprint,
                          'causal_json':json.dumps(v['payload'],allow_nan=False)}
                         for v in collected.values()]
            csv_write(output/'replay_events.csv',
                      ('event_key','symbol','side','retest_ts','known_ts',
                       'first_replay_observed_ms','observation_type','source_hash','causal_json'),
                      replay_rows)
            csv_write(output/'download_errors.csv',('symbol','error'),errors)
            csv_write(output/'history_gaps.csv',('symbol','one_min_gap_count','fifteen_min_gap_count','one_min_first_gap','fifteen_min_first_gap'),gaps_report)
            write_progress(checkpoint_file, signature, completed)
    comparisons, metrics = compare_events(collected, existing, fingerprint)
    csv_write(output/'comparison.csv',('event_key','comparison','retest_ts','symbol','side','source_hash_equal','numeric_differences'),comparisons)
    missing=[x for x in comparisons if x['comparison']=='REPLAY_ONLY']
    csv_write(output/'not_in_journal.csv',('event_key','comparison','retest_ts','symbol','side','source_hash_equal','numeric_differences'),missing)
    source_hashes = sorted({str(x['source_hash']) for x in existing.values()})
    summary={'mode':args.mode,'collision_policy':args.collision_policy,
             'start_arg':args.start,'end_arg':args.end,
             'start_ms':start_ms,'end_ms':end_ms,'canonical_hash':fingerprint,
             'journal_hashes':source_hashes,'journal_hash_equal':all(x==fingerprint for x in source_hashes),
             'symbols_total':len(symbols),'symbols_ok':len(symbols)-len(errors),
             'symbols_failed':len(errors),'step_minutes':args.step_minutes,
             'replay_events':len(collected),'journal_events':len(existing),
             'metrics':metrics,'data_gaps':len(gaps_report),'note':'NOT FORWARD; no context reconstructed; read-only journal'}
    (output/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False))
    print(json.dumps(summary,indent=2,ensure_ascii=False),flush=True)
    if errors or gaps_report:
        print('PARTIAL DATA - do not infer missing event counts',flush=True)
        return 2
    if args.mode=='parity' and (not existing or not metrics['FULL_NUMERIC_PARITY'] or not summary['journal_hash_equal']):
        print('PARITY NOT ESTABLISHED: do not trust backfill counts yet',flush=True)
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main())
