"""Causal context and unchanged outcome contract tests (research only)."""
from copy import deepcopy
import json
from pathlib import Path
import pytest

from research.micro_flow_v3.microstructure_exhaustion_features import _hash_json

from research.micro_flow_v3.microstructure_context_features import (
    build_pre_impulse_context, evaluate_context_cohorts,save_verified_context_run,
)
from research.micro_flow_v3.swing_sweep_discovery import DiscoveryError,validate_buckets

BASE=1792393200000


def _fixture(n=1000, impulse_second=780):
    rows=[]
    for i in range(n):
        ms=BASE+i*1000
        px=2400.0-i*.002
        rows.append({'event_type':'BUCKET_1S','symbol':'ETHUSDT','segment_id':1,
            'second_start_ms':ms,'second_end_ms_exclusive':ms+1000,
            'available_wall_ns':(ms+1200)*1_000_000,
            'first_seq':2*i+1,'last_seq':2*i+2,
            'open':px,'high':px+.01,'low':px-.01,'close':px,
            'buy_qty':1.,'sell_qty':2.,'trades':3})
    ms=BASE+impulse_second*1000
    events=[{'event_id':f'1:EXHAUST:BUY:{ms}', 'segment_id':1,'pressure_side':'BUY',
             'hypothesis_direction':'SHORT',
             'impulse':{'observed_second_start_ms':ms,
                         'available_wall_ns':rows[impulse_second]['available_wall_ns']},
             'impulse_features':{'window_start_ms':ms-4000}}]
    ae={'events':events,'source_buckets_sha256':_hash_json(validate_buckets(rows))}
    return rows,ae


def _swing(basetime, *, confirmed, side='HIGH', tf=1):
    return {'segment_id':1,'side':side,'timeframe_minutes':tf,
            'confirmed_available_wall_ns':(BASE+confirmed*1000)*1_000_000,
            'pivot_ms':BASE+basetime*1000,'level':2400.,
            'swing_id':f'{tf}:{basetime}:{side}'}


def _outcome_stub(ctx, *, observed=True):
    eid=ctx['rows'][0]['event_id']
    def make(variant):
        state='OBSERVED_PROXY' if observed and variant=='FAILURE' else 'NO_ENTRY_REFERENCE'
        outcomes={f'{h}s':{'state':state,'return_directional_pct':.2 if observed and variant=='FAILURE' else None}
                  for h in (60,180,300,600,900,1800,3600)}
        return {'event_id':eid,'variant':variant,'segment_id':1,'pressure_side':'BUY',
                'hypothesis_direction':'SHORT','feature_group':'AGGRESSIVE_FLOW_PRICE_DISAGREEMENT',
                'feature_rejection':True,'feature_state':'OBSERVED','confirmation_state':'AVAILABLE',
                'reference_state':'AVAILABLE' if variant=='FAILURE' else 'NO_SIGNAL',
                'entry_reference':({'entry_reference_price':2398.} if variant=='FAILURE' else None),'horizons':outcomes}
    return {'source_buckets_sha256':ctx['source_buckets_sha256'],
            'cases':[make('FAILURE'),make('FAILURE_SWING_BREAK')]}


def test_context_strictly_excludes_impulse_and_future_changes():
    rows,ae=_fixture()
    ctx=build_pre_impulse_context(rows,ae,{1:[],5:[]})
    c=ctx['rows'][0]
    assert c['pre_event_last_observed_second_ms']==BASE+775_000
    assert c['pre_event_available_wall_ns']<c['impulse_available_wall_ns']
    assert c['context_group']=='COUNTER_TREND_AGGRESSION'
    assert ctx['summary']['impulses']==1
    assert c['windows']['5m']['return_pct']<0
    altered=deepcopy(rows)
    for i in range(776,len(altered)):
        altered[i]['open']+=100
        altered[i]['close']+=100
        altered[i]['high']+=100
        altered[i]['low']+=100
    mod=deepcopy(ae)
    mod['source_buckets_sha256']=_hash_json(validate_buckets(altered))
    ctx2=build_pre_impulse_context(altered,mod,{1:[],5:[]})
    assert ctx2['rows'][0]['windows']==c['windows']
    assert ctx2['rows'][0]['context_group']==c['context_group']


def test_only_pre_impulse_confirmed_swings_are_available():
    rows,ae=_fixture()
    prior=_swing(600,confirmed=690)
    future=_swing(700,confirmed=781)
    ctx=build_pre_impulse_context(rows,ae,{1:[prior,future],5:[]})
    chosen=ctx['rows'][0]['known_swings']['1m']['HIGH']
    assert chosen['swing_id']==prior['swing_id']
    assert chosen['confirmed_available_wall_ns'] < ctx['rows'][0]['impulse_available_wall_ns']


def test_short_history_is_not_fake_trend_and_no_cross_segment():
    rows,ae=_fixture(n=140,impulse_second=90)
    ctx=build_pre_impulse_context(rows,ae,{1:[],5:[]})
    assert ctx['rows'][0]['context_group']=='CONTEXT_UNAVAILABLE'
    assert ctx['rows'][0]['windows']['5m']['state']=='INSUFFICIENT_PRIOR_CONTEXT'


def test_missing_seconds_are_explicit_without_backfill():
    rows,ae=_fixture()
    rows=[r for r in rows if not (BASE+600_000<=r['second_start_ms']<BASE+630_000)]
    ae['source_buckets_sha256']=_hash_json(validate_buckets(rows))
    ctx=build_pre_impulse_context(rows,ae,{1:[],5:[]})
    assert ctx['rows'][0]['context_group']=='CONTEXT_UNAVAILABLE'
    assert ctx['rows'][0]['windows']['5m']['max_observed_gap_seconds']>10


def test_keeps_no_entry_and_all_horizons_without_hindsight_selection():
    rows,ae=_fixture()
    ctx=build_pre_impulse_context(rows,ae,{1:[],5:[]})
    outcome=_outcome_stub(ctx)
    study=evaluate_context_cohorts(ctx,outcome)
    assert len(study['cases'])==2
    assert len(study['summary'])==2*2*3*4*7
    a=[r for r in study['summary'] if r['pressure_side']=='BUY' and r['variant']=='FAILURE' and
       r['cohort']=='DIVERGENCE_AND_REJECTION' and r['horizon_seconds']==600 and
       r['context_group']=='COUNTER_TREND_AGGRESSION'][0]
    assert a['strict_observed']==1
    assert a['mean_gross_pct']==.2
    assert a['mean_net_12bps_illustrative_pct']==pytest.approx(.08)
    b=[r for r in study['summary'] if r['pressure_side']=='BUY' and r['variant']=='FAILURE_SWING_BREAK' and
       r['cohort']=='DIVERGENCE_AND_REJECTION' and r['horizon_seconds']==600 and
       r['context_group']=='COUNTER_TREND_AGGRESSION'][0]
    assert b['strict_observed']==0 and b['entry_references']==0
    assert b['mean_gross_pct'] is None


def test_rejects_wrong_provenance_and_duplicate_ids():
    rows,ae=_fixture()
    ctx=build_pre_impulse_context(rows,ae,{1:[],5:[]})
    outcome=_outcome_stub(ctx)
    outcome['source_buckets_sha256']='bad'
    with pytest.raises(DiscoveryError): evaluate_context_cohorts(ctx,outcome)
    ae['events'].append(deepcopy(ae['events'][0]))
    with pytest.raises(DiscoveryError): build_pre_impulse_context(rows,ae,{1:[],5:[]})


def test_save_refuses_missing_raw(tmp_path):
    rows,ae=_fixture()
    ctx=build_pre_impulse_context(rows,ae,{1:[],5:[]})
    outcome=_outcome_stub(ctx)
    study=evaluate_context_cohorts(ctx,outcome)
    sess={'tape_path':str(tmp_path/'missing.sqlite3'),
          'replay_path':str(tmp_path/'missing.jsonl'),'summary':{}}
    with pytest.raises(DiscoveryError): save_verified_context_run(tmp_path,sess,ctx,outcome,study)


def test_context_never_uses_earlier_segment_even_if_timestamps_are_adjacent():
    rows,ae=_fixture()
    # The impulse is only 80s into segment 2; segment 1 contains ample history.
    for r in rows[700:]:
        r['segment_id']=2
    ae['events'][0]['segment_id']=2
    ae['events'][0]['event_id']=ae['events'][0]['event_id'].replace('1:EXHAUST','2:EXHAUST')
    ae['source_buckets_sha256']=_hash_json(validate_buckets(rows))
    ctx=build_pre_impulse_context(rows,ae,{1:[],5:[]})
    case=ctx['rows'][0]
    assert case['context_group']=='CONTEXT_UNAVAILABLE'
    assert case['windows']['5m']['state']=='INSUFFICIENT_PRIOR_CONTEXT'
    assert case['windows']['5m']['observed_seconds']<300
