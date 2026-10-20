"""Micro Flow V3: strictly PRE-impulse price/flow/structure research context.

Research-only. No detector or entry policy is changed. The prior windows end
before the aggressive impulse's initial 1s observation, not at its hindsight
failure or its 1s publication. No trades or candle seconds are interpolated.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import csv
import json
import math
import uuid

from research.micro_flow_v3.microstructure_exhaustion_features import _hash_json
from research.micro_flow_v3.swing_sweep_discovery import DiscoveryError, validate_buckets, file_sha256

SCHEMA = 'micro-flow-v3-pre-impulse-structure-context-v1'
STUDY_SCHEMA = 'micro-flow-v3-context-cohorts-outcomes-v1'
CONTEXT_WINDOWS_MINUTES = (5, 10)
HORIZONS_SECONDS = (60, 180, 300, 600, 900, 1800, 3600)
COHORTS = ('ALL_IMPULSES','DIVERGENCE_ONLY','DIVERGENCE_AND_REJECTION')


def _prior_window(rows, starts, last_idx, minutes, *, min_coverage=.60,
                  max_observed_gap_seconds=10):
    """Returns explicit provenance and coverage; never labels gaps as no-trade."""
    end = starts[last_idx]
    width = minutes*60
    begin = end - (width-1)*1000
    j = bisect_left(starts, begin, 0, last_idx+1)
    sample = rows[j:last_idx+1]
    gaps = [int(starts[k]-starts[k-1])//1000 for k in range(j+1,last_idx+1)]
    missing = sum(max(0,g-1) for g in gaps)
    coverage = len(sample)/width
    meta = {'state':'INSUFFICIENT_PRIOR_CONTEXT', 'minutes':minutes,
        'window_start_ms':begin, 'window_end_second_ms':end,
        'observed_seconds':len(sample),'coverage_fraction':coverage,
        'max_observed_gap_seconds':max(gaps,default=0),
        'missing_internal_seconds':missing,
        'available_wall_ns':int(rows[last_idx]['available_wall_ns']),
        'last_pre_impulse_seq':rows[last_idx]['last_seq'],
        'first_seq':sample[0]['first_seq'] if sample else None}
    if (coverage < min_coverage or not sample or
            sample[0]['second_start_ms']-begin > max_observed_gap_seconds*1000 or
            max(gaps,default=0)>max_observed_gap_seconds):
        return meta
    op, close = float(sample[0]['open']), float(sample[-1]['close'])
    hi = max(float(r['high']) for r in sample)
    lo = min(float(r['low']) for r in sample)
    buy = sum(float(r['buy_qty']) for r in sample)
    sell = sum(float(r['sell_qty']) for r in sample)
    gross = buy+sell
    return {**meta,'state':'OBSERVED_CONTEXT_WITH_EXPLICIT_GAPS',
        'open_price':op,'close_price':close,'return_pct':(close/op-1)*100,
        'cvd_delta_qty':buy-sell,'gross_executed_qty':gross,
        'imbalance':(buy-sell)/gross if gross else None,
        'range_high':hi,'range_low':lo,
        'range_position':((close-lo)/(hi-lo)) if hi>lo else None,
        'range_pct':(hi-lo)/op*100,
        'note':'An observed-second context window; does NOT certify feed health.'}


def _last_known_swing(swings, *, segment, side, pre_available_ns, pre_time_ms):
    eligible = [s for s in swings if int(s['segment_id'])==segment and
        s['side']==side and int(s['confirmed_available_wall_ns']) <= pre_available_ns and
        int(s['pivot_ms']) < pre_time_ms]
    if not eligible:
        return None
    s=max(eligible,key=lambda x:(int(x['confirmed_available_wall_ns']),x['swing_id']))
    return {'swing_id':s['swing_id'],'level':float(s['level']),
            'confirmed_available_wall_ns':int(s['confirmed_available_wall_ns']),
            'pivot_ms':int(s['pivot_ms']), 'timeframe_minutes':int(s['timeframe_minutes'])}


def build_pre_impulse_context(buckets, aggression, swings_by_timeframe, *,
                              min_coverage=.60, max_observed_gap_seconds=10):
    """One context row for EVERY pre-existing aggressive-impulse event.

    Cohort uses only price and CVD sign before the impulse window; range and
    swings are independent, continuous descriptive features, not entry gates.
    """
    if not (0 < min_coverage <= 1 and 1 <= max_observed_gap_seconds <= 60):
        raise DiscoveryError('Invalid context coverage policy')
    data=validate_buckets(buckets)
    digest=_hash_json(data)
    if aggression.get('source_buckets_sha256')!=digest:
        raise DiscoveryError('Aggression and context Replay differ')
    if set(swings_by_timeframe) != {1,5}:
        raise DiscoveryError('Context requires separately confirmed 1m and 5m swings')
    for tf,ss in swings_by_timeframe.items():
        if any(int(s['timeframe_minutes'])!=tf for s in ss):
            raise DiscoveryError('Swing timeframe/level mismatch')
    groups=defaultdict(list)
    for r in data:
        groups[r['segment_id']].append(r)
    starts={seg:[r['second_start_ms'] for r in rows] for seg,rows in groups.items()}
    context=[]
    seen=set()
    for e in aggression['events']:
        eid=e['event_id']
        if eid in seen:
            raise DiscoveryError('Duplicate impulse event ID')
        seen.add(eid)
        seg=int(e['segment_id'])
        if seg not in groups or e['pressure_side'] not in ('BUY','SELL'):
            raise DiscoveryError('Bad event segment or side')
        seq,ss=groups[seg],starts[seg]
        signal_ms=int(e['impulse']['observed_second_start_ms'])
        idx=bisect_left(ss,signal_ms)
        if idx==len(ss) or ss[idx]!=signal_ms or int(seq[idx]['available_wall_ns'])!=int(e['impulse']['available_wall_ns']):
            raise DiscoveryError('Impulsive second differs from Replay')
        # This point is *strictly before* the 5-second impulse starts.
        impulse_start=int(e['impulse_features']['window_start_ms'])
        j=bisect_left(ss,impulse_start)-1
        windows={}
        pre_ns=None
        prior_price=None
        swings={}
        group='CONTEXT_UNAVAILABLE'
        if j>=0:
            pre_ns=int(seq[j]['available_wall_ns'])
            if pre_ns>=int(e['impulse']['available_wall_ns']) or ss[j]>=impulse_start:
                raise DiscoveryError('Pre-impulse context leaks into impulse')
            prior_price=float(seq[j]['close'])
            windows={f'{n}m':_prior_window(seq,ss,j,n,min_coverage=min_coverage,
                       max_observed_gap_seconds=max_observed_gap_seconds)
                     for n in CONTEXT_WINDOWS_MINUTES}
            for tf in (1,5):
                swings[str(tf)+'m']={}
                for side in ('HIGH','LOW'):
                    s=_last_known_swing(swings_by_timeframe[tf],segment=seg,side=side,
                        pre_available_ns=pre_ns,pre_time_ms=ss[j])
                    if s:
                        s['signed_distance_pct']=(prior_price/float(s['level'])-1)*100
                        s['absolute_distance_pct']=abs(s['signed_distance_pct'])
                    swings[str(tf)+'m'][side]=s
            five=windows['5m']
            if five['state']=='OBSERVED_CONTEXT_WITH_EXPLICIT_GAPS':
                price_sign=math.copysign(1,five['return_pct']) if five['return_pct'] else 0
                cvd_sign=math.copysign(1,five['cvd_delta_qty']) if five['cvd_delta_qty'] else 0
                press=1 if e['pressure_side']=='BUY' else -1
                if price_sign==press and cvd_sign==press:
                    group='AGGRESSION_WITH_PRIOR_TREND'
                elif price_sign==-press and cvd_sign==-press:
                    group='COUNTER_TREND_AGGRESSION'
                else:
                    group='MIXED_PRIOR_CONTEXT'
        context.append({'event_id':eid,'segment_id':seg,
            'pressure_side':e['pressure_side'],'hypothesis_direction':e['hypothesis_direction'],
            'impulse_second_start_ms':signal_ms,
            'impulse_start_ms':impulse_start,
            'impulse_available_wall_ns':int(e['impulse']['available_wall_ns']),
            'pre_event_last_observed_second_ms':ss[j] if j>=0 else None,
            'pre_event_available_wall_ns':pre_ns,
            'pre_event_last_seq':seq[j]['last_seq'] if j>=0 else None,
            'pre_event_close_price':prior_price,
            'windows':windows,'known_swings':swings,'context_group':group,
            'contract':'STRICTLY_PRIOR_TO_AGGRESSIVE_IMPULSE_WINDOW'})
    return {'schema':SCHEMA,'source_buckets_sha256':digest,
        'source_aggression_events_sha256':_hash_json(aggression['events']),
        'source_1m_swings_sha256':_hash_json(swings_by_timeframe[1]),
        'source_5m_swings_sha256':_hash_json(swings_by_timeframe[5]),
        'parameters':{'windows_minutes':list(CONTEXT_WINDOWS_MINUTES),
                      'min_coverage':min_coverage,
                      'max_observed_gap_seconds':max_observed_gap_seconds,
                      'group_policy':'SIGNS_5M_PRICE_AND_CVD_STRICTLY_PRE_IMPULSE',
                      'research_only':True},
        'rows':context,
        'summary':{'impulses':len(context),
                   'by_group':{group:sum(x['context_group']==group for x in context)
                      for group in ('AGGRESSION_WITH_PRIOR_TREND','COUNTER_TREND_AGGRESSION',
                                    'MIXED_PRIOR_CONTEXT','CONTEXT_UNAVAILABLE')}}}


def evaluate_context_cohorts(context, feature_outcomes, *, episode_seconds=180):
    """Join pre-impulse contexts to UNCHANGED A/B outcomes, retain every case.

    Cohorts are descriptive and outcome-blind. Episodes are spaced trigger
    windows (not independent returns when horizons are longer). No OOS claim.
    """
    if context.get('schema')!=SCHEMA or feature_outcomes.get('source_buckets_sha256')!=context['source_buckets_sha256']:
        raise DiscoveryError('Context/outcome source mismatch')
    if not 30<=episode_seconds<=3600:
        raise DiscoveryError('Invalid episode spacing')
    c={r['event_id']:r for r in context['rows']}
    if len(c)!=len(context['rows']):
        raise DiscoveryError('Duplicate context IDs')
    cases=feature_outcomes['cases']
    seen=set()
    events_by_seg_side=defaultdict(list)
    for r in context['rows']:
        events_by_seg_side[(r['segment_id'],r['pressure_side'])].append(r)
    rank={}
    for key,ordered in events_by_seg_side.items():
        ordered.sort(key=lambda x:(x['impulse_available_wall_ns'],x['event_id']))
        anchor=-10**30
        ep=-1
        for r in ordered:
            stamp=r['impulse_available_wall_ns']//1_000_000
            if stamp-anchor>=episode_seconds*1000:
                ep+=1
                anchor=stamp
                first=True
            else:
                first=False
            rank[r['event_id']]={'episode_id':f'{key[0]}:{key[1]}:{ep}',
                                 'episode_first':first}
    merged=[]
    for case in cases:
        k=(case['event_id'],case['variant'])
        if k in seen or case['event_id'] not in c:
            raise DiscoveryError('Repeated or absent A/B event')
        seen.add(k)
        old=c[case['event_id']]
        if (old['segment_id']!=case['segment_id'] or old['pressure_side']!=case['pressure_side']):
            raise DiscoveryError('Side or segment mismatch')
        merged.append({'event_id':case['event_id'],'variant':case['variant'],
            'pressure_side':case['pressure_side'],'hypothesis_direction':case['hypothesis_direction'],
            'segment_id':case['segment_id'],'context_group':old['context_group'],
            'prior_return_5m_pct':old['windows'].get('5m',{}).get('return_pct'),
            'prior_return_10m_pct':old['windows'].get('10m',{}).get('return_pct'),
            'prior_cvd_delta_5m':old['windows'].get('5m',{}).get('cvd_delta_qty'),
            'prior_range_position_10m':old['windows'].get('10m',{}).get('range_position'),
            'known_1m_high_distance_pct': (old['known_swings'].get('1m',{}).get('HIGH') or {}).get('absolute_distance_pct'),
            'known_1m_low_distance_pct': (old['known_swings'].get('1m',{}).get('LOW') or {}).get('absolute_distance_pct'),
            'known_5m_high_distance_pct': (old['known_swings'].get('5m',{}).get('HIGH') or {}).get('absolute_distance_pct'),
            'known_5m_low_distance_pct': (old['known_swings'].get('5m',{}).get('LOW') or {}).get('absolute_distance_pct'),
            'feature_group':case['feature_group'],
            'feature_rejection':case['feature_rejection'],
            'feature_state':case['feature_state'],
            'confirmation_state':case['confirmation_state'],
            'reference_state':case['reference_state'],
            'entry_reference':case['entry_reference'],
            'episode_id':rank[case['event_id']]['episode_id'],
            'episode_first':rank[case['event_id']]['episode_first'],
            'horizons':case['horizons']})
    expected={(eid,v) for eid in c for v in ('FAILURE','FAILURE_SWING_BREAK')}
    if seen!=expected or len(merged)!=len(expected):
        raise DiscoveryError('Context study must include every impulse and both variants exactly once')
    dimensions=['AGGRESSION_WITH_PRIOR_TREND','COUNTER_TREND_AGGRESSION',
                'MIXED_PRIOR_CONTEXT','CONTEXT_UNAVAILABLE']
    summaries=[]
    for side in ('BUY','SELL'):
        for variant in ('FAILURE','FAILURE_SWING_BREAK'):
            pool=[r for r in merged if r['pressure_side']==side and r['variant']==variant]
            for cohort in COHORTS:
                if cohort=='ALL_IMPULSES':
                    sampled=pool
                elif cohort=='DIVERGENCE_ONLY':
                    sampled=[r for r in pool if r['feature_group']=='AGGRESSIVE_FLOW_PRICE_DISAGREEMENT']
                else:
                    sampled=[r for r in pool if r['feature_group']=='AGGRESSIVE_FLOW_PRICE_DISAGREEMENT'
                             and r['feature_rejection'] is True]
                for ctx in dimensions:
                    subset=[r for r in sampled if r['context_group']==ctx]
                    for h in HORIZONS_SECONDS:
                        key=f'{h}s'
                        good=[(r,r['horizons'][key]) for r in subset if r['horizons'][key]['state']=='OBSERVED_PROXY']
                        vals=[outcome['return_directional_pct'] for _,outcome in good]
                        med=sorted(vals)
                        median=((med[(len(med)-1)//2]+med[len(med)//2])/2) if med else None
                        ep={r['episode_id'] for r in subset}
                        first=[outcome['return_directional_pct'] for r,outcome in good if r['episode_first']]
                        summaries.append({'pressure_side':side,'variant':variant,'cohort':cohort,
                            'context_group':ctx,'horizon_seconds':h,'impulses':len(subset),
                            'trigger_episodes_180s':len(ep),
                            'entry_references':sum(r['entry_reference'] is not None for r in subset),
                            'strict_observed':len(good),
                            'strict_coverage_pct':100*len(good)/len(subset) if subset else None,
                            'no_strict_outcome':len(subset)-len(good),
                            'mean_gross_pct':sum(vals)/len(vals) if vals else None,
                            'median_gross_pct':median,
                            'gross_positive':sum(v>0 for v in vals),
                            'mean_net_12bps_illustrative_pct':(sum(vals)/len(vals)-.12) if vals else None,
                            'first_in_episode_strict_observed':len(first),
                            'first_in_episode_mean_gross_pct':sum(first)/len(first) if first else None,
                            'max_horizon_policy':'STRICT_OBSERVED_ONLY_NO_ENDPOINT_OUTCOMES'})
    return {'schema':STUDY_SCHEMA, 'parameters':{'episode_seconds':episode_seconds,
        'research_role':'DISCOVERY_NO_OUT_OF_SAMPLE','no_fill_simulated':True,
        'outcome_policy':'UNCHANGED_STRICT_A_B_NO_TRAJECTORY_INTERPOLATION'},
        'source_buckets_sha256':context['source_buckets_sha256'],
        'source_context_sha256':_hash_json(context),
        'source_feature_outcomes_sha256':_hash_json(feature_outcomes),
        'cases':merged,'summary':summaries}


def save_verified_context_run(root, session, context, outcome_study, study):
    """Fail closed if RAW/Replay is unavailable, original data is never written."""
    if study.get('schema')!=STUDY_SCHEMA or study.get('source_context_sha256')!=_hash_json(context) or study.get('source_feature_outcomes_sha256')!=_hash_json(outcome_study):
        raise DiscoveryError('Context study provenance mismatch')
    raw,rep=Path(session['tape_path']),Path(session['replay_path'])
    summary=session['summary']
    if not raw.is_file() or raw.stat().st_size<=0 or not rep.is_file():
        raise DiscoveryError('RAW and Replay required to persist')
    if file_sha256(rep)!=summary.get('replay_jsonl_sha256'):
        raise DiscoveryError('Replay SHA256 mismatch')
    from research.micro_flow_v3.tape import replay_digest
    cnt,digest=replay_digest(raw)
    if cnt!=summary.get('source_tape_rows') or digest!=summary.get('source_tape_digest_sha256'):
        raise DiscoveryError('RAW logical digest mismatch')
    out=Path(root)/'reports/micro_flow_v3/microstructure_context/runs'/(
        datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'_'+uuid.uuid4().hex[:12])
    out.mkdir(parents=True,exist_ok=False)
    for name,collection in [('context.jsonl',context['rows']),('cases.jsonl',study['cases'])]:
        with (out/name).open('x',encoding='utf-8') as f:
            for row in collection:
                f.write(json.dumps(row,separators=(',', ':'),sort_keys=True,allow_nan=False)+'\n')
    with (out/'summary.csv').open('x',encoding='utf-8',newline='') as f:
        if study['summary']:
            writer=csv.DictWriter(f,fieldnames=list(study['summary'][0]))
            writer.writeheader();writer.writerows(study['summary'])
    root=Path(root)
    modules=('research/micro_flow_v3/microstructure_context_features.py',
        'research/micro_flow_v3/microstructure_exhaustion_features.py',
        'research/micro_flow_v3/microstructure_feature_outcomes.py',
        'research/micro_flow_v3/aggression_exhaustion_discovery.py',
        'research/micro_flow_v3/exhaustion_entry_research.py',
        'research/micro_flow_v3/swing_sweep_discovery.py',
        'dashboard/micro_flow_v3_context_tab.py',
        'dashboard/micro_flow_v3_explore_tab.py')
    code_hashes={}
    for name in modules:
        p=root/name
        if not p.is_file():
            raise DiscoveryError(f'Missing source module at save: {name}')
        code_hashes[name]=file_sha256(p)
    manifest={'schema':STUDY_SCHEMA,'run_created_at_utc':datetime.now(timezone.utc).isoformat(),
        'session_tape':raw.name,'source_rows':cnt,'source_raw_digest':digest,
        'source_raw_file_sha256':file_sha256(raw),
        'source_replay_sha256':summary['replay_jsonl_sha256'],
        'context_schema':context['schema'],'context_parameters':context['parameters'],
        'context_sha256':_hash_json(context), 'study_sha256':_hash_json(study),
        'source_outcome_study_sha256':_hash_json(outcome_study),
        'code_sha256':code_hashes,
        'artifacts_sha256':{name:file_sha256(out/name)
            for name in ('context.jsonl','cases.jsonl','summary.csv')},
        'research_only':True,'no_fills':True,'strict_outcome_policy':True,
        'dataset_role':'EXPLORATORY_NOT_OUT_OF_SAMPLE'}
    with (out/'manifest.json').open('x',encoding='utf-8') as f:
        json.dump(manifest,f,indent=2,sort_keys=True,allow_nan=False)
    return out
