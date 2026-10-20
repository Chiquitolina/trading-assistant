"""Long-horizon exploratory outcomes must never overwrite causal microstructure."""
from __future__ import annotations
from copy import deepcopy
import pytest
from research.micro_flow_v3.microstructure_exhaustion_features import (
    _hash_json,build_exhaustion_feature_catalog,
)
from research.micro_flow_v3.microstructure_feature_outcomes import (
    HORIZONS_SECONDS,evaluate_feature_outcome_horizons,save_feature_outcome_run,
)
from research.micro_flow_v3.exhaustion_entry_research import SCHEMA as ENTRY_SCHEMA
from research.micro_flow_v3.swing_sweep_discovery import DiscoveryError,validate_buckets

BASE=1792393000000


def setup(n=3750, *,gap_after=None, segment_after=None, entry_b=True):
    r=[]
    for i in range(n):
        t=BASE+(i+(7 if gap_after is not None and i>gap_after else 0))*1000
        seg=1 if segment_after is None or i<segment_after else 2
        price=2400+i*0.004
        r.append({'event_type':'BUCKET_1S','symbol':'ETHUSDT','segment_id':seg,
            'second_start_ms':t,'second_end_ms_exclusive':t+1000,
            'available_wall_ns':(t+1500)*1000000,'available_ms':t+1500,
            'first_seq':i+1,'last_seq':i+1,'first_agg_id':i+1,'last_agg_id':i+1,
            'open':str(price),'high':str(price+.02),'low':str(price-.02),
            'close':str(price+.005), 'buy_qty':'5','sell_qty':'1',
            'delta_qty':'4','cvd_qty':str(4*(i+1)),'trades':2})
    rows=validate_buckets(r)
    impulse=rows[75]; eid=f'1:EXHAUST:BUY:{impulse["second_start_ms"]}'
    e={'event_id':eid,'pressure_side':'BUY','hypothesis_direction':'SHORT',
       'segment_id':1,'impulse':{'observed_second_start_ms':impulse['second_start_ms'],
          'available_wall_ns':impulse['available_wall_ns'],
          'observed_close':impulse['close'], 'first_seq':impulse['first_seq'],
          'last_seq':impulse['last_seq']}}
    a={'schema':'micro-flow-v3-aggression-exhaustion-discovery-v1',
       'events':[e],'parameters':{'test':'fixed'},'source_buckets_sha256':_hash_json(rows)}
    features=build_exhaustion_feature_catalog(rows,a)
    cases=[]
    for variant,index in [('FAILURE',77),('FAILURE_SWING_BREAK',90)]:
        signal=rows[index-1]
        ref=rows[index]
        cases.append({'event_id':eid,'segment_id':1,'variant':variant,
         'pressure_side':'BUY','hypothesis_direction':'SHORT',
         'signal_state':'AVAILABLE','entry_state':'REFERENCE_NOT_EXECUTABLE_FILL',
         'signal':{'available_wall_ns':signal['available_wall_ns']},
         'entry_reference':{'entry_reference_second_start_ms':ref['second_start_ms'],
            'entry_reference_price':float(ref['open']),
            'entry_reference_first_seq':ref['first_seq'],
            'entry_reference_available_wall_ns':ref['available_wall_ns']}})
    if not entry_b:
        cases[1]['entry_reference']=None
        cases[1]['signal']=None
        cases[1]['signal_state']='NO_PREKNOWN_SWING'
        cases[1]['entry_state']='NO_SIGNAL'
    entry={'schema':ENTRY_SCHEMA,'source_buckets_sha256':a['source_buckets_sha256'],
          'source_exhaustion_events_sha256':_hash_json(a['events']),
          'entries':cases}
    return rows,a,features,entry


def test_long_horizons_30_and_60_minutes_explicit():
    rows,a,f,e=setup()
    result=evaluate_feature_outcome_horizons(rows,a,f,e)
    assert result['parameters']['horizons_seconds']==list(HORIZONS_SECONDS)
    assert len(result['cases'])==2
    assert result['cases'][0]['horizons']['1800s']['state']=='OBSERVED_PROXY'
    assert result['cases'][0]['horizons']['3600s']['state']=='OBSERVED_PROXY'
    assert result['cases'][0]['horizons']['3600s']['mfe_directional_pct']>=0
    assert len(result['summary'])>0
    assert len(result['paired'])==len(HORIZONS_SECONDS)
    assert result['paired'][-1]['both_observed']==1


def test_gaps_censor_strict_outcomes_but_endpoint_is_diagnostic():
    rows,a,f,e=setup(gap_after=100)
    study=evaluate_feature_outcome_horizons(rows,a,f,e)
    h=study['cases'][0]['horizons']['3600s']
    assert h['state']=='CENSORED_OBSERVATION_GAP'
    assert h['return_directional_pct'] is None
    assert h['mfe_directional_pct'] is None
    assert h['endpoint_only_diagnostic']['state']=='ENDPOINT_OBSERVED_PATH_NOT_VERIFIED'
    assert h['endpoint_only_diagnostic']['missing_seconds']==7
    assert study['summary'][-1]['strict_observed']==0 or study['summary'][-1]['horizon_seconds']!=3600


def test_no_cross_session_endpoint():
    rows,a,f,e=setup(segment_after=500)
    study=evaluate_feature_outcome_horizons(rows,a,f,e)
    h=study['cases'][0]['horizons']['3600s']
    assert h['state']=='CENSORED_NO_FRESH_ENDPOINT'
    assert h['endpoint_only_diagnostic']['state']=='NO_FRESH_ENDPOINT'


def test_keep_both_variants_even_with_no_entry():
    rows,a,f,e=setup(entry_b=False)
    study=evaluate_feature_outcome_horizons(rows,a,f,e)
    assert len(study['cases'])==2
    b=study['cases'][1]
    assert all(k['state']=='NO_ENTRY_REFERENCE' for k in b['horizons'].values())
    assert study['paired'][-1]['both_observed']==0


def test_catalog_and_entry_must_share_same_replay():
    rows,a,f,e=setup()
    e['source_buckets_sha256']='changed'
    with pytest.raises(DiscoveryError,match='source mismatch'):
        evaluate_feature_outcome_horizons(rows,a,f,e)


def test_entry_reference_is_checked_against_replay():
    rows,a,f,e=setup()
    e['entries'][0]['entry_reference']['entry_reference_price']=3000
    with pytest.raises(DiscoveryError,match='differs from Replay'):
        evaluate_feature_outcome_horizons(rows,a,f,e)


def test_future_change_affects_outcomes_not_causal_feature_membership():
    rows,a,f,e=setup()
    first=evaluate_feature_outcome_horizons(rows,a,f,e)
    newer=deepcopy(rows)
    for r in newer[150:]:
        r['close']=str(float(r['close'])-30)
        r['low']=str(float(r['low'])-30)
    newer=validate_buckets(newer)
    a['source_buckets_sha256']=_hash_json(newer)
    f['source_buckets_sha256']=a['source_buckets_sha256']
    e['source_buckets_sha256']=a['source_buckets_sha256']
    newer_result=evaluate_feature_outcome_horizons(newer,a,f,e)
    assert first['cases'][0]['feature_data']==newer_result['cases'][0]['feature_data']
    assert first['cases'][0]['horizons']['180s']['return_directional_pct']!=newer_result['cases'][0]['horizons']['180s']['return_directional_pct']


def test_reject_missing_raw_before_writing(tmp_path):
    rows,a,f,e=setup()
    study=evaluate_feature_outcome_horizons(rows,a,f,e)
    session={'buckets':rows,'summary':{'replay_jsonl_sha256':'x'},
             'tape_path':str(tmp_path/'missing.sqlite3'),
             'replay_path':str(tmp_path/'missing.jsonl'),
             'summary_path':str(tmp_path/'missing.json')}
    with pytest.raises(DiscoveryError, match='Missing original RAW'):
        save_feature_outcome_run(tmp_path,session,a,f,e,study)
    assert not (tmp_path/'reports').exists()
