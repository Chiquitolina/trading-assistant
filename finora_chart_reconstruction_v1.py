"""Build structural chart marks from closed hourly candles and a chosen cutoff."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
from pathlib import Path

import numpy as np
import pandas as pd

HOUR = 3_600_000
DAY = 24 * HOUR
MODEL_VERSION = '1.1'
BASE = Path('reports/reaction_lab/finora_reconstruction_v1')
FIELDS = ['timestamp', 'open', 'high', 'low', 'close']


@dataclass(frozen=True)
class Model:
    major_left_right: int = 3
    local_left_right: int = 2
    days: int = 12
    supply_count: int = 2
    merge_level_pct: float = 0.20
    supply_rule: str = 'structure_v11'
    neutral_body_fraction: float = 0.25
    max_supply_width_pct: float = 3.0


def stamp(value):
    date = pd.Timestamp(value)
    if date.tzinfo is None:
        raise ValueError('La hora necesita zona UTC')
    return int(date.timestamp() * 1000)


def utc(value):
    return pd.to_datetime(int(value), unit='ms', utc=True).strftime('%d %b %H:%M UTC')


def read_source(project, key):
    folder = Path(project) / BASE
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    source = next(row for row in manifest['datasets'] if row['key'] == key)
    path = folder / source['file']
    if path.parent != folder:
        raise ValueError('La fuente horaria debe estar dentro de su carpeta')
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != source['sha256']:
        raise ValueError('La fuente horaria difiere de su manifiesto')
    frame = pd.read_csv(io.BytesIO(content), float_precision='round_trip')
    return validate_hourly(frame), source


def validate_hourly(frame):
    if not set(FIELDS).issubset(frame.columns) or frame.empty:
        raise ValueError('Faltan las velas horarias de origen')
    out = frame[FIELDS].copy()
    for column in FIELDS:
        out[column] = pd.to_numeric(out[column], errors='raise')
    if (not np.isfinite(out.to_numpy(dtype=float)).all()
            or not out.timestamp.mod(HOUR).eq(0).all()
            or not out.timestamp.diff().iloc[1:].eq(HOUR).all()):
        raise ValueError('Reloj horario inválido, discontinuo o duplicado')
    out['timestamp'] = out.timestamp.astype('int64')
    prices = out[['open', 'high', 'low', 'close']]
    if (not prices.gt(0).all().all() or out.high.lt(prices.max(axis=1)).any()
            or out.low.gt(prices.min(axis=1)).any()):
        raise ValueError('OHLC horario inválido')
    out['available_ts'] = out.timestamp + HOUR
    return out.reset_index(drop=True)


def four_hour(hourly):
    grouped = hourly.groupby((hourly.timestamp // (4 * HOUR)) * 4 * HOUR, sort=True)
    result = grouped.agg(open=('open', 'first'), high=('high', 'max'), low=('low', 'min'),
                         close=('close', 'last'), count=('timestamp', 'size')).reset_index()
    result = result[result['count'].eq(4)].drop(columns='count')
    result['available_ts'] = result.timestamp + 4 * HOUR
    return result.reset_index(drop=True)


def points(hourly, bars, left_right, width):
    from engine.live.research.swing_detector import SwingDetector

    detector = SwingDetector(left_bars=left_right, right_bars=left_right)
    result = []
    for point in detector.detect_all(bars[FIELDS].to_dict('records')):
        where = hourly[(hourly.timestamp >= point.pivot_timestamp)
                       & (hourly.timestamp < point.pivot_timestamp + width)]
        field = 'high' if point.side == 'HIGH' else 'low'
        matches = where[where[field].eq(point.price)]
        row = dict(side=point.side, price=float(point.price), pivot_open=int(point.pivot_timestamp),
                   known_ts=int(point.confirmed_timestamp) + width, anchor_count=len(matches))
        row['anchor_ts'] = int(matches.timestamp.iloc[0]) if len(matches) == 1 else None
        result.append(row)
    return result


def structural_lines(major, merge_level_pct):
    if not major:
        return []
    highs = [point for point in major if point['side'] == 'HIGH']
    lows = [point for point in major if point['side'] == 'LOW']
    chosen = []
    if highs:
        high = max(highs, key=lambda row: (row['price'], row['known_ts']))
        chosen.append(('Máximo del tramo', high))
        before = [point for point in lows if point['pivot_open'] < high['pivot_open']]
        after = [point for point in lows if point['pivot_open'] > high['pivot_open']]
        if before:
            chosen.append(('Base anterior al máximo', min(before, key=lambda row: row['price'])))
        if after:
            chosen.append(('Mínimo posterior al máximo', min(after, key=lambda row: row['price'])))
        chosen.append(('Último máximo confirmado de 4h', max(highs, key=lambda row: row['pivot_open'])))
    if lows:
        chosen.append(('Mínimo del tramo', min(lows, key=lambda row: row['price'])))
        chosen.append(('Último mínimo confirmado de 4h', max(lows, key=lambda row: row['pivot_open'])))
    grouped = {}
    for role, point in chosen:
        key = point['side'], point['price']
        if key in grouped:
            grouped[key]['role'] += ' · ' + role
        else:
            grouped[key] = dict(point, role=role)
    merged = []
    for row in sorted(grouped.values(), key=lambda point: point['price']):
        match = next((point for point in merged if point['side'] == row['side']
                      and abs(point['price'] - row['price']) / min(point['price'], row['price']) * 100
                      <= merge_level_pct), None)
        if match is None:
            merged.append(row)
            continue
        roles = match['role'] + ' · ' + row['role']
        if row['known_ts'] > match['known_ts']:
            match.update(row)
        match['role'] = roles
    return sorted(merged, key=lambda row: row['price'])


def failed_at(hourly, side, low, high, born):
    later = hourly[hourly.timestamp >= born]
    invalid = later[later.close.lt(low) if side == 'BUY' else later.close.gt(high)]
    return None if invalid.empty else int(invalid.available_ts.iloc[0])


def demand_zone(hourly, major, last_price):
    lows = sorted((point for point in major if point['side'] == 'LOW'),
                  key=lambda row: row['pivot_open'], reverse=True)
    for point in lows:
        if point['anchor_count'] != 1:
            continue
        anchor = hourly[hourly.timestamp.eq(point['anchor_ts'])].iloc[0]
        lo, hi = float(anchor.low), float(anchor.high)
        if hi >= last_price or failed_at(hourly, 'BUY', lo, hi, point['known_ts']) is not None:
            continue
        return dict(side='BUY', low=lo, high=hi, entry_edge=hi, origin_ts=int(anchor.timestamp),
                    known_ts=point['known_ts'], rule='Vela horaria del mínimo confirmado de 4h',
                    gap_low=None, gap_high=None)
    return None


def supply_zones_v1(hourly, local, model, last_price):
    candidates = []
    bars = list(hourly.itertuples(index=False))
    # The last rising candle preceding a bearish gap is an explicit hypothesis.
    for i in range(1, len(bars) - 1):
        left, impulse, right = bars[i - 1:i + 2]
        if impulse.close >= impulse.open or right.high >= left.low:
            continue
        previous = [bar for bar in bars[max(0, i - 8):i] if bar.close > bar.open]
        if not previous:
            continue
        bar = previous[-1]
        candidates.append(dict(side='SELL', low=bar.low, high=bar.high, entry_edge=bar.low,
                               origin_ts=int(bar.timestamp), known_ts=int(right.available_ts),
                               rule='Última vela alcista antes de un hueco bajista',
                               gap_low=float(right.high), gap_high=float(left.low)))
    for point in local:
        if point['side'] != 'HIGH' or point['anchor_count'] != 1:
            continue
        bar = hourly[hourly.timestamp.eq(point['anchor_ts'])].iloc[0]
        candidates.append(dict(side='SELL', low=float(bar.low), high=float(bar.high), entry_edge=float(bar.low),
                               origin_ts=int(bar.timestamp), known_ts=point['known_ts'],
                               rule=f'Vela del máximo horario confirmado {model.local_left_right}×{model.local_left_right}',
                               gap_low=None, gap_high=None))
    start = int(hourly.available_ts.iloc[-1]) - model.days * DAY
    valid = [row for row in candidates if row['origin_ts'] >= start and row['low'] > last_price
             and failed_at(hourly, 'SELL', row['low'], row['high'], row['known_ts']) is None]
    # A fixed model preference for compact surviving zones. No reference prices enter here.
    valid.sort(key=lambda row: ((row['high'] - row['low']) / row['high'],
                               row['low'] - last_price, -row['known_ts']))
    selected = []
    for row in valid:
        if any(max(row['low'], old['low']) < min(row['high'], old['high']) for old in selected):
            continue
        selected.append(row)
        if len(selected) == model.supply_count:
            break
    return selected


def bearish_gaps(hourly):
    """Each gap is available only at the close of its third candle."""
    bars = list(hourly.itertuples(index=False))
    result = []
    for i in range(1, len(bars) - 1):
        left, middle, right = bars[i - 1:i + 2]
        if middle.close >= middle.open or right.high >= left.low:
            continue
        up = [bar for bar in bars[max(0, i - 8):i] if bar.close > bar.open]
        later = hourly[hourly.timestamp >= right.available_ts]
        filled = later[later.high >= left.low]
        result.append(dict(low=float(right.high), high=float(left.low),
                           origin_ts=int(left.timestamp), impulse_ts=int(middle.timestamp),
                           known_ts=int(right.available_ts),
                           filled_ts=None if filled.empty else int(filled.available_ts.iloc[0]),
                           base_ts=int(left.timestamp), last_up_ts=int(up[-1].timestamp) if up else None))
    return result


def supply_v11(hourly, major, local, model, last_price):
    """Experimental compact bases, one representative per confirmed 4h downswing.

    Parameters are shared by all instruments. Neither symbols nor reference
    prices enter the detector. Highest and latest eligible contexts are retained.
    """
    gaps = bearish_gaps(hourly)
    lookup = {int(bar.timestamp): bar for bar in hourly.itertuples(index=False)}
    highs = sorted((p for p in major if p['side'] == 'HIGH'), key=lambda p: p['anchor_ts'])
    lows = [p for p in major if p['side'] == 'LOW']
    representatives = []
    start = int(hourly.available_ts.iloc[-1]) - model.days * DAY

    def candidate(origin, formed, context, rule, gap=None):
        bar = lookup[origin]
        lo, hi = float(bar.low), float(bar.high)
        if (origin < start or hi <= lo or lo <= last_price
                or (hi - lo) / hi * 100 > model.max_supply_width_pct
                or failed_at(hourly, 'SELL', lo, hi, formed) is not None):
            return None
        return dict(side='SELL', low=lo, high=hi, entry_edge=lo, origin_ts=origin,
                    formed_ts=formed, known_ts=max(formed, context), rule=rule,
                    gap_low=None if gap is None else gap['low'],
                    gap_high=None if gap is None else gap['high'])

    for peak in highs:
        later_lows = [p for p in lows if p['anchor_ts'] > peak['anchor_ts']]
        end = min(later_lows, key=lambda p: p['anchor_ts']) if later_lows else None
        until = end['anchor_ts'] if end else int(hourly.available_ts.iloc[-1])
        evidence_ts = max(peak['known_ts'], end['known_ts'] if end else 0)
        leg = [g for g in gaps if peak['anchor_ts'] <= g['impulse_ts'] <= until]
        neutral, opposing = [], []
        for gap in leg:
            bar = lookup[gap['base_ts']]
            fraction = abs(bar.close - bar.open) / (bar.high - bar.low) if bar.high > bar.low else 0.0
            if fraction <= model.neutral_body_fraction:
                row = candidate(gap['base_ts'], gap['known_ts'], evidence_ts,
                                'Base neutra anterior a hueco bajista · tramo 4h', gap)
                if row is not None:
                    neutral.append(row)
            if gap['last_up_ts'] is not None:
                row = candidate(gap['last_up_ts'], gap['known_ts'], evidence_ts,
                                'Última vela alcista anterior a hueco bajista · tramo 4h', gap)
                if row is not None:
                    opposing.append(row)
        caps = []
        for point in local:
            if (point['side'] != 'HIGH' or point['anchor_count'] != 1
                    or not peak['anchor_ts'] - 8 * HOUR <= point['anchor_ts'] <= peak['anchor_ts']
                    or abs(point['price'] - peak['price']) / peak['price'] * 100 > model.merge_level_pct):
                continue
            row = candidate(point['anchor_ts'], point['known_ts'], evidence_ts,
                            'Vela compacta del máximo horario junto al máximo 4h')
            if row is not None:
                caps.append(row)
        if neutral:
            chosen = min(neutral, key=lambda row: (row['formed_ts'], row['origin_ts']))
        elif caps:
            chosen = min(caps, key=lambda row: ((row['high'] - row['low']) / row['high'], row['origin_ts']))
        elif opposing:
            chosen = min(opposing, key=lambda row: (row['formed_ts'], row['origin_ts']))
        else:
            continue
        chosen.update(peak_price=peak['price'], peak_ts=peak['anchor_ts'],
                      context_known_ts=evidence_ts, context_end_ts=until)
        representatives.append(chosen)

    selected = []
    if representatives:
        dominant = max(representatives, key=lambda row: (row['peak_price'], -row['peak_ts']))
        recent = max(representatives, key=lambda row: row['peak_ts'])
        order = [dominant, recent] + sorted(representatives, key=lambda row: row['peak_ts'], reverse=True)
        for row in order:
            if any(max(row['low'], old['low']) < min(row['high'], old['high']) for old in selected):
                continue
            selected.append(dict(row))
            if len(selected) == model.supply_count:
                break

    nearby = []
    for gap in gaps:
        if gap['filled_ts'] is not None:
            continue
        for zone in selected:
            distance = max(0., gap['low'] - zone['high'], zone['low'] - gap['high'])
            if (zone['peak_ts'] <= gap['impulse_ts'] <= zone['context_end_ts']
                    and distance <= zone['high'] - zone['low']):
                nearby.append(dict(gap, formed_ts=gap['known_ts'],
                                   known_ts=max(gap['known_ts'], zone['context_known_ts'])))
                break
    for row in representatives:
        row['selected'] = any(row['peak_ts'] == x['peak_ts'] and row['origin_ts'] == x['origin_ts'] for x in selected)
    return selected, nearby, representatives


def reconstruct(frame, cutoff, model=Model()):
    if (not 1 <= model.major_left_right <= 8 or not 1 <= model.local_left_right <= 8
            or not 2 <= model.days <= 30 or not 1 <= model.supply_count <= 4
            or not 0 <= model.merge_level_pct <= 1
            or model.supply_rule not in ('structure_v11', 'compact_v1')
            or not 0 <= model.neutral_body_fraction <= 1
            or not 0 < model.max_supply_width_pct <= 100):
        raise ValueError('Parámetros de reconstrucción inválidos')
    frame = validate_hourly(frame)
    if not int(frame.timestamp.iloc[0]) < cutoff <= int(frame.available_ts.iloc[-1]):
        raise ValueError('Corte fuera de la cobertura horaria')
    hourly = frame[frame.available_ts <= int(cutoff)].copy()
    if len(hourly) < (2 * model.major_left_right + 1) * 4:
        raise ValueError('Faltan velas cerradas para confirmar la estructura de 4h')
    start = max(int(hourly.timestamp.iloc[0]), int(cutoff) - model.days * DAY)
    major = points(hourly, four_hour(hourly), model.major_left_right, 4 * HOUR)
    major = [point for point in major if point['anchor_ts'] is not None and point['anchor_ts'] >= start]
    local = points(hourly, hourly, model.local_left_right, HOUR)
    local = [point for point in local if point['anchor_ts'] is not None and point['anchor_ts'] >= start]
    last_price = float(hourly.close.iloc[-1])
    lines = structural_lines(major, model.merge_level_pct)
    demand = demand_zone(hourly, major, last_price)
    if model.supply_rule == 'structure_v11':
        zones, gaps, candidates = supply_v11(hourly, major, local, model, last_price)
    else:
        zones, gaps, candidates = supply_zones_v1(hourly, local, model, last_price), [], []
    if demand is not None:
        zones.append(demand)
    return dict(cutoff=int(cutoff), start=start, last_price=last_price, lines=lines, zones=zones,
                bars=hourly[hourly.timestamp >= start].reset_index(drop=True),
                major_pivots=major, supply_rule=model.supply_rule, gaps=gaps, supply_candidates=candidates)
