#!/usr/bin/env python3
"""Catalogo de pivotes confirmados BTC 60D sobre el freeze 1m verificado.

Research OFFLINE, read-only para fuentes; produce archivos NUEVOS. No LIVE/Redis/network.
Distingue la representacion temporal del pivote de su identidad fisica 1m.
Temporalidades 1m/5m/15m/30m/1h/4h. Excluye frames superiores incompletos.
No incorpora retests, HLL, departure, outcomes ni filtros operativos.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
from collections import defaultdict, Counter
from datetime import datetime, timezone

from engine.live.research.swing_detector import SwingDetector

VERSION = 'btc-multiswing-60d-catalog-v1.1-6tf'
SOURCE_REL = Path('reports/reaction_lab/btc_multiswing_60d_v1/btc_sep09_nov08_60d_v1')
OUTPUT_REL = Path('reports/reaction_lab/btc_multiswing_60d_v1/btc_sep09_nov08_swing_catalog_6tf_v1')
TF_MS = {
    '1m': 60_000,
    '5m': 300_000,
    '15m': 900_000,
    '30m': 1_800_000,
    '1h': 3_600_000,
    '4h': 14_400_000,
}
# Los cinco primeros usan los parametros del visor Continuous ya existente.
# 4h (2/2) es una extension experimental, aun no validada por outcomes.
DETECTOR_BARS = {
    '1m': 5, '5m': 5, '15m': 3,
    '30m': 3, '1h': 2, '4h': 2,
}
MINUTE_MS = 60_000
SOURCE_FIELDS = ('timestamp', 'open', 'high', 'low', 'close')
CATALOG_FIELDS = (
    'pivot_id', 'timeframe', 'side', 'price', 'pivot_ts', 'confirmed_candle_open_ts',
    'available_ts', 'left_bars', 'right_bars', 'prominence_pct',
    'physical_minute_ts', 'physical_identity_state', 'physical_id',
)
GROUP_FIELDS = (
    'physical_id', 'side', 'physical_minute_ts', 'price', 'timeframes',
    'representation_count', 'first_available_ts', 'all_members_available_ts',
    'retrospective_group_warning',
)


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def ms(iso_string: str) -> int:
    return int(datetime.fromisoformat(iso_string.replace('Z', '+00:00')).timestamp() * 1000)


def sha256(path: Path) -> str:
    dig = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            dig.update(block)
    return dig.hexdigest()


def stable_id(*parts: object) -> str:
    return hashlib.sha256('|'.join(map(str, parts)).encode('utf-8')).hexdigest()[:24]


def load_verified_file(path: Path, expected_hash: str, start: int, end: int) -> list[dict]:
    if not path.is_file():
        raise FileNotFoundError(path)
    actual_hash = sha256(path)
    if actual_hash != expected_hash:
        raise ValueError(f'HASH_MISMATCH: {path} expected={expected_hash} got={actual_hash}')
    result = []
    with path.open(newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or not set(SOURCE_FIELDS).issubset(reader.fieldnames):
            raise ValueError(f'SOURCE_SCHEMA_MISMATCH: {path}')
        for index, raw in enumerate(reader):
            if index >= (end - start) // MINUTE_MS:
                raise ValueError(f'TOO_MANY_BARS: {path}')
            ts = int(raw['timestamp'])
            expected_ts = start + index * MINUTE_MS
            if ts != expected_ts:
                raise ValueError(f'GAP_DUPLICATE_OR_ORDER: {path} at index={index}')
            vals = {name: float(raw[name]) for name in SOURCE_FIELDS[1:]}
            if (any(not math.isfinite(x) or x <= 0 for x in vals.values())
                or vals['high'] < max(vals['open'], vals['close'], vals['low'])
                or vals['low'] > min(vals['open'], vals['close'], vals['high'])):
                raise ValueError(f'BAD_OHLC: {path} ts={ts}')
            result.append({'timestamp': ts, **vals})
    if len(result) != (end - start) // MINUTE_MS:
        raise ValueError(f'BAD_BAR_COUNT: {path} got={len(result)}')
    return result


def load_frozen(source: Path) -> tuple[list[dict], dict, int, int]:
    manifest_path = source / 'manifest.json'
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('integrity') != 'CONTIGUOUS_1M_VERIFIED':
        raise ValueError('SOURCE_NOT_VERIFIED')
    if manifest.get('symbol') != 'BTCUSDT':
        raise ValueError('INVALID_SYMBOL')
    if manifest.get('hll_30m_parity', {}).get('status') != 'PARITY_OK':
        raise ValueError('HLL_PARITY_NOT_ESTABLISHED')
    main_start = ms(manifest['main_start_utc'])
    main_end = ms(manifest['main_end_exclusive_utc'])
    warm_start = ms(manifest['warmup_start_utc'])
    warm_end = ms(manifest['warmup_end_exclusive_utc'])
    if (warm_start >= warm_end or warm_end != main_start
        or main_start != ms('2026-09-09T01:00:00Z')
        or main_end != ms('2026-11-08T01:00:00Z')):
        raise ValueError('FROZEN_WINDOW_MISMATCH')
    digests = manifest['files_sha256']
    warm = load_verified_file(source / 'BTCUSDT_1m_warmup.csv', digests['BTCUSDT_1m_warmup.csv'], warm_start, warm_end)
    main = load_verified_file(source / 'BTCUSDT_1m.csv', digests['BTCUSDT_1m.csv'], main_start, main_end)
    if len(warm) != manifest['warmup_bars_1m'] or len(main) != manifest['main_bars_1m']:
        raise ValueError('MANIFEST_BAR_COUNT_MISMATCH')
    return warm + main, manifest, main_start, main_end


def aggregate_tf(minute: list[dict], width: int, *, drop_incomplete_tail: bool = False) -> list[dict]:
    """Agrega desde limites UTC; NUNCA publica la vela TF final incompleta.

    Con drop_incomplete_tail=False falla cerrado frente a intervalos no alineados.
    Con True solo se descarta el sufijo final incompleto (util para 4h).
    """
    if width <= 0 or not minute:
        raise ValueError('TF_UNALIGNED_LENGTH')
    step = width * MINUTE_MS
    if minute[0]['timestamp'] % step:
        raise ValueError('TF_UNALIGNED_START')
    if len(minute) % width and not drop_incomplete_tail:
        raise ValueError('TF_UNALIGNED_LENGTH')
    length = (len(minute) // width) * width
    out = []
    for i in range(0, length, width):
        chunk = minute[i:i+width]
        ts = chunk[0]['timestamp']
        if any(r['timestamp'] != ts + j*MINUTE_MS for j,r in enumerate(chunk)):
            raise ValueError('TF_INPUT_NOT_CONTIGUOUS')
        out.append({'timestamp': ts, 'open': chunk[0]['open'],
                    'high': max(x['high'] for x in chunk),
                    'low': min(x['low'] for x in chunk),
                    'close': chunk[-1]['close']})
    return out


def physical_identity(point, timeframe: str, minute: list[dict], origin: int) -> tuple[int | None, str]:
    width = TF_MS[timeframe] // MINUTE_MS
    base = (point.pivot_timestamp - origin) // MINUTE_MS
    if base < 0 or base + width > len(minute):
        return None, 'MISSING_1M'
    segment = minute[base:base+width]
    if any(r['timestamp'] != point.pivot_timestamp + j*MINUTE_MS for j,r in enumerate(segment)):
        return None, 'MISSING_1M'
    values = [row['high'] if point.side == 'HIGH' else row['low'] for row in segment]
    extremum = max(values) if point.side == 'HIGH' else min(values)
    if not math.isclose(extremum, float(point.price), rel_tol=1e-10, abs_tol=1e-10):
        return None, 'PRICE_MISMATCH'
    matches = [r['timestamp'] for r,v in zip(segment,values)
               if math.isclose(v, extremum, rel_tol=1e-10, abs_tol=1e-10)]
    if len(matches) != 1:
        return None, 'TIED_EXTREMA'
    return matches[0], 'VERIFIED'


def detect_catalog(minute: list[dict], main_start: int, main_end: int,
                   detector_bars: dict[str,int] | None = None,
                   timeframes: tuple[str, ...] | None = None) -> tuple[list[dict], list[dict]]:
    if not minute or minute[0]['timestamp'] >= main_start or minute[-1]['timestamp'] + MINUTE_MS != main_end:
        raise ValueError('INPUT_WINDOW_INVALID')
    if any(b['timestamp'] != minute[0]['timestamp'] + i*MINUTE_MS for i,b in enumerate(minute)):
        raise ValueError('INPUT_GAP_OR_DUPLICATE')
    bars = DETECTOR_BARS if detector_bars is None else detector_bars
    selected = tuple(TF_MS) if timeframes is None else tuple(timeframes)
    if not selected or len(selected) != len(set(selected)) or any(tf not in TF_MS for tf in selected):
        raise ValueError('INVALID_TIMEFRAMES')
    origin = minute[0]['timestamp']
    catalog = []
    for tf in selected:
        width = TF_MS[tf] // MINUTE_MS
        # Si main_end cae a la 01:00 UTC, la vela 4h de 00:00–04:00
        # esta ABIERTA. Descartarla antes de pasar datos al detector.
        frames = minute if width == 1 else aggregate_tf(
            minute, width, drop_incomplete_tail=True)
        n = int(bars[tf])
        if n < 1:
            raise ValueError('INVALID_DETECTOR_BARS')
        detector = SwingDetector(left_bars=n, right_bars=n, min_prominence_pct=0.0)
        for point in detector.detect_all(frames):
            availability = int(point.confirmed_timestamp) + TF_MS[tf]
            # El detector usa timestamp de APERTURA de vela confirmatoria.
            # Solo se puede publicar al CIERRE del ultimo frame necesario.
            if availability > minute[-1]['timestamp'] + MINUTE_MS:
                raise ValueError('UNFINISHED_TF_CONFIRMATION')
            if not (main_start <= int(point.pivot_timestamp) < main_end) or availability > main_end:
                continue
            phys, state = physical_identity(point, tf, minute, origin)
            physical_id = stable_id('BTCUSDT',point.side,phys) if state == 'VERIFIED' else ''
            catalog.append({
                'pivot_id': stable_id('BTCUSDT',tf,point.side,point.pivot_timestamp),
                'timeframe':tf, 'side':point.side, 'price':point.price,
                'pivot_ts':int(point.pivot_timestamp),
                'confirmed_candle_open_ts':int(point.confirmed_timestamp),
                'available_ts':availability, 'left_bars':n, 'right_bars':n,
                'prominence_pct':point.prominence_pct,
                'physical_minute_ts': phys if state=='VERIFIED' else '',
                'physical_identity_state':state,
                'physical_id':physical_id,
            })
    catalog.sort(key=lambda x:(x['pivot_ts'], x['timeframe'], x['side']))
    grouped: dict[str,list[dict]] = defaultdict(list)
    for row in catalog:
        if row['physical_identity_state'] == 'VERIFIED':
            grouped[row['physical_id']].append(row)
    groups=[]
    for pid, members in grouped.items():
        members.sort(key=lambda x:(x['available_ts'],x['timeframe']))
        tfs = sorted(set(m['timeframe'] for m in members), key=lambda tf: TF_MS[tf])
        if any(not math.isclose(float(m['price']),float(members[0]['price']), rel_tol=1e-10, abs_tol=1e-10)
               for m in members):
            raise ValueError('PHYSICAL_ID_CONFLICT')
        groups.append({
            'physical_id':pid,'side':members[0]['side'],
            'physical_minute_ts':members[0]['physical_minute_ts'],
            'price':members[0]['price'], 'timeframes':','.join(tfs),
            'representation_count':len(members),
            'first_available_ts':min(m['available_ts'] for m in members),
            'all_members_available_ts':max(m['available_ts'] for m in members),
            'retrospective_group_warning':'RECALCULATE_MEMBERS_AS_OF_CUTOFF',
        })
    groups.sort(key=lambda x:(x['physical_minute_ts'],x['side']))
    return catalog, groups


def write_csv(path: Path, fields: tuple[str,...], rows: list[dict]) -> None:
    with path.open('x',newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator='\n')
        w.writeheader()
        w.writerows(rows)


def build(source: Path, output: Path) -> dict:
    if output.exists():
        raise FileExistsError(f'No se sobrescribe la salida: {output}')
    minute, src_manifest, start, end = load_frozen(source)
    catalog, groups = detect_catalog(minute,start,end)
    state_counts = Counter(x['physical_identity_state'] for x in catalog)
    tf_counts = Counter(x['timeframe'] for x in catalog)
    shared = sum(int(x['representation_count']) > 1 for x in groups)
    manifest = {
        'version':VERSION, 'symbol':'BTCUSDT', 'research_only':True,
        'network_used':False, 'modifies_live':False,
        'source_frozen_directory':str(source),
        'source_csv_sha256':src_manifest['files_sha256']['BTCUSDT_1m.csv'],
        'source_warmup_sha256':src_manifest['files_sha256']['BTCUSDT_1m_warmup.csv'],
        'source_hll_30m_parity':src_manifest['hll_30m_parity']['status'],
        'start_utc':iso(start), 'end_exclusive_utc':iso(end),
        'timeframes':list(TF_MS), 'detector_bars':dict(DETECTOR_BARS),
        'tf_aggregation':'UTC ALIGNED OHLC; only fully closed TF bars',
        'tf_discarded_tail_minutes':{
            tf: len(minute) % (TF_MS[tf] // MINUTE_MS) for tf in TF_MS
        },
        'tf_last_complete_candle_end_ts':{
            tf: (minute[0]['timestamp'] + (
                len(minute) // (TF_MS[tf] // MINUTE_MS)
            ) * TF_MS[tf]) for tf in TF_MS
        },
        'high_tf_pivot_rules_note':(
            '30m 3x3; 1h 2x2 aligned with Continuous viewer; '
            '4h 2x2 research proposal; not a trading filter'
        ),
        'confirmation_semantics':'confirmed_candle_open_ts + timeframe_duration_ms = available_ts',
        'pivot_count':len(catalog), 'counts_per_timeframe':dict(tf_counts),
        'physical_identity_counts':dict(state_counts),
        'verified_distinct_physical_extrema':len(groups),
        'shared_physical_extrema':shared,
        'warning':'Los grupos son retrospectivos: para features PRE filtrar miembros por available_ts <= cutoff; no usar etiquetas del grupo completo.',
        'files_sha256':{},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=output.name+'.stage-',dir=output.parent))
    try:
        for name, cols, rows in [
            ('confirmed_swings.csv',CATALOG_FIELDS,catalog),
            ('physical_extrema_groups.csv',GROUP_FIELDS,groups),
        ]:
            write_csv(staging/name,cols,rows)
            manifest['files_sha256'][name]=sha256(staging/name)
        with (staging/'manifest.json').open('x',encoding='utf-8') as f:
            json.dump(manifest,f,indent=2,ensure_ascii=False)
            f.write('\n')
        if output.exists():
            raise FileExistsError(f'La salida apareció durante el build: {output}')
        os.rename(staging, output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return manifest


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan', action='store_true')
    mode.add_argument('--run', action='store_true')
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--source', type=Path, default=SOURCE_REL)
    parser.add_argument('--output', type=Path, default=OUTPUT_REL)
    args=parser.parse_args()
    root=args.root.resolve()
    source=args.source if args.source.is_absolute() else root/args.source
    output=args.output if args.output.is_absolute() else root/args.output
    if args.plan:
        manifest_path=source/'manifest.json'
        print('BTC_MULTISWING_60D_CATALOG_PLAN',json.dumps({
            'mode':'PLAN_READ_ONLY','version':VERSION,'source':str(source),
            'source_exists':source.is_dir(),
            'source_manifest_exists':manifest_path.is_file(),
            'output':str(output),'output_exists':output.exists(),
            'detector_bars':DETECTOR_BARS,'timeframes':list(TF_MS),
            'four_hour_trailing_partial_bar_policy':'DROP_BEFORE_DETECTION',
            'modifies_live':False},ensure_ascii=False))
        return 0
    result=build(source,output)
    print('BTC_MULTISWING_60D_CATALOG_FROZEN',json.dumps({
        'output':str(output),'pivot_count':result['pivot_count'],
        'counts_per_timeframe':result['counts_per_timeframe'],
        'physical_identity_counts':result['physical_identity_counts'],
        'verified_distinct_physical_extrema':result['verified_distinct_physical_extrema'],
        'shared_physical_extrema':result['shared_physical_extrema'],
        'modifies_live':False},ensure_ascii=False))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
