#!/usr/bin/env python3
"""Reaction Lab: independent final 3-cycle Redis/SQLite preflight.

Run from the VPS directory containing the two original Reaction Lab scripts.
Creates ONLY a timestamped test directory beside this script. Never installs
Streamlit bridge, changes old monitors/pickles, or touches production SQLite.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

MINUTE = 60_000


def fp(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def audit_redis(client, symbols, lookback=120):
    """Read-only, symbol-by-symbol closed-candle completeness check."""
    pipe = client.pipeline(transaction=False)
    for s in symbols:
        pipe.lrange(f'market-data:v1:history:{s}:1m', -lookback, -1)
        pipe.lrange(f'market-data:v1:history:{s}:15m', -24, -1)
    responses = pipe.execute()
    report = {'symbols': len(symbols), 'one_minute_present': 0,
              'fifteen_minute_present': 0, 'one_minute_stale': [],
              'fifteen_minute_stale': [], 'one_minute_gap': [],
              'fifteen_minute_gap': [], 'one_minute_duplicates': [],
              'fifteen_minute_duplicates': [], 'latest_1m': {}}
    now = int(time.time() * 1000)
    for i, symbol in enumerate(symbols):
        for tf, raws, step, stale_after in (
            ('one_minute', responses[2 * i], MINUTE, 180_000),
            ('fifteen_minute', responses[2 * i + 1], 15 * MINUTE, 30 * MINUTE),
        ):
            stamps = []
            for raw in raws:
                try:
                    bar = json.loads(raw)
                    ts = int(bar['timestamp'])
                    close_ts = int(bar.get('close_timestamp', ts + step - 1))
                    if close_ts <= now:
                        stamps.append(ts)
                except (ValueError, TypeError, KeyError):
                    continue
            if not stamps:
                continue
            report[tf + '_present'] += 1
            unique = sorted(set(stamps))
            if len(stamps) != len(unique):
                report[tf + '_duplicates'].append(symbol)
            if any(b - a != step for a, b in zip(unique, unique[1:])):
                report[tf + '_gap'].append(symbol)
            if now - (unique[-1] + step) > stale_after:
                report[tf + '_stale'].append(symbol)
            if tf == 'one_minute':
                report['latest_1m'][symbol] = unique[-1]
    return report


def call(cmd, cwd, report, label):
    print('\n>>>', label, flush=True)
    t = time.monotonic()
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=240)
    duration = round(time.monotonic() - t, 2)
    lines = proc.stdout.splitlines()
    records = []
    for line in lines:
        if line.startswith('{'):
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    if not records or proc.returncode:
        print('\n'.join(lines[-20:]), flush=True)
        print(proc.stderr[-6000:], flush=True)
        raise RuntimeError(f'{label}: returncode={proc.returncode}; no valid JSON output' if not records
                           else f'{label}: returncode={proc.returncode}')
    result = records[-1]
    report['runs'].append({'label': label, 'duration_s': duration, 'result': result})
    if proc.stderr.strip():
        print('STDERR:', proc.stderr[-1000:], flush=True)
    print(json.dumps({k: v for k, v in result.items()
                      if k in ('events_seen', 'new_events', 'total_events', 'duration_seconds',
                               'redis_1m_ok', 'redis_15m_ok', 'latest_lag_seconds',
                               'registered', 'progress', 'database')}, ensure_ascii=False), flush=True)
    return result


def snapshot_paths(db, module):
    if not db.is_file():
        return {}
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    try:
        rows = con.execute('SELECT event_key,count,path_blob FROM events').fetchall()
        return {key: module.decode_path(blob) for key, count, blob in rows if count}
    finally:
        con.close()


def audit_integrity(scanner_db, paths_db, mod, old_paths, expected_all_keys):
    errors = []
    warnings = []
    sc = sqlite3.connect(f'file:{scanner_db}?mode=ro', uri=True)
    pc = sqlite3.connect(f'file:{paths_db}?mode=ro', uri=True)
    try:
        scanner = sc.execute('''SELECT event_key,symbol,side,retest_ts,known_ts,
                                  first_seen_ms,observation_type,source_hash FROM events''').fetchall()
        journal = {}
        for key, sym, side, ts, known, first, obs, src_hash in scanner:
            journal[key] = (sym, side, ts)
            if key != f'{sym}|{side}|{ts}' or known != ts + MINUTE or first < known:
                errors.append(f'Invalid scanner identity/causality: {key}')
            if obs not in ('RECOVERED_WINDOW', 'LIVE_OBSERVED'):
                errors.append(f'Unknown scanner observation type: {key}')
        observation_types = {a: n for a, n in sc.execute(
            'SELECT observation_type,COUNT(*) FROM events GROUP BY observation_type')}
        distinct_hashes = sc.execute('SELECT COUNT(DISTINCT source_hash) FROM events').fetchone()[0]
        if distinct_hashes > 1:
            errors.append('Multiple canonical source hashes mixed in scanner journal')
        cycles = sc.execute('SELECT symbols_requested,symbols_with_1m,symbols_with_15m,slow_cycle,error FROM cycles ORDER BY cycle_id').fetchall()
        if len(cycles) != 3:
            errors.append(f'Expected 3 scanner cycles, got {len(cycles)}')
        for no, (requested, one, fifteen, slow, err) in enumerate(cycles, 1):
            if (requested, one, fifteen) != (290, 290, 290) or slow or err:
                errors.append(f'Incomplete/error scanner cycle {no}: {(requested, one, fifteen, slow, err)}')
        rows = pc.execute('''SELECT event_key,symbol,side,retest_ts,entry_ts,
                             state,count,last_ts,path_blob FROM events''').fetchall()
        collector_keys = {r[0] for r in rows}
        if collector_keys != set(expected_all_keys):
            errors.append(f'Combined-source coverage mismatch: missing={len(set(expected_all_keys)-collector_keys)} extra={len(collector_keys-set(expected_all_keys))}')
        if not set(journal).issubset(collector_keys):
            errors.append(f'{len(set(journal)-collector_keys)} new scanner REACTIONs missing from collector')
        states = {}
        total = 0
        grown = unchanged = 0
        for key, symbol, side, retest, entry, state, count, last, blob in rows:
            states[state] = states.get(state, 0) + 1
            if (symbol, side, retest) != journal.get(key):
                errors.append(f'Collector identity conflict: {key}')
            if entry != retest + MINUTE:
                errors.append(f'Wrong entry timestamp: {key}')
            try:
                path = mod.decode_path(blob)
            except Exception as exc:
                errors.append(f'Invalid OHLC compressed path {key}: {exc}')
                continue
            total += len(path)
            if count != len(path) or count > 360 or count < 0:
                errors.append(f'Invalid path count: {key} {count}/{len(path)}')
            if (count and (not path or last != path[-1][0])) or (not count and last is not None):
                errors.append(f'Invalid last_ts: {key}')
            if state == 'COMPLETE' and count != 360:
                errors.append(f'COMPLETE != 360 bars: {key}')
            if state in ('MISSING_ENTRY', 'GAP_WAITING', 'GAP_EXPIRED') and key in journal:
                errors.append(f'Gap/missing entry in scanner cohort: {key} ({state})')
            if state == 'STALE_BEFORE_COLLECTOR' and count:
                errors.append(f'STALE event unexpectedly has bars: {key}')
            for offset, (ts, o, h, low, close) in enumerate(path):
                if ts != entry + offset * MINUTE:
                    errors.append(f'Noncontiguous bar: {key} offset={offset}')
                    break
                if not (low > 0 and h >= max(o, close, low) and low <= min(o, close, h)):
                    errors.append(f'Invalid OHLC: {key} offset={offset}')
                    break
            before = old_paths.get(key)
            if before is not None:
                if path[:len(before)] != before:
                    errors.append(f'Path rewrote/lost prior bars: {key}')
                elif len(path) > len(before):
                    grown += 1
                else:
                    unchanged += 1
        if states.get('STALE_BEFORE_COLLECTOR'):
            warnings.append(f'{states["STALE_BEFORE_COLLECTOR"]} historical events already outside the 400m Redis window (expected with old V1/V2 monitors)')
        return {'scanner_events': len(journal), 'observation_types': observation_types,
                'cycles': len(cycles),
                'collector_events': len(rows), 'states': states, 'saved_bars': total,
                'paths_grown_since_first_cycle': grown, 'paths_unchanged': unchanged,
                'errors': errors[:30], 'error_count': len(errors), 'warnings': warnings}
    finally:
        sc.close()
        pc.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--project', type=Path, default=Path('/home/multiservices-1m'))
    ap.add_argument('--symbols-file', type=Path, default=None)
    args = ap.parse_args()
    project = args.project.resolve()
    frozen = (args.symbols_file or project / 'refs/v2_oct_20261011/oct_dashboard_symbols_290.txt').resolve()
    code = Path(__file__).resolve().parent
    for filename in ('reaction_lab_continuous_scanner.py', 'reaction_lab_auto_paths.py'):
        if not (code / filename).is_file():
            ap.error(f'Missing {filename} next to this preflight script: {code}')
    if not frozen.is_file() or not (project / 'dashboard/app.py').is_file():
        ap.error('Missing frozen 290-symbol list or dashboard/app.py')
    symbols = sorted(set(x.strip().upper() for x in frozen.read_text().splitlines() if x.strip()))
    if len(symbols) != 290:
        ap.error(f'Expected exactly 290 frozen symbols; found {len(symbols)}')
    sys.path.insert(0, str(code))
    import reaction_lab_auto_paths as paths
    import reaction_lab_continuous_scanner as scanner
    default_symbols = set(scanner.get_symbols(project))
    work = code / ('final_preflight_' + datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f'))
    isolated = work / 'isolated_project'
    scanner_db = isolated / 'reports/reaction_lab/scanner_events_v1.sqlite3'
    paths_db = isolated / 'reports/reaction_lab/auto_paths_v1.sqlite3'
    scanner_db.parent.mkdir(parents=True, exist_ok=False)
    if (project / '.env').exists():
        (isolated / '.env').symlink_to(project / '.env')
    # Read the 4 real legacy monitors via read-only symlinks; the full collector
    # is exercised without copying or modifying those CSVs.
    for source, relative in paths.SOURCE_FILES.items():
        if source == 'continuous_scan':
            continue
        real = project / relative
        if real.is_file():
            alias = isolated / relative
            alias.parent.mkdir(parents=True, exist_ok=True)
            alias.symlink_to(real)
    result = {'workdir': str(work), 'project': str(project), 'runs': [],
              'universe': {'frozen': len(symbols), 'default': len(default_symbols),
                           'missing_in_default': sorted(set(symbols) - default_symbols),
                           'extra_in_default': sorted(default_symbols - set(symbols))},
              'errors': [], 'warnings': []}
    original_hash = fp(project / 'dashboard/app.py')
    print('FINAL REACTION LAB PREFLIGHT (three cycles; ~3 minutes)', flush=True)
    print('Test files ONLY in:', work, flush=True)
    print('Frozen / default symbols:', 290, '/', len(default_symbols), flush=True)
    try:
        import redis
        try:
            from dotenv import load_dotenv
            load_dotenv(project / '.env')
        except ImportError:
            pass
        rc = redis.Redis(host=os.getenv('REDIS_HOST', '127.0.0.1'),
                         port=int(os.getenv('REDIS_PORT', '6379')),
                         db=int(os.getenv('REDIS_DB', '0')), socket_timeout=12)
        rc.ping()
        result['before_redis'] = audit_redis(rc, symbols)
        print('Initial per-symbol Redis snapshot:',
              {k: len(v) if isinstance(v, list) else v for k, v in result['before_redis'].items()
               if k != 'latest_1m'}, flush=True)
        old_paths = {}
        scan_argv = [sys.executable, '-u', str(code / 'reaction_lab_continuous_scanner.py'),
                     '--project', str(project), '--symbols-file', str(frozen),
                     '--db', str(scanner_db), '--scan-once']
        path_argv = [sys.executable, '-u', str(code / 'reaction_lab_auto_paths.py'),
                     '--project', str(isolated), '--db', str(paths_db), '--run-once']
        last_started = None
        for i in range(3):
            if last_started is not None:
                waiting = 60 - (time.monotonic() - last_started)
                if waiting > 0:
                    print(f'Waiting {waiting:.1f}s to keep ~60-second cycle start cadence...', flush=True)
                    time.sleep(waiting)
            last_started = time.monotonic()
            scan = call(scan_argv, code, result, f'Scanner cycle {i + 1}/3')
            if scan.get('redis_1m_ok') != 290 or scan.get('redis_15m_ok') != 290:
                result['errors'].append(f'Scan {i + 1}: incomplete Redis read')
            if scan.get('duration_seconds', 999) >= 60:
                result['errors'].append(f'Scan {i + 1}: cycle took >=60s')
            collect = call(path_argv, code, result, f'Collector process restart {i + 1}/3')
            if i == 0:
                old_paths = snapshot_paths(paths_db, paths)
            if collect.get('progress', {}).get('unavailable_redis', 0):
                result['errors'].append(f'Collector {i+1}: Redis unavailable')
        result['after_redis'] = audit_redis(rc, symbols)
        before = result['before_redis']['latest_1m']
        after = result['after_redis']['latest_1m']
        no_progress = [s for s in symbols if s in before and s in after and after[s] <= before[s]]
        result['symbols_without_1m_progress'] = no_progress
        all_events, memberships, source_details = paths.read_sources(isolated)
        result['expected_all_sources'] = {
            'unique_events': len(all_events),
            'sources': {name: details['events'] for name, details in source_details.items()},
            'cross_profile_duplicates': sum(len(m) - 1 for m in memberships.values()),
        }
        result['integrity'] = audit_integrity(scanner_db, paths_db, paths, old_paths, set(all_events))
        result['errors'] += result['integrity']['errors']
        if result['integrity']['error_count'] > len(result['integrity']['errors']):
            result['errors'].append('Further path errors omitted from display')
        if result['integrity'].get('observation_types', {}).get('LIVE_OBSERVED', 0) == 0:
            result['warnings'].append(
                'No LIVE_OBSERVED events were detected in this short pilot; prospective signal recall remains unverified')
        for label in ('before_redis', 'after_redis'):
            snap = result[label]
            if snap['one_minute_present'] != 290 or snap['fifteen_minute_present'] != 290:
                result['errors'].append(f'{label}: missing Redis histories')
            for key in ('one_minute_gap', 'fifteen_minute_gap', 'one_minute_duplicates',
                        'fifteen_minute_duplicates', 'one_minute_stale', 'fifteen_minute_stale'):
                if snap[key]:
                    result['errors'].append(f'{label}: {len(snap[key])} symbols with {key} (e.g. {snap[key][:5]})')
        if no_progress:
            result['errors'].append(f'{len(no_progress)} symbols did not advance their 1m closes: {no_progress[:10]}')
        if result['universe']['missing_in_default'] or result['universe']['extra_in_default']:
            result['errors'].append('Default scanner universe differs from frozen 290-symbol universe')
    except Exception as exc:
        result['errors'].append(f'Aborted with error: {type(exc).__name__}: {exc}')
    finally:
        result['production_app_unchanged'] = fp(project / 'dashboard/app.py') == original_hash
        if not result['production_app_unchanged']:
            result['errors'].append('Production dashboard/app.py hash changed during preflight; investigate writer')
        result['verdict'] = 'PASS' if not result['errors'] else 'REVIEW_REQUIRED'
        # Include details for audit, no credentials, no user data from CSV rows.
        (work / 'report.json').write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
        print('\n================== FINAL RESULT ==================', flush=True)
        print('Verdict:', result['verdict'], flush=True)
        print('Production app unchanged:', result['production_app_unchanged'], flush=True)
        if 'expected_all_sources' in result:
            print('All V1/V2/continuous sources:', result['expected_all_sources'], flush=True)
        if 'integrity' in result:
            print('Path/Journal integrity:', {k: v for k, v in result['integrity'].items()
                                               if k not in ('errors', 'warnings')}, flush=True)
        print('Symbols without 1m progress:', len(result.get('symbols_without_1m_progress', [])), flush=True)
        print('Warnings:', len(result['warnings']), flush=True)
        for item in result['warnings'][:5]:
            print('  !', item, flush=True)
        print('Errors:', len(result['errors']), flush=True)
        for item in result['errors'][:15]:
            print('  -', item, flush=True)
        print('Full report:', work / 'report.json', flush=True)
    return 0 if result['verdict'] == 'PASS' else 2

if __name__ == '__main__':
    sys.exit(main())
