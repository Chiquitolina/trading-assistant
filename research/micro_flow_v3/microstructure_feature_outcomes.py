"""Micro Flow V3: causal feature/outcome linkage, exploratory and read-only.

Cohort and feature values are fixed at original impulse publication. Subsequent
failure/swing-break states and returns are NEVER fed into feature membership.
Each horizon is measured from the *entry reference* (observed 1s OPEN, not fill).
Continuous-path outcomes use the unchanged `_entry_outcomes` policy. A separate
endpoint-only diagnostic can describe the last observed price with gaps, but it
must NOT be treated as tradeable, or used for MFE/MAE/TP/SL.
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

from research.micro_flow_v3.microstructure_exhaustion_features import (
    SCHEMA as FEATURE_SCHEMA, _hash_json, build_exhaustion_feature_catalog,
)
from research.micro_flow_v3.exhaustion_entry_research import (
    SCHEMA as ENTRY_SCHEMA, VARIANTS,
)
from research.micro_flow_v3.sweep_entry_research import _entry_outcomes
from research.micro_flow_v3.swing_sweep_discovery import DiscoveryError, file_sha256, validate_buckets

SCHEMA = 'micro-flow-v3-feature-outcome-horizons-v1'
HORIZONS_SECONDS = (60, 180, 300, 600, 900, 1800, 3600)
GROUP_LABELS = (
    'AGGRESSIVE_FLOW_PRICE_ALIGNED',
    'AGGRESSIVE_FLOW_LOW_PRICE_PROGRESS',
    'AGGRESSIVE_FLOW_PRICE_DISAGREEMENT',
    'FLOW_NOT_ALIGNED_WITH_IMPULSE_SIDE',
    'FEATURE_UNAVAILABLE',
)


def _aggregate(items):
    """Descriptive only; each row is an observed proxy, not independent trade."""
    n = len(items)
    return {
        'n': n,
        'mean_gross_pct': sum(x['return_directional_pct'] for x in items)/n if n else None,
        'mean_net_illustrative_pct': sum(x['net_illustrative_pct'] for x in items)/n if n else None,
        'mean_mfe_pct': sum(x['mfe_directional_pct'] for x in items)/n if n else None,
        'mean_mae_pct': sum(x['mae_directional_pct'] for x in items)/n if n else None,
        'gross_positive': sum(x['return_directional_pct']>0 for x in items),
        'net_positive_illustrative': sum(x['net_illustrative_pct']>0 for x in items),
    }


def _endpoint_only(rows, starts, entry_i, price, direction, horizon_s, freshness_s):
    """Endpoint-price diagnostic WITHOUT path completeness, never an outcome/fill.

    Only computes if endpoint in same known segment and was actually observed
    near the requested clock-time. Does not interpolate. Clearly counts gaps.
    """
    target=starts[entry_i]+horizon_s*1000
    j=bisect_left(starts,target)
    if j>=len(rows) or starts[j]-target>freshness_s*1000:
        return {'state':'NO_FRESH_ENDPOINT', 'directional_endpoint_pct':None,
                'missing_seconds':None,'max_unobserved_interval_seconds':None}
    if rows[j]['available_wall_ns']<=rows[entry_i]['available_wall_ns']:
        raise DiscoveryError('Endpoint cannot precede reference availability')
    gaps=[(starts[k]-starts[k-1])//1000 for k in range(entry_i+1,j+1)]
    missing=sum(max(0,int(g)-1) for g in gaps)
    return {'state':'ENDPOINT_OBSERVED_PATH_NOT_VERIFIED',
            'directional_endpoint_pct':direction*(float(rows[j]['close'])/price-1)*100,
            'missing_seconds':missing,'max_unobserved_interval_seconds':max(gaps,default=0),
            'endpoint_available_wall_ns':rows[j]['available_wall_ns']}


def evaluate_feature_outcome_horizons(buckets, aggression, features, entries, *,
                                      feature_window_seconds=15,
                                      horizons_seconds=HORIZONS_SECONDS,
                                      estimated_round_trip_cost_bps=12.,
                                      endpoint_freshness_seconds=3):
    """Attach strict future outcomes to *all* event IDs and both fixed variants.

    The previously calculated shorter-horizon entry study is left unchanged.
    Adds long horizons without loosening the 3s continuity policy. Endpoint-only
    diagnostic is separate and is NOT counted as a complete outcome.
    """
    horizons=tuple(int(n) for n in horizons_seconds)
    if horizons != HORIZONS_SECONDS:
        raise DiscoveryError('Versioned horizon set must stay fixed')
    if not (0<=float(estimated_round_trip_cost_bps)<=100 and
            math.isfinite(float(estimated_round_trip_cost_bps)) and
            1<=endpoint_freshness_seconds<=30):
        raise DiscoveryError('Invalid study settings')
    if features.get('schema') != FEATURE_SCHEMA or entries.get('schema') != ENTRY_SCHEMA:
        raise DiscoveryError('Feature/entry schema mismatch')
    rows=validate_buckets(buckets)
    digest=_hash_json(rows)
    if not (digest==features.get('source_buckets_sha256')==
            entries.get('source_buckets_sha256')==aggression.get('source_buckets_sha256')):
        raise DiscoveryError('Feature/entry/exhaustion Replay source mismatch')
    if (_hash_json(aggression['events'])!=entries['source_exhaustion_events_sha256']
            or _hash_json(aggression['parameters'])!=features['source_aggression_parameters_sha256']):
        raise DiscoveryError('Aggression cohort differs from feature and entry inputs')
    window_key=f'{feature_window_seconds}s'
    if window_key not in features['summary']['by_window']:
        raise DiscoveryError('Requested feature window is missing')
    by_id={f['event_id']:f for f in features['features']}
    event_ids=[x['event_id'] for x in aggression['events']]
    if (len(set(event_ids))!=len(event_ids) or len(by_id)!=len(event_ids)
            or set(by_id)!=set(event_ids)):
        raise DiscoveryError('Impulse membership mismatch')
    groups=defaultdict(list)
    for row in rows:
        groups[row['segment_id']].append(row)
    starts={seg:[r['second_start_ms'] for r in seq] for seg,seq in groups.items()}
    got=set()
    cases=[]
    cost_pct=float(estimated_round_trip_cost_bps)/100
    for case in entries['entries']:
        event_id=case['event_id']; variant=case['variant']
        k=(event_id,variant)
        if event_id not in by_id or variant not in VARIANTS or k in got:
            raise DiscoveryError('Duplicate or unknown entry case')
        got.add(k)
        feat=by_id[event_id]
        if (case['pressure_side']!=feat['pressure_side'] or
                int(case['segment_id'])!=int(feat['segment_id']) or
                case['hypothesis_direction']!=feat['hypothesis_direction']):
            raise DiscoveryError('Case side/segment mismatch')
        metric=feat['windows'][window_key]
        label=(metric.get('flow_price_alignment') if metric['state']=='OBSERVED'
               else 'FEATURE_UNAVAILABLE')
        if label not in GROUP_LABELS:
            raise DiscoveryError('Unknown versioned alignment label')
        ref=case.get('entry_reference')
        results={}
        if ref:
            seq=groups[int(case['segment_id'])]
            ss=starts[int(case['segment_id'])]
            i=bisect_left(ss,int(ref['entry_reference_second_start_ms']))
            if i>=len(seq) or ss[i]!=int(ref['entry_reference_second_start_ms']):
                raise DiscoveryError('Reference not found in its segment')
            r=seq[i]
            if (int(r['available_wall_ns'])!=int(ref['entry_reference_available_wall_ns']) or
                int(r['first_seq'])!=int(ref['entry_reference_first_seq']) or
                float(r['open'])!=float(ref['entry_reference_price'])):
                raise DiscoveryError('Entry reference differs from Replay')
            if r['available_wall_ns']<=feat['signal_available_wall_ns']:
                raise DiscoveryError('Entry precedes feature publication')
            signal=case.get('signal')
            if not signal or r['available_wall_ns']<=signal['available_wall_ns']:
                raise DiscoveryError('Entry precedes confirmation publication')
            side='HIGH' if case['pressure_side']=='BUY' else 'LOW'
            obs=_entry_outcomes(seq,ss,i,float(ref['entry_reference_price']),side,
                                horizons,endpoint_freshness_seconds)
            direction=-1 if side=='HIGH' else 1
            for h in horizons:
                outcome=obs[f'{h}s']
                complete=outcome['state']=='OBSERVED_PROXY'
                diagnostic=(_endpoint_only(seq,ss,i,float(ref['entry_reference_price']),
                               direction,h,endpoint_freshness_seconds)
                            if not complete else None)
                results[f'{h}s']={**outcome,
                   'net_illustrative_pct': (outcome['return_directional_pct']-cost_pct
                                                if complete else None),
                   'endpoint_only_diagnostic':diagnostic}
        else:
            for h in horizons:
                results[f'{h}s']={'state':'NO_ENTRY_REFERENCE',
                    'return_directional_pct':None,'net_illustrative_pct':None,
                    'mfe_directional_pct':None,'mae_directional_pct':None,
                    'endpoint_only_diagnostic':None}
        cases.append({
            'event_id':event_id,'variant':variant,'segment_id':int(case['segment_id']),
            'pressure_side':feat['pressure_side'],
            'hypothesis_direction':feat['hypothesis_direction'],
            'impulse_available_wall_ns':feat['signal_available_wall_ns'],
            'impulse_seq_range':[feat['signal_first_seq'],feat['signal_last_seq']],
            'feature_window':window_key,'feature_state':metric['state'],
            'feature_group':label,'feature_data':metric if metric['state']=='OBSERVED' else None,
            'feature_rejection':(metric.get('rejection_after_prior_extreme_observed')
                                  if metric['state']=='OBSERVED' else None),
            'confirmation_state':case['signal_state'],'reference_state':case['entry_state'],
            'entry_reference':ref,'horizons':results,
            'research_only':True,'fill_simulated':False,
        })
    if got != {(id_,v) for id_ in event_ids for v in VARIANTS}:
        raise DiscoveryError('A/B rows missing original impulses')
    # fixed cohorts, never optimized or chosen according to their future returns
    summaries=[]
    for variant in VARIANTS:
        for side in ('BUY','SELL'):
            sample=[e for e in cases if e['variant']==variant and e['pressure_side']==side]
            for kind,labels in [('alignment',GROUP_LABELS),('rejection',('TRUE','FALSE','FEATURE_UNAVAILABLE'))]:
                for label in labels:
                    def group(e):
                        if kind=='alignment': return e['feature_group']
                        r=e['feature_rejection']
                        return 'FEATURE_UNAVAILABLE' if r is None else ('TRUE' if r else 'FALSE')
                    subset=[e for e in sample if group(e)==label]
                    for h in horizons:
                        key=f'{h}s'
                        good=[e['horizons'][key] for e in subset if e['horizons'][key]['state']=='OBSERVED_PROXY']
                        stats=_aggregate(good)
                        summaries.append({'variant':variant,'pressure_side':side,
                            'group_dimension':kind,'group':label,'horizon_seconds':h,
                            'impulses':len(subset),
                            'entry_references':sum(e['entry_reference'] is not None for e in subset),
                            'strict_observed':len(good),
                            'not_strict_observed':len(subset)-len(good),
                            'endpoint_only_diagnostics':sum(
                               e['horizons'][key].get('endpoint_only_diagnostic',{}).get('state')==
                               'ENDPOINT_OBSERVED_PATH_NOT_VERIFIED'
                               for e in subset if e['horizons'][key].get('endpoint_only_diagnostic')),
                            **stats})
    # paired only: cases where both variants are strictly observed, keep source IDs
    paired=[]
    by_case={(x['event_id'],x['variant']):x for x in cases}
    for h in horizons:
        key=f'{h}s'
        diffs=[]
        for eid in event_ids:
            a=by_case[(eid,'FAILURE')]['horizons'][key]
            b=by_case[(eid,'FAILURE_SWING_BREAK')]['horizons'][key]
            if a['state']==b['state']=='OBSERVED_PROXY':
                diffs.append(b['net_illustrative_pct']-a['net_illustrative_pct'])
        paired.append({'horizon_seconds':h,'both_observed':len(diffs),
                       'mean_B_minus_A_pct':sum(diffs)/len(diffs) if diffs else None,
                       'interpretation':'CONDITIONAL_NON_RANDOM_PAIRED_COMPARISON'})
    return {'schema':SCHEMA,'research_only':True,
        'source_buckets_sha256':digest,
        'source_features_sha256':_hash_json(features),
        'source_entry_sha256':_hash_json(entries),
        'source_exhaustion_events_sha256':_hash_json(aggression['events']),
        'parameters':{'feature_window_seconds':feature_window_seconds,
            'horizons_seconds':list(horizons),
            'estimated_round_trip_cost_bps':float(estimated_round_trip_cost_bps),
            'endpoint_freshness_seconds':endpoint_freshness_seconds,
            'strict_outcome_policy':'SAME_SEGMENT_CONTINUOUS_OBSERVED_1S_PATH_MAX_GAP',
            'endpoint_diagnostic_policy':'SAME_SEGMENT_FRESH_PRICE_ONLY_WITH_UNVERIFIED_PATH',
            'research_cohort':'ALL_ORIGINAL_IMPULSES_TIMES_TWO_ENTRY_VARIANTS',
            'dataset_role':'EXPLORATORY_NO_OOS_CLAIM'},
        'cases':cases,'summary':summaries,'paired':paired}


def save_feature_outcome_run(root, session, aggression, features, entries, study):
    """Persist verified immutable files; never rewrite RAW/Replay, fail closed."""
    if study.get('schema')!=SCHEMA:
        raise DiscoveryError('Invalid feature/outcome schema')
    params=study['parameters']
    again=evaluate_feature_outcome_horizons(session['buckets'],aggression,features,entries,
          feature_window_seconds=params['feature_window_seconds'],
          estimated_round_trip_cost_bps=params['estimated_round_trip_cost_bps'],
          endpoint_freshness_seconds=params['endpoint_freshness_seconds'])
    if _hash_json(again)!=_hash_json(study):
        raise DiscoveryError('Feature/outcome run differs from deterministic calculation')
    root=Path(root)
    summary=session['summary']
    raw=Path(session['tape_path'])
    replay=Path(session['replay_path'])
    sfile=Path(session['summary_path'])
    if (not raw.is_file() or raw.stat().st_size<=0 or
            not replay.is_file() or not sfile.is_file()):
        raise DiscoveryError('Missing original RAW or Replay for saving')
    if file_sha256(replay)!=summary['replay_jsonl_sha256']:
        raise DiscoveryError('Replay SHA256 mismatch')
    from research.micro_flow_v3.tape import replay_digest
    tape_rows,digest=replay_digest(raw)
    if tape_rows != summary['source_tape_rows'] or digest != summary['source_tape_digest_sha256']:
        raise DiscoveryError('RAW logical digest mismatch')
    output=root/'reports/micro_flow_v3/microstructure_feature_outcomes/runs'/(
          datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'_'+uuid.uuid4().hex[:12])
    output.mkdir(parents=True, exist_ok=False)
    def dump_json(name,value):
        p=output/name
        with p.open('x',encoding='utf-8') as f:
            json.dump(value,f,sort_keys=True,ensure_ascii=False,indent=2,allow_nan=False)
            f.write('\n')
        return p
    cases=output/'cases.jsonl'
    with cases.open('x',encoding='utf-8') as f:
        for row in study['cases']:
            f.write(json.dumps(row,sort_keys=True,separators=(',', ':'),allow_nan=False)+'\n')
    aggregates=dump_json('aggregates.json',{'summary':study['summary'],'paired':study['paired']})
    required=[
      'research/micro_flow_v3/microstructure_feature_outcomes.py',
      'research/micro_flow_v3/microstructure_exhaustion_features.py',
      'research/micro_flow_v3/exhaustion_entry_research.py',
      'research/micro_flow_v3/sweep_entry_research.py',
      'research/micro_flow_v3/aggression_exhaustion_discovery.py',
      'research/micro_flow_v3/replay.py',
    ]
    algorithm={name:file_sha256(root/name) for name in required}
    manifest={'schema':SCHEMA,'run_id':output.name,'symbol':summary['symbol'],
        'created_utc':datetime.now(timezone.utc).isoformat(),
        'source_tape':session['tape'],'raw_path':str(raw),
        'raw_rows':tape_rows,'raw_logical_sha256':digest,'raw_file_sha256':file_sha256(raw),
        'replay_path':str(replay),'replay_sha256':file_sha256(replay),
        'replay_summary_sha256':file_sha256(sfile),
        'study_input_sha256':{'features':study['source_features_sha256'],
           'entries':study['source_entry_sha256'],
           'events':study['source_exhaustion_events_sha256'],
           'buckets':study['source_buckets_sha256']},
        'parameters':study['parameters'],
        'algorithm_sha256':algorithm,
        'artifacts_sha256':{'cases.jsonl':file_sha256(cases),'aggregates.json':file_sha256(aggregates)},
        'research_contract':{
            'source':'AS_OBSERVED_SAME_SEGMENT',
            'membership':'ALL_IMPULSES_AND_BOTH_VARIANTS_INCLUDING_NO_SIGNAL',
            'features':'IMPULSE_AVAILABLE_TIME_ONLY',
            'outcomes':'STRICT_CONTIGUOUS_PATH_AFTER_REFERENCE_ONLY',
            'diagnostics':'ENDPOINT_ONLY_WITH_GAPS_IS_NOT_A_TRADE_RESULT',
            'execution':'NO_FILLED_TRADES_AND_12BPS_COST_ILLUSTRATIVE',
            'validation':'EXPLORATORY_NO_INDEPENDENT_OOS',
        }}
    dump_json('manifest.json',manifest)
    return output
