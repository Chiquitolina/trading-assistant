"""Exploratory, read-only Micro Flow V3 sweep entry and aggression-effort research.

Two independent candidate entry policies (reclaim vs retest); an additional
aggression/price-response diagnostic, NOT an exhaustion entry signal. All
observable clocks are taken from the original causal 1s Replay. No trade fills,
API requests, backfills, SQLite writes, or LIVE integration.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import math
import uuid

from research.micro_flow_v3.swing_sweep_discovery import (
    DiscoveryError, file_sha256, validate_buckets,
)
from research.micro_flow_v3.aggression_features import effort_snapshot

SCHEMA = 'micro-flow-v3-sweep-entry-research-v1'
HORIZONS_S = (30, 60, 180, 300)
VARIANTS = ('RECLAIM', 'RETEST')


def _sha_json(obj):
    return sha256(json.dumps(obj, sort_keys=True, separators=(',', ':'),
                             allow_nan=False).encode('utf-8')).hexdigest()


def _ms_ceil(ns):
    return (int(ns) + 999_999) // 1_000_000


def _next_open_reference(rows, starts, signal, *, max_lag_seconds):
    """Next *fully later* observed second, not same-second OHLC / lucky low.

    The open is an *observed reference*, not a market order fill. Its publication
    is strictly after signal time; future labels start only after this bucket.
    """
    if signal is None:
        return None, None
    ns = int(signal['available_wall_ns'])
    earliest_second = (ns + 999_999_999) // 1_000_000_000 * 1000
    i = bisect_left(starts, earliest_second)
    if i >= len(rows):
        return None, None
    row = rows[i]
    lag_ms = row['second_start_ms'] - _ms_ceil(ns)
    if lag_ms < 0 or lag_ms > max_lag_seconds * 1000:
        return None, None
    if row['available_wall_ns'] <= ns:
        raise DiscoveryError('Entry reference was observed before its signal')
    return i, {
        'entry_reference_second_start_ms':row['second_start_ms'],
        'entry_reference_price':float(row['open']),
        'entry_reference_available_wall_ns':row['available_wall_ns'],
        'entry_reference_available_ms':_ms_ceil(row['available_wall_ns']),
        'entry_reference_first_seq':row['first_seq'],
        'entry_reference_last_seq':row['last_seq'],
        'entry_reference_lag_ms':lag_ms,
        'entry_reference_kind':'NEXT_OBSERVED_1S_OPEN_NOT_EXECUTABLE_FILL',
    }


def _entry_outcomes(rows, starts, index, price, side, horizons, freshness_seconds):
    """No outcome/extent can contain the entry reference's own 1s OHLC."""
    direction = -1 if side == 'HIGH' else 1
    entry = rows[index]
    result = {}
    for horizon in horizons:
        key = f'{horizon}s'
        target = entry['second_start_ms'] + horizon * 1000
        j = bisect_left(starts, target)
        enough = (j < len(rows) and 0 <= starts[j] - target <= freshness_seconds*1000
                  and rows[j]['available_wall_ns'] > entry['available_wall_ns'])
        if not enough:
            result[key] = {'state':'CENSORED_NO_FRESH_ENDPOINT',
                           'return_directional_pct':None, 'mfe_directional_pct':None,
                           'mae_directional_pct':None, 'observed_seconds':None,
                           'coverage_fraction':None, 'endpoint_available_wall_ns':None}
            continue
        path = rows[index+1:j+1]
        # If the segment has a long unobserved interval, DO NOT pretend
        # OHLC extremes across it establish a continuous tradeable path.
        continuity = all(rows[k]['second_start_ms']-rows[k-1]['second_start_ms']
                         <= freshness_seconds*1000 for k in range(index+1,j+1))
        if not continuity:
            result[key] = {'state':'CENSORED_OBSERVATION_GAP',
                           'return_directional_pct':None, 'mfe_directional_pct':None,
                           'mae_directional_pct':None, 'observed_seconds':len(path),
                           'coverage_fraction':len(path)/horizon,
                           'endpoint_available_wall_ns':rows[j]['available_wall_ns']}
            continue
        # For LONG: favorable high, adverse low. SHORT: favorable low, adverse high.
        if direction > 0:
            mfe = max([0.]+[(r['high']/price-1)*100 for r in path])
            mae = min([0.]+[(r['low']/price-1)*100 for r in path])
        else:
            mfe = max([0.]+[(1-r['low']/price)*100 for r in path])
            mae = min([0.]+[(1-r['high']/price)*100 for r in path])
        result[key] = {
            'state':'OBSERVED_PROXY',
            'return_directional_pct':direction*(rows[j]['close']/price-1)*100,
            'mfe_directional_pct':mfe,
            'mae_directional_pct':mae,
            'observed_seconds':len(path),
            'coverage_fraction':len(path)/horizon,
            'endpoint_available_wall_ns':rows[j]['available_wall_ns'],
        }
    return result


def _aggression_diagnostic(rows, starts, event, *, window_seconds,
                           min_volume_relative, min_imbalance):
    """Shared causal effort engine at the sweep instant; not a trade trigger."""
    sweep = event['sweep']
    i = bisect_left(starts, sweep['observed_second_start_ms'])
    if i >= len(rows) or rows[i]['second_start_ms'] != sweep['observed_second_start_ms']:
        raise DiscoveryError('Sweep cannot be located in its source segment')
    snapshot = effort_snapshot(rows, starts, i, window_seconds=window_seconds)
    if snapshot is None:
        return {
            'effort_state':'INSUFFICIENT_CAUSAL_COVERAGE',
            'high_effort_candidate':False,'exhaustion_confirmed':False,
            'sweep_relative_volume':event.get('aggressive_volume_relative'),
            'sweep_imbalance':event.get('aggressive_imbalance'),
            'reclaim_seen_later':event.get('reclaim') is not None,
        }
    side = 'BUY' if event['swing_side'] == 'HIGH' else 'SELL'
    high_effort = bool(snapshot['pressure_side'] == side and
        snapshot['relative_volume'] >= min_volume_relative and
        abs(snapshot['imbalance']) >= min_imbalance)
    low,high=snapshot['window_low'],snapshot['window_high']
    extreme=low if side=='SELL' else high
    close=snapshot['window_close_price']
    frac=(close-low)/(high-low) if side=='SELL' and high>low else (
         (high-close)/(high-low) if high>low else None)
    return {
        'effort_state':'OBSERVED',
        **snapshot,
        'signed_price_displacement_pct':(
            snapshot['pressure_signed_displacement_pct'] if snapshot['pressure_side']==side
            else -snapshot['pressure_signed_displacement_pct']),
        'signed_impact_pct_per_100eth_delta':(
            snapshot['signed_impact_pct_per_100eth_net_delta']),
        'recovery_from_window_extreme_fraction':frac,
        'sweep_relative_volume':snapshot['relative_volume'],
        'sweep_imbalance':snapshot['imbalance'],
        'high_effort_candidate':high_effort,
        'candidate_definition':'ALIGNED_AGGRESSION_AT_SWEEP_NOT_EXHAUSTION_CONFIRMED',
        'exhaustion_confirmed':False,
        'reclaim_seen_later':event.get('reclaim') is not None,
        'reclaim_delay_seconds':(
            (event['reclaim']['available_wall_ns']-sweep['available_wall_ns'])/1e9
            if event.get('reclaim') else None),
    }


def analyze_sweep_entries(buckets, discovery, *, entry_freshness_seconds=3,
                          horizon_freshness_seconds=3, exhaustion_window_seconds=5,
                          min_volume_relative=2.0, min_abs_imbalance=0.70,
                          horizons=HORIZONS_S):
    """Analyze entire discover() cohort without filtering winning outcomes.

    No new detector thresholds are fitted here. Returns two comparable
    *conditional* entry cohorts and one unfiltered sweep-time effort diagnostic.
    """
    if not 1 <= entry_freshness_seconds <= 30 or not 1 <= horizon_freshness_seconds <= 30:
        raise DiscoveryError('Invalid observed-time freshness')
    if not 1 <= exhaustion_window_seconds <= 60:
        raise DiscoveryError('Invalid aggression window')
    if min_volume_relative < 0 or not 0 <= min_abs_imbalance <= 1:
        raise DiscoveryError('Invalid descriptive aggression thresholds')
    if tuple(horizons) != HORIZONS_S:
        raise DiscoveryError('Horizons are fixed at 30/60/180/300s for V1')
    buckets = validate_buckets(buckets)
    groups = defaultdict(list)
    for row in buckets:
        groups[row['segment_id']].append(row)
    group_starts = {seg:[r['second_start_ms'] for r in data]
                    for seg,data in groups.items()}
    cases, diagnostics = [], []
    for event in discovery['events']:
        seg = int(event['segment_id'])
        if seg not in groups:
            raise DiscoveryError('Event references missing source segment')
        data = groups[seg]
        starts = group_starts[seg]
        sweep = event['sweep']
        sweep_i = bisect_left(starts, int(sweep['observed_second_start_ms']))
        if sweep_i >= len(data) or starts[sweep_i] != sweep['observed_second_start_ms']:
            raise DiscoveryError('Source event does not match the source Replay')
        if data[sweep_i]['available_wall_ns'] != int(sweep['available_wall_ns']):
            raise DiscoveryError('Event/Replay clocks disagree')
        diag = _aggression_diagnostic(data,starts,event,
                  window_seconds=exhaustion_window_seconds,
                  min_volume_relative=min_volume_relative,
                  min_imbalance=min_abs_imbalance)
        diagnostics.append({'event_id':event['event_id'],
                            'segment_id':seg,'sweep':sweep,
                            'hypothesis_direction':event['hypothesis_direction'],
                            'swing_side':event['swing_side'], **diag})
        for variant,stage_name in [('RECLAIM','reclaim'),('RETEST','retest')]:
            signal = event[stage_name]
            case = {'event_id':event['event_id'], 'variant':variant,
                    'segment_id':seg, 'swing_side':event['swing_side'],
                    'hypothesis_direction':event['hypothesis_direction'],
                    'swing_level':event['level'],
                    'signal_state':'AVAILABLE' if signal else 'NOT_REACHED',
                    'signal_stage':stage_name,
                    'signal_available_wall_ns':signal['available_wall_ns'] if signal else None,
                    'signal_available_ms':signal['available_ms'] if signal else None,
                    'entry_reference':None, 'entry_state':'NO_SIGNAL' if signal is None else 'PENDING',
                    'outcomes':{}, 'costs_excluded':True}
            if signal is not None:
                index, reference = _next_open_reference(data,starts,signal,
                              max_lag_seconds=entry_freshness_seconds)
                if reference is None:
                    case['entry_state']='CENSORED_NO_FRESH_NEXT_SECOND'
                else:
                    case['entry_reference']=reference
                    case['entry_state']='OBSERVED_REFERENCE_NOT_FILL'
                    case['outcomes']=_entry_outcomes(data,starts,index,
                          reference['entry_reference_price'],event['swing_side'],
                          horizons,horizon_freshness_seconds)
            cases.append(case)
    counts = {}
    for variant in VARIANTS:
        subset = [case for case in cases if case['variant']==variant]
        counts[variant] = {
            'total_sweeps':len(subset),
            'signal_seen':sum(c['signal_state']=='AVAILABLE' for c in subset),
            'fresh_entry_references':sum(c['entry_reference'] is not None for c in subset),
            'observed_180s':sum(c['outcomes'].get('180s',{}).get('state')=='OBSERVED_PROXY'
                                 for c in subset),
        }
    return {
        'schema':SCHEMA,
        'parameters':{
            'entry_freshness_seconds':entry_freshness_seconds,
            'horizon_freshness_seconds':horizon_freshness_seconds,
            'exhaustion_window_seconds':exhaustion_window_seconds,
            'min_volume_relative':min_volume_relative,
            'min_abs_imbalance':min_abs_imbalance,
            'horizons_seconds':list(horizons),
            'entry_policy':'FIRST_SUBSEQUENT_OBSERVED_1S_OPEN_REFERENCE_NOT_FILL',
            'outcome_policy':'ENDPOINT_AND_CONTIGUOUS_OBSERVED_1S_ONLY',
        },
        'source_discovery_parameters':discovery['parameters'],
        'source_discovery_events_sha256':_sha_json(discovery['events']),
        'entries':cases, 'effort_diagnostics':diagnostics,
        'summary':{
            'total_sweeps':len(discovery['events']),
            'variants':counts,
            'high_effort_sweeps':sum(d['high_effort_candidate'] for d in diagnostics),
            'high_effort_with_later_reclaim':sum(
                d['high_effort_candidate'] and d['reclaim_seen_later'] for d in diagnostics),
        },
    }


def save_entry_research_run(root, session, discovery, research, *,
                            exhaustion=None, verify_raw=True):
    """Persist an immutable EXPERIMENTAL run with source/code SHA256, not orders.

    Refuse lost original RAW by default and never write to the tape/replay.
    """
    root = Path(root)
    if exhaustion is not None:
        if exhaustion.get('schema') != 'micro-flow-v3-aggression-exhaustion-discovery-v1':
            raise DiscoveryError('Unknown exhaustion research schema')
        if exhaustion.get('source_buckets_sha256') != _sha_json(validate_buckets(session['buckets'])):
            raise DiscoveryError('Exhaustion/source Replay buckets mismatch')
    replay = Path(session['replay_path'])
    summ = Path(session['summary_path'])
    tape = Path(session['tape_path'])
    s = session['summary']
    if research['source_discovery_events_sha256'] != _sha_json(discovery['events']):
        raise DiscoveryError('Discovery events modified after analysis')
    if research['source_discovery_parameters'] != discovery['parameters']:
        raise DiscoveryError('Discovery parameters mismatch')
    if file_sha256(replay) != s['replay_jsonl_sha256']:
        raise DiscoveryError('Replay digest mismatch')
    if len(session['buckets']) != int(s['buckets_emitted']):
        raise DiscoveryError('Replay count mismatch')
    raw_status = 'NOT_REVERIFIED'
    raw_digest = None
    if verify_raw:
        from research.micro_flow_v3.tape import replay_digest
        count, digest = replay_digest(tape)
        if count != s['source_tape_rows'] or digest != s['source_tape_digest_sha256']:
            raise DiscoveryError('RAW source logical digest mismatch')
        raw_status = 'VERIFIED_LOGICAL_DIGEST'
        raw_digest = file_sha256(tape)
    name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:12]
    target = root/'reports/micro_flow_v3/sweep_entry_research/runs'/name
    target.mkdir(parents=True, exist_ok=False)
    artifacts = {}
    all_artifacts = (
        ('entries','entries.jsonl', research),
        ('effort_diagnostics','effort_diagnostics.jsonl', research),
        ('events','sweep_events.jsonl', discovery),
        ('candles','sweep_candles.jsonl', discovery),
        ('swings','sweep_swings.jsonl', discovery),
    )
    for key,filename,data in all_artifacts:
        path = target/filename
        with path.open('x',encoding='utf8') as f:
            for row in data[key]:
                f.write(json.dumps(row,sort_keys=True,allow_nan=False)+'\n')
        artifacts[filename] = file_sha256(path)
    if exhaustion is not None:
        p=target/'aggression_exhaustion_events.jsonl'
        with p.open('x',encoding='utf8') as f:
            for row in exhaustion['events']:
                f.write(json.dumps(row,sort_keys=True,allow_nan=False)+'\n')
        artifacts[p.name]=file_sha256(p)
        p=target/'aggression_exhaustion_summary.json'
        with p.open('x',encoding='utf8') as f:
            json.dump({'schema':exhaustion['schema'], 'parameters':exhaustion['parameters'],
                       'summary':exhaustion['summary']},f,indent=2,sort_keys=True,allow_nan=False)
            f.write('\n')
        artifacts[p.name]=file_sha256(p)
    sources = {
        'research/micro_flow_v3/aggression_features.py':root/'research/micro_flow_v3/aggression_features.py',
        'research/micro_flow_v3/sweep_entry_research.py':Path(__file__),
        'research/micro_flow_v3/swing_sweep_discovery.py':root/'research/micro_flow_v3/swing_sweep_discovery.py',
        'research/micro_flow_v3/replay.py':root/'research/micro_flow_v3/replay.py',
        'research/micro_flow_v3/tape.py':root/'research/micro_flow_v3/tape.py',
        'engine/live/research/swing_detector.py':root/'engine/live/research/swing_detector.py',
    }
    if exhaustion is not None:
        sources['research/micro_flow_v3/aggression_exhaustion_discovery.py'] = (
            root/'research/micro_flow_v3/aggression_exhaustion_discovery.py')
    manifest = {
        'schema':SCHEMA, 'run_id':name,
        'created_utc':datetime.now(timezone.utc).isoformat(),
        'research_role':'EXPLORATORY_NOT_VALIDATION_OOS',
        'source_sqlite':str(tape), 'source_tape_name':session['tape'],
        'source_sqlite_logical_digest_sha256':s['source_tape_digest_sha256'],
        'source_sqlite_file_sha256':raw_digest, 'raw_status_at_save':raw_status,
        'source_sqlite_rows':s['source_tape_rows'],
        'source_replay':str(replay),'source_replay_sha256':file_sha256(replay),
        'source_replay_summary':str(summ),'source_replay_summary_sha256':file_sha256(summ),
        'source_discovery_events_sha256':research['source_discovery_events_sha256'],
        'source_discovery_parameters':discovery['parameters'],
        'parameters':research['parameters'],'summary':research['summary'],
        'exhaustion_parameters':exhaustion['parameters'] if exhaustion is not None else None,
        'exhaustion_summary':exhaustion['summary'] if exhaustion is not None else None,
        'exhaustion_source_buckets_sha256':exhaustion['source_buckets_sha256'] if exhaustion is not None else None,
        'algorithm_files_sha256':{name:file_sha256(path) for name,path in sources.items()},
        'artifacts_sha256':artifacts,
        'contract':{
            'no_cross_session_or_segment':True,
            'signal_availability':'Replay available_wall_ns for reclaim/retest',
            'entry_reference':'next full observed 1s bucket OPEN after signal time; NOT a fill',
            'entry_price_known_at':'entry bucket available_wall_ns, not its opening time',
            'outcomes':'future-only, censored on missing/far observation, no cost/fill',
            'exhaustion':'independent impulse / weakening / failure; NOT proven absorption',
            'independent_exhaustion_events':exhaustion is not None,
            'immutable_source':'never edited',
        },
    }
    with (target/'manifest.json').open('x',encoding='utf8') as f:
        json.dump(manifest,f,indent=2,sort_keys=True,allow_nan=False)
        f.write('\n')
    return target
