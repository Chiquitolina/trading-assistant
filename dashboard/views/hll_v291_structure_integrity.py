#!/usr/bin/env python3
"""HLL V2.9.1: audit PRE ascending trendline integrity without altering V2.8.2.

Research only. Generates a separate, one-row-per-attempt audited taxonomy. A
trendline is established only after its THIRD 2x2 pivot is confirmed; a breach
needs two CLOSED 30m candles below the projected line by >= .35 formation ATR.
This is a deterministic exploratory definition, NOT a trading signal.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from dashboard.views.hll_v281_structure_patterns import load_candles
except ModuleNotFoundError:
    from hll_v281_structure_patterns import load_candles

TF = 1_800_000
WINDOWS = (24, 48, 96)
COHORT = Path('reports/horizontal_level_structure_quality_v282/runs/core15_pre_all_v282/pre_cohort.csv')
CANDLES = Path('reports/horizontal_level_lifecycle_v21/candles_aug20_oct25_core15_btc')
OUTPUT = Path('reports/horizontal_level_structure_integrity_v291/runs/core15_trendline_v291')
BROKEN = 'ASCENDING_TREND_BROKEN'
ATR_BREACH = 0.35
MIN_SPAN_BARS = 8


def _hash(path: Path) -> str:
    obj = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            obj.update(chunk)
    return obj.hexdigest()


def _pivots(raw):
    if isinstance(raw, str) and raw.strip():
        parsed = json.loads(raw)
        return sorted(parsed, key=lambda p: int(p['open_ts']))
    return []


def trendline_break(candles: pd.DataFrame, pivot_json: str, pre_closed_ts: int,
                    window_bars: int, atr: float) -> dict:
    """Find confirmed rising three-pivot support later broken by two closes.

    Past and future bars are strictly clipped to PRE. Pivot timestamps reflect
    the 2x2 confirmation clock stored in V2.8.2; no future bar is inspected.
    """
    empty = dict(broken=False, anchor_first_ts=None, anchor_last_ts=None,
                 anchor_confirmed_ts=None, broken_closed_ts=None,
                 line_price_at_break=None, line_price_first=None, line_price_at_pre=None,
                 break_depth_atr=None, slope_atr_per_bar=None)
    if not (np.isfinite(atr) and atr > 0):
        return empty
    pre = int(pre_closed_ts)
    from_ts = pre - int(window_bars) * TF
    selected = candles[(candles.timestamp >= from_ts) & (candles.timestamp < pre)].copy()
    if len(selected) != window_bars or (selected.timestamp.diff().dropna() != TF).any():
        return empty
    pivots = [p for p in _pivots(pivot_json)
              if from_ts <= int(p['open_ts']) < pre
              and int(p['known_closed_ts']) <= pre]
    # A line needs THREE ascending independent pivots, and cannot be established
    # until the third pivot's right-hand confirming candles are closed.
    if len(pivots) < 3:
        return empty
    selected = selected.reset_index(drop=True)
    ts = selected.timestamp.to_numpy(dtype=np.int64)
    closes = selected.close.to_numpy(dtype=float)
    candidates = []
    active_lines = []
    for i in range(len(pivots) - 2):
        three = pivots[i:i + 3]
        first, middle, last = three
        times = np.array([int(p['open_ts']) for p in three], dtype=np.int64)
        lows = np.array([float(p['low']) for p in three], dtype=float)
        xi = (times - times[0]).astype(float) / TF
        if xi[-1] < MIN_SPAN_BARS or (np.diff(lows) < 0.10 * atr).any():
            continue
        # Robust to uneven pivot spacing: least-squares rising lower-bound line.
        slope, intercept = np.polyfit(xi, lows, deg=1)
        if slope <= 0:
            continue
        third_known = int(last['known_closed_ts'])
        start_check = int(np.searchsorted(ts, third_known))
        line = intercept + slope * (ts.astype(float) - times[0]) / TF
        active_lines.append(dict(broken=False, anchor_first_ts=int(times[0]),
            anchor_last_ts=int(times[-1]), anchor_confirmed_ts=third_known,
            broken_closed_ts=None, line_price_at_break=None,
            line_price_first=float(intercept), line_price_at_pre=float(line[-1]),
            break_depth_atr=None, slope_atr_per_bar=float(slope / atr)))
        if start_check >= len(ts) - 1:
            continue
        below = closes < line - ATR_BREACH * atr
        for j in range(max(start_check, 0) + 1, len(ts)):
            if below[j - 1] and below[j]:
                # Two consecutive closes, both known no later than PRE cutoff.
                known = int(ts[j]) + TF
                if known > pre:
                    continue
                depth = float((line[j] - closes[j]) / atr)
                candidates.append(dict(
                    broken=True, anchor_first_ts=int(times[0]),
                    anchor_last_ts=int(times[-1]), anchor_confirmed_ts=third_known,
                    broken_closed_ts=known,
                    line_price_at_break=float(line[j]),
                    line_price_first=float(intercept), line_price_at_pre=float(line[-1]),
                    break_depth_atr=depth,
                    slope_atr_per_bar=float(slope / atr),
                ))
                break
    if not candidates:
        return max(active_lines, key=lambda x:x["anchor_last_ts"]) if active_lines else empty
    # Latest breach is the most relevant visible compromised trendline.
    return max(candidates, key=lambda x: (x['broken_closed_ts'], x['anchor_last_ts']))


def audit_cohort(cohort: pd.DataFrame, candles_dir: Path, max_symbols=0) -> pd.DataFrame:
    required = {'attempt_id', 'symbol', 'pre_closed_ts', 'atr_pre'}
    for w in WINDOWS:
        required.update({f'pre_pattern_{w}', f'pre{w}_pivot_lows_json', f'pre_{w}_coverage'})
    if required - set(cohort):
        raise ValueError('Faltan columnas V2.8.2: ' + ', '.join(sorted(required - set(cohort))))
    if cohort.attempt_id.isna().any() or cohort.attempt_id.duplicated().any():
        raise ValueError('IDs PRE duplicados / vacíos')
    symbols = sorted(cohort.symbol.unique())
    if max_symbols:
        symbols = symbols[:max_symbols]
    results = []
    for pos, symbol in enumerate(symbols, 1):
        file = candles_dir / f'{symbol}_30m.csv'
        if not file.is_file():
            raise FileNotFoundError(file)
        candles = load_candles(file)
        for row in cohort.loc[cohort.symbol.eq(symbol)].to_dict('records'):
            item = dict(attempt_id=row['attempt_id'])
            for w in WINDOWS:
                original = row[f'pre_pattern_{w}']
                prefix = f'pre{w}_'
                check = (trendline_break(candles, row[f'{prefix}pivot_lows_json'],
                                         int(row['pre_closed_ts']), w, float(row['atr_pre']))
                         if original in ('ASCENDING_CLEAN', 'ASCENDING_CHOPPY')
                         and bool(row[f'pre_{w}_coverage'])
                         else dict(broken=False, anchor_first_ts=None, anchor_last_ts=None,
                                   anchor_confirmed_ts=None, broken_closed_ts=None,
                                   line_price_at_break=None, line_price_first=None,
                                   line_price_at_pre=None, break_depth_atr=None,
                                   slope_atr_per_bar=None))
                item[f'pre_pattern_v291_{w}'] = BROKEN if (original == 'ASCENDING_CLEAN' and check['broken']) else original
                for key, val in check.items():
                    item[f'trendline{w}_{key}'] = val
            results.append(item)
        print('HLL_V291_SYMBOL', json.dumps(dict(i=pos, symbol=symbol,
            attempts=int(cohort.symbol.eq(symbol).sum()))), flush=True)
    table = pd.DataFrame(results)
    if len(table) != int(cohort.symbol.isin(symbols).sum()) or table.attempt_id.duplicated().any():
        raise ValueError('Cardinalidad PRE inconsistente')
    return table


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument('--plan', action='store_true')
    g.add_argument('--run', action='store_true')
    parser.add_argument('--cohort', type=Path, default=COHORT)
    parser.add_argument('--candles-dir', type=Path, default=CANDLES)
    parser.add_argument('--output-dir', type=Path, default=OUTPUT)
    parser.add_argument('--max-symbols', type=int, default=0)
    args = parser.parse_args(argv)
    if args.max_symbols < 0:
        raise SystemExit('--max-symbols inválido')
    if not args.cohort.is_file():
        raise SystemExit('Falta el PRE V2.8.2: ' + str(args.cohort))
    cohort = pd.read_csv(args.cohort, low_memory=False)
    symbols = sorted(cohort.symbol.unique())
    if args.max_symbols:
        symbols = symbols[:args.max_symbols]
    plan = dict(version='HLL_V291_TRENDLINE_INTEGRITY', mode='OFFLINE_READ_ONLY',
                attempts=int(cohort.symbol.isin(symbols).sum()), symbols=len(symbols),
                windows=WINDOWS, threshold_breach_atr=ATR_BREACH,
                min_trend_span_bars=MIN_SPAN_BARS, original_pre_unchanged=True,
                no_live=True, output=str(args.output_dir))
    print('HLL_V291_PLAN', json.dumps(plan), flush=True)
    if args.plan:
        return 0
    output = args.output_dir.resolve()
    for inp in (args.cohort.resolve(), args.candles_dir.resolve()):
        if output == inp or inp in output.parents:
            raise SystemExit('No escribir dentro de insumos')
    if output.exists() and any(output.iterdir()):
        raise SystemExit('OUTPUT_EXISTS: no sobrescribir un run previo')
    if shutil.disk_usage(Path.cwd()).free < 150 * 1024 * 1024:
        raise SystemExit('Espacio insuficiente en disco')
    audited = audit_cohort(cohort, args.candles_dir, args.max_symbols)
    output.mkdir(parents=True, exist_ok=True)
    audited.to_csv(output / 'integrity_audit.csv', index=False)
    original = cohort.loc[cohort.symbol.isin(symbols)].set_index('attempt_id')
    revised = audited.set_index('attempt_id')
    summary = {}
    for w in WINDOWS:
        a = original[f'pre_pattern_{w}']
        b = revised[f'pre_pattern_v291_{w}']
        flag = revised[f'trendline{w}_broken']
        summary[str(w)] = dict(original_A=int(a.eq('ASCENDING_CLEAN').sum()),
            revised_A=int(b.eq('ASCENDING_CLEAN').sum()),
            A_reclassified=int((a.eq('ASCENDING_CLEAN') & b.eq(BROKEN)).sum()),
            flagged_ascending=int(flag.sum()))
    (output / 'manifest.json').write_text(json.dumps(dict(**plan,
        original_sha256=_hash(args.cohort), summary=summary,
        caution='Exploratory geometric integrity flag, not profitable signal; no re-simulation and no changes to original V2.8.2.'),
        indent=2, ensure_ascii=False), encoding='utf-8')
    print('HLL_V291_DONE', json.dumps(dict(attempts=len(audited), summary=summary,
        output=str(output)), ensure_ascii=False), flush=True)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
