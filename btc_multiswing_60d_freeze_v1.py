#!/usr/bin/env python3
"""Congela BTCUSDT 1m de 60 días a partir de cinco CSV históricos existentes.

Uso SOLO offline. No consulta redes, no importa LIVE y nunca sobrescribe salidas.
La comparación con HLL (opcional) es diagnóstica; no modifica los datos.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Sequence

VERSION = 'btc-multiswing-60d-freeze-v1.0'
MINUTE_MS = 60_000
TF30_MS = 30 * MINUTE_MS
FIELDS = ('timestamp', 'open', 'high', 'low', 'close', 'volume', 'quoteVolume')
OUTPUT_ROOT = Path('reports/reaction_lab/btc_multiswing_60d_v1/btc_sep09_nov08_60d_v1')
MAIN_START = '2026-09-09T01:00:00Z'
MAIN_END = '2026-11-08T01:00:00Z'


def ms(iso: str) -> int:
    dt = datetime.fromisoformat(iso.replace('Z', '+00:00'))
    if dt.tzinfo is None or dt.second or dt.microsecond:
        raise ValueError(f'UTC y minuto exacto requeridos: {iso}')
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def utc(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, tz=timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Source:
    label: str
    path: Path
    start: int
    end: int
    expected_sha256: str | None = None


def default_sources(root: Path) -> list[Source]:
    visual = root / 'reports/reaction_lab/btc_visual_candles_v1'
    return [
        Source('hll_full290', root / 'reports/horizontal_level_full290_v301/minutes/BTCUSDT_1m.csv',
               ms('2026-08-20T00:00:00Z'), ms('2026-10-06T00:00:00Z')),
        Source('gap_oct06_07', visual / 'btc_gap_oct06_oct08_v1/BTCUSDT_1m.csv',
               ms('2026-10-06T00:00:00Z'), ms('2026-10-08T00:00:00Z'),
               'cf4ee6b80d0f4681ae1f3708bb9b7ef17c7bb454364acaecc91b6edfb5200fc2'),
        Source('holdout_oct08_21', root / 'reports/horizontal_breakout_lab_v1/frozen_v13/symbol_holdout_40_20261008_20261020/native/batch_01/BTCUSDT_1m.csv',
               ms('2026-10-08T00:00:00Z'), ms('2026-10-22T00:00:00Z')),
        Source('gap_oct22_23', visual / 'btc_gap_oct22_oct24_v1/BTCUSDT_1m.csv',
               ms('2026-10-22T00:00:00Z'), ms('2026-10-24T00:00:00Z'),
               '3bb61c7ed46b451333d999621a986b82e21c2aa06131889bc7bb781ae3d9b28a'),
        Source('visual_oct24_nov08', visual / 'btc_oct24_nov08_visual_v1/BTCUSDT_1m.csv',
               ms('2026-10-24T00:00:00Z'), ms('2026-11-08T01:00:00Z'),
               '58fcaf726f910b220fce6c9253567d41eee0aaa4f6bebff6b0f0a61076726a1d'),
    ]


def number(s: str, *, positive: bool = False) -> Decimal:
    try:
        val = Decimal(str(s))
    except (InvalidOperation, TypeError, ValueError) as e:
        raise ValueError(f'Número no válido: {s!r}') from e
    if not val.is_finite() or (positive and val <= 0) or (not positive and val < 0):
        raise ValueError(f'Número fuera de rango: {s!r}')
    return val


def check_row(row: dict[str, str], expected: int, label: str) -> dict[str, str]:
    try:
        actual = int(row['timestamp'])
    except (ValueError, TypeError, KeyError) as e:
        raise ValueError(f'{label}: timestamp inválido') from e
    if actual != expected:
        raise ValueError(f'{label}: se esperaba {utc(expected)}, recibido {actual} ({utc(actual) if actual > 0 else "inválido"}); gap/duplicado/desorden')
    o, h, l, c = (number(row[k], positive=True) for k in ('open', 'high', 'low', 'close'))
    number(row['volume'])
    number(row['quoteVolume'])
    if h < max(o, c, l) or l > min(o, c, h):
        raise ValueError(f'{label}: OHLC inconsistente en {utc(expected)}')
    return {k: str(row[k]) for k in FIELDS}


def validate_sources(sources: Sequence[Source], full_start: int, end: int) -> tuple[list[dict[str, str]], list[dict]]:
    if not sources or full_start % MINUTE_MS or end % MINUTE_MS or full_start >= end:
        raise ValueError('Intervalo global inválido')
    ordered = list(sources)
    if ordered[0].start != full_start or ordered[-1].end != end:
        raise ValueError('Las fuentes no cubren los extremos del intervalo')
    for left, right in zip(ordered, ordered[1:]):
        if left.end != right.start:
            raise ValueError(f'Hueco/solapamiento entre fuentes: {left.label} y {right.label}')
    rows: list[dict[str, str]] = []
    provenance = []
    for src in ordered:
        if src.start % MINUTE_MS or src.end % MINUTE_MS or src.start >= src.end:
            raise ValueError(f'Intervalo fuente inválido: {src.label}')
        if not src.path.is_file():
            raise FileNotFoundError(f'Falta fuente: {src.path}')
        digest = sha256(src.path)
        if src.expected_sha256 and digest.lower() != src.expected_sha256.lower():
            raise ValueError(f'Hash de fuente congelada no coincide: {src.label} {digest}')
        count = 0
        with src.path.open('r', newline='', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames or not set(FIELDS).issubset(reader.fieldnames):
                raise ValueError(f'{src.label}: requiere columnas {FIELDS}; tiene {reader.fieldnames}')
            for item in reader:
                ts = src.start + count * MINUTE_MS
                if ts >= src.end:
                    raise ValueError(f'{src.label}: más velas de las declaradas')
                rows.append(check_row(item, ts, src.label))
                count += 1
        expected_count = (src.end - src.start) // MINUTE_MS
        if count != expected_count:
            raise ValueError(f'{src.label}: {count} velas, esperadas {expected_count}')
        provenance.append({'label': src.label, 'path': str(src.path), 'start_utc': utc(src.start),
                           'end_exclusive_utc': utc(src.end), 'bars': count, 'sha256': digest})
    total_expected = (end - full_start) // MINUTE_MS
    if len(rows) != total_expected:
        raise ValueError(f'El consolidado tiene {len(rows)} en vez de {total_expected} velas')
    return rows, provenance


def aggregate_30m(rows: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    if not rows or len(rows) % 30 or int(rows[0]['timestamp']) % TF30_MS:
        raise ValueError('Ventana 30m incompleta o desalineada')
    out: list[dict[str, str]] = []
    for start in range(0, len(rows), 30):
        part = rows[start:start + 30]
        first_ts = int(part[0]['timestamp'])
        if len(part) != 30 or any(int(r['timestamp']) != first_ts + i * MINUTE_MS
                                  for i, r in enumerate(part)):
            raise ValueError(f'Gap en agregado 30m: {first_ts}')
        out.append({
            'timestamp': str(first_ts),
            'open': part[0]['open'],
            'high': format(max(number(r['high'], positive=True) for r in part), 'f'),
            'low': format(min(number(r['low'], positive=True) for r in part), 'f'),
            'close': part[-1]['close'],
            'volume': format(sum((number(r['volume']) for r in part), Decimal(0)), 'f'),
            'quoteVolume': format(sum((number(r['quoteVolume']) for r in part), Decimal(0)), 'f'),
        })
    return out


def compare_hll(hll_path: Path, derived: Sequence[dict[str, str]]) -> dict:
    """Diagnóstico NO bloqueante; no declara paridad ante faltantes/incompatibilidad."""
    report = {'source': str(hll_path), 'status': 'UNAVAILABLE', 'compared': 0,
              'missing': 0, 'mismatched': 0, 'examples': []}
    if not hll_path.is_file():
        return report
    with hll_path.open(newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or not set(('timestamp', 'open', 'high', 'low', 'close')).issubset(reader.fieldnames):
            report['status'] = 'INCOMPATIBLE_SCHEMA'
            report['columns'] = reader.fieldnames
            return report
        lookup = {}
        for r in reader:
            try:
                t = int(r['timestamp'])
            except (ValueError, TypeError):
                report['status'] = 'BAD_TIMESTAMP'
                return report
            if t in lookup:
                report['status'] = 'DUPLICATE_HLL_TS'
                return report
            lookup[t] = r
    for r in derived:
        ts = int(r['timestamp'])
        other = lookup.get(ts)
        if other is None:
            report['missing'] += 1
            if len(report['examples']) < 8:
                report['examples'].append({'ts_utc': utc(ts), 'kind': 'MISSING_HLL'})
            continue
        report['compared'] += 1
        bad_fields = []
        for key in ('open', 'high', 'low', 'close'):
            try:
                if number(other[key], positive=True) != number(r[key], positive=True):
                    bad_fields.append(key)
            except ValueError:
                bad_fields.append(key)
        if bad_fields:
            report['mismatched'] += 1
            if len(report['examples']) < 8:
                report['examples'].append({'ts_utc': utc(ts), 'kind': 'OHLC_MISMATCH', 'fields': bad_fields})
    report['status'] = 'PARITY_OK' if report['missing'] == report['mismatched'] == 0 else 'PARITY_NOT_ESTABLISHED'
    report['expected'] = len(derived)
    return report


def write_csv(path: Path, rows: Sequence[dict[str, str]]) -> None:
    with path.open('x', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator='\n')
        w.writeheader()
        w.writerows(rows)


def build(sources: Sequence[Source], main_start: int, end: int, output_dir: Path,
          hll_30m: Path | None = None) -> dict:
    if main_start % TF30_MS or end % TF30_MS or end - main_start != 60 * 24 * 60 * MINUTE_MS:
        raise ValueError('La ventana principal debe tener exactamente 60 días y estar alineada a 30m')
    if output_dir.exists():
        raise FileExistsError(f'Salida congelada ya existe, no se sobrescribe: {output_dir}')
    full_start = sources[0].start
    if not full_start < main_start < end:
        raise ValueError('Se requiere warmup anterior al período principal')
    all_rows, provenance = validate_sources(sources, full_start, end)
    shift = (main_start - full_start) // MINUTE_MS
    warmup, main = all_rows[:shift], all_rows[shift:]
    if len(main) != 86400 or not warmup:
        raise ValueError('Tamaño de warmup o ventana principal inválido')
    derived = aggregate_30m(main)
    parity = compare_hll(hll_30m, derived) if hll_30m is not None else {'status': 'NOT_REQUESTED'}
    manifest = {
        'version': VERSION, 'symbol': 'BTCUSDT', 'research_only': True,
        'network_used': False, 'modifies_live': False,
        'integrity': 'CONTIGUOUS_1M_VERIFIED',
        'main_start_utc': utc(main_start), 'main_end_exclusive_utc': utc(end),
        'warmup_start_utc': utc(full_start), 'warmup_end_exclusive_utc': utc(main_start),
        'main_bars_1m': len(main), 'warmup_bars_1m': len(warmup), 'derived_bars_30m': len(derived),
        'sources': provenance, 'hll_30m_parity': parity,
        'files_sha256': {},
        'note': 'OHLC30m derivado para paridad; no reemplaza archivos HLL existentes ni autoriza identidad de pivotes.'
    }
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=output_dir.name + '.stage-', dir=output_dir.parent))
    try:
        names = [('BTCUSDT_1m.csv', main), ('BTCUSDT_1m_warmup.csv', warmup),
                 ('BTCUSDT_30m_derived.csv', derived)]
        for filename, records in names:
            write_csv(tmp / filename, records)
            manifest['files_sha256'][filename] = sha256(tmp / filename)
        with (tmp / 'manifest.json').open('x', encoding='utf-8') as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
            f.write('\n')
        if output_dir.exists():
            raise FileExistsError(f'Salida apareció durante el procesamiento: {output_dir}')
        os.rename(tmp, output_dir)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan', action='store_true')
    mode.add_argument('--run', action='store_true')
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--output', type=Path, default=OUTPUT_ROOT)
    parser.add_argument('--hll-30m', type=Path, default=Path('reports/horizontal_level_hierarchy_v1/runs/btc_60d_hierarchy_v1/candles_30m.csv'))
    args = parser.parse_args()
    root = args.root.resolve()
    srcs = default_sources(root)
    destination = args.output if args.output.is_absolute() else root / args.output
    hll = args.hll_30m if args.hll_30m.is_absolute() else root / args.hll_30m
    plan = {'version': VERSION, 'mode': 'PLAN_READ_ONLY', 'main_start': MAIN_START,
            'main_end_exclusive': MAIN_END, 'expected_main_bars': 86400,
            'expected_warmup_bars': (ms(MAIN_START) - srcs[0].start) // MINUTE_MS,
            'output': str(destination), 'output_exists': destination.exists(),
            'hll_30m_exists': hll.is_file(), 'sources': [
                {'name': s.label, 'path': str(s.path), 'exists': s.path.is_file(),
                 'expected_bars': (s.end - s.start) // MINUTE_MS} for s in srcs],
            'modifies_live': False}
    if args.plan:
        print('BTC_MULTISWING_60D_PLAN', json.dumps(plan, ensure_ascii=False))
        return 0
    manifest = build(srcs, ms(MAIN_START), ms(MAIN_END), destination, hll)
    print('BTC_MULTISWING_60D_FROZEN', json.dumps({
        'output': str(destination), 'integrity': manifest['integrity'],
        'main_bars_1m': manifest['main_bars_1m'],
        'warmup_bars_1m': manifest['warmup_bars_1m'],
        'derived_bars_30m': manifest['derived_bars_30m'],
        'hll_30m_parity': manifest['hll_30m_parity'],
        'files_sha256': manifest['files_sha256'], 'modifies_live': False}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
