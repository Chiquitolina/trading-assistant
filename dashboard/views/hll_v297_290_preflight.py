#!/usr/bin/env python3
"""HLL V2.9.7: read-only inventory BEFORE scaling frozen PRE+FLIP research to 290 symbols.

Does not collect data, run HLL detectors or modify LIVE. Each file is checked for
existence, size and *edge timestamps*; gaps inside CSVs are NOT audited here.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_EVENTS = Path('reports/candidate_long_horizon/runs/20260907_20260930_dd94dc31235f/events.csv')
DEFAULT_PRE = Path('reports/horizontal_level_structure_quality_v282/runs/core15_pre_all_v282/pre_cohort.csv')
DEFAULT_30M = Path('reports/horizontal_level_lifecycle_v21/candles_aug20_oct25_core15_btc')
DEFAULT_1M = Path('reports/horizontal_level_cross_research_v22/extended_1m_core15_v25')
DEFAULT_ATLAS = Path('reports/horizontal_level_lifecycle_v21/core15_extended_sep_oct_v21')
DEFAULT_SIGNALS = Path('reports/horizontal_level_cross_research_v22/runs/core15_extended_sep_oct_v22')
DEFAULT_OUTPUT = Path('reports/horizontal_level_290_preflight_v297/runs/sep290_preflight_v297')
START = '2026-09-07T00:00:00+00:00'
END = '2026-10-01T00:00:00+00:00'


def _timestamp(s: str) -> int:
    return int(datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp() * 1000)


def _utc(ms):
    return None if ms is None else datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def _digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for blob in iter(lambda: f.read(1024 * 1024), b''):
            h.update(blob)
    return h.hexdigest()


def _fieldnames(path):
    with open(path, encoding='utf-8-sig', newline='') as f:
        return next(csv.reader(f))


def _symbols_from_csv(path, column=None):
    if not path.is_file():
        raise FileNotFoundError('Falta CSV del universo congelado: ' + str(path))
    symbols = set()
    with open(path, encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        choices = ('symbol', 'Symbol', 'SYMBOL', 'ticker', 'symbol_name')
        col = column or next((x for x in choices if x in (reader.fieldnames or [])), None)
        if col is None:
            raise ValueError('No se encontró columna de símbolos. Campos: ' + str(reader.fieldnames))
        for row in reader:
            s = (row.get(col) or '').strip()
            if s:
                symbols.add(s)
    if not symbols:
        raise ValueError('Universo vacío en ' + str(path))
    return sorted(symbols), col


def _csv_edges(path):
    """Check only first/last data row without loading (potentially huge) 1m files."""
    result = {'exists': path.is_file(), 'bytes': None, 'first_utc': None,
              'last_utc': None, 'has_timestamp_column': False,
              'csv_edge_error': None}
    if not path.is_file():
        return result
    result['bytes'] = path.stat().st_size
    try:
        with path.open('rb') as f:
            head = f.readline()
            header = next(csv.reader([head.decode('utf-8-sig').strip()]))
            if 'timestamp' not in header:
                result['csv_edge_error'] = 'MISSING_TIMESTAMP'
                return result
            ti = header.index('timestamp')
            first = f.readline()
            if not first:
                result['csv_edge_error'] = 'EMPTY_CSV'
                return result
            first_ts = int(float(next(csv.reader([first.decode('utf-8-sig').strip()]))[ti]))
            f.seek(0, 2)
            size = f.tell()
            # Last nonempty line from last at most 64 KiB: wider than ordinary OHLC rows.
            n = min(size, 65536)
            f.seek(-n, 2)
            tail = f.read().decode('utf-8-sig', errors='replace').splitlines()
            last_line = next((s for s in reversed(tail) if s.strip()), None)
            last_ts = int(float(next(csv.reader([last_line]))[ti]))
            result.update(first_utc=_utc(first_ts),last_utc=_utc(last_ts),
                          first_ms=first_ts,last_ms=last_ts,has_timestamp_column=True)
    except Exception as exc:
        result['csv_edge_error'] = type(exc).__name__ + ':' + str(exc)[:140]
    return result


def _atlas_symbols(path):
    cfg = path / 'run_config.json'
    if not cfg.is_file():
        return set(), 'MISSING_CONFIG'
    try:
        d = json.loads(cfg.read_text(encoding='utf-8'))
        return set(map(str, d.get('symbols', []))), None
    except (ValueError, OSError, TypeError) as exc:
        return set(), 'INVALID_CONFIG:' + str(exc)[:100]


def _signal_symbols(path):
    freeze = path / 'freeze.json'
    if not freeze.is_file():
        return set(), 'MISSING_FREEZE'
    try:
        d = json.loads(freeze.read_text(encoding='utf-8'))
        return set(map(str, d.get('symbols', []))), None
    except (ValueError, OSError, TypeError) as exc:
        return set(), 'INVALID_FREEZE:' + str(exc)[:100]


def survey(universe, baseline, candles30, candles1, atlas, signals, start_ms, end_ms):
    existing, _ = _symbols_from_csv(baseline) if baseline.is_file() else ([], None)
    base_set = set(existing)
    atlas_syms, atlas_error = _atlas_symbols(atlas)
    signal_syms, signal_error = _signal_symbols(signals)
    output = []
    for sym in universe:
        m30 = _csv_edges(candles30 / (sym + '_30m.csv'))
        m1 = _csv_edges(candles1 / (sym + '_1m.csv'))
        good30 = m30.get('first_ms') is not None and m30['first_ms'] <= start_ms and m30['last_ms'] >= end_ms-1_800_000
        good1 = m1.get('first_ms') is not None and m1['first_ms'] <= start_ms and m1['last_ms'] >= end_ms-60_000
        shards = all((atlas / 'shards' / (sym + '.' + suffix + '.csv')).is_file()
                     for suffix in ('formation', 'events', 'ends'))
        output.append({'symbol': sym, 'was_in_core15_pre': sym in base_set,
                       '30m_exists': m30['exists'], '30m_size_bytes': m30['bytes'],
                       '30m_first_utc': m30['first_utc'], '30m_last_utc': m30['last_utc'],
                       '30m_covers_requested_edges': good30,
                       '30m_edge_error': m30['csv_edge_error'],
                       '1m_exists': m1['exists'], '1m_size_bytes': m1['bytes'],
                       '1m_first_utc': m1['first_utc'], '1m_last_utc': m1['last_utc'],
                       '1m_covers_requested_edges': good1,
                       '1m_edge_error': m1['csv_edge_error'],
                       'v21_atlas_listed': sym in atlas_syms,
                       'v21_all_three_shards': shards,
                       'v22_freeze_listed': sym in signal_syms,
                       'v21_v22_ready': sym in atlas_syms and shards and sym in signal_syms})
    meta = {'requested': len(universe), 'baseline_core15_file_count': len(base_set),
            'baseline_overlap': len(set(universe) & base_set),
            'new_vs_original': len(set(universe) - base_set),
            'baseline_symbols_absent_from_290': sorted(base_set - set(universe)),
            '30m_existing': sum(row['30m_exists'] for row in output),
            '30m_edge_coverage': sum(row['30m_covers_requested_edges'] for row in output),
            '1m_existing': sum(row['1m_exists'] for row in output),
            '1m_edge_coverage': sum(row['1m_covers_requested_edges'] for row in output),
            'v21_v22_ready': sum(row['v21_v22_ready'] for row in output),
            'atlas_warning': atlas_error, 'signal_warning': signal_error,
            'caution': 'Edge coverage alone DOES NOT prove 30m/1m continuity or valid HLL signals.'}
    return output, meta


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan', action='store_true', help='Read-only: print summary, do not write files')
    mode.add_argument('--run', action='store_true', help='Write new, immutable plan artifacts')
    ap.add_argument('--universe-csv', type=Path, default=DEFAULT_EVENTS)
    ap.add_argument('--symbol-column', default=None)
    ap.add_argument('--expected-symbols', type=int, default=290)
    ap.add_argument('--baseline-pre', type=Path, default=DEFAULT_PRE)
    ap.add_argument('--candles-30m-dir', type=Path, default=DEFAULT_30M)
    ap.add_argument('--candles-1m-dir', type=Path, default=DEFAULT_1M)
    ap.add_argument('--atlas-run', type=Path, default=DEFAULT_ATLAS)
    ap.add_argument('--signal-run', type=Path, default=DEFAULT_SIGNALS)
    ap.add_argument('--start-utc', default=START)
    ap.add_argument('--end-utc', default=END)
    ap.add_argument('--output-dir', type=Path, default=DEFAULT_OUTPUT)
    args = ap.parse_args(argv)
    syms, source_column = _symbols_from_csv(args.universe_csv,args.symbol_column)
    if args.expected_symbols and len(syms) != args.expected_symbols:
        raise SystemExit('UNIVERSE_MISMATCH: se esperaban %d, encontrados %d en %s. No asumimos que el universo de otro estudio equivale a 290.' % (args.expected_symbols,len(syms),args.universe_csv))
    start_ms, end_ms = _timestamp(args.start_utc), _timestamp(args.end_utc)
    if start_ms >= end_ms:
        raise SystemExit('Rango UTC inválido')
    rows, meta = survey(syms,args.baseline_pre,args.candles_30m_dir,args.candles_1m_dir,
                        args.atlas_run,args.signal_run,start_ms,end_ms)
    meta.update({'version':'HLL_V297_290_PREFLIGHT','mode':'READ_ONLY_INVENTORY',
                 'symbols':len(syms), 'source_universe':str(args.universe_csv),
                 'symbol_column':source_column, 'source_universe_sha256':_digest(args.universe_csv),
                 'start_utc':args.start_utc, 'end_utc':args.end_utc,
                 'minute_dir':str(args.candles_1m_dir),'candles_dir':str(args.candles_30m_dir),
                 'output_dir':str(args.output_dir),
                 'does_not_evaluate_strategy':True,
                 'no_interpolation_of_gaps':True})
    print('HLL_V297_PLAN '+json.dumps(meta,ensure_ascii=False,sort_keys=True), flush=True)
    if args.plan:
        return 0
    if args.output_dir.exists():
        raise SystemExit('OUTPUT_EXISTS: no sobrescribir ' + str(args.output_dir))
    args.output_dir.parent.mkdir(parents=True,exist_ok=True)
    args.output_dir.mkdir()
    with (args.output_dir / 'universe_290.txt').open('w',encoding='utf-8') as f:
        f.write(''.join(sym+'\n' for sym in syms))
    with (args.output_dir / 'coverage_by_symbol.csv').open('w',newline='',encoding='utf-8') as f:
        writer = csv.DictWriter(f,fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
    (args.output_dir / 'manifest.json').write_text(json.dumps(meta,indent=2,ensure_ascii=False),encoding='utf-8')
    print('HLL_V297_DONE '+json.dumps({'output':str(args.output_dir),**{k:meta[k] for k in ('symbols','30m_edge_coverage','1m_edge_coverage','v21_v22_ready')}},ensure_ascii=False),flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
