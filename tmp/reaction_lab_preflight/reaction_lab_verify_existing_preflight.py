#!/usr/bin/env python3
"""Read-only verification of EXISTING Reaction Lab final-preflight SQLite databases.

Fixes the earlier preflight audit's mistaken comparison of ALL V1/V2 events
against the scanner-only event dictionary. Does not rerun scans or collectors.

Run beside reaction_lab_auto_paths.py:
  python3 reaction_lab_verify_existing_preflight.py --report /path/to/final_preflight_*/report.json
"""
from __future__ import annotations
import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
import sys

MINUTE = 60_000
HORIZON = 360
VALID_STATES = {'WAITING', 'ACTIVE', 'COMPLETE', 'STALE_BEFORE_COLLECTOR',
                'MISSING_ENTRY', 'GAP_WAITING', 'GAP_EXPIRED'}


def db_readonly(db: Path):
    if not db.is_file():
        raise FileNotFoundError(db)
    return sqlite3.connect(f'file:{db.resolve()}?mode=ro', uri=True)


def verify(report_path: Path) -> dict:
    report = json.loads(report_path.read_text(encoding='utf-8'))
    work = Path(report['workdir']).resolve()
    isolated = work / 'isolated_project'
    journal_path = isolated / 'reports/reaction_lab/scanner_events_v1.sqlite3'
    path_db = isolated / 'reports/reaction_lab/auto_paths_v1.sqlite3'

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import reaction_lab_auto_paths as paths

    errors = []
    warnings = []
    def problem(message):
        errors.append(message)

    expected, memberships, source_details = paths.read_sources(isolated)
    original = report.get('integrity', {})
    with db_readonly(journal_path) as sc, db_readonly(path_db) as pc:
        journal_rows = sc.execute('''SELECT event_key,symbol,side,retest_ts,known_ts,
                            first_seen_ms,observation_type,source_hash FROM events''').fetchall()
        journal = {}
        observations = Counter()
        hashes = set()
        for key, symbol, side, retest, known, first, obs, source_hash in journal_rows:
            journal[key] = (symbol, side, retest)
            observations[obs] += 1
            hashes.add(source_hash)
            if key != f'{symbol}|{side}|{retest}':
                problem(f'Scanner noncanonical identity: {key}')
            if side not in ('LONG', 'SHORT') or known != retest + MINUTE or first < known:
                problem(f'Scanner causality/known timestamp: {key}')
            if obs not in ('LIVE_OBSERVED', 'RECOVERED_WINDOW'):
                problem(f'Unknown observation type: {key} {obs}')
        if len(hashes) > 1:
            problem('Multiple source hashes in scanner journal')

        cycles = sc.execute('''SELECT symbols_requested,symbols_with_1m,
                symbols_with_15m,slow_cycle,error FROM cycles ORDER BY cycle_id''').fetchall()
        if len(cycles) != 3:
            problem(f'Expected 3 cycles, found {len(cycles)}')
        for i, (n, one, fifteen, slow, error) in enumerate(cycles, 1):
            if (n, one, fifteen) != (290, 290, 290) or slow or error:
                problem(f'Scanner cycle {i} failed coverage/timing: {(n,one,fifteen,slow,error)}')

        stored = pc.execute('''SELECT event_key,symbol,side,retest_ts,entry_ts,
                                      state,count,last_ts,path_blob FROM events''').fetchall()
        stored_keys = {r[0] for r in stored}
        if stored_keys != set(expected):
            problem(f'Collector source coverage: missing={len(set(expected)-stored_keys)} extra={len(stored_keys-set(expected))}')
        if not set(journal).issubset(stored_keys):
            problem(f'{len(set(journal)-stored_keys)} scanner events missing from collector')

        actual_memberships = {(key, source): cohort for key,source,cohort in pc.execute(
            'SELECT event_key,source,cohort FROM event_sources')}
        expected_memberships = {(key, source): cohort
                                for key, by_source in memberships.items()
                                for source, cohort in by_source.items()}
        if actual_memberships != expected_memberships:
            missing = set(expected_memberships) - set(actual_memberships)
            extra = set(actual_memberships) - set(expected_memberships)
            mismatched = [k for k in actual_memberships.keys() & expected_memberships.keys()
                          if actual_memberships[k] != expected_memberships[k]]
            problem(f'Profile memberships: missing={len(missing)} extra={len(extra)} wrong_cohort={len(mismatched)}')

        states = Counter()
        stored_bars = 0
        for key, symbol, side, retest, entry, state, count, last_ts, blob in stored:
            states[state] += 1
            # FIX: every persisted source has its OWN canonical ID; only require
            # scanner-journal membership consistency for events truly in scanner.
            if key != f'{symbol}|{side}|{retest}' or side not in ('LONG', 'SHORT'):
                problem(f'Collector noncanonical identity: {key}')
            expected_fields = expected.get(key)
            if expected_fields is not None and (symbol, side, retest) != (
                    expected_fields[0], expected_fields[1], expected_fields[3]):
                problem(f'Collector vs source identity mismatch: {key}')
            if key in journal and (symbol, side, retest) != journal[key]:
                problem(f'Collector vs scanner identity mismatch: {key}')
            if entry != retest + MINUTE:
                problem(f'Incorrect entry timestamp: {key}')
            if state not in VALID_STATES:
                problem(f'Unknown collector state: {key} {state}')
            try:
                bars = paths.decode_path(blob)
            except Exception as exc:
                problem(f'Invalid compressed path: {key}: {type(exc).__name__}: {exc}')
                continue
            stored_bars += len(bars)
            if len(bars) != count or not 0 <= count <= HORIZON:
                problem(f'Path count mismatch: {key} count={count}, decoded={len(bars)}')
            if (bars and last_ts != bars[-1][0]) or (not bars and last_ts is not None):
                problem(f'Invalid last timestamp: {key}')
            if state == 'COMPLETE' and count != HORIZON:
                problem(f'COMPLETE without {HORIZON} bars: {key}')
            if state == 'STALE_BEFORE_COLLECTOR' and count != 0:
                problem(f'STALE event with data: {key}')
            if state in ('MISSING_ENTRY', 'GAP_WAITING', 'GAP_EXPIRED') and key in journal:
                problem(f'Scanner cohort missing bars/gap: {key} ({state})')
            for offset, (ts, op, hi, lo, cl) in enumerate(bars):
                if ts != entry + offset * MINUTE:
                    problem(f'Non-contiguous path: {key} bar={offset}')
                    break
                if not (lo > 0 and hi >= max(op, cl, lo) and lo <= min(op, cl, hi)):
                    problem(f'Invalid OHLC: {key} bar={offset}')
                    break

        if states['STALE_BEFORE_COLLECTOR']:
            warnings.append(f'{states["STALE_BEFORE_COLLECTOR"]} historical events are STALE; their missing initial paths cannot be recovered from the 400-candle Redis buffer')
        if observations['LIVE_OBSERVED'] < 10:
            warnings.append('Only %d LIVE_OBSERVED event(s): three cycles do not prove prospective recall over days' % observations['LIVE_OBSERVED'])
        durations = [r.get('result', {}).get('duration_seconds') for r in report.get('runs', [])
                     if r.get('label', '').startswith('Scanner cycle')]
        if durations and max(durations) > 45:
            warnings.append('Scanner duration up to %.2fs; limited margin for sustained 60-second cadence' % max(durations))
        if report.get('symbols_without_1m_progress'):
            problem(f'{len(report["symbols_without_1m_progress"])} symbols failed 1m progress')
        universe = report.get('universe', {})
        if universe.get('missing_in_default') or universe.get('extra_in_default'):
            problem('Frozen and default symbol universes differ')
        for label in ('before_redis', 'after_redis'):
            snap = report.get(label)
            if not snap:
                problem(f'Missing original Redis observation: {label}')
                continue
            if snap.get('one_minute_present') != 290 or snap.get('fifteen_minute_present') != 290:
                problem(f'{label}: incomplete symbol coverage')
            for metric in ('one_minute_gap', 'fifteen_minute_gap', 'one_minute_duplicates',
                           'fifteen_minute_duplicates', 'one_minute_stale', 'fifteen_minute_stale'):
                if snap.get(metric):
                    problem(f'{label}: {metric} for {len(snap[metric])} symbols')
        if not report.get('production_app_unchanged'):
            problem('Original dashboard/app.py changed during preflight')

    return {
        'verdict': 'PASS_WITH_WARNINGS' if not errors and warnings else ('PASS' if not errors else 'REVIEW_REQUIRED'),
        'source_events_unique': len(expected),
        'scanner_events': len(journal),
        'scanner_observation_types': dict(observations),
        'scanner_cycles': len(cycles),
        'collector_events': len(stored),
        'profile_memberships': len(actual_memberships),
        'saved_bars': stored_bars,
        'states': dict(states),
        'old_false_positive_count': original.get('error_count'),
        'original_growth_check': {
            'paths_grown_since_first_cycle': original.get('paths_grown_since_first_cycle'),
            'paths_unchanged': original.get('paths_unchanged')},
        'warnings': warnings,
        'errors_count': len(errors),
        'first_errors': errors[:25],
        'note': 'Read-only recheck on final saved databases. First-to-final path prefix preservation was checked in the original 3-cycle preflight; this recheck cannot reconstruct deleted cycle-1 snapshots.'
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--report', type=Path, required=True, help='Existing final_preflight_*/report.json')
    args = ap.parse_args()
    try:
        result = verify(args.report)
    except Exception as exc:
        print(f'REVIEW_REQUIRED: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result['errors_count'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
