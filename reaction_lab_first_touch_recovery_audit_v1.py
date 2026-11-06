#!/usr/bin/env python3
"""Auditor SOLO LECTURA de cobertura histórica de First Touch Paths V2.

No abre conexiones Redis/REST, no cambia SQLite, no arranca/para workers.
Compara prefijos 1m contiguos de First Touch y Auto Paths por identidad
física (symbol, side, retest_ts, entry_ts), comprobando OHLC si hay solape.

Salida --run: CSV de eventos, ventanas agrupadas y resumen JSON, únicamente
bajo un directorio de reportes nuevo. --plan: solo salida de consola.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import struct
import time
import zlib

VERSION = 'FT_RECOVERY_COVERAGE_AUDIT_V1'
MINUTE = 60_000
HORIZON = 360
HORIZONS = (15, 30, 60, 120, 180, 360)
# Solamente los casos históricos inactivos, no los ACTIVE/GAP_WAITING.
TARGET_STATES = ('STALE_BEFORE_COLLECTOR', 'GAP_EXPIRED', 'MISSING_ENTRY')
BAR = struct.Struct('<qdddd')
# Máximo que aceptaría una consulta Binance Futures /fapi/v1/klines.
# Aquí solo se ESTIMAN solicitudes, no se realizan.
PAGE_BARS = 1500
ROOT = Path('reports/reaction_lab')

FIELDS = [
    'touch_key', 'symbol', 'side', 'status', 'retest_ts', 'retest_utc',
    'entry_ts', 'ft_state', 'ft_count', 'auto_event_key', 'auto_state',
    'auto_count', 'auto_is_continuous_scan', 'audit_state',
    'prefix_available', 'added_from_auto', 'source_parity_bars',
    'full_15m', 'full_30m', 'full_60m', 'full_120m', 'full_180m', 'full_360m',
    'missing_bars_to_360', 'first_missing_ts', 'first_missing_utc',
    'ready_360m_asof', 'requires_other_source', 'note'
]
WINDOW_FIELDS = ['symbol', 'start_ts', 'start_utc', 'end_exclusive_ts',
                 'end_exclusive_utc', 'minutes', 'estimated_pages_1500']


def utc(ts):
    return datetime.fromtimestamp(int(ts) / 1000, timezone.utc).isoformat()


def open_ro(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA query_only=ON')
    con.execute('PRAGMA busy_timeout=30000')
    return con


def check_table(con, table, expected):
    actual = {row['name'] for row in con.execute(f'PRAGMA table_info({table})')}
    missing = set(expected) - actual
    if missing:
        raise ValueError(f'Schema no compatible en {table}: faltan {sorted(missing)}')


def decode_prefix(blob, count, entry):
    """Rechazar contenido no contiguo, OHLC inverosímil o conteo incoherente."""
    count = int(count)
    if count < 0 or count > HORIZON:
        raise ValueError(f'count fuera de [0,{HORIZON}]: {count}')
    if count == 0:
        if blob:
            raise ValueError('blob existente con count=0')
        return []
    if not blob:
        raise ValueError('count positivo sin blob')
    raw = zlib.decompress(blob)
    if len(raw) != count * BAR.size:
        raise ValueError('longitud del blob incompatible con count')
    out = []
    for i in range(count):
        ts, o, h, lo, c = BAR.unpack_from(raw, i * BAR.size)
        if (ts != int(entry) + i * MINUTE or
                any(not math.isfinite(x) or x <= 0 for x in (o, h, lo, c)) or
                h < max(o, lo, c) or lo > min(o, h, c)):
            raise ValueError(f'vela inválida o no contigua, índice={i}')
        out.append((ts, o, h, lo, c))
    return out


def same_bar(a, b):
    if a[0] != b[0]:
        return False
    # SQLite AUTO y First Touch usan exactamente struct <qdddd>.
    # La tolerancia solo absorbe diferencias menores en redondeos de fuente.
    return all(math.isclose(a[i], b[i], rel_tol=1e-10, abs_tol=1e-12)
               for i in range(1, 5))


def identity(row):
    return str(row['symbol']).upper(), str(row['side']).upper(), int(row['retest_ts'])


def audit_event(ft, auto_candidates, asof_ms):
    """Audita una trayectoria histórica; no modifica los datos de origen."""
    ft_count = int(ft['count'])
    entry = int(ft['entry_ts'])
    retest = int(ft['retest_ts'])
    result = {
        'touch_key': str(ft['touch_key']),
        'symbol': str(ft['symbol']).upper(),
        'side': str(ft['side']).upper(),
        'status': str(ft['status']),
        'retest_ts': retest, 'retest_utc': utc(retest),
        'entry_ts': entry,
        'ft_state': str(ft['state']), 'ft_count': ft_count,
        'auto_event_key': '', 'auto_state': '', 'auto_count': 0,
        'auto_is_continuous_scan': 0, 'audit_state': 'NOT_EVALUATED',
        'prefix_available': 0, 'added_from_auto': 0,
        'source_parity_bars': 0, 'ready_360m_asof': 0,
        'requires_other_source': 1, 'note': ''
    }
    for h in HORIZONS:
        result[f'full_{h}m'] = 0
    if entry != retest + MINUTE or entry % MINUTE:
        result['audit_state'] = 'INVALID_FIRST_TOUCH_IDENTITY'
        result['note'] = 'entry_ts no coincide con retest_ts + 1m'
        result['missing_bars_to_360'] = ''
        result['first_missing_ts'] = ''
        result['first_missing_utc'] = ''
        return result
    try:
        ft_bars = decode_prefix(ft['path_blob'], ft_count, entry)
    except (ValueError, zlib.error, struct.error) as exc:
        result['audit_state'] = 'FT_PATH_CORRUPT'
        result['note'] = str(exc)
        result['missing_bars_to_360'] = ''
        result['first_missing_ts'] = ''
        result['first_missing_utc'] = ''
        return result
    prefix = len(ft_bars)
    chosen = None
    if len(auto_candidates) > 1:
        result['audit_state'] = 'AMBIGUOUS_AUTO_IDENTITY'
        result['note'] = 'Más de un evento Auto Paths comparte símbolo/lado/retest_ts'
    elif not auto_candidates:
        result['audit_state'] = 'NO_AUTO_MATCH'
    else:
        auto = auto_candidates[0]
        result['auto_event_key'] = str(auto['event_key'])
        result['auto_state'] = str(auto['state'])
        result['auto_count'] = int(auto['count'])
        result['auto_is_continuous_scan'] = int(bool(auto.get('continuous_scan')))
        if int(auto['entry_ts']) != entry:
            result['audit_state'] = 'AUTO_ENTRY_MISMATCH'
            result['note'] = 'Auto Paths tiene un entry_ts distinto'
        else:
            try:
                auto_bars = decode_prefix(auto['path_blob'], int(auto['count']), entry)
            except (ValueError, zlib.error, struct.error) as exc:
                result['audit_state'] = 'AUTO_PATH_CORRUPT'
                result['note'] = str(exc)
            else:
                overlap = min(len(ft_bars), len(auto_bars))
                result['source_parity_bars'] = overlap
                if any(not same_bar(ft_bars[i], auto_bars[i]) for i in range(overlap)):
                    result['audit_state'] = 'AUTO_OHLC_CONFLICT'
                    result['note'] = f'OHLC inconsistente en {overlap} velas solapadas'
                    result['source_parity_bars'] = 0
                else:
                    chosen = auto_bars
                    if len(auto_bars) >= HORIZON and prefix < HORIZON:
                        result['audit_state'] = 'AUTO_FULL_360'
                    elif len(auto_bars) > prefix:
                        result['audit_state'] = 'AUTO_EXTENDS_PREFIX'
                    else:
                        result['audit_state'] = 'AUTO_NO_ADDITIONAL_BARS'
                    if overlap == 0 and len(auto_bars) > 0:
                        result['note'] = 'Identidad temporal OK; sin OHLC First Touch para paridad'
    if chosen is not None and len(chosen) > prefix:
        result['added_from_auto'] = len(chosen) - prefix
        prefix = len(chosen)
    result['prefix_available'] = prefix
    for h in HORIZONS:
        result[f'full_{h}m'] = int(prefix >= h)
    result['missing_bars_to_360'] = HORIZON - prefix
    if prefix < HORIZON:
        missing = entry + prefix * MINUTE
        result['first_missing_ts'] = missing
        result['first_missing_utc'] = utc(missing)
    else:
        result['first_missing_ts'] = ''
        result['first_missing_utc'] = ''
    result['ready_360m_asof'] = int(asof_ms >= entry + HORIZON * MINUTE - 1)
    result['requires_other_source'] = int(prefix < HORIZON)
    return result


def merge_intervals(intervals):
    """Fusionar ventanas [start,end) solapadas o adyacentes por símbolo."""
    grouped = defaultdict(list)
    for symbol, start, end in intervals:
        if end > start:
            grouped[str(symbol).upper()].append((int(start), int(end)))
    merged = []
    for symbol in sorted(grouped):
        spans = sorted(grouped[symbol])
        start, end = spans[0]
        for a, b in spans[1:]:
            if a <= end:
                end = max(end, b)
            else:
                merged.append((symbol, start, end))
                start, end = a, b
        merged.append((symbol, start, end))
    return merged


def windows_from_events(rows, asof_ms):
    windows = []
    for r in rows:
        if (r['requires_other_source'] != 1 or
                r['audit_state'] in ('FT_PATH_CORRUPT', 'INVALID_FIRST_TOUCH_IDENTITY',
                                     'AUTO_OHLC_CONFLICT', 'AMBIGUOUS_AUTO_IDENTITY',
                                     'AUTO_ENTRY_MISMATCH', 'AUTO_PATH_CORRUPT') or
                not r['ready_360m_asof']):
            continue
        start = int(r['first_missing_ts'])
        windows.append((r['symbol'], start, int(r['entry_ts']) + HORIZON * MINUTE))
    merged = merge_intervals(windows)
    records = []
    for symbol, start, end in merged:
        bars = (end - start) // MINUTE
        records.append({
            'symbol': symbol, 'start_ts': start, 'start_utc': utc(start),
            'end_exclusive_ts': end, 'end_exclusive_utc': utc(end),
            'minutes': bars, 'estimated_pages_1500': (bars + PAGE_BARS - 1) // PAGE_BARS
        })
    return records


def summary_report(rows, windows, ft_stats, asof_ms, auto_exists):
    counts = Counter(r['audit_state'] for r in rows)
    by_state = Counter(r['ft_state'] for r in rows)
    by_status = Counter(r['status'] for r in rows)
    by_date = defaultdict(lambda: {'events': 0, 'auto_full360': 0, 'requires_other_source': 0})
    by_status_coverage = defaultdict(lambda: {'events': 0, 'auto_full360': 0, 'covered180': 0})
    for r in rows:
        day = r['retest_utc'][:10]
        by_date[day]['events'] += 1
        by_date[day]['auto_full360'] += r['audit_state'] == 'AUTO_FULL_360'
        by_date[day]['requires_other_source'] += r['requires_other_source'] == 1
        s = by_status_coverage[r['status']]
        s['events'] += 1
        s['auto_full360'] += r['audit_state'] == 'AUTO_FULL_360'
        s['covered180'] += r['full_180m'] == 1
    n_mature = sum(r['ready_360m_asof'] == 1 for r in rows)
    exclusions = Counter(r['audit_state'] for r in rows if r['audit_state'] in (
        'FT_PATH_CORRUPT', 'INVALID_FIRST_TOUCH_IDENTITY', 'AUTO_OHLC_CONFLICT',
        'AMBIGUOUS_AUTO_IDENTITY', 'AUTO_ENTRY_MISMATCH', 'AUTO_PATH_CORRUPT'))
    return {
        'schema': VERSION, 'asof_utc': utc(asof_ms), 'read_only_sources': True,
        'no_downloads': True, 'no_source_db_mutations': True,
        'scope_states': list(TARGET_STATES),
        'source_db_exists': {'first_touch_paths_v2': True, 'auto_paths_v1': bool(auto_exists)},
        'first_touch_all_states': dict(sorted(ft_stats.items())),
        'target_events': len(rows), 'target_by_state': dict(sorted(by_state.items())),
        'target_by_original_status': dict(sorted(by_status.items())),
        'audit_states': dict(sorted(counts.items())),
        'full_horizon_counts': {str(h): sum(r[f'full_{h}m'] == 1 for r in rows)
                                for h in HORIZONS},
        'auto_candidate_matches': sum(bool(r['auto_event_key']) for r in rows),
        'auto_fully_recovers_360': counts['AUTO_FULL_360'],
        'auto_adds_bars_events': sum(r['added_from_auto'] > 0 for r in rows),
        'auto_added_bar_event_units': sum(r['added_from_auto'] for r in rows),
        'matched_continuous_scan_memberships': sum(r['auto_is_continuous_scan'] for r in rows),
        'events_still_needing_360': sum(r['requires_other_source'] == 1 for r in rows),
        'events_mature_asof': n_mature,
        'manual_review_exclusions': dict(sorted(exclusions.items())),
        'merged_missing_source_windows': len(windows),
        'merged_missing_source_symbols': len({w['symbol'] for w in windows}),
        'merged_missing_source_unique_symbol_minutes': sum(w['minutes'] for w in windows),
        'estimated_max_1500_bar_pages': sum(w['estimated_pages_1500'] for w in windows),
        'by_date': dict(sorted(by_date.items())),
        'by_original_status': dict(sorted(by_status_coverage.items())),
        'caveats': [
            'Coincidencia Auto Paths = símbolo/lado/retest_ts/entry_ts; la identidad por sí sola no verifica OHLC si First Touch count=0.',
            'La falta de Auto Paths no demuestra que falten velas en otras fuentes locales o Binance.',
            'Los FULL_360 basados en Auto Paths son elegibilidad de reutilización offline; no modifican los estados originales.',
            'La estimación de páginas considera todos los intervalos ausentes para 360m, no descuenta CSV/HLL ni cachés externos.',
            'Ventanas agrupadas son un plan teórico; puede haber delistings, huecos o distintas fuentes históricas.',
            'El análisis lee las dos bases secuencialmente y no constituye snapshot transaccional entre ellas.',
            'No incluir ACTIVE o GAP_WAITING protege al colector en curso.'
        ]
    }


def load_auto_index(path):
    if not path.is_file():
        return {}, None
    con = open_ro(path)
    try:
        check_table(con, 'events', ('event_key', 'symbol', 'side', 'retest_ts', 'entry_ts', 'state', 'count', 'path_blob'))
        index = defaultdict(list)
        for row in con.execute('SELECT event_key,symbol,side,retest_ts,entry_ts,state,count FROM events'):
            index[identity(row)].append(dict(row))
        return index, con
    except Exception:
        con.close()
        raise


def load_ft_targets(path):
    with closing(open_ro(path)) as con:
        check_table(con, 'paths', ('touch_key','symbol','side','status','retest_ts',
                                  'entry_ts','state','count','path_blob'))
        all_states = dict(con.execute('SELECT state,COUNT(*) FROM paths GROUP BY state').fetchall())
        placeholders = ','.join('?' for _ in TARGET_STATES)
        query = ('SELECT touch_key,symbol,side,status,retest_ts,entry_ts,state,count,path_blob '
                 f'FROM paths WHERE state IN ({placeholders}) ORDER BY retest_ts,touch_key')
        return [dict(row) for row in con.execute(query, TARGET_STATES)], all_states


def run_audit(project, asof_ms=None):
    project = Path(project).resolve()
    asof_ms = int(time.time() * 1000) if asof_ms is None else int(asof_ms)
    base = project / ROOT
    fts, ft_stats = load_ft_targets(base / 'first_touch_paths_v2.sqlite3')
    auto_path = base / 'auto_paths_v1.sqlite3'
    auto_index, auto_con = load_auto_index(auto_path)
    results = []
    has_membership = _has_event_sources(auto_con) if auto_con is not None else False
    try:
        for ft in fts:
            candidates = auto_index.get(identity(ft), [])
            prepared = []
            if len(candidates) == 1:
                auto = dict(candidates[0])
                match = auto_con.execute('SELECT path_blob FROM events WHERE event_key=?',
                                         (auto['event_key'],)).fetchone()
                auto['path_blob'] = match['path_blob'] if match is not None else None
                auto['continuous_scan'] = bool(auto_con.execute(
                    'SELECT 1 FROM event_sources WHERE event_key=? AND source=? LIMIT 1',
                    (auto['event_key'], 'continuous_scan')).fetchone()) if has_membership else False
                prepared.append(auto)
            elif len(candidates) > 1:
                prepared = candidates
            results.append(audit_event(ft, prepared, asof_ms))
    finally:
        if auto_con is not None:
            auto_con.close()
    windows = windows_from_events(results, asof_ms)
    report = summary_report(results, windows, ft_stats, asof_ms, auto_path.is_file())
    report['input_paths'] = {
        'first_touch_paths_v2': str(base / 'first_touch_paths_v2.sqlite3'),
        'auto_paths_v1': str(auto_path)
    }
    return report, results, windows


def _has_event_sources(con):
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_sources'").fetchone())


def write_csv(path, headers, data):
    with path.open('x', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(data)


def write_reports(dest, report, events, windows):
    dest = Path(dest).resolve()
    if dest.exists():
        raise FileExistsError(f'El destino ya existe: {dest} (no sobrescribir)')
    dest.mkdir(parents=True)
    write_csv(dest / 'events.csv', FIELDS, events)
    write_csv(dest / 'missing_windows_merged.csv', WINDOW_FIELDS, windows)
    (dest / 'summary.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    manifest = {'schema': VERSION, 'files': {}}
    for fname in ('events.csv', 'missing_windows_merged.csv', 'summary.json'):
        manifest['files'][fname] = hashlib.sha256((dest / fname).read_bytes()).hexdigest()
    (dest / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path.cwd())
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--plan', action='store_true', help='Auditar y mostrar resultado sin crear reportes')
    modes.add_argument('--run', action='store_true', help='Auditar y exportar CSV/JSON nuevos')
    parser.add_argument('--out', type=Path, help='Directorio NUEVO para reportes si --run')
    parser.add_argument('--asof-ms', type=int, help='Corte UTC en milisegundos (solo reproducción/tests)')
    args = parser.parse_args(argv)
    if args.out and not args.run:
        parser.error('--out requiere --run')
    report, events, windows = run_audit(args.project, args.asof_ms)
    result = {key: report[key] for key in (
        'schema', 'asof_utc', 'target_events', 'target_by_state', 'target_by_original_status',
        'audit_states', 'full_horizon_counts', 'auto_candidate_matches',
        'auto_fully_recovers_360', 'auto_adds_bars_events', 'events_still_needing_360',
        'manual_review_exclusions', 'merged_missing_source_windows',
        'merged_missing_source_symbols', 'merged_missing_source_unique_symbol_minutes',
        'estimated_max_1500_bar_pages')}
    if args.run:
        stamp = datetime.fromtimestamp((args.asof_ms if args.asof_ms is not None
                                        else time.time()*1000)/1000, timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        dest = args.out or Path(args.project) / ROOT / 'first_touch_recovery_audit_v1' / f'run_{stamp}'
        write_reports(dest, report, events, windows)
        result['reports_dir'] = str(Path(dest).resolve())
    print('FT_RECOVERY_AUDIT_V1 ' + json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
