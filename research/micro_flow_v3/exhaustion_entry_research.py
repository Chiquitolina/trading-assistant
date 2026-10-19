"""Micro Flow V3 | causal failure-vs-structural-break entry RESEARCH.

No market orders, no prediction by hindsight, no write to original RAW/Replay.
The existing independent exhaustion impulse cohort is unchanged. Every impulse
is retained in both policies, including those without a signal or a level.

A FAILURE: first observed second AFTER the already published failure.
B SWING_BREAK: after that failure, wait for a later 1s CLOSE across a 1m/5m
  SwingDetector level that was ALREADY published before the impulse.

1s subsequent OPEN = retrospective *reference*, NEVER an executable price.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import math
import sqlite3
import uuid

from research.micro_flow_v3.swing_sweep_discovery import (
    DiscoveryError, file_sha256, validate_buckets,
)
from research.micro_flow_v3.sweep_entry_research import (
    _next_open_reference, _entry_outcomes,
)

SCHEMA = 'micro-flow-v3-exhaustion-entry-research-v1'
VARIANTS = ('FAILURE', 'FAILURE_SWING_BREAK')
HORIZONS_S = (60, 180, 300, 600, 900, 1800)


def _hash_json(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                             allow_nan=False).encode('utf-8')).hexdigest()


def _stage(row, label):
    return {
        'stage': label, 'observed_second_start_ms': row['second_start_ms'],
        'available_wall_ns': row['available_wall_ns'],
        'available_ms': (row['available_wall_ns']+999_999)//1_000_000,
        'observed_close': row['close'],
        'first_seq': row['first_seq'], 'last_seq': row['last_seq'],
    }


def _known_swing(event, swings, *, swing_max_age_minutes):
    """Select newest *known* level on the correct side of the impulse price.

    The pre-existing 'swing' is confirmed by complete candles using the frozen
    SwingDetector. We require timestamp and level to be available before impulse
    publication, and level to still be on the intended break side at impulse.
    Never choose a swing based on where price moves after the impulse.
    """
    impulse = event['impulse']
    seg = int(event['segment_id'])
    side = 'LOW' if event['pressure_side']=='BUY' else 'HIGH'
    price = float(impulse['observed_close'])
    cutoff = impulse['available_wall_ns'] - int(swing_max_age_minutes*60*1e9)
    eligible = [s for s in swings if
        int(s['segment_id'])==seg and s['side']==side and
        cutoff <= int(s['confirmed_available_wall_ns']) < impulse['available_wall_ns'] and
        ((float(s['level']) < price) if side=='LOW' else (float(s['level']) > price))]
    if not eligible:
        return None
    chosen=max(eligible, key=lambda s:(s['confirmed_available_wall_ns'],s['swing_id']))
    return {
        'swing_id':chosen['swing_id'],
        'timeframe_minutes':chosen['timeframe_minutes'],
        'bars':chosen['bars'], 'side':chosen['side'],
        'level':float(chosen['level']),
        'pivot_ms':chosen['pivot_ms'],
        'confirmed_available_wall_ns':chosen['confirmed_available_wall_ns'],
        'confirmed_available_ms':chosen['confirmed_available_ms'],
        'source_policy':'NEWEST_PRE_IMPULSE_CONFIRMED_SWING_ON_BREAK_SIDE',
    }


def _structural_break(event, data, starts, level, *, break_tolerance_pct,
                      gap_seconds, followup_seconds):
    """Require failure before breakout. Never jump over an unobserved gap."""
    failure = event.get('failure')
    if failure is None or level is None:
        return None, None
    failure_start = int(failure['observed_second_start_ms'])
    i = bisect_left(starts, failure_start)
    if i >= len(data) or starts[i]!=failure_start or data[i]['available_wall_ns']!=failure['available_wall_ns']:
        raise DiscoveryError('Failure stage does not match source Replay')
    side = event['pressure_side']
    limit = int(event['impulse']['observed_second_start_ms'])+followup_seconds*1000
    prev = failure_start
    for j in range(i+1,len(data)):
        row=data[j]
        now=row['second_start_ms']
        if now > limit:
            return None, 'NO_BREAK_WITHIN_FOLLOWUP'
        if now-prev > gap_seconds*1000:
            return None, 'CENSORED_OBSERVATION_GAP'
        prev=now
        if row['available_wall_ns']<=failure['available_wall_ns']:
            raise DiscoveryError('Structural break was not published after failure')
        close=float(row['close'])
        if (side=='BUY' and close < float(level['level'])*(1-break_tolerance_pct/100)) or (
            side=='SELL' and close > float(level['level'])*(1+break_tolerance_pct/100)):
            return _stage(row,'POST_FAILURE_CONFIRMED_SWING_BREAK'), 'AVAILABLE'
    return None, 'CENSORED_SESSION_END'


def evaluate_exhaustion_entries(buckets, exhaustion, swings, *,
                                horizons=HORIZONS_S, estimated_round_trip_cost_bps=12.0,
                                swing_max_age_minutes=120,
                                break_tolerance_pct=0.01,
                                entry_freshness_seconds=3,
                                horizon_freshness_seconds=3):
    """Two causal entry hypotheses over *all* existing impulse events.

    Outcomes/costs never gate candidate membership or signal selection.
    The A/B subsets differ due to an additional future observable confirmation;
    paired differences are conditional, NOT randomized treatment effects.
    """
    if tuple(horizons)!=HORIZONS_S:
        raise DiscoveryError('Research horizons fixed at 60/180/300/600/900/1800s')
    if (not math.isfinite(float(estimated_round_trip_cost_bps)) or
        not 0<=estimated_round_trip_cost_bps<=100 or
        not 1<=swing_max_age_minutes<=1440 or
        not 0<=break_tolerance_pct<=1 or
        not 1<=entry_freshness_seconds<=30 or
        not 1<=horizon_freshness_seconds<=30):
        raise DiscoveryError('Invalid causal entry study parameters')
    if exhaustion.get('schema')!='micro-flow-v3-aggression-exhaustion-discovery-v1':
        raise DiscoveryError('Exhaustion detector version mismatch')
    rows=validate_buckets(buckets)
    if _hash_json(rows)!=exhaustion['source_buckets_sha256']:
        raise DiscoveryError('Replay changed since exhaustion discovery')
    if not isinstance(swings,list):
        raise DiscoveryError('Known swing snapshot required')
    groups=defaultdict(list)
    for r in rows:
        groups[r['segment_id']].append(r)
    for seg in groups:
        groups[seg].sort(key=lambda r:r['second_start_ms'])
    starts={seg:[r['second_start_ms'] for r in data] for seg,data in groups.items()}
    # This cohort is independent of selected swings, but the B confirmation uses
    # a swing snapshot generated in the selected 1m or 5m analysis.
    cases=[]
    ids=set()
    for e in exhaustion['events']:
        event_id=e['event_id']
        if event_id in ids:
            raise DiscoveryError('Duplicate impulse event ID')
        ids.add(event_id)
        seg=int(e['segment_id'])
        if seg not in groups:
            raise DiscoveryError('Impulse references missing causal segment')
        data=groups[seg]
        ss=starts[seg]
        impulse=e['impulse']
        at=bisect_left(ss,int(impulse['observed_second_start_ms']))
        if at>=len(data) or ss[at]!=impulse['observed_second_start_ms'] or (
            data[at]['available_wall_ns']!=impulse['available_wall_ns']):
            raise DiscoveryError('Impulse stage does not match Replay')
        failure=e.get('failure')
        if failure and failure['available_wall_ns']<=impulse['available_wall_ns']:
            raise DiscoveryError('Failure cannot precede impulse')
        known=_known_swing(e,swings,swing_max_age_minutes=swing_max_age_minutes)
        break_stage, break_state = _structural_break(
            e,data,ss,known,break_tolerance_pct=break_tolerance_pct,
            gap_seconds=exhaustion['parameters']['max_observation_gap_seconds'],
            followup_seconds=exhaustion['parameters']['followup_seconds'],
        )
        for variant in VARIANTS:
            stage = failure if variant=='FAILURE' else break_stage
            if stage is None:
                if failure is None:
                    state = ('NO_FAILURE_CENSORED' if e['followup_state']!='FOLLOWUP_WINDOW_COMPLETED'
                             else 'NO_FAILURE')
                elif variant=='FAILURE_SWING_BREAK':
                    state = 'NO_PREKNOWN_SWING' if known is None else break_state
                else:
                    raise DiscoveryError('Inconsistent failure classification')
            else:
                state='AVAILABLE'
            ref=None
            outcomes={}
            entry_state='NO_SIGNAL' if stage is None else 'PENDING'
            if stage is not None:
                j, ref=_next_open_reference(data,ss,stage,
                                             max_lag_seconds=entry_freshness_seconds)
                if ref is None:
                    entry_state='CENSORED_NO_NEXT_OBSERVED_SECOND'
                else:
                    entry_state='REFERENCE_NOT_EXECUTABLE_FILL'
                    raw=_entry_outcomes(data,ss,j,ref['entry_reference_price'],
                        'HIGH' if e['pressure_side']=='BUY' else 'LOW',
                        horizons,horizon_freshness_seconds)
                    for key, o in raw.items():
                        gross=o.get('return_directional_pct')
                        # Fixed illustrative trading cost, not observed execution.
                        outcomes[key]={**o,
                            'estimated_round_trip_cost_bps':float(estimated_round_trip_cost_bps),
                            'net_after_illustrative_cost_pct':(
                                gross-estimated_round_trip_cost_bps/100
                                if gross is not None else None),
                            'outcome_reference_kind':'RETROSPECTIVE_1S_OPEN_NOT_FILL',
                        }
            cases.append({
                'event_id':event_id, 'segment_id':seg,
                'variant':variant, 'pressure_side':e['pressure_side'],
                'hypothesis_direction':e['hypothesis_direction'],
                'impulse':impulse, 'impulse_features':e['impulse_features'],
                'weakening':e.get('weakening'), 'failure':failure,
                'followup_state':e['followup_state'],
                'preknown_swing':known,
                'break_stage':break_stage if variant=='FAILURE_SWING_BREAK' else None,
                'signal_state':state, 'signal':stage,
                'signal_available_wall_ns':stage['available_wall_ns'] if stage else None,
                'entry_state':entry_state, 'entry_reference':ref,
                'outcomes':outcomes,
                'costs_not_observed':True,
                'trade_simulated':False,
            })
    if len(cases)!=2*len(exhaustion['events']):
        raise DiscoveryError('Some impulse candidates were silently omitted')
    by_variant={}
    for variant in VARIANTS:
        subset=[c for c in cases if c['variant']==variant]
        by_side={}
        for side in ('BUY','SELL'):
            sidecases=[c for c in subset if c['pressure_side']==side]
            per_h={}
            for horizon in horizons:
                k=f'{horizon}s'
                observed=[c['outcomes'][k] for c in sidecases
                          if c['outcomes'].get(k,{}).get('state')=='OBSERVED_PROXY']
                n=len(observed)
                per_h[k]={
                    'observed':n, 'censored_or_no_signal':len(sidecases)-n,
                    'mean_gross_pct':sum(o['return_directional_pct'] for o in observed)/n if n else None,
                    'mean_net_illustrative_pct':sum(o['net_after_illustrative_cost_pct'] for o in observed)/n if n else None,
                    'mean_mfe_pct':sum(o['mfe_directional_pct'] for o in observed)/n if n else None,
                    'mean_mae_pct':sum(o['mae_directional_pct'] for o in observed)/n if n else None,
                    'positive_gross':sum(o['return_directional_pct']>0 for o in observed),
                    'positive_net_illustrative':sum(o['net_after_illustrative_cost_pct']>0 for o in observed),
                }
            by_side[side]={
                'impulses':len(sidecases),
                'failures':sum(c['failure'] is not None for c in sidecases),
                'swing_available_at_impulse':sum(c['preknown_swing'] is not None for c in sidecases),
                'signals':sum(c['signal_state']=='AVAILABLE' for c in sidecases),
                'entry_references':sum(c['entry_reference'] is not None for c in sidecases),
                'horizons':per_h,
            }
        by_variant[variant]=by_side
    # Paired subset: no comparison of unpaired populations as evidence of edge.
    paired={}
    for horizon in horizons:
        key=f'{horizon}s'
        pairs=defaultdict(dict)
        for case in cases:
            if case['outcomes'].get(key,{}).get('state')=='OBSERVED_PROXY':
                pairs[case['event_id']][case['variant']]=case['outcomes'][key]
        both=[v for v in pairs.values() if all(k in v for k in VARIANTS)]
        paired[key]={
            'paired_observed_impulses':len(both),
            'mean_net_B_minus_A_pct':(
                sum(x['FAILURE_SWING_BREAK']['net_after_illustrative_cost_pct']-
                    x['FAILURE']['net_after_illustrative_cost_pct'] for x in both)/len(both)
                if both else None),
            'warning':'PAIRED_DESCRIPTIVE_ONLY_NOT_RANDOMIZED_OR_EXECUTABLE',
        }
    return {
        'schema':SCHEMA,
        'source_exhaustion_schema':exhaustion['schema'],
        'source_exhaustion_events_sha256':_hash_json(exhaustion['events']),
        'source_exhaustion_parameters':exhaustion['parameters'],
        'source_buckets_sha256':exhaustion['source_buckets_sha256'],
        'source_preknown_swings_sha256':_hash_json(swings),
        'parameters':{
            'horizons_seconds':list(horizons),
            'estimated_round_trip_cost_bps':float(estimated_round_trip_cost_bps),
            'swing_max_age_minutes':swing_max_age_minutes,
            'break_tolerance_pct':break_tolerance_pct,
            'entry_freshness_seconds':entry_freshness_seconds,
            'horizon_freshness_seconds':horizon_freshness_seconds,
            'entry_policy':'NEXT_FULLY_LATER_OBSERVED_1S_OPEN_REFERENCE_NOT_FILL',
            'break_policy':'CLOSE_BEYOND_PREKNOWN_SWING_IN_LATER_1S_AFTER_FAILURE',
            'outcome_policy':'CENSOR_NONCONTIGUOUS_OR_MISSING_ENDPOINT',
            'dataset_role':'EXPLORATORY_NEVER_AUTOMATIC_OOS',
        },
        'entries':cases,
        'summary':{
            'impulses':len(exhaustion['events']),
            'rows':len(cases),
            'by_variant':by_variant,
            'paired':paired,
            'limitation':'EVENTS_CAN_OVERLAP; RETURNS ARE OBSERVED_PRICE_PROXIES, NOT FILLS',
        },
    }


def save_exhaustion_entry_run(root, session, exhaustion, swings, study, *, verify_raw=True):
    """Write-only research artifacts after source checks; never touch tape/replay.

    Runs are unique new directories. Refuses to persist when RAW digest differs.
    """
    root=Path(root)
    if study.get('schema')!=SCHEMA or exhaustion.get('schema')!=study['source_exhaustion_schema']:
        raise DiscoveryError('Study/exhaustion schema mismatch')
    if (_hash_json(exhaustion['events'])!=study['source_exhaustion_events_sha256'] or
        study['source_exhaustion_parameters']!=exhaustion['parameters'] or
        _hash_json(swings)!=study['source_preknown_swings_sha256']):
        raise DiscoveryError('Research input cohort or structural levels changed')
    if _hash_json(validate_buckets(session['buckets']))!=study['source_buckets_sha256']:
        raise DiscoveryError('Study is not from provided Replay')
    replay=Path(session['replay_path'])
    summary_path=Path(session['summary_path'])
    tape=Path(session['tape_path'])
    source_summary=session['summary']
    actual_replay_hash=file_sha256(replay)
    if actual_replay_hash!=source_summary['replay_jsonl_sha256']:
        raise DiscoveryError('Source Replay SHA256 mismatch')
    if int(source_summary['buckets_emitted'])!=len(session['buckets']):
        raise DiscoveryError('Source Replay row count mismatch')
    tape_digest_status='NOT_REVERIFIED'
    tape_file_sha=None
    if verify_raw:
        # Refuse incomplete/actively written tapes, even if their current row
        # digest happens to match a stale replay summary.
        with sqlite3.connect(tape.resolve().as_uri()+'?mode=ro',
                             uri=True,timeout=2) as con:
            last=con.execute('SELECT kind, reason FROM tape ORDER BY seq DESC LIMIT 1').fetchone()
        if last!=('MARKER','PROCESS_STOP'):
            raise DiscoveryError('RAW source is not a closed PROCESS_STOP tape')
        from research.micro_flow_v3.tape import replay_digest
        n,digest=replay_digest(tape)
        if (n!=int(source_summary['source_tape_rows']) or
            digest!=source_summary['source_tape_digest_sha256']):
            raise DiscoveryError('RAW logical digest mismatch')
        tape_digest_status='VERIFIED_LOGICAL_DIGEST'
        tape_file_sha=file_sha256(tape)
    now=datetime.now(timezone.utc)
    run_id=now.strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:12]
    output=root/'reports/micro_flow_v3/exhaustion_entry_research/runs'/run_id
    output.mkdir(parents=True,exist_ok=False)
    artifacts={}
    for name,records in (
        ('entries.jsonl', study['entries']),
        ('impulses.jsonl', exhaustion['events']),
        ('known_swings.jsonl', swings),
    ):
        dest=output/name
        with dest.open('x',encoding='utf-8') as f:
            for row in records:
                f.write(json.dumps(row,sort_keys=True,allow_nan=False)+'\n')
        artifacts[name]=file_sha256(dest)
    with (output/'results_summary.json').open('x',encoding='utf-8') as f:
        json.dump(study['summary'],f,indent=2,sort_keys=True,allow_nan=False)
        f.write('\n')
    artifacts['results_summary.json']=file_sha256(output/'results_summary.json')
    code_paths={
        'research/micro_flow_v3/exhaustion_entry_research.py':Path(__file__),
        'research/micro_flow_v3/aggression_exhaustion_discovery.py':root/'research/micro_flow_v3/aggression_exhaustion_discovery.py',
        'research/micro_flow_v3/aggression_features.py':root/'research/micro_flow_v3/aggression_features.py',
        'research/micro_flow_v3/sweep_entry_research.py':root/'research/micro_flow_v3/sweep_entry_research.py',
        'research/micro_flow_v3/swing_sweep_discovery.py':root/'research/micro_flow_v3/swing_sweep_discovery.py',
        'research/micro_flow_v3/replay.py':root/'research/micro_flow_v3/replay.py',
        'research/micro_flow_v3/tape.py':root/'research/micro_flow_v3/tape.py',
        'engine/live/research/swing_detector.py':root/'engine/live/research/swing_detector.py',
    }
    manifest={
        'schema':SCHEMA,'run_id':run_id,
        'created_utc':now.isoformat(),
        'research_role':'EXPLORATORY_NOT_OOS_VALIDATION',
        'source_sqlite':str(tape),
        'source_sqlite_rows':source_summary['source_tape_rows'],
        'source_sqlite_logical_sha256':source_summary['source_tape_digest_sha256'],
        'source_sqlite_file_sha256':tape_file_sha,
        'source_sqlite_verification':tape_digest_status,
        'source_replay':str(replay),
        'source_replay_sha256':actual_replay_hash,
        'source_replay_summary':str(summary_path),
        'source_replay_summary_sha256':file_sha256(summary_path),
        'source_buckets_sha256':study['source_buckets_sha256'],
        'source_exhaustion_events_sha256':study['source_exhaustion_events_sha256'],
        'source_exhaustion_parameters':study['source_exhaustion_parameters'],
        'source_preknown_swings_sha256':study['source_preknown_swings_sha256'],
        'parameters':study['parameters'],
        'summary':study['summary'],
        'algorithm_files_sha256':{key:file_sha256(p) for key,p in code_paths.items()},
        'artifacts_sha256':artifacts,
        'contract':{
            'immutable_sources':'NEVER_MODIFY_RAW_OR_REPLAY',
            'cohort':'ALL_DETECTED_IMPULSES_IN_BOTH_POLICIES',
            'structure':'CONFIRMED_1M_OR_5M_SWING_AVAILABLE_BEFORE_IMPULSE',
            'signal':'OBSERVED_1S_CLOSE_AFTER_FAILURE; WALL_NS_AVAILABILITY',
            'entry':'LATER_OBSERVED_SECOND_OPEN_PROXY_KNOWN_ONLY_WHEN_BUCKET_PUBLISHED',
            'outcomes':'FUTURE_OBSERVATIONS_ONLY; CENSORED_ON_GAPS; NO_FILLS',
            'costs':'ILLUSTRATIVE_FIXED_BPS_NOT_EXECUTION',
            'leakage_policy':'STRUCTURE_SELECTED_WITHOUT_LATER_PRICES',
            'oos_validation':False,
        },
    }
    with (output/'manifest.json').open('x',encoding='utf-8') as f:
        json.dump(manifest,f,indent=2,sort_keys=True,allow_nan=False)
        f.write('\n')
    return output
