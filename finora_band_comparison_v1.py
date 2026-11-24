"""Compare dated public chart levels with the lab's confirmed geometry in offline replay."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import re
import shutil
import tempfile
from dataclasses import dataclass, replace

import pandas as pd

from multisymbol_band_reaction_pilot_v1 import (
    Config, M, build_geometry, closed_bars, digest, iso, ms, validate_minute,
)


BASE = Path('reports/reaction_lab/finora_comparison_v1')
DEFAULT_RUN = 'binance_nov20_24_v1'
VERSION = 'finora-comparison-v1'
MARKET = 'BINANCE_USDT_M_PERPETUAL'
TIMEFRAMES = {'5m': 5, '15m': 15, '30m': 30, '1h': 60, '4h': 240, '1D': 1440}
PIVOT_WINDOW = 5
HISTORY_BARS = 250
REPLAY_HOURS_BEFORE = 96
FAMILY_ORDER = ('SWING_5X5', 'HLL', 'MULTI_SWING', 'FVG', 'FVG_BOS', 'OB_BOS')
MATCH_COLS = ('family', 'timeframe', 'rank', 'candidate_id', 'side', 'low', 'high',
              'origin_ts', 'available_ts', 'invalidated_pre_ts',
              'error_low', 'error_high', 'error_max_bps', 'iou', 'covers_level', 'exact')


@dataclass(frozen=True)
class Reference:
    case_id: str
    symbol: str
    timeframe: str
    asof_ts: int
    kind: str
    label: str
    low: float
    high: float
    source_url: str
    context: str
    pivot_open_ts: int | None


@dataclass(frozen=True)
class Candidate:
    family: str
    timeframe: str
    candidate_id: str
    side: str
    low: float
    high: float
    origin_ts: int
    available_ts: int
    invalidated_pre_ts: int | None = None


def load_references(path):
    output = {}
    for row in pd.read_csv(path, dtype=str, keep_default_na=False).to_dict('records'):
        identifier, symbol, tf = (row[k] for k in ('case_id', 'symbol', 'timeframe'))
        if (not re.fullmatch(r'[a-z0-9_]+', identifier) or identifier in output
                or not re.fullmatch(r'[A-Z0-9]{2,32}USDT', symbol) or tf not in TIMEFRAMES):
            raise ValueError(f'Referencia inválida o repetida: {identifier}')
        lo, hi = float(row['low']), float(row['high'])
        kind = row['kind']
        at = ms(row['asof_utc'])
        pivot = ms(row['pivot_open_utc']) if row['pivot_open_utc'] else None
        if (not math.isfinite(lo) or not math.isfinite(hi) or lo <= 0 or hi < lo
                or kind not in ('LEVEL', 'ZONE') or (kind == 'LEVEL') != (lo == hi)
                or at % M or (pivot is not None and (kind != 'LEVEL' or pivot >= at
                                                    or pivot % (TIMEFRAMES[tf] * M)))):
            raise ValueError(f'Reloj o precio inválido: {identifier}')
        output[identifier] = Reference(identifier, symbol, tf, at, kind, row['label'], lo, hi,
                                       row['source_url'], row['context'], pivot)
    if not output:
        raise ValueError('Se necesita al menos una referencia fechada')
    return output


def load_minutes(path, source):
    if (source['market'] != MARKET or source['timeframe'] != '1m'
            or source['filename'] != path.name or digest(path) != source['sha256']):
        raise ValueError(f'Mercado, escala o hash incorrecto: {path.name}')
    minutes = validate_minute(pd.read_csv(path, float_precision='round_trip'))
    if (int(minutes.timestamp.iloc[0]) != source['start_ms']
            or int(minutes.timestamp.iloc[-1]) + M != source['end_exclusive_ms']):
        raise ValueError(f'Cobertura distinta del manifiesto: {path.name}')
    return minutes


def load_native_4h(data_dir, source, minutes):
    path = data_dir / f"{source['symbol']}_4h.csv"
    if not path.is_file():
        return None
    spec = json.loads((data_dir / 'fuentes_4h.json').read_text(encoding='utf-8'))
    if (spec['filename'] != path.name or spec['symbol'] != source['symbol']
            or spec['market'] != MARKET or spec['timeframe'] != '4h' or digest(path) != spec['sha256']):
        raise ValueError(f'Velas nativas de 4h inválidas: {path.name}')
    frame = pd.read_csv(path, float_precision='round_trip')
    width = TIMEFRAMES['4h'] * M
    if not frame.timestamp.diff().iloc[1:].eq(width).all() or not frame.timestamp.mod(width).eq(0).all():
        raise ValueError(f'Velas de 4h desalineadas: {path.name}')
    frame['available_ts'] = frame.timestamp + width
    aggregated = closed_bars(minutes, TIMEFRAMES['4h'])
    if aggregated.empty:
        raise ValueError('Falta cobertura para cotejar las velas nativas de 4h')
    native_overlap = frame[frame.timestamp.isin(aggregated.timestamp)].reset_index(drop=True)
    # The overlapping closed bars must agree on open, high, low and close.
    if not native_overlap[list(aggregated.columns)].equals(aggregated):
        raise ValueError(f'Velas nativas de 4h difieren del agregado: {path.name}')
    return frame


def detect_structure(bars, tf):
    from engine.live.research.swing_detector import SwingDetector

    candles = bars.to_dict('records')
    width = TIMEFRAMES[tf] * M
    points = SwingDetector(left_bars=PIVOT_WINDOW, right_bars=PIVOT_WINDOW).detect_all(candles)
    pivots = {}
    for point in points:
        pivots.setdefault(int(point.confirmed_timestamp) + width, []).append(point)
    swings, breaks = [], {}
    last = {}
    consumed = set()
    trend = None
    for i, bar in enumerate(candles):
        for side, direction in (('HIGH', 'UP'), ('LOW', 'DOWN')):
            point = last.get(side)
            if point is None or point.candidate_id in consumed:
                continue
            crossed = bar['close'] > point.high if direction == 'UP' else bar['close'] < point.low
            if crossed:
                kind = 'BOS' if trend == direction else ('CHOCH' if trend else 'INITIAL')
                breaks[i] = (kind, direction)
                trend = direction
                consumed.add(point.candidate_id)
        # A break may use the swing only after its confirmation close is known.
        for point in pivots.get(int(bar['available_ts']), []):
            swing = Candidate('SWING_5X5', tf, f'{tf}:{point.side}:{point.pivot_timestamp}', point.side,
                              point.price, point.price, int(point.pivot_timestamp), int(bar['available_ts']))
            last[point.side] = swing
            swings.append(swing)
    return swings, breaks


def detect_impulse_zones(bars, tf, breaks):
    candles = list(bars.itertuples(index=False))
    zones = []
    for i in range(1, len(candles)):
        left, middle = candles[i - 1], candles[i]
        if middle.close == middle.open:
            continue
        side = 'UP' if middle.close > middle.open else 'DOWN'
        bos = breaks.get(i) == ('BOS', side)
        bullish = side == 'UP'
        oppose = left.close < left.open if bullish else left.close > left.open
        if bos and oppose and left.high > left.low:
            zones.append(Candidate('OB_BOS', tf, f'{tf}:OB:{middle.timestamp}', side, left.low, left.high,
                                   int(left.timestamp), int(middle.available_ts)))
        if i + 1 == len(candles):
            continue
        third = candles[i + 1]
        lo, hi = (left.high, third.low) if bullish else (third.high, left.low)
        if hi > lo:
            family = 'FVG_BOS' if bos else 'FVG'
            zones.append(Candidate(family, tf, f'{tf}:FVG:{middle.timestamp}', side, lo, hi,
                                   int(left.timestamp), int(third.available_ts)))
    return zones


def impulse_status(zone, bars):
    later = bars[bars.timestamp >= zone.available_ts]
    beyond = later.close < zone.low if zone.side == 'UP' else later.close > zone.high
    invalid = later.loc[beyond, 'available_ts']
    return None if invalid.empty else int(invalid.iloc[0])


def own_geometries(minutes, symbol):
    end = int(minutes.timestamp.iloc[-1]) + M
    start = int(minutes.timestamp.iloc[0]) + Config().warmup_hours * 60 * M
    _, _, stages, _ = build_geometry(minutes, symbol, start, end, Config(symbols=(symbol,)))
    return stages


def available_stages(stages, cutoff):
    selected = stages[stages.known_from <= cutoff].sort_values(
        ['known_from', 'independent_extrema'], kind='stable')
    return [Candidate('MULTI_SWING', '', str(row.region_id), row.side, row.band_low, row.band_high,
                      int(row.known_from), int(row.known_from))
            for row in selected.drop_duplicates('region_id', keep='last').itertuples(index=False)]


def lab_horizontals(hll_root, symbol, source):
    directory = hll_root / symbol
    manifest_path = directory / 'manifest.json'
    if not manifest_path.is_file():
        return [], 'MISSING_ATLAS_EXPORT'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    path = directory / 'formations.csv'
    if (manifest['symbol'] != symbol or manifest['input_1m_sha256'] != source['sha256']
            or digest(path) != manifest['formations_sha256']):
        raise ValueError(f'El atlas HLL no corresponde a las velas de {symbol}')
    rows = pd.read_csv(path, float_precision='round_trip')
    required = {'level_id', 'symbol', 'origin', 'center', 'formation_closed_ts', 'second_pivot_open_ts'}
    if not required.issubset(rows.columns) or not rows.symbol.eq(symbol).all():
        raise ValueError(f'Esquema HLL inválido: {symbol}')
    lines = []
    for row in rows.itertuples(index=False):
        seen, pivot = int(row.formation_closed_ts), int(row.second_pivot_open_ts)
        if (not math.isfinite(row.center) or row.center <= 0 or pivot >= seen
                or seen > source['end_exclusive_ms'] or seen % (30 * M)):
            raise ValueError(f'Formación HLL inválida: {row.level_id}')
        lines.append(Candidate('HLL', '30m', row.level_id, row.origin, row.center, row.center, pivot, seen))
    return lines, 'FORMATIONS_VERIFIED'


def score(reference, candidate):
    lo_error = candidate.low - reference.low
    hi_error = candidate.high - reference.high
    if reference.kind == 'ZONE':
        union = max(reference.high, candidate.high) - min(reference.low, candidate.low)
        overlap = max(0., min(reference.high, candidate.high) - max(reference.low, candidate.low))
        covers = None
        iou = overlap / union
    else:
        covers = candidate.low <= reference.low <= candidate.high
        iou = None
    worst = max(abs(lo_error), abs(hi_error))
    return dict(vars(candidate), error_low=lo_error, error_high=hi_error,
                error_max_bps=worst / ((reference.low + reference.high) / 2) * 10_000,
                iou=iou, covers_level=covers, exact=worst <= 1e-10)


def compare(reference, candidates):
    rows = [score(reference, c) for c in candidates if c.available_ts <= reference.asof_ts]
    rows.sort(key=lambda r: (r['family'], r['timeframe'], r['error_max_bps'],
                             -r['available_ts'], r['candidate_id']))
    ranks = {}
    for row in rows:
        key = row['family'], row['timeframe']
        ranks[key] = ranks.get(key, 0) + 1
        row['rank'] = ranks[key]
    return rows


def first_contact(minutes, low, high, start, end):
    window = minutes[(minutes.timestamp >= start) & (minutes.timestamp + M <= end)]
    hits = window[(window.low <= high) & (window.high >= low)]
    return None if hits.empty else int(hits.timestamp.iloc[0]) + M


def match_published_swing(reference, swings):
    if reference.pivot_open_ts is None:
        return None
    return any(s.origin_ts == reference.pivot_open_ts
               and reference.low == s.low and s.available_ts <= reference.asof_ts for s in swings)


def evaluate_case(reference, minutes, stages, hll_lines, hll_status, native_4h=None):
    at = reference.asof_ts
    close_time = int(minutes.timestamp.iloc[-1]) + M
    if not (int(minutes.timestamp.iloc[0]) + 11 * TIMEFRAMES[reference.timeframe] * M <= at <= close_time):
        raise ValueError(f'Cobertura insuficiente para {reference.case_id}')
    candidates = [*available_stages(stages, at), *hll_lines]
    # Smaller timeframes are recorded as diagnostics with their own clocks.
    compared = (tuple(dict.fromkeys((reference.timeframe, '30m', '15m', '5m')))
                if reference.kind == 'ZONE' else (reference.timeframe,))
    coverage = {}
    for tf in compared:
        if tf == '4h' and native_4h is not None:
            bars = native_4h[native_4h.available_ts <= at].tail(HISTORY_BARS)
        else:
            bars = closed_bars(minutes, TIMEFRAMES[tf], end=at).tail(HISTORY_BARS)
        coverage[tf] = len(bars)
        swings, breaks = detect_structure(bars, tf)
        if tf == reference.timeframe:
            candidates.extend(swings)
            swing_exact = match_published_swing(reference, swings)
        if reference.kind == 'ZONE':
            for zone in detect_impulse_zones(bars, tf, breaks):
                invalid = impulse_status(zone, bars)
                candidates.append(replace(zone, invalidated_pre_ts=invalid))
    matches = compare(reference, candidates)
    last_minute = minutes[minutes.timestamp + M <= at].iloc[-1]
    last_tf_close = (at // (TIMEFRAMES[reference.timeframe] * M)) * TIMEFRAMES[reference.timeframe] * M
    # The publication time is reported to the minute. Use only later minute opens.
    contact = first_contact(minutes, reference.low, reference.high, at + M, close_time)
    candidate_counts = {}
    exact_counts = {}
    for row in matches:
        key = ' '.join((row['family'], row['timeframe'])).strip()
        candidate_counts[key] = candidate_counts.get(key, 0) + 1
        exact_counts[key] = exact_counts.get(key, 0) + int(row['exact'])
    meta = dict(vars(reference), data_end_ts=close_time, last_tf_close_ts=last_tf_close,
                last_1m_close=float(last_minute.close), coverage=coverage, hll_status=hll_status,
                exact_swing_at_expected_time=swing_exact, counts=candidate_counts, exact_counts=exact_counts)
    post = dict(checked_through_ts=close_time, first_contact_closed_ts=contact, closes_5m=[])
    if contact is not None:
        for bar in closed_bars(minutes, 5, start=contact, end=close_time).head(3).itertuples(index=False):
            relation = 'ABOVE' if bar.close > reference.high else ('BELOW' if bar.close < reference.low else 'INSIDE')
            post['closes_5m'].append(dict(closed_ts=int(bar.available_ts), close=float(bar.close), relation=relation))
    return matches, meta, post


def write_rows(path, fields, rows):
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def build(root, dataset, run_tag, selected=None):
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', run_tag):
        raise ValueError('Nombre de corrida inválido')
    output = Path(root) / BASE / 'runs' / run_tag
    if output.exists():
        raise FileExistsError(output)
    references_path = dataset / 'referencias.csv'
    references = load_references(references_path)
    if selected is not None:
        references = {selected: references[selected]}
    sources_path = dataset / 'datos' / 'fuentes.json'
    source_rows = json.loads(sources_path.read_text(encoding='utf-8'))
    sources = {s['symbol']: s for s in source_rows}
    if len(sources) != len(source_rows):
        raise ValueError('Símbolos duplicados en el manifiesto de velas')
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f'.{run_tag}-', dir=output.parent))
    rendered = []
    try:
        for symbol in dict.fromkeys(r.symbol for r in references.values()):
            source = sources[symbol]
            minutes = load_minutes(dataset / 'datos' / f'{symbol}_1m.csv', source)
            stages = own_geometries(minutes, symbol)
            lines, hll_status = lab_horizontals(dataset / 'hll', symbol, source)
            native_4h = load_native_4h(dataset / 'datos', source, minutes)
            for reference in (case for case in references.values() if case.symbol == symbol):
                rows, meta, post = evaluate_case(reference, minutes, stages, lines, hll_status, native_4h)
                folder = staging / reference.case_id
                folder.mkdir()
                write_rows(folder / 'candidatos.csv', MATCH_COLS, rows)
                (folder / 'referencia.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
                (folder / 'post.json').write_text(json.dumps(post, indent=2), encoding='utf-8')
                window = int(reference.asof_ts) - REPLAY_HOURS_BEFORE * 60 * M
                candles = minutes[minutes.timestamp >= window]
                candles.to_csv(folder / 'velas_1m.csv', index=False)
                rendered.append(reference.case_id)
                print(f'{reference.case_id} | {iso(reference.asof_ts)} | {len(rows)} candidatos', flush=True)
        manifest = {'version': VERSION, 'cases': rendered, 'market': MARKET,
                    'reference_sha256': digest(references_path), 'input_manifest_sha256': digest(sources_path),
                    'market_sha256': {symbol: sources[symbol]['sha256']
                                      for symbol in dict.fromkeys(r.symbol for r in references.values())},
                    'files_sha256': {str(p.relative_to(staging)): digest(p)
                                     for p in sorted(staging.rglob('*')) if p.is_file()}}
        (staging / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        # A competing completed run contains files, so rename cannot replace it.
        staging.rename(output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path('.'))
    parser.add_argument('--dataset', type=Path, help='Carpeta con referencias.csv, datos/ y opcionalmente hll/')
    parser.add_argument('--run-tag', default=DEFAULT_RUN)
    parser.add_argument('--case', help='Reproduce un solo caso por case_id')
    args = parser.parse_args(argv)
    try:
        out = build(args.project, args.dataset or (args.project / BASE), args.run_tag, args.case)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(2, f'{exc}\n')
    print(out)


if __name__ == '__main__':
    main()
