#!/usr/bin/env python3
"""Offline V1.2: join immutable V1.1.1 PRE features to verified 1m forward paths.

Read-only source SQLite. Does not start collectors, use Redis, change scanner,
modify LIVE, or rewrite original event status. One output per touch_key.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import csv
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import struct
import zlib

MINUTE = 60_000
BAR = struct.Struct('<qdddd')  # (open_ts, open, high, low, close), canonical v2 encoding
PATH_RELATIVE = Path('reports/reaction_lab/first_touch_paths_v2.sqlite3')
SOURCE_SCHEMA = 'REACTION_MULTI_SWING_QUALITY_DATASET_V11'
DATASET_SCHEMA = 'REACTION_MULTI_SWING_OUTCOMES_V12'
# Pre-registered research questions, not trade parameters.
FAVORABLE_PCT = 0.5
FAVORABLE_HORIZON = 30
BREAK_BAND_PCT = 0.10
BREAK_HORIZON = 60
OUTCOME_COLS = [
    'v12_path_state', 'v12_path_count', 'v12_path_status', 'v12_entry_ts',
    'v12_entry_open', 'v12_entry_beyond_swing', 'v12_path_covered_30m', 'v12_path_covered_60m',
    'v12_quality_state', 'v12_failure_reason',
    'v12_defense_30m', 'v12_defense_first_bar',
    'v12_opposite_break_60m', 'v12_opposite_break_confirmed_ts',
]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def load_features(csv_path: Path):
    csv_path = Path(csv_path)
    manifest_path = csv_path.with_name('manifest.json')
    if not manifest_path.is_file():
        raise FileNotFoundError('Falta manifest.json junto a features.csv')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('dataset_version') != SOURCE_SCHEMA:
        raise ValueError('Se requieren features V1.1.1/V1.1 y su manifiesto')
    if manifest.get('output_sha256') != sha256_file(csv_path):
        raise ValueError('SHA256 de features.csv difiere del manifest: fuente alterada')
    if manifest.get('structural_cutoff') != 'retest_ts (PRE_TOUCH)':
        raise ValueError('Cutoff de características PRE inesperado')
    with csv_path.open(newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or not all(k in reader.fieldnames for k in ('touch_key', 'original_status', 'retest_ts', 'known_ts', 'swing_price', 'side', 'symbol')):
            raise ValueError('Faltan columnas de identidad y nivel en features.csv')
        rows = list(reader)
    keys = [r['touch_key'] for r in rows]
    if not all(keys) or len(keys) != len(set(keys)):
        raise ValueError('touch_key duplicado o vacío en features.csv')
    return rows, manifest


def open_ro(path: Path):
    if not path.is_file():
        raise FileNotFoundError(f'Base de trayectorias inexistente: {path}')
    con = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
    con.execute('PRAGMA query_only=ON')
    return con


def decode_path(blob, count: int, entry_ts: int):
    if count < 0 or count > 360:
        raise ValueError('count fuera de 0..360')
    if count == 0:
        if blob is not None:
            raise ValueError('path_blob presente con count=0')
        return []
    if not blob:
        raise ValueError('path_blob faltante')
    data = zlib.decompress(blob)
    if len(data) != count * BAR.size:
        raise ValueError('path_blob no coincide con count')
    result = [BAR.unpack_from(data, i * BAR.size) for i in range(count)]
    for i, (ts, op, hi, lo, cl) in enumerate(result):
        if int(ts) != entry_ts + i * MINUTE:
            raise ValueError('camino 1m discontinuo o con timestamp incorrecto')
        if not all(math.isfinite(v) and v > 0 for v in (op, hi, lo, cl)) or hi < max(op, lo, cl) or lo > min(op, hi, cl):
            raise ValueError('OHLC inválido')
    return result


def defense_30m(bars, side: str, swing_price: float):
    """Target +0.5% from actual next 1m OPEN, vs wick crossing original swing.

    Doesn't include the initial retest candle. If both barriers touch in the
    same 1m OHLC, intrabar order is unknown (never call it a success).
    """
    if not bars:
        return 'CENSORED', None
    entry = bars[0][1]
    if side == 'LONG':
        target = entry * (1 + FAVORABLE_PCT / 100)
    elif side == 'SHORT':
        target = entry * (1 - FAVORABLE_PCT / 100)
    else:
        raise ValueError('side desconocido')
    for i, (_, _op, hi, lo, _cl) in enumerate(bars[:FAVORABLE_HORIZON], 1):
        favorable = hi >= target if side == 'LONG' else lo <= target
        invalid = lo < swing_price if side == 'LONG' else hi > swing_price
        if favorable and invalid:
            return 'AMBIGUOUS_SAME_BAR', i
        if invalid:
            return 'INVALIDATION_FIRST', i
        if favorable:
            return 'FAVORABLE_FIRST', i
    return ('NEITHER', None) if len(bars) >= FAVORABLE_HORIZON else ('CENSORED', None)


def opposite_break_60m(bars, side: str, swing_price: float):
    """Two consecutive closed 1m candles beyond opposite swing±0.10%.

    Returns the *second candle close time*, never the opening timestamp or
    the original PRE event timestamp. No reclassification of REACTION.
    """
    if side == 'LONG':
        threshold = swing_price * (1 - BREAK_BAND_PCT / 100)
        outside = lambda close: close < threshold
    elif side == 'SHORT':
        threshold = swing_price * (1 + BREAK_BAND_PCT / 100)
        outside = lambda close: close > threshold
    else:
        raise ValueError('side desconocido')
    previous = False
    for ts, _op, _hi, _lo, cl in bars[:BREAK_HORIZON]:
        now = outside(cl)
        if previous and now:
            return 'OPPOSITE_CONFIRMED', int(ts) + MINUTE
        previous = now
    return ('NOT_CONFIRMED', None) if len(bars) >= BREAK_HORIZON else ('CENSORED', None)


def new_outcome(feature: dict, path: tuple | None):
    """Render only new prefixed fields. Existing PRE features are immutable."""
    out = dict.fromkeys(OUTCOME_COLS, '')
    out.update(v12_quality_state='NOT_COVERED', v12_defense_30m='NOT_EVALUABLE',
               v12_opposite_break_60m='NOT_EVALUABLE')
    if path is None:
        out['v12_failure_reason'] = 'NO_PATH_ROW'
        return out
    (symbol, side, status, retest_ts, entry_ts, state, count, blob) = path
    out.update(v12_path_state=state, v12_path_count=count,
               v12_path_status=status, v12_entry_ts=entry_ts)
    identity = (str(symbol), str(side), str(status), int(retest_ts), int(entry_ts))
    expected = (feature['symbol'], feature['side'], feature['original_status'],
                int(feature['retest_ts']), int(feature['known_ts']))
    if identity != expected or int(feature['known_ts']) != int(feature['retest_ts']) + MINUTE:
        out.update(v12_quality_state='IDENTITY_MISMATCH', v12_failure_reason='SOURCE_EVENT_IDENTITY_MISMATCH')
        return out
    if feature.get('v11_state') != 'V11_OK':
        out.update(v12_quality_state='PRE_NOT_COVERED', v12_failure_reason='V11_NOT_FULL_COVERAGE')
        return out
    try:
        price = float(feature['swing_price'])
        if not math.isfinite(price) or price <= 0:
            raise ValueError('swing_price no positivo')
        bars = decode_path(blob, int(count), int(entry_ts))
    except (ValueError, TypeError, zlib.error, struct.error, OverflowError) as exc:
        out.update(v12_quality_state='BAD_PATH', v12_failure_reason=str(exc))
        return out
    if not bars:
        out.update(v12_quality_state='NO_PATH_BARS',
                   v12_failure_reason=str(state))
        return out
    # Keep cohort denominators comparable to canonical offline auditors:
    # a partial horizon is CENSORED even if an early barrier was touched.
    result30, first = (defense_30m(bars, side, price)
                       if len(bars) >= FAVORABLE_HORIZON else ('CENSORED', None))
    result60, confirmed_ts = (opposite_break_60m(bars, side, price)
                              if len(bars) >= BREAK_HORIZON else ('CENSORED', None))
    entry_beyond = (bars[0][1] < price if side == 'LONG' else bars[0][1] > price)
    out.update(v12_quality_state='JOINED', v12_entry_open=bars[0][1],
               v12_entry_beyond_swing=int(entry_beyond),
               v12_path_covered_30m=int(len(bars) >= FAVORABLE_HORIZON),
               v12_path_covered_60m=int(len(bars) >= BREAK_HORIZON),
               v12_defense_30m=result30,
               v12_defense_first_bar='' if first is None else first,
               v12_opposite_break_60m=result60,
               v12_opposite_break_confirmed_ts='' if confirmed_ts is None else confirmed_ts)
    return out


def join_rows(features, con):
    out = []
    tally = Counter()
    for f in features:
        path = con.execute('''SELECT symbol,side,status,retest_ts,entry_ts,state,count,path_blob
            FROM paths WHERE touch_key=?''', (f['touch_key'],)).fetchone()
        x = new_outcome(f, path)
        joined = dict(f)
        # Never overwrite the original PRE fields or original_status.
        if any(k in joined for k in OUTCOME_COLS):
            raise ValueError('Input contains v12 columns; refusing overwrite')
        joined.update(x)
        out.append(joined)
        tally['quality:' + x['v12_quality_state']] += 1
        tally['defense:' + x['v12_defense_30m']] += 1
        tally['break:' + x['v12_opposite_break_60m']] += 1
    return out, dict(sorted(tally.items()))


def save_results(out_dir: Path, rows, summary: dict):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=False)
    columns = list(dict.fromkeys(k for row in rows for k in row)) or ['touch_key']
    csv_path = out_dir / 'features_outcomes.csv'
    with csv_path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    summary['output_sha256'] = sha256_file(csv_path)
    (out_dir / 'manifest.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding='utf-8')
    return csv_path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--project', type=Path, default=Path.cwd())
    ap.add_argument('--features', type=Path, required=True, help='V1.1.1 features.csv con manifest.json')
    ap.add_argument('--paths-db', type=Path, default=None, help='SQLite original, se abre solo lectura')
    ap.add_argument('--out', type=Path, required=True, help='Directorio NUEVO de resultados')
    args = ap.parse_args(argv)
    features, source_manifest = load_features(args.features)
    db = args.paths_db or (args.project / PATH_RELATIVE)
    with closing(open_ro(db)) as con:
        rows, tally = join_rows(features, con)
    summary = {
        'dataset_schema': DATASET_SCHEMA,
        'feature_source_schema': SOURCE_SCHEMA,
        'feature_source_sha256': sha256_file(args.features),
        'feature_manifest_sha256': sha256_file(args.features.with_name('manifest.json')),
        'code_sha256': sha256_file(Path(__file__)),
        'paths_source_readonly': str(db),
        'n_events': len(rows),
        'original_statuses': dict(Counter(f['original_status'] for f in features)),
        'counts': tally,
        'definition': {
            'cutoff_features': source_manifest.get('structural_cutoff'),
            'evaluation_starts_at': 'next 1m OPEN; retest_ts+60000',
            'favorable_target_pct': FAVORABLE_PCT,
            'favorable_horizon_minutes': FAVORABLE_HORIZON,
            'favorable_reference': 'next_1m_open',
            'wick_invalidation': 'strict cross of original 15m swing price',
            'same_bar': 'AMBIGUOUS_SAME_BAR',
            'cohort_coverage_rule': 'Full 30m/60m contiguous horizon required; otherwise CENSORED',
            'opposite_break_band_pct': BREAK_BAND_PCT,
            'opposite_break_rule': 'two consecutive closed 1m candles beyond original swing plus/minus band',
            'opposite_break_horizon_minutes': BREAK_HORIZON,
            'opposite_break_confirmed_ts': 'close of second out-of-band candle',
        },
        'warning': ('No costs, no trades, no training. Only REACTION in pilot; avoid predictive '
                    'or statistical conclusions. NOT_COVERED / CENSORED are not failures.'),
    }
    result = save_results(args.out, rows, summary)
    print(json.dumps({'csv': str(result), 'manifest': str(args.out / 'manifest.json'),
                      'events': len(rows), 'counts': tally}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
