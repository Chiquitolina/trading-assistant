#!/usr/bin/env python3
"""Offline multi-symbol band tests. New research definition; never alters REACTION.

Inputs are existing contiguous 1m CSVs or the verified BTC freeze. Geometry is
the existing six-TF Multi-Swing V2 algorithm, with symbol-specific identities.
All event, reference and outcome clocks are separate. No order simulation.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile

import numpy as np
import pandas as pd

M = 60_000
H = 60 * M
VERSION = 'multisymbol-band-response-v1.0'
OUTPUT = Path('reports/reaction_lab/multisymbol_band_response_v1/runs')
MINUTES = Path('reports/horizontal_level_full290_v301/minutes')
SYMBOLS = ('BTCUSDT', 'BERAUSDT', 'CGPTUSDT', 'DYDXUSDT', 'CAKEUSDT')
FIELDS = ['timestamp', 'open', 'high', 'low', 'close']
TEST_FIELDS = ['test_id', 'symbol', 'region_id', 'geometry_id', 'interaction_group_id',
               'band_origin', 'geometry_known_ts', 'band_low', 'band_high',
               'hour_open_ts', 'first_touch_open_ts', 'available_ts',
               'prior_h1_side', 'prior_1m_side', 'approach_side']
EVENT_FIELDS = ['test_id', 'symbol', 'region_id', 'event', 'available_ts',
                'price', 'side', 'direction', 'evidence_closed_ts']
REFERENCE_FIELDS = ['test_id', 'symbol', 'region_id', 'signal', 'signal_ts',
                    'available_ts', 'price', 'direction', 'reference_side']
OUTCOME_FIELDS = ['test_id', 'symbol', 'signal', 'signal_ts', 'reference_ts',
                  'horizon_minutes', 'available_ts', 'state', 'markout_pct',
                  'mfe_pct', 'mae_pct']


def iso(ts):
    return datetime.fromtimestamp(int(ts) / 1000, timezone.utc).isoformat()


def ms(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('TIMEZONE_REQUIRED')
    return int(dt.timestamp() * 1000)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def identity(*parts):
    return hashlib.sha256('|'.join(map(str, parts)).encode()).hexdigest()[:24]


@dataclass(frozen=True)
class Config:
    symbols: tuple[str, ...] = SYMBOLS
    # Match the V2.4A/V1.3 audited band universe. The catalog still contains 6TF;
    # adding 1m/5m here would silently create a different set of bands.
    band_timeframes: tuple[str, ...] = ('15m', '30m', '1h', '4h')
    start: int = ms('2026-09-09T01:00:00Z')
    end: int = ms('2026-11-08T01:00:00Z')
    warmup_hours: int = 48
    tolerance_pct: float = 0.20
    lookback_days: int = 7
    min_separation_minutes: int = 60
    followup_hours: int = 12
    horizons_minutes: tuple[int, ...] = (30, 60, 180, 720)

    def validate(self):
        if (not self.symbols or len(set(self.symbols)) != len(self.symbols)
                or any(not re.fullmatch(r'[A-Z0-9]{2,32}', s) for s in self.symbols)):
            raise ValueError('INVALID_SYMBOLS')
        if (self.start >= self.end or self.start % H or self.end % H
                or self.warmup_hours < 24 or self.warmup_hours % 4 or self.followup_hours < 1
                or not self.horizons_minutes or min(self.horizons_minutes) < 1):
            raise ValueError('INVALID_WINDOW_OR_HORIZONS')
        if (not self.band_timeframes or len(set(self.band_timeframes)) != len(self.band_timeframes)
                or any(tf not in ('1m','5m','15m','30m','1h','4h') for tf in self.band_timeframes)):
            raise ValueError('INVALID_BAND_TIMEFRAMES')


def validate_minute(frame):
    if not set(FIELDS).issubset(frame.columns) or frame.empty:
        raise ValueError('INVALID_1M_SCHEMA_OR_EMPTY')
    out = frame[FIELDS].copy()
    for col in FIELDS:
        out[col] = pd.to_numeric(out[col], errors='raise')
    vals = out.to_numpy(dtype=float)
    if not np.isfinite(vals).all():
        raise ValueError('NONFINITE_1M')
    stamps = out.timestamp.to_numpy(dtype=float)
    if (stamps != np.floor(stamps)).any() or (stamps % M).any():
        raise ValueError('NONINTEGER_OR_UNALIGNED_1M')
    out['timestamp'] = out.timestamp.astype('int64')
    if (np.diff(out.timestamp.to_numpy()) != M).any():
        raise ValueError('GAP_DUPLICATE_OR_ORDER_1M')
    prices = out[['open', 'high', 'low', 'close']]
    if ((prices <= 0).any().any()
            or (out.high < prices.max(axis=1)).any()
            or (out.low > prices.min(axis=1)).any()):
        raise ValueError('INVALID_OHLC')
    return out.reset_index(drop=True)


def closed_bars(frame, width_minutes, start=None, end=None):
    """Integer UTC buckets; complete bars only, also works with pandas 3."""
    width = int(width_minutes) * M
    if width <= 0:
        raise ValueError('INVALID_TF')
    if frame.empty:
        return pd.DataFrame(columns=[*FIELDS, 'available_ts'])
    visible = frame if end is None else frame[frame.timestamp + M <= int(end)]
    grouped = visible.assign(bucket=(visible.timestamp // width) * width).groupby('bucket')
    bars = grouped.agg(open=('open', 'first'), high=('high', 'max'),
                       low=('low', 'min'), close=('close', 'last'),
                       count=('timestamp', 'size'), first=('timestamp', 'min'),
                       last=('timestamp', 'max')).reset_index()
    valid = ((bars['count'] == width_minutes) & (bars['first'] == bars.bucket)
             & (bars['last'] == bars.bucket + width - M))
    bars = bars[valid].rename(columns={'bucket': 'timestamp'})
    if start is not None:
        bars = bars[bars.timestamp >= int(start)]
    bars['available_ts'] = bars.timestamp + width
    return bars[[*FIELDS, 'available_ts']].reset_index(drop=True)


def load_source(root, symbol, candles_dir=None):
    root = Path(root)
    if candles_dir is None and symbol == 'BTCUSDT':
        from btc_multiswing_60d_explorer_v1 import FREEZE_REL, load_snapshot
        if (root / FREEZE_REL / 'manifest.json').is_file():
            frame, _, _ = load_snapshot(root)
            paths = [root / FREEZE_REL / p for p in
                     ('manifest.json', 'BTCUSDT_1m.csv', 'BTCUSDT_1m_warmup.csv')]
            return validate_minute(frame), {
                'kind': 'BTC_VERIFIED_FREEZE',
                'files_sha256': {str(p.relative_to(root)): digest(p) for p in paths}}
    path = (Path(candles_dir) if candles_dir else root / MINUTES) / f'{symbol}_1m.csv'
    if not path.is_file():
        raise FileNotFoundError(path)
    before = digest(path)
    frame = validate_minute(pd.read_csv(path, float_precision='round_trip'))
    if digest(path) != before:
        raise ValueError('SOURCE_CHANGED_WHILE_READING')
    return frame, {'kind': 'EXISTING_HLL_1M_CSV',
                   'files_sha256': {str(path.resolve()): before},
                   'provenance': 'LOCAL_CONTENT_HASH_NOT_ORIGINAL_MANIFEST_ATTESTATION'}


def preflight(root, config, candles_dir=None):
    config.validate()
    frames, sources, rows = {}, {}, []
    for symbol in config.symbols:
        try:
            frame, source = load_source(root, symbol, candles_dir)
            frames[symbol], sources[symbol] = frame, source
            rows.append({'symbol': symbol, 'status': 'READY', 'bars_1m': len(frame),
                         'source_start_utc': iso(frame.timestamp.iloc[0]),
                         'source_end_exclusive_utc': iso(frame.timestamp.iloc[-1] + M)})
        except FileNotFoundError as exc:
            rows.append({'symbol': symbol, 'status': 'MISSING_SOURCE', 'detail': str(exc)})
        except (ValueError, KeyError, OSError, pd.errors.ParserError) as exc:
            rows.append({'symbol': symbol, 'status': 'INVALID_SOURCE', 'detail': str(exc)})
    plan = {'version': VERSION, 'requested_start_utc': iso(config.start),
            'requested_end_exclusive_utc': iso(config.end), 'sources': rows,
            'ready': len(frames) == len(config.symbols)}
    if not plan['ready']:
        return plan, frames, sources
    # Align the main interval to H1 and the warmup origin to the UTC 4h grid.
    start = max(config.start, *[int(f.timestamp.iloc[0]) + config.warmup_hours * H
                               for f in frames.values()])
    start = ((start + H - 1) // H) * H
    end = min(config.end, *[int(f.timestamp.iloc[-1]) + M for f in frames.values()])
    end = (end // H) * H
    warm_start = ((start - config.warmup_hours * H + 4 * H - 1) // (4 * H)) * (4 * H)
    # Rounding warmup upward could remove up to three hours. Instead extend the
    # main start until a full requested warmup exists on an aligned 4h boundary.
    warm_start -= 4 * H if start - warm_start < config.warmup_hours * H else 0
    if any(warm_start < int(f.timestamp.iloc[0]) for f in frames.values()):
        start = ((start + 4 * H - 1) // (4 * H)) * (4 * H)
        warm_start = start - config.warmup_hours * H
        warm_start = (warm_start // (4 * H)) * (4 * H)
    if start >= end or end - start < 2 * H:
        plan.update(ready=False, reason='INSUFFICIENT_COMMON_HISTORY')
        return plan, frames, sources
    plan.update(start_ts=start, end_ts=end, warmup_start_ts=warm_start,
                common_start_utc=iso(start), common_end_exclusive_utc=iso(end),
                common_days=(end - start) / (24 * H),
                full_requested_interval=(start == config.start and end == config.end))
    return plan, frames, sources


def build_geometry(frame, symbol, start, end, config):
    from btc_multiswing_60d_catalog_v1 import CATALOG_FIELDS, detect_catalog, stable_id
    from btc_multiswing_structural_confluence_v2 import construct_regions
    records, _ = detect_catalog(frame[FIELDS].to_dict('records'), start, end)
    if symbol != 'BTCUSDT':
        for row in records:
            row['pivot_id'] = stable_id(symbol, row['timeframe'], row['side'], row['pivot_ts'])
            if row['physical_identity_state'] == 'VERIFIED':
                row['physical_id'] = stable_id(symbol, row['side'], row['physical_minute_ts'])
    catalog = pd.DataFrame(records, columns=CATALOG_FIELDS)
    regions, stages, audit = construct_regions(
        catalog[catalog.timeframe.isin(config.band_timeframes)], end, tolerance_pct=config.tolerance_pct,
        lookback_days=config.lookback_days,
        min_separation_minutes=config.min_separation_minutes, min_extrema=2)
    if symbol != 'BTCUSDT':
        mapping = {}
        for row in regions.itertuples(index=False):
            first, second = row.members.split(',')[:2]
            raw = f'{symbol}|MULTISWING_V2|{row.side}|{first}|{second}'
            mapping[row.region_id] = 'msv2_' + hashlib.sha256(raw.encode()).hexdigest()[:20]
        regions['region_id'] = regions.region_id.map(mapping)
        stages['region_id'] = stages.region_id.map(mapping)
    catalog['symbol'] = symbol
    regions['symbol'] = symbol
    stages['symbol'] = symbol
    # known_until is an original visual TTL, never a lifecycle or causality
    # boundary. This experiment uses successive known_from clocks only.
    stages = stages.drop(columns=['known_until'], errors='ignore')
    return catalog, regions, stages, audit


def btc_geometry_parity(root, stages, start, end, config):
    """If available, verify the exact causal prefix of the audited BTC bands."""
    default = Config()
    if (start != default.start or end > default.end or
            any(getattr(config,k) != getattr(default,k) for k in
                ('band_timeframes','tolerance_pct','lookback_days','min_separation_minutes'))):
        return {'status': 'NOT_APPLICABLE_DIFFERENT_INTERVAL_OR_GEOMETRY'}
    folder = Path(root)/'reports/reaction_lab/btc_multiswing_60d_v1/btc_sep09_nov08_structural_behavior_pre_safety_v121'
    name = 'geometric_stage_history_v121.csv'
    if not (folder/'manifest.json').is_file() or not (folder/name).is_file():
        return {'status': 'AUDITED_PARENT_NOT_PRESENT'}
    manifest = json.loads((folder/'manifest.json').read_text())
    if digest(folder/name) != manifest['files_sha256'][name]:
        raise ValueError('BTC_PARITY_PARENT_HASH_MISMATCH')
    old = pd.read_csv(folder/name, float_precision='round_trip')
    old = old[old.known_from <= end]
    keys = ['region_id','known_from','independent_extrema']
    old = old.sort_values(keys).reset_index(drop=True)
    new = stages.sort_values(keys).reset_index(drop=True)
    if (len(old) != len(new) or not old[keys+['side']].equals(new[keys+['side']])
            or not np.allclose(old[['band_low','band_high']], new[['band_low','band_high']],
                               rtol=0,atol=1e-8)):
        raise ValueError('AUDITED_BTC_BAND_GEOMETRY_NOT_REPRODUCED')
    return {'status': 'AUDITED_BTC_PREFIX_PARITY_OK', 'bands': int(new.region_id.nunique()),
            'stages':len(new), 'parent_sha256':digest(folder/name)}


def position(price, lower, upper):
    return 'BELOW' if price < lower else ('ABOVE' if price > upper else 'INSIDE')


def initial_state(approach, final_side, stable=True):
    if not stable:
        return 'GEOMETRY_CHANGED', 'NA'
    if approach == 'CONFLICT':
        return 'APPROACH_CONFLICT', 'NA'
    if approach == 'INSIDE':
        return ('ACCEPT_INSIDE_H1' if final_side == 'INSIDE' else f'EXIT_{final_side}_H1'), 'NA'
    if final_side == approach:
        return ('DEFENSE_ABOVE_H1', 'LONG') if approach == 'ABOVE' else ('REJECTION_BELOW_H1', 'SHORT')
    return ('ACCEPT_INSIDE_H1' if final_side == 'INSIDE' else 'BREAK_THROUGH_H1'), 'NA'


def measure_tests(frame, stages, symbol, start, end, config):
    """Band x actually touched closed H1; no future geometry at hour open."""
    minute = frame.set_index('timestamp', drop=False)
    h1 = closed_bars(frame, 60).set_index('timestamp', drop=False)
    h4 = closed_bars(frame, 240)
    tests, events, references, outcomes = [], [], [], []
    audit = {'h1_range_without_1m_touch': 0, 'lifecycle': 'KNOWN_GEOMETRY_NO_ACTIVE_CLAIM'}

    def emit(test, event, when, price, side='NA', direction='NA', evidence_closed_ts=None):
        events.append(dict(test_id=test['test_id'], symbol=symbol,
                           region_id=test['region_id'], event=event,
                           available_ts=int(when), price=float(price),
                           side=side, direction=direction,
                           evidence_closed_ts=int(when if evidence_closed_ts is None else evidence_closed_ts)))

    def add_reference(test, signal, when, direction):
        ref_ts = int(when) + M  # strictly later than signal, same execution convention
        if ref_ts not in minute.index:
            emit(test, f'{signal}_REFERENCE_CENSORED', when, float(minute.loc[when-M, 'close']))
            return
        price = float(minute.loc[ref_ts, 'open'])
        references.append(dict(test_id=test['test_id'], symbol=symbol,
            region_id=test['region_id'], signal=signal, signal_ts=int(when),
            available_ts=ref_ts, price=price, direction=direction,
            reference_side=position(price, test['band_low'], test['band_high'])))
        sign = 1 if direction == 'LONG' else -1
        for horizon in config.horizons_minutes:
            available = ref_ts + int(horizon) * M
            result = dict(test_id=test['test_id'], symbol=symbol, signal=signal,
                signal_ts=int(when), reference_ts=ref_ts, horizon_minutes=int(horizon),
                available_ts=min(available, end), state='CENSORED', markout_pct=None,
                mfe_pct=None, mae_pct=None)
            if available <= end:
                path = minute.loc[ref_ts:available-M]
                result.update(state='COMPLETE',
                    markout_pct=sign * (float(path.close.iloc[-1]) / price - 1) * 100,
                    mfe_pct=max(0., (float(path.high.max())/price-1)*100) if sign > 0 else
                            max(0., (1-float(path.low.min())/price)*100),
                    mae_pct=max(0., (1-float(path.low.min())/price)*100) if sign > 0 else
                            max(0., (float(path.high.max())/price-1)*100))
            outcomes.append(result)

    for rid, part in stages.groupby('region_id', sort=True):
        part = part.sort_values('known_from').reset_index(drop=True)
        clocks = part.known_from.to_numpy(dtype='int64')
        last_hour, last_geom, group_id = None, None, None

        def stable_until(index, when):
            last = int(np.searchsorted(clocks, when, side='right'))
            base = part.iloc[index]
            relevant = part.iloc[index:last]
            return bool(((relevant.band_low == base.band_low) &
                         (relevant.band_high == base.band_high)).all())

        for bar in h1.itertuples(index=False):
            hour, close_ts = int(bar.timestamp), int(bar.available_ts)
            if hour < start or close_ts > end or hour < clocks[0]:
                continue
            idx = int(np.searchsorted(clocks, hour, side='right')) - 1
            stage = part.iloc[idx]
            lo, hi = float(stage.band_low), float(stage.band_high)
            if bar.high < lo or bar.low > hi:
                continue
            segment = minute.loc[hour:close_ts-M]
            touches = segment[(segment.high >= lo) & (segment.low <= hi)]
            if touches.empty:
                audit['h1_range_without_1m_touch'] += 1
                continue
            touch = int(touches.timestamp.iloc[0])
            prior_hour = hour-H
            if prior_hour not in h1.index or touch-M not in minute.index:
                continue
            prior_h1 = position(float(h1.loc[prior_hour, 'close']), lo, hi)
            prior_1m = position(float(minute.loc[touch-M, 'close']), lo, hi)
            approach = prior_h1 if prior_h1 == prior_1m else 'CONFLICT'
            geom = identity(symbol, rid, lo, hi)
            tid = 'brh1_' + identity(VERSION, symbol, rid, geom, hour)
            if last_hour != hour-H or last_geom != geom:
                group_id = 'bri_' + identity(symbol, rid, geom, hour)
            last_hour, last_geom = hour, geom
            test = dict(test_id=tid, symbol=symbol, region_id=rid, geometry_id=geom,
                interaction_group_id=group_id, band_origin=str(stage.side),
                geometry_known_ts=int(stage.known_from), band_low=lo, band_high=hi,
                hour_open_ts=hour, first_touch_open_ts=touch, available_ts=touch+M,
                prior_h1_side=prior_h1, prior_1m_side=prior_1m, approach_side=approach)
            tests.append(test)
            emit(test, 'CONTACT_1M_IN_H1', touch+M, float(minute.loc[touch, 'close']), prior_1m)
            final_side = position(float(bar.close), lo, hi)
            label, direction = initial_state(approach, final_side, stable_until(idx, close_ts))
            emit(test, label, close_ts, bar.close, final_side, direction)
            context = h4[h4.available_ts <= close_ts]
            if not context.empty:
                ctx = context.iloc[-1]
                emit(test, 'CONTEXT_LAST_CLOSED_4H', close_ts, ctx.close,
                     position(float(ctx.close), lo, hi), evidence_closed_ts=int(ctx.available_ts))
            if direction == 'NA':
                continue
            add_reference(test, 'RESPONSE_H1', close_ts, direction)
            target = approach
            if hour+H not in h1.index:
                emit(test, 'H1_HOLD_CENSORED', end, bar.close, final_side, direction)
                continue
            hold_bar = h1.loc[hour+H]
            hold_ts = close_ts+H
            if not stable_until(idx, hold_ts):
                emit(test, 'H1_HOLD_GEOMETRY_CHANGED', hold_ts, hold_bar.close, direction=direction)
                continue
            hold_side = position(float(hold_bar.close), lo, hi)
            hold_label = ('H1_HOLD_CONFIRMED' if hold_side == target else
                          ('H1_HOLD_INSIDE' if hold_side == 'INSIDE' else 'H1_HOLD_FAILED'))
            emit(test, hold_label, hold_ts, hold_bar.close, hold_side, direction)
            if hold_label == 'H1_HOLD_CONFIRMED':
                add_reference(test, 'H1_HOLD_CONFIRMED', hold_ts, direction)
            # Track structural failures separately from gross price paths.
            reentered = failed = False
            stopped = False
            follow_end = close_ts + config.followup_hours * H
            for follow in h1[(h1.timestamp >= close_ts) &
                             (h1.available_ts <= min(end, follow_end))].itertuples(index=False):
                when = int(follow.available_ts)
                if not stable_until(idx, when):
                    emit(test, 'FOLLOWUP_GEOMETRY_CHANGED', when, follow.close, direction=direction)
                    stopped = True
                    break
                side = position(float(follow.close), lo, hi)
                if side == 'INSIDE' and not reentered:
                    emit(test, 'FIRST_H1_REENTRY_INSIDE', when, follow.close, side, direction)
                    reentered = True
                if side not in (target, 'INSIDE') and not failed:
                    emit(test, 'FIRST_H1_OPPOSITE_CLOSE', when, follow.close, side, direction)
                    failed = True
            if not stopped:
                when = min(end, follow_end)
                label = 'FOLLOWUP_COMPLETE' if follow_end <= end else 'FOLLOWUP_CENSORED'
                emit(test, label, when, float(minute.loc[when-M, 'close']), direction=direction)
    return (pd.DataFrame(tests, columns=TEST_FIELDS),
            pd.DataFrame(events, columns=EVENT_FIELDS).sort_values('available_ts'),
            pd.DataFrame(references, columns=REFERENCE_FIELDS).sort_values('available_ts'),
            pd.DataFrame(outcomes, columns=OUTCOME_FIELDS).sort_values('available_ts'), audit)


def visible_rows(frame, cutoff):
    """Use independently on contacts, events, references AND outcomes."""
    return frame[frame.available_ts <= int(cutoff)].copy()


def summarize(symbol, regions, tests, events, outcomes):
    counts = events.event.value_counts().to_dict()
    row = dict(symbol=symbol, bands=len(regions), tests_h1=len(tests),
        interaction_groups=int(tests.interaction_group_id.nunique()),
        initial_responses=int(counts.get('DEFENSE_ABOVE_H1', 0) + counts.get('REJECTION_BELOW_H1', 0)),
        holds=int(counts.get('H1_HOLD_CONFIRMED', 0)),
        holds_inside=int(counts.get('H1_HOLD_INSIDE', 0)),
        holds_failed=int(counts.get('H1_HOLD_FAILED', 0)),
        holds_censored=int(counts.get('H1_HOLD_CENSORED', 0)),
        holds_geometry_changed=int(counts.get('H1_HOLD_GEOMETRY_CHANGED', 0)),
        geometry_changed=int(counts.get('GEOMETRY_CHANGED', 0)),
        approach_conflicts=int(counts.get('APPROACH_CONFLICT', 0)),
        inside_closes=int(counts.get('ACCEPT_INSIDE_H1', 0)),
        break_through=int(counts.get('BREAK_THROUGH_H1', 0)))
    for signal, prefix in [('RESPONSE_H1', 'response'), ('H1_HOLD_CONFIRMED', 'hold')]:
        for direction in ('LONG', 'SHORT'):
            row[f'{prefix}_{direction.lower()}'] = int(len(events[
                (events.event.isin(['DEFENSE_ABOVE_H1', 'REJECTION_BELOW_H1'])
                 if signal == 'RESPONSE_H1' else events.event.eq(signal)) &
                events.direction.eq(direction)]))
        for horizon in sorted(outcomes.horizon_minutes.unique()):
            complete = outcomes[(outcomes.signal == signal) &
                                (outcomes.horizon_minutes == horizon) &
                                (outcomes.state == 'COMPLETE')]
            key = f'{prefix}_{int(horizon)}m'
            row[f'{key}_n'] = len(complete)
            row[f'{key}_median_markout_pct'] = (float(complete.markout_pct.median())
                                               if len(complete) else None)
    return row


def code_hashes(root):
    paths = ['multisymbol_band_reaction_pilot_v1.py', 'btc_multiswing_60d_catalog_v1.py',
             'btc_multiswing_60d_explorer_v1.py', 'btc_multiswing_structural_confluence_v2.py',
             'engine/live/research/swing_detector.py']
    return {p: digest(Path(root)/p) for p in paths}


def run_pilot(root, config, candles_dir=None, run_tag=None):
    root = Path(root).resolve()
    plan, frames, sources = preflight(root, config, candles_dir)
    if not plan['ready']:
        raise ValueError('PREFLIGHT_NOT_READY: ' + json.dumps(plan, ensure_ascii=False))
    tag = run_tag or datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', tag):
        raise ValueError('INVALID_RUN_TAG')
    dest = root / OUTPUT / tag
    if dest.exists():
        raise FileExistsError(f'IMMUTABLE_RUN_EXISTS: {dest}')
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=f'.{tag}-', dir=dest.parent))
    summary, audits = [], {}
    try:
        for symbol in config.symbols:
            print(json.dumps({'status': 'PROCESSING', 'symbol': symbol}), flush=True)
            frame = frames[symbol]
            frame = frame[(frame.timestamp >= plan['warmup_start_ts']) &
                          (frame.timestamp < plan['end_ts'])].reset_index(drop=True)
            catalog, regions, stages, geometry_audit = build_geometry(
                frame, symbol, plan['start_ts'], plan['end_ts'], config)
            if symbol == 'BTCUSDT' and sources[symbol]['kind'] == 'BTC_VERIFIED_FREEZE':
                geometry_audit['audited_parent_parity'] = btc_geometry_parity(
                    root, stages, plan['start_ts'], plan['end_ts'], config)
            tests, events, refs, outcomes, measurement_audit = measure_tests(
                frame, stages, symbol, plan['start_ts'], plan['end_ts'], config)
            folder = temp / symbol
            folder.mkdir()
            for name, data in [('candles_1m', frame), ('catalog', catalog),
                               ('regions', regions), ('stages', stages), ('tests_h1', tests),
                               ('events', events), ('references', refs), ('outcomes', outcomes)]:
                data.to_csv(folder / f'{name}.csv', index=False)
            summary.append(summarize(symbol, regions, tests, events, outcomes))
            audits[symbol] = dict(geometry=geometry_audit, measurement=measurement_audit)
        pd.DataFrame(summary).to_csv(temp / 'summary.csv', index=False)
        manifest = dict(version=VERSION, config=asdict(config), coverage=plan,
            sources=sources, code_sha256=code_hashes(root), audits=audits,
            units='BAND_X_TOUCHED_CLOSED_H1; correlated hours grouped; NOT independent trades',
            context_4h='Last completed bar only; no independent corroboration claim',
            geometry='Known before H1 open; no visual TTL and no ACTIVE claim',
            reference='1m open strictly after signal: signal_ts + 60000',
            validation='EXPLORATORY_BASELINE_NO_OUT_OF_SAMPLE_EDGE_OR_NET_PNL',
            files_sha256={str(p.relative_to(temp)): digest(p)
                          for p in sorted(temp.rglob('*.csv'))})
        (temp/'manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False)+'\n')
        # Atomic publication of a complete NEW directory. Never changes parents.
        temp.rename(dest)
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return dest, pd.DataFrame(summary), manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--symbols', default=','.join(SYMBOLS))
    parser.add_argument('--candles-dir', type=Path)
    parser.add_argument('--start', default='2026-09-09T01:00:00Z')
    parser.add_argument('--end', default='2026-11-08T01:00:00Z')
    parser.add_argument('--run-tag')
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--plan', action='store_true')
    action.add_argument('--run', action='store_true')
    args = parser.parse_args(argv)
    config = Config(symbols=tuple(s.strip().upper() for s in args.symbols.split(',')),
                    start=ms(args.start), end=ms(args.end))
    if args.plan:
        plan, _, _ = preflight(args.root, config, args.candles_dir)
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        return 0 if plan['ready'] else 2
    try:
        dest, summary, _ = run_pilot(args.root, config, args.candles_dir, args.run_tag)
    except (ValueError, OSError) as exc:
        print(json.dumps({'status': 'FAILED', 'error': str(exc)}, ensure_ascii=False))
        return 2
    print(summary.to_string(index=False))
    print(json.dumps({'status': 'COMPLETE', 'output': str(dest)}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
