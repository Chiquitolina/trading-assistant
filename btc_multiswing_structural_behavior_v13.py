#!/usr/bin/env python3
"""Multi-Swing V1.3: immutable fixed-versus-causal-stage replay, offline.

Scope: SAME V1.2 episode windows and band identities, 15m/1h events, closed
4h context. Not an exhaustive continuous-event census, new detector, signal,
execution model, or profitability test. V1.2.2 dependencies remain retrospective.

Three streams separate changes in geometry from changes in wick evidence:
A = exact original V1.1-15m / V1.2-1h engines, parity checked against V1.2;
C = fixed episode geometry with closed-1m touch/separation evidence;
B = evolving stage at EACH close and closed-1m causal geometry for ranges.
Counters retain past causal observations; no future stage reclassifies them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

import btc_multiswing_structural_behavior_v11 as p11
import btc_multiswing_structural_behavior_v12 as p12
from btc_multiswing_60d_explorer_v1 import (
    M, FREEZE_REL, CATALOG_REL, aggregate_closed, digest, iso, load_snapshot,
)

VERSION = 'btc-multiswing-structural-behavior-fixed-vs-causal-stage-v1.3'
BASE = Path('reports/reaction_lab/btc_multiswing_60d_v1')
DEFAULT_V12 = BASE / 'btc_sep09_nov08_structural_behavior_15m_1h_v12'
DEFAULT_V121 = BASE / 'btc_sep09_nov08_structural_behavior_pre_safety_v121'
DEFAULT_V122 = BASE / 'btc_sep09_nov08_structural_behavior_clock_dependency_v122'
DEFAULT_OUTPUT = BASE / 'btc_sep09_nov08_structural_behavior_fixed_vs_asof_v13'
ABS_TOL = 1e-6
HORIZON_MINUTES = 1440
DEPARTURE_PCT = .15
TF_MINUTES = {'M15_CLOSED': 15, 'H1_CLOSED': 60}
CASE_REGIONS = ('msv2_f2cf7789ed5ecf4f4f81', 'msv2_69f4a7f7cf54ab7a4214')
CASE_SPECS = (
    ('HIGH_20261022', CASE_REGIONS[0], 1792663380000),  # canonical visit 19, 10:03 UTC
    ('LOW_20261023', CASE_REGIONS[1], 1792775940000),   # canonical visit 29, 17:19 UTC
)


def identity(prefix, values):
    return prefix + hashlib.sha256(json.dumps(values, separators=(',', ':'),
                                             ensure_ascii=False).encode()).hexdigest()[:24]


def clean(value):
    return '' if value is None or pd.isna(value) else str(value)


def same_bounds(a, b):
    return (a is not None and b is not None and
            abs(float(a['band_low']) - float(b['band_low'])) <= ABS_TOL and
            abs(float(a['band_high']) - float(b['band_high'])) <= ABS_TOL)


def stage_key(s):
    return None if s is None else (int(s['known_from']), int(s['independent_extrema']))


class StageIndex:
    """known_until is visual; only known_from controls information availability."""
    def __init__(self, frame):
        needed = {'region_id', 'known_from', 'band_low', 'band_high', 'independent_extrema'}
        if not needed <= set(frame):
            raise ValueError('V13_STAGE_SCHEMA')
        if frame.region_id.isna().any() or frame.region_id.astype(str).str.strip().eq('').any():
            raise ValueError('V13_INVALID_STAGE_REGION')
        self.groups = {}
        for region, group in frame.groupby('region_id', sort=False):
            g = group.sort_values(['known_from', 'independent_extrema'], kind='mergesort').copy()
            numeric = g[['known_from', 'band_low', 'band_high', 'independent_extrema']].apply(pd.to_numeric, errors='coerce')
            if (not np.isfinite(numeric.to_numpy(dtype=float)).all() or
                    g.duplicated(['known_from', 'independent_extrema']).any() or
                    (g.band_low <= 0).any() or (g.band_high < g.band_low).any() or
                    (g.known_from % M != 0).any() or (g.independent_extrema < 2).any() or
                    (g.independent_extrema % 1 != 0).any()):
                raise ValueError('V13_INVALID_STAGE_CLOCK_OR_GEOMETRY')
            self.groups[str(region)] = (
                g.known_from.to_numpy(dtype='int64'), g.to_dict('records'),
                g.band_low.to_numpy(dtype=float), g.band_high.to_numpy(dtype=float))

    def at(self, region, timestamp):
        entry = self.groups.get(str(region))
        if entry is None: return None
        i = int(np.searchsorted(entry[0], int(timestamp), side='right')) - 1
        return entry[1][i] if i >= 0 else None

    def minute_bounds(self, region, opens):
        entry = self.groups.get(str(region))
        if entry is None:
            return np.zeros(len(opens), dtype=bool), np.full(len(opens), np.nan), np.full(len(opens), np.nan)
        i = np.searchsorted(entry[0], opens, side='right') - 1
        known = i >= 0
        safe = np.maximum(i, 0)
        return known, np.where(known, entry[2][safe], np.nan), np.where(known, entry[3][safe], np.nan)


class MinutePath:
    """Only fully closed minutes; range uses stage known at minute OPEN.

    A stage confirmed at the minute close is available for the close-position
    predicate then, but is never projected backward onto that minute's range.
    A range touch is descriptive: high/low order is still unknown.
    """
    def __init__(self, minute, stages):
        self.minute, self.stages = minute, stages
        if (np.any(np.diff(minute['open_ts']) != M) or
                not np.array_equal(minute['closed_ts'], minute['open_ts'] + M)):
            raise ValueError('V13_GAPPED_MINUTE_SOURCE')
        self.cache = {}

    def evidence(self, region, bar_open, bar_close, fixed=None):
        key = (str(region), int(bar_open), int(bar_close),
               None if fixed is None else (float(fixed['band_low']), float(fixed['band_high']), int(fixed['known_from'])))
        if key in self.cache: return self.cache[key]
        a = int(np.searchsorted(self.minute['open_ts'], bar_open))
        b = int(np.searchsorted(self.minute['open_ts'], bar_close))
        opens = self.minute['open_ts'][a:b]
        expected = (bar_close - bar_open) // M
        known = np.zeros(0, dtype=bool)
        if len(opens) == expected and len(opens) and opens[0] == bar_open:
            if fixed is None:
                known, low, high = self.stages.minute_bounds(region, opens)
            else:
                known = opens >= int(fixed['known_from'])
                low = np.full(len(opens), float(fixed['band_low']))
                high = np.full(len(opens), float(fixed['band_high']))
        if len(known) != expected or not len(known) or not known.all():
            result = {'known': False, 'touch': False, 'separated_above': False,
                      'separated_below': False, 'first_touch_known_ts': None,
                      'reason': 'INCOMPLETE_MINUTE_PATH_OR_STAGE'}
        else:
            hi, lo = self.minute['high'][a:b], self.minute['low'][a:b]
            hits = (hi >= low) & (lo <= high)
            first = int(np.flatnonzero(hits)[0]) if hits.any() else None
            result = {'known': True, 'touch': bool(hits.any()),
                      'separated_above': bool(np.all(lo > high)),
                      'separated_below': bool(np.all(hi < low)),
                      'first_touch_known_ts': int(opens[first] + M) if first is not None else None,
                      'reason': 'CLOSED_1M_RANGES_ASOF_MINUTE_OPEN_NO_INTRABAR_ORDER'}
        self.cache[key] = result
        return result


def classify(major, *, episode, source_end, tf_minutes, stages, path,
             fixed_control=False, horizon_minutes=HORIZON_MINUTES,
             departure_pct=DEPARTURE_PCT):
    """V1.2 state branches preserved, geometry and range evidence explicit."""
    ep = dict(episode)
    eid, region = str(ep['episode_id']), str(ep['region_id'])
    touch = int(ep['pre_cutoff_ts'])
    if tf_minutes not in (15, 60) or touch % M or not 0 < departure_pct <= 2:
        raise ValueError('V13_CLASSIFIER_ARGS')
    prefix = 'M15' if tf_minutes == 15 else 'H1'
    initial = {'band_low': float(ep['stage_low']), 'band_high': float(ep['stage_high']),
               'known_from': int(ep['stage_known_from']), 'independent_extrema': int(ep.get('stage_extrema', 2))}
    origin_stage = initial if fixed_control else stages.at(region, touch)
    if origin_stage is None:
        return [], [], {'episode_id': eid, 'source_tf': prefix + '_CLOSED', 'status': 'NO_ORIGIN_STAGE'}
    approach = p11.price_position(float(ep['pre_close_1m']), origin_stage['band_low'], origin_stage['band_high'])
    # Same causal initialization contract as the parent: M15 arrival-side seed;
    # H1 last CLOSED price classified using geometry available at episode PRE.
    previous = approach
    if tf_minutes == 60:
        q = int(np.searchsorted(major['closed_ts'], touch, side='right')) - 1
        if q >= 0:
            previous = p11.price_position(float(major['close'][q]), origin_stage['band_low'], origin_stage['band_high'])
    active, complete = None, True
    runs = {'ABOVE': 0, 'BELOW': 0, 'INSIDE': 0}
    away_runs = {'ABOVE': 0, 'BELOW': 0}
    rows, decisions, history = [], [], []
    seen = set()
    idx = p11._future_close_bars(major, touch, source_end, horizon_minutes)
    policy = 'FIXED_EPISODE_1M_CONTROL' if fixed_control else 'EVOLVING_ASOF_EACH_CLOSE'
    for k in idx:
        ts, op = int(major['closed_ts'][k]), int(major['open_ts'][k])
        s = initial if fixed_control and ts >= initial['known_from'] else stages.at(region, ts) if not fixed_control else None
        if s is None:
            complete = False
            runs = dict.fromkeys(runs, 0); away_runs = dict.fromkeys(away_runs, 0)
            active = None; previous = None; history = []
            decisions.append({'episode_id': eid, 'region_id': region, 'source_tf': prefix + '_CLOSED',
                              'confirmation_ts': ts, 'bar_open_ts': op, 'data_complete': False,
                              'reason': 'NO_STAGE_ASOF', 'current_position': '', 'active_parent_key': ''})
            continue
        close = float(major['close'][k])
        current = p11.price_position(close, s['band_low'], s['band_high'])
        prior_stage = history[-1]['stage_key'] if history else None
        history.append({'ts': ts, 'stage_key': stage_key(s), 'position': current})
        history = history[-3:]
        emitted = []
        range_evidence = None

        def emit(kind, direction='', parent_event=None, ambiguous=False, touch_known=None):
            comparison_key = f'{eid}|{prefix}|{kind}|{ts}'
            if comparison_key in seen: return None
            seen.add(comparison_key)
            ident = identity('mso13_', [policy, eid, prefix, kind, ts])
            r = {'asof_transition_id': ident, 'comparison_key': comparison_key,
                 'episode_id': eid, 'region_id': region, 'band_origin_side': str(ep['band_origin_side']),
                 'source_tf': prefix + '_CLOSED', 'event_type': kind, 'direction': direction,
                 'origin_position': approach, 'confirmation_ts': ts, 'confirmation_utc': iso(ts),
                 'bar_open_ts': op, 'confirmation_close': close, 'close_position': current,
                 'band_low_asof': float(s['band_low']), 'band_high_asof': float(s['band_high']),
                 'stage_known_from': int(s['known_from']), 'stage_extrema': int(s['independent_extrema']),
                 'stage_age_minutes': (ts - int(s['known_from'])) // M,
                 'parent_asof_transition_id': parent_event['id'] if parent_event else '',
                 'parent_comparison_key': parent_event['key'] if parent_event else '',
                 'parent_stage_known_from': parent_event['stage_known_from'] if parent_event else None,
                 'parent_geometry_changed': bool(parent_event and not same_bounds(parent_event, s)),
                 'data_complete': complete, 'geometry_policy': policy,
                 'last_evidence_stage_keys': json.dumps([x['stage_key'] for x in history], separators=(',', ':')),
                 'stage_changed_since_previous_close': prior_stage is not None and prior_stage != stage_key(s),
                 'evidence_status': 'OHLC_INTRABAR_SEQUENCE_UNKNOWN' if ambiguous else 'CLOSED_BAR_CONFIRMED',
                 'range_touch_known_ts': touch_known,
                 'data_role': 'CAUSAL_CLOSE_CONFIRMED_DESCRIPTIVE_EVENT_NOT_PRE_FEATURE'}
            rows.append(r); emitted.append(comparison_key)
            return {'id': ident, 'key': comparison_key, 'stage_known_from': int(s['known_from']),
                    'band_low': float(s['band_low']), 'band_high': float(s['band_high'])}

        if previous is not None and current != previous:
            emit('CLOSE_' + current, current)
        for side in runs:
            runs[side] = runs[side] + 1 if current == side else 0
        if runs['INSIDE'] == 3:
            emit('ACCEPT_INSIDE_3C')
        for side in ('ABOVE', 'BELOW'):
            bound = float(s['band_high'] if side == 'ABOVE' else s['band_low'])
            away = (close - bound) / bound * 100 if side == 'ABOVE' else (bound - close) / bound * 100
            away_runs[side] = away_runs[side] + 1 if current == side and away >= departure_pct else 0
            if away_runs[side] == 2:
                emit('DEPART_' + side + '_2C', side)
        if active is None:
            if approach in ('ABOVE', 'BELOW'):
                far = 'BELOW' if approach == 'ABOVE' else 'ABOVE'
                if runs[far] == 2:
                    parent_event = emit('BREAK_' + far + '_2C', far)
                    active = {**parent_event, 'direction': far, 'origin': approach,
                              'separated': False, 'retest': False, 'reentered': False}
                elif away_runs[approach] == 2:
                    emit('DEFEND_ORIGIN_2C', approach)
            elif approach == 'INSIDE':
                for far in ('ABOVE', 'BELOW'):
                    if runs[far] == 2:
                        parent_event = emit('EXIT_' + far + '_2C', far)
                        active = {**parent_event, 'direction': far, 'origin': 'INSIDE',
                                  'separated': False, 'retest': False, 'reentered': False}
                        break
        else:
            far, origin = active['direction'], active['origin']
            if current != far and not active['reentered']:
                emit('REENTER_' + ('ORIGIN' if origin != 'INSIDE' and current == origin else 'BAND') + '_FIRST_CLOSE',
                     current, active)
                active['reentered'] = True
            if origin != 'INSIDE' and runs[origin] == 2:
                emit('FAILED_BREAK_RETURN_ORIGIN_2C', origin, active)
                active = None
            elif origin == 'INSIDE' and runs['INSIDE'] == 2:
                emit('FAILED_EXIT_RETURN_INSIDE_2C', 'INSIDE', active)
                active = None
            else:
                range_evidence = path.evidence(region, op, ts, initial if fixed_control else None)
                if not range_evidence['known']:
                    complete = False
                else:
                    if active['separated'] and not active['retest'] and range_evidence['touch'] and op >= touch:
                        emit('RETEST_TOUCH', far, active, True, range_evidence['first_touch_known_ts'])
                        active['retest'] = True; active['separated'] = False
                    elif active['retest'] and runs[far] >= 2:
                        # Preserve parent's global side streak, including its
                        # pre-retest history. This replay is not a hold-rule redesign.
                        emit('RETEST_HOLD_' + far + '_2C', far, active)
                        active['retest'] = False
                    if current == far and range_evidence['separated_above' if far == 'ABOVE' else 'separated_below']:
                        active['separated'] = True
        decisions.append({'episode_id': eid, 'region_id': region, 'source_tf': prefix + '_CLOSED',
                          'confirmation_ts': ts, 'bar_open_ts': op, 'current_position': current,
                          'band_low_asof': float(s['band_low']), 'band_high_asof': float(s['band_high']),
                          'stage_known_from': int(s['known_from']),
                          'runs_above': runs['ABOVE'], 'runs_below': runs['BELOW'], 'runs_inside': runs['INSIDE'],
                          'away_runs_above': away_runs['ABOVE'], 'away_runs_below': away_runs['BELOW'],
                          'active_parent_key': active['key'] if active else '',
                          'data_complete': complete,
                          'reason': 'COMPLETE_CLOSED_DATA' if complete else 'EARLIER_EVIDENCE_UNKNOWN',
                          'range_evidence': range_evidence['reason'] if range_evidence else 'NOT_REQUIRED_AT_THIS_BRANCH',
                          'emitted_event_keys': json.dumps(emitted, separators=(',', ':'))})
        previous = current
    return rows, decisions, {'episode_id': eid, 'source_tf': prefix + '_CLOSED',
                             'status': 'COMPLETE_CLOSED_DATA' if complete else 'INDETERMINATE_PATH_OR_STAGE',
                             'horizon_target_ts': touch + horizon_minutes*M,
                             'observed_until_ts': min(source_end, touch + horizon_minutes*M),
                             'right_censored_by_source': source_end < touch + horizon_minutes*M,
                             'observed_tf_closes': len(idx),
                             'origin_geometry_matches_parent': same_bounds(initial, origin_stage),
                             'origin_position_matches_parent': approach == str(ep['approach_position'])}


def original_replay(major, episodes, source_end):
    rows = []
    for ep in episodes.to_dict('records'):
        for tf in (15, 60):
            kwargs = dict(episode_id=str(ep['episode_id']), region_id=str(ep['region_id']),
                          origin_side=str(ep['band_origin_side']), approach=str(ep['approach_position']),
                          band_low=float(ep['stage_low']), band_high=float(ep['stage_high']),
                          touch_open=int(ep['pre_cutoff_ts']), source_end=source_end, tf_minutes=tf,
                          horizon_minutes=HORIZON_MINUTES, departure_pct=DEPARTURE_PCT)
            if tf == 15:
                rows.extend(p11.classify_transitions(major[tf], **kwargs))
            else:
                prior = p12._asof(major[60], kwargs['touch_open'], kwargs['band_low'], kwargs['band_high'], 60)['position']
                rows.extend(p12.classify_transitions_v12(major[tf],
                    previous_closed_position=None if prior == 'UNKNOWN' else prior, **kwargs))
    return pd.DataFrame(rows)


def assert_baseline_parity(old, reproduced):
    fields = ['region_id', 'episode_id', 'source_tf', 'event_type', 'direction', 'confirmation_ts',
              'bar_open_ts', 'confirmation_close', 'band_low_pre', 'band_high_pre',
              'parent_transition_id', 'close_position']
    a, b = old.fillna('').set_index('transition_id'), reproduced.fillna('').set_index('transition_id')
    if a.index.duplicated().any() or b.index.duplicated().any() or set(a.index) != set(b.index):
        raise ValueError('V13_BASELINE_IDS_NOT_REPRODUCED')
    a, b = a[fields].sort_index(), b[fields].sort_index()
    if (a != b).any(axis=None):
        raise ValueError('V13_BASELINE_DATA_NOT_REPRODUCED')
    return {'rows_reproduced': len(a), 'different_ids': 0, 'different_rows': 0}


def family(kind):
    for prefix, label in [('CLOSE_', 'CLOSE'), ('DEPART_', 'DEPART'), ('BREAK_', 'BREAK'),
                          ('EXIT_', 'EXIT'), ('DEFEND_', 'DEFEND'), ('REENTER_', 'REENTER'),
                          ('FAILED_BREAK_', 'FAILED_BREAK'), ('FAILED_EXIT_', 'FAILED_EXIT'),
                          ('RETEST_HOLD_', 'RETEST_HOLD')]:
        if kind.startswith(prefix): return label
    return kind


def compare_observations(old, new, decisions):
    """Exact label match first; replacement only in one unique same-family slot."""
    originals = old.fillna('').to_dict('records')
    fresh = new.fillna('').to_dict('records')
    by_key = {r['comparison_key']: r for r in fresh}
    if len(by_key) != len(fresh): raise ValueError('V13_DUPLICATE_ASOF_OBSERVATION_KEY')
    slots_old, slots_new = defaultdict(list), defaultdict(list)
    def slot(r): return (str(r['episode_id']), str(r['source_tf']), int(r['confirmation_ts']), family(str(r['event_type'])))
    for r in originals: slots_old[slot(r)].append(r)
    for r in fresh: slots_new[slot(r)].append(r)
    tick = {(r['episode_id'], r['source_tf'], int(r['confirmation_ts'])): r
            for r in decisions.to_dict('records')}
    matched, result = set(), []
    for a in originals:
        b = by_key.get(a['transition_id'])
        correspondence = 'EXACT_KIND_AT_SAME_CLOSE'
        if b is None:
            candidates = slots_new.get(slot(a), [])
            # No arbitrary pairing when either side has multiple alternatives.
            unmatched = [r for r in candidates if r['comparison_key'] not in set(x['transition_id'] for x in slots_old[slot(a)])]
            if len(slots_old[slot(a)]) == 1 and len(unmatched) == 1:
                b = unmatched[0]; correspondence = 'UNIQUE_SAME_FAMILY_AT_SAME_EPISODE_AND_CLOSE'
        d = tick.get((a['episode_id'], a['source_tf'], int(a['confirmation_ts'])))
        complete = bool(d is not None and d['data_complete'])
        same_lineage = bool(b is not None and clean(a['parent_transition_id']) == clean(b['parent_comparison_key']))
        if not complete:
            status, reason = 'INDETERMINATE', d['reason'] if d else 'NO_ASOF_DECISION'
        elif b is None:
            status, reason = 'REMOVED_UNDER_ASOF', 'STREAK_OR_ACTIVE_PARENT_OR_CAUSAL_RANGE_CONDITION_NOT_MET'
        elif a['event_type'] != b['event_type'] or clean(a['direction']) != clean(b['direction']):
            status, reason = 'CHANGED_LABEL', correspondence
        elif not same_lineage:
            status, reason = 'SAME_LABEL_DIFFERENT_LINEAGE', 'PARENT_CONFIRMATION_CHANGED'
        else:
            status, reason = 'SAME_LABEL_AND_LINEAGE', 'SAME_TYPE_DIRECTION_CLOSE_AND_PARENT_KEY'
        if b is not None:
            if b['asof_transition_id'] in matched:
                raise ValueError('V13_AMBIGUOUS_MULTIPLE_OLD_TO_NEW_WITHIN_EPISODE')
            matched.add(b['asof_transition_id'])
        result.append({'transition_id': a['transition_id'],
            'physical_transition_id': a['physical_transition_id'], 'episode_id': a['episode_id'],
            'region_id': a['region_id'], 'source_tf': a['source_tf'],
            'confirmation_ts': int(a['confirmation_ts']), 'confirmation_utc': a['confirmation_utc'],
            'parent_event_type': a['event_type'], 'parent_direction': clean(a['direction']),
            'comparison_status': status, 'comparison_reason': reason,
            'asof_transition_id': b['asof_transition_id'] if b else '',
            'asof_physical_transition_id': b.get('asof_physical_transition_id', '') if b else '',
            'asof_event_type': b['event_type'] if b else '', 'asof_direction': clean(b['direction']) if b else '',
            'same_parent_key': same_lineage,
            'fixed_low': float(a['band_low_pre']), 'fixed_high': float(a['band_high_pre']),
            'asof_low': float(d['band_low_asof']) if d and 'band_low_asof' in d else None,
            'asof_high': float(d['band_high_asof']) if d and 'band_high_asof' in d else None,
            'asof_close_position': d['current_position'] if d else '',
            'stage_known_from': d.get('stage_known_from') if d else None,
            'data_role': 'RETROSPECTIVE_COMPARISON_NOT_PRE_FEATURE'})
    added = new.loc[~new.asof_transition_id.isin(matched)].copy()
    added['comparison_scope'] = 'NEW_WITHIN_FROZEN_PARENT_EPISODE_WINDOWS_NOT_GLOBAL_CENSUS'
    return pd.DataFrame(result), added


def canonicalize_asof(events):
    events = events.copy()
    events['asof_physical_transition_id'] = [identity('msp13_', [str(r.region_id), str(r.source_tf),
        str(r.event_type), clean(r.direction), int(r.confirmation_ts), int(r.stage_known_from),
        float(r.band_low_asof), float(r.band_high_asof)]) for r in events.itertuples(index=False)]
    groups = events.groupby('asof_physical_transition_id', sort=False)
    agg = groups.agg(observation_count=('asof_transition_id', 'size'),
                     episode_count=('episode_id', 'nunique'), origin_context_count=('origin_position', 'nunique')).reset_index()
    physical = (events.sort_values(['confirmation_ts', 'episode_id', 'asof_transition_id'], kind='mergesort')
                .drop_duplicates('asof_physical_transition_id').merge(agg, on='asof_physical_transition_id', validate='one_to_one'))
    return events, physical


def physical_comparison(old_physical, observations):
    grouped = {}
    for pid, g in observations.groupby('physical_transition_id', sort=False):
        states = sorted(set(g.comparison_status))
        if 'INDETERMINATE' in states: status = 'INDETERMINATE_IN_SOME_CONTEXTS'
        elif states == ['SAME_LABEL_AND_LINEAGE']: status = 'UNCHANGED_ALL_CONTEXTS'
        elif set(states) <= {'SAME_LABEL_AND_LINEAGE', 'SAME_LABEL_DIFFERENT_LINEAGE'}:
            status = 'LABEL_SAME_LINEAGE_CHANGED'
        elif states == ['REMOVED_UNDER_ASOF']: status = 'REMOVED_ALL_CONTEXTS'
        elif states == ['CHANGED_LABEL'] and len(g[['asof_event_type', 'asof_direction']].drop_duplicates()) == 1:
            status = 'CHANGED_ALL_CONTEXTS'
        else: status = 'CONTEXT_DEPENDENT_DIVERGENCE'
        grouped[pid] = {'physical_transition_id': pid, 'physical_comparison_status': status,
            'comparison_contexts': len(g), 'observation_statuses': '|'.join(states),
            'asof_physical_ids': '|'.join(sorted(set(g.asof_physical_transition_id.dropna()) - {''}))}
    result = old_physical.merge(pd.DataFrame(grouped.values()), on='physical_transition_id', validate='one_to_one')
    if len(result) != len(old_physical) or result.physical_comparison_status.isna().any():
        raise ValueError('V13_PHYSICAL_PARENT_COVERAGE_LOST')
    return result


def link_timeframes(events, max_lag_minutes=120):
    """Retrospective H1-M15 relation, with actual geometry on both clocks.

    Same episode, exact kind/direction/origin and compatible parent lineage.
    Different as-of stages are recorded rather than equated or hidden.
    """
    all_rows = events.fillna('').to_dict('records')
    by_id = {r['asof_transition_id']: r for r in all_rows}
    m15 = defaultdict(list)
    for r in all_rows:
        if r['source_tf'] == 'M15_CLOSED': m15[r['episode_id']].append(r)
    result = []
    for h in all_rows:
        if h['source_tf'] != 'H1_CLOSED': continue
        ts = int(h['confirmation_ts'])
        item = {'h1_asof_transition_id': h['asof_transition_id'],
                'h1_asof_physical_transition_id': h['asof_physical_transition_id'],
                'episode_id': h['episode_id'], 'region_id': h['region_id'], 'event_type': h['event_type'],
                'direction': clean(h['direction']), 'h1_confirmation_ts': ts,
                'm15_asof_transition_id': '', 'm15_asof_physical_transition_id': '',
                'm15_confirmation_ts': None, 'lag_minutes': None, 'link_status': 'NO_MATCH',
                'geometry_changed_between_confirmations': None,
                'h1_stage_known_from': h['stage_known_from'], 'm15_stage_known_from': None,
                'h1_low': h['band_low_asof'], 'h1_high': h['band_high_asof'],
                'm15_low': None, 'm15_high': None,
                'data_role': 'RETROSPECTIVE_ASSOCIATION_NEVER_A_PRE_FEATURE'}
        if h['event_type'].startswith(('CLOSE_', 'DEPART_')):
            item['link_status'] = 'NOT_APPLICABLE'; result.append(item); continue
        candidates = [r for r in m15.get(h['episode_id'], [])
            if r['event_type'] == h['event_type'] and clean(r['direction']) == clean(h['direction'])
            and r['origin_position'] == h['origin_position'] and int(r['confirmation_ts']) <= ts]
        within = [r for r in candidates if ts - int(r['confirmation_ts']) <= max_lag_minutes * M]
        if candidates and not within:
            item['link_status'] = 'OUTSIDE_WINDOW'; result.append(item); continue
        compatible = []
        for r in within:
            a, b = clean(h['parent_asof_transition_id']), clean(r['parent_asof_transition_id'])
            if not a and not b: compatible.append(r); continue
            if not a or not b or a not in by_id or b not in by_id: continue
            x, y = by_id[a], by_id[b]
            if (x['event_type'] == y['event_type'] and clean(x['direction']) == clean(y['direction'])
                    and int(x['confirmation_ts']) <= ts and int(y['confirmation_ts']) <= ts
                    and abs(int(x['confirmation_ts']) - int(y['confirmation_ts'])) <= max_lag_minutes * M):
                compatible.append(r)
        if within and not compatible:
            item['link_status'] = 'PARENT_MISMATCH'; result.append(item); continue
        if compatible:
            latest = max(int(r['confirmation_ts']) for r in compatible)
            best = [r for r in compatible if int(r['confirmation_ts']) == latest]
            if len({r['asof_physical_transition_id'] for r in best}) > 1:
                item['link_status'] = 'AMBIGUOUS'; result.append(item); continue
            r = min(best, key=lambda x: x['asof_transition_id'])
            changed = (abs(float(h['band_low_asof']) - float(r['band_low_asof'])) > ABS_TOL or
                       abs(float(h['band_high_asof']) - float(r['band_high_asof'])) > ABS_TOL)
            item.update(m15_asof_transition_id=r['asof_transition_id'],
                        m15_asof_physical_transition_id=r['asof_physical_transition_id'],
                        m15_confirmation_ts=int(r['confirmation_ts']), lag_minutes=(ts-latest)//M,
                        link_status='SAME_CLOSE' if ts == latest else 'LAGGED_CONFIRMATION',
                        geometry_changed_between_confirmations=changed,
                        m15_stage_known_from=r['stage_known_from'], m15_low=r['band_low_asof'], m15_high=r['band_high_asof'])
        result.append(item)
    return pd.DataFrame(result)


def episode_sequences(episodes, events, source_end):
    records = []
    grouped = {ep: g.sort_values(['confirmation_ts', 'asof_transition_id'])
               for ep, g in events.groupby('episode_id', sort=False)}
    for ep in episodes.to_dict('records'):
        item = {k: ep[k] for k in ('episode_id', 'region_id', 'band_origin_side', 'pre_cutoff_ts',
                'pre_cutoff_utc', 'approach_position', 'stage_low', 'stage_high',
                'pre_last_1h_close_ts', 'pre_last_1h_close', 'pre_last_4h_close_ts', 'pre_last_4h_close')}
        g = grouped.get(ep['episode_id'])
        for tf in ('M15_CLOSED', 'H1_CLOSED'):
            selected = (g.loc[g.source_tf.eq(tf) & ~g.event_type.str.startswith(('CLOSE_', 'DEPART_'))]
                        if g is not None else pd.DataFrame())
            label = 'm15' if tf == 'M15_CLOSED' else 'h1'
            item[label + '_structural_event_count'] = len(selected)
            item[label + '_observed_sequence'] = (' | '.join(
                f"{r.confirmation_utc}: {r.event_type} [{clean(r.direction)}]"
                for r in selected.itertuples(index=False)) if len(selected) else '')
        item['scope'] = 'FROZEN_PARENT_EPISODE_WINDOW_HISTORY_NOT_AN_ACTIVE_STATE'
        item['horizon_target_ts'] = int(ep['pre_cutoff_ts']) + HORIZON_MINUTES*M
        item['observed_until_ts'] = min(source_end, item['horizon_target_ts'])
        item['right_censored_by_source'] = source_end < item['horizon_target_ts']
        records.append(item)
    return pd.DataFrame(records)


def case_context(major, stages):
    rows = []
    for name, region, cutoff in CASE_SPECS:
        for tf in (15, 60, 240):
            bars = major[tf]
            lo, hi = cutoff - 14*1440*M, cutoff + 24*60*M
            idx = np.flatnonzero((bars['closed_ts'] >= lo) & (bars['closed_ts'] <= hi))
            for k in idx:
                t = int(bars['closed_ts'][k]); s = stages.at(region, t)
                rows.append({'case_name': name, 'region_id': region, 'case_cutoff_ts': cutoff,
                    'tf_minutes': tf, 'bar_open_ts': int(bars['open_ts'][k]), 'closed_ts': t,
                    'closed_utc': iso(t), 'open': float(bars['open'][k]), 'high': float(bars['high'][k]),
                    'low': float(bars['low'][k]), 'close': float(bars['close'][k]),
                    'stage_known_from': int(s['known_from']) if s else None,
                    'band_low_asof': float(s['band_low']) if s else None,
                    'band_high_asof': float(s['band_high']) if s else None,
                    'closed_position_asof': p11.price_position(float(bars['close'][k]),s['band_low'],s['band_high']) if s else '',
                    'relative_to_selected_pre': 'PRE_CLOSED' if t <= cutoff else 'POST_CLOSED',
                    'note': '4H_CONTEXT_ONLY; CLASSIFIED_AT_OWN_CLOSE; NO_OPEN_HTF_IN_PRE'})
    return pd.DataFrame(rows)


def build_pre(events, minute, major, stages, episode_index):
    """Before confirming candle; decision fields/labels never enter this table."""
    eps = episode_index.set_index('episode_id')
    rows = []
    for r in events.itertuples(index=False):
        t = int(r.bar_open_ts); s = stages.at(r.region_id, t)
        item = {'asof_transition_id': r.asof_transition_id, 'episode_id': r.episode_id,
                'region_id': r.region_id, 'pre_cutoff_ts': t, 'pre_cutoff_utc': iso(t),
                'pre_stage_known': s is not None,
                'pre_stage_low': float(s['band_low']) if s else None,
                'pre_stage_high': float(s['band_high']) if s else None,
                'pre_stage_known_from': int(s['known_from']) if s else None,
                'pre_closed_first_touch_known': int(eps.loc[r.episode_id, 'pre_cutoff_ts']) + M <= t,
                'pre_role': 'BEFORE_CONFIRMING_CANDLE_OPEN_NOT_AT_CONFIRMING_CLOSE'}
        for name, bars in [('1m', minute), ('15m', major[15]), ('1h', major[60]), ('4h', major[240])]:
            i = int(np.searchsorted(bars['closed_ts'], t, side='right')) - 1
            px = float(bars['close'][i]) if i >= 0 else None
            item['pre_last_' + name + '_close_ts'] = int(bars['closed_ts'][i]) if i >= 0 else None
            item['pre_last_' + name + '_close'] = px
            item['pre_last_' + name + '_position_at_cutoff_stage'] = p11.price_position(px, s['band_low'], s['band_high']) if px is not None and s else None
        item['pre_structural_context_eligible'] = item['pre_stage_known'] and item['pre_closed_first_touch_known']
        rows.append(item)
    return pd.DataFrame(rows)


def checked_parent(folder, version):
    m = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    if m.get('version') != version or not m.get('immutable_run') or not m.get('no_live_imports_or_network'):
        raise ValueError('V13_INVALID_PARENT_MANIFEST: ' + str(folder))
    for name, expected in m['files_sha256'].items():
        if Path(name).name != name or digest(folder / name) != expected:
            raise ValueError('V13_PARENT_HASH_MISMATCH: ' + name)
    return m


def run(root, v12, v121, v122):
    versions = [p12.VERSION, 'btc-multiswing-structural-behavior-pre-safety-v1.2.1',
                'btc-multiswing-structural-behavior-clock-dependency-v1.2.2']
    paths = [v12, v121, v122]
    manifests = [checked_parent(p, v) for p, v in zip(paths, versions)]
    parent_hashes = {name: digest(path / 'manifest.json') for name, path in zip(('v12', 'v121', 'v122'), paths)}
    if manifests[1].get('parent_v12_manifest_sha256') != parent_hashes['v12']:
        raise ValueError('V13_V121_PARENT_DISAGREEMENT')
    for name in ('v12', 'v121'):
        if manifests[2].get('parent_' + name + '_manifest_sha256') != parent_hashes[name]:
            raise ValueError('V13_V122_PARENT_DISAGREEMENT')
    full, _, source = load_snapshot(root)
    for manifest in manifests:
        for key in ('start', 'end', 'source_sha256', 'catalog_sha256'):
            if manifest['source_provenance'].get(key) != source[key]:
                raise ValueError('V13_FROZEN_SOURCE_DRIFT: ' + key)
    if p11.HORIZON_MINUTES != HORIZON_MINUTES or p11.DEPARTURE_PCT != DEPARTURE_PCT:
        raise ValueError('V13_PARENT_POLICY_CHANGED')
    episodes = pd.read_csv(v12 / 'episode_index_v12.csv', low_memory=False)
    old = pd.read_csv(v12 / 'transition_observations_v12.csv', low_memory=False)
    old_physical = pd.read_csv(v12 / 'physical_transitions_v12.csv', low_memory=False)
    stages_df = pd.read_csv(v121 / 'geometric_stage_history_v121.csv')
    stages = StageIndex(stages_df)
    minute = p11._closed_series(full, M)
    start, end = int(full.timestamp.iloc[0]), int(source['end'])
    major = {tf: p11._closed_series(aggregate_closed(full, name, start, end), tf * M)
             for tf, name in [(15, '15m'), (60, '1h'), (240, '4h')]}
    baseline = original_replay(major, episodes, end)
    parity = assert_baseline_parity(old, baseline)
    print('MS_BEHAVIOR_V13_BASELINE_PARITY', json.dumps(parity), flush=True)
    path = MinutePath(minute, stages)
    dynamic_rows, decision_rows, control_rows, health_rows = [], [], [], []
    for i, ep in enumerate(episodes.to_dict('records')):
        for tf in (15, 60):
            b, d, health = classify(major[tf], episode=ep, source_end=end, tf_minutes=tf, stages=stages, path=path)
            c, _, _ = classify(major[tf], episode=ep, source_end=end, tf_minutes=tf, stages=stages, path=path, fixed_control=True)
            dynamic_rows.extend(b); decision_rows.extend(d); control_rows.extend(c); health_rows.append(health)
        if (i + 1) % 200 == 0:
            print('MS_BEHAVIOR_V13_PROGRESS', json.dumps({'episodes': i + 1, 'total': len(episodes)}), flush=True)
    events, new_physical = canonicalize_asof(pd.DataFrame(dynamic_rows))
    decisions = pd.DataFrame(decision_rows)
    compare, added = compare_observations(old, events, decisions)
    # C uses its own decision flags, otherwise the B unknown state would confound C.
    control = pd.DataFrame(control_rows)
    ckeys = set(control.comparison_key)
    okeys = set(old.transition_id)
    control_stats = {'old_events_absent_in_fixed_1m_control': len(okeys - ckeys),
                     'control_events_absent_in_original': len(ckeys - okeys)}
    # Match control labels for attribution on every original observation.
    control_directions = control.set_index('comparison_key').direction.fillna('').to_dict()
    compare['fixed_1m_control_same_label_at_same_close'] = [t in ckeys and
        clean(control_directions[t]) == clean(direction)
        for t, direction in zip(compare.transition_id, compare.parent_direction)]
    control_stats['same_key_but_direction_changed'] = int((compare.transition_id.isin(ckeys) &
        ~compare.fixed_1m_control_same_label_at_same_close).sum())
    control_parents = control.set_index('comparison_key').parent_comparison_key.to_dict()
    old_parents = old.set_index('transition_id').parent_transition_id.fillna('').to_dict()
    compare['fixed_1m_control_same_parent'] = [clean(control_parents.get(t, '')) == clean(old_parents[t]) and t in ckeys
                                             for t in compare.transition_id]
    physical = physical_comparison(old_physical, compare)
    pre = build_pre(events, minute, major, stages, episodes)
    links = link_timeframes(events)
    sequences = episode_sequences(episodes, events, end)
    summary_by_family = (compare.groupby(['source_tf', 'parent_event_type', 'comparison_status'], dropna=False)
                         .size().rename('observations').reset_index())
    health = pd.DataFrame(health_rows)
    # Case chronologies: original parent episode windows plus PRE 1h/4h snapshots.
    case_ids = set(episodes.loc[episodes.region_id.isin(CASE_REGIONS), 'episode_id'])
    case_events = events.loc[events.episode_id.isin(case_ids)].copy()
    case_comparison = compare.loc[compare.episode_id.isin(case_ids)].copy()
    case_outcomes = []
    for r in case_events.drop_duplicates('asof_physical_transition_id').to_dict('records'):
        wrapped = {**r, 'transition_id': r['asof_transition_id']}
        for outcome in p11.evaluate_transition(minute, wrapped, end=end, horizons=(60, 180, 360)):
            outcome['asof_physical_transition_id'] = r['asof_physical_transition_id']
            case_outcomes.append(outcome)
    clocks = events.groupby(['confirmation_ts', 'source_tf', 'event_type', 'direction'], dropna=False).region_id.transform('nunique')
    events['concurrent_observed_bands'] = clocks
    # Read-only parents are reverified after the complete computation.
    for path_parent, version, name in zip(paths, versions, ('v12', 'v121', 'v122')):
        if digest(path_parent / 'manifest.json') != parent_hashes[name]:
            raise ValueError('V13_PARENT_CHANGED_DURING_REPLAY')
        checked_parent(path_parent, version)
    summary = {'version': VERSION, 'baseline_parity': parity, 'fixed_1m_control': control_stats,
        'counts': {'episodes': len(episodes), 'parent_physical_events': len(old_physical),
                   'right_censored_episodes_by_source': int((episodes.pre_cutoff_ts + HORIZON_MINUTES*M > end).sum()),
                   'parent_observations': len(old), 'asof_observations': len(events),
                   'asof_physical_descriptive_events': len(new_physical), 'decision_closes': len(decisions),
                   'new_unmatched_observations_within_parent_windows': len(added),
                   'comparison_statuses_observations': compare.comparison_status.value_counts().to_dict(),
                   'comparison_statuses_parent_physical': physical.physical_comparison_status.value_counts().to_dict(),
                   'episode_tf_health': health.status.value_counts().to_dict(),
                   'h1_m15_link_statuses': links.link_status.value_counts().to_dict()},
        'parent_manifest_sha256': parent_hashes, 'source_provenance': source,
        'event_labels_recalculated': True, 'original_parents_modified': False,
        'scope': 'EXISTING_1143_PARENT_EPISODES_NOT_EXHAUSTIVE_CONTINUOUS_EVENT_CENSUS',
        'policy': {'horizon_minutes': HORIZON_MINUTES, 'departure_pct': DEPARTURE_PCT,
                   'closed_price_geometry': 'LATEST_STAGE_KNOWN_AT_EACH_CLOSE',
                   'range_geometry': 'LATEST_STAGE_KNOWN_AT_EACH_CLOSED_1M_OPEN',
                   'counter_policy': 'CAUSAL_SIDE_STREAKS_ACROSS_STAGES_NO_RETROACTIVE_RECLASSIFICATION',
                   'retest_hold_counter': 'PARENT_GLOBAL_SIDE_STREAK_PRESERVED_NOT_RESET_AT_RETEST',
                   'context_4h': 'CLOSED_ONLY_AT_PRE_NO_4H_EVENT_ENGINE',
                   'geometry_equality_abs_usd': ABS_TOL},
        'limitations': [
            'New events are new within original episode windows, not a universal continuous census.',
            'Same-label events with different parents have changed structural lineage.',
            'Physical IDs and concurrency groups are retrospective audit keys, not independent samples or PRE features.',
            'A 2-close crossing alone does not establish loss of previously defended support or recovery of a previously lost structure.',
            'A closed-1m wick contact does not reveal intraminute high/low order or trade execution.',
            'Case outcomes are descriptive after confirmation; no costs, fills, PnL, benchmark or OOS.',
        ]}
    tables = {'observation_comparison_v13.csv': compare, 'parent_physical_comparison_v13.csv': physical,
              'asof_transition_observations_v13.csv': events, 'asof_physical_transitions_v13.csv': new_physical,
              'new_events_within_parent_windows_v13.csv': added, 'transition_pre_asof_v13.csv': pre,
              'decision_state_audit_v13.csv': decisions, 'summary_by_family_v13.csv': summary_by_family,
              'episode_tf_health_v13.csv': health, 'case_chronologies_v13.csv': case_events,
              'case_comparison_v13.csv': case_comparison, 'case_post_descriptive_v13.csv': pd.DataFrame(case_outcomes),
              'h1_m15_links_asof_v13.csv': links, 'episode_sequences_v13.csv': sequences,
              'case_context_closed_bars_v13.csv': case_context(major, stages),
              'case_geometric_stages_v13.csv': stages_df.loc[stages_df.region_id.isin(CASE_REGIONS)].copy()}
    return tables, summary


def write_immutable(output, tables, summary, root, parents):
    output = Path(output).resolve()
    protected = [Path(p).resolve() for p in parents] + [(root / FREEZE_REL).resolve(), (root / CATALOG_REL).resolve()]
    if any(output == p or output.is_relative_to(p) for p in protected):
        raise ValueError('V13_OUTPUT_INSIDE_IMMUTABLE_INPUT')
    if output.exists(): raise FileExistsError('V13_IMMUTABLE_OUTPUT_ALREADY_EXISTS: ' + str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ms_v13_', dir=output.parent) as tmp:
        work = Path(tmp)
        for name, frame in tables.items(): frame.to_csv(work / name, index=False, float_format='%.12g')
        (work / 'behavior_summary_v13.json').write_text(json.dumps(summary, sort_keys=True, indent=2) + '\n', encoding='utf-8')
        files = sorted(work.iterdir())
        source_names = [Path(__file__).name, 'btc_multiswing_structural_behavior_v11.py',
                        'btc_multiswing_structural_behavior_v12.py', 'btc_multiswing_60d_explorer_v1.py']
        source_folder = Path(__file__).resolve().parent
        manifest = {'version': VERSION, 'immutable_run': True, 'read_only_parents': True,
                    'no_live_imports_or_network': True, 'source_provenance': summary['source_provenance'],
                    'parent_manifest_sha256': summary['parent_manifest_sha256'],
                    'source_code_sha256': {n: digest(source_folder / n) for n in source_names},
                    'files_sha256': {p.name: digest(p) for p in files}}
        (work / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n', encoding='utf-8')
        output.mkdir()  # atomic reservation; never replaces an existing directory
        for f in files: f.replace(output / f.name)
        (work / 'manifest.json').replace(output / 'manifest.json')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan', action='store_true'); mode.add_argument('--run', action='store_true')
    parser.add_argument('--project-root', type=Path, default=Path('.'))
    parser.add_argument('--parent-v12', type=Path, default=DEFAULT_V12)
    parser.add_argument('--parent-v121', type=Path, default=DEFAULT_V121)
    parser.add_argument('--parent-v122', type=Path, default=DEFAULT_V122)
    parser.add_argument('--output-dir', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv); root = args.project_root.resolve()
    def absolute(p): return (p if p.is_absolute() else root / p).resolve()
    parents = [absolute(p) for p in (args.parent_v12, args.parent_v121, args.parent_v122)]
    output = absolute(args.output_dir)
    protected = parents + [(root / FREEZE_REL).resolve(), (root / CATALOG_REL).resolve()]
    if any(output == p or output.is_relative_to(p) for p in protected):
        parser.error('output must be outside all immutable inputs')
    print('MS_BEHAVIOR_V13_PLAN', json.dumps({'version': VERSION, 'root': str(root),
        'output': str(output), 'parents': list(map(str, parents)), 'event_sources': ['15m', '1h'],
        'context': '4h closed', 'scope': 'ORIGINAL_PARENT_EPISODE_WINDOWS', 'no_live': True}), flush=True)
    if args.plan: return 0
    if output.exists(): raise FileExistsError('V13_IMMUTABLE_OUTPUT_ALREADY_EXISTS: ' + str(output))
    tables, summary = run(root, *parents)
    write_immutable(output, tables, summary, root, parents)
    print('MS_BEHAVIOR_V13_FINISHED', json.dumps({'output': str(output), **summary['counts'],
          'baseline_different_rows': summary['baseline_parity']['different_rows'],
          'original_parents_modified': False}), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
