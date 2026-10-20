"""Fase 4: no lookahead, cross-segment isolation, censoring, provenance."""
from copy import deepcopy
from pathlib import Path
import hashlib
import json

import pytest

from research.micro_flow_v3.volume_aggression_confluence import (
    _hash, observed_volume_candles, discover_volume_exhaustion,
    compare_confluence, save_confluence_run,
)
from research.micro_flow_v3.swing_sweep_discovery import DiscoveryError, validate_buckets


def _data(*, minutes=48, seg=1, base_sec=0):
    """Observed seconds, not invented 1s zero-volume data.

    Minimum 48 minute bars incl. volume spike near minute 40; all 1m
    baseline/RSI history is known before it. Publication is delayed 1.2s.
    """
    rows=[]
    for minute in range(minutes):
        last=100.+minute*.025
        for sec in range(1,51):
            start=(base_sec+minute*60+sec)*1000
            price=last+(sec-25)*.0005
            qty=10 if minute==40 else 1
            rows.append({
                'event_type':'BUCKET_1S', 'symbol':'ETHUSDT','segment_id':seg,
                'second_start_ms':start,'second_end_ms_exclusive':start+1000,
                'available_wall_ns':(start+1200)*1_000_000,
                'available_ms':start+1200,
                'first_seq':seg*1_000_000+minute*100+sec,
                'last_seq':seg*1_000_000+minute*100+sec,
                'trades':1,'open':str(price),'high':str(price),
                'low':str(price),'close':str(price),
                'buy_qty':str(qty*.9),'sell_qty':str(qty*.1),
                'delta_qty':str(qty*.8),
                'cvd_qty':str(minute*40+sec*.8),
            })
    return rows


def _ae(rows, events):
    return {'schema':'micro-flow-v3-aggression-exhaustion-discovery-v1',
            'source_buckets_sha256':_hash(validate_buckets(rows)),
            'parameters':{'synthetic_test_events':True},'events':events}


def _impulse(rows, second, side='BUY', seg=1):
    bucket=next(r for r in rows if r['second_start_ms']==second*1000 and r['segment_id']==seg)
    return {'event_id':f'{seg}:EXHAUST:{side}:{second*1000}',
            'segment_id':seg,'pressure_side':side,
            'impulse':{'observed_second_start_ms':bucket['second_start_ms'],
                       'available_wall_ns':bucket['available_wall_ns'],
                       'available_ms':bucket['available_ms'],
                       'observed_close':float(bucket['close']),
                       'first_seq':bucket['first_seq'],'last_seq':bucket['last_seq']}}


def _ve(rows, *, tf=1, **kwargs):
    c=observed_volume_candles(rows,tf)
    return discover_volume_exhaustion(c,timeframe_minutes=tf,**kwargs)


def test_candles_ohlcv_volume_preserve_aggtrade_totals_and_coverage():
    rows=_data()
    candles=observed_volume_candles(rows,1)
    assert len(candles)==46  # discard partial 1st and last source windows
    c=next(x for x in candles if x['timestamp']==40*60000)
    assert c['buy_qty']==450.
    assert c['sell_qty']==50.
    assert c['volume_qty']==500.
    assert c['delta_qty']==400.
    assert c['seconds_observed']==50
    assert c['seconds_coverage_fraction']==pytest.approx(50/60)
    assert c['available_wall_ns'] > c['end_ms_exclusive']*1_000_000
    assert c['first_seq'] < c['last_seq']
    assert c['volume_source'].startswith('SUM_OBSERVED_AGGTRADES')


def test_ve_rsi_high_volume_and_publication_are_causal():
    rows=_data()
    ve=_ve(rows)
    event=next(e for e in ve['events'] if e['candle_start_ms']==40*60000)
    assert event['pressure_side']=='BUY'
    assert event['volume_ratio_prior_candles'] > 5
    assert event['rsi_wilder']>=65
    assert event['available_wall_ns'] > event['candle_end_ms_exclusive']*1_000_000
    assert event['volume_spikes_last4']>=1
    assert event['first_seq'] < event['last_seq']


def test_ve_cannot_read_future_candles_or_bridge_missing_minute():
    rows=_data()
    ve=_ve(rows)
    early=[e for e in ve['events'] if e['candle_start_ms']<=40*60000]
    changed=deepcopy(rows)
    for r in changed:
        if r['second_start_ms']>41*60000:
            r['close']=r['open']=r['low']=r['high']='200'
    ve_after=_ve(changed)
    assert early == [e for e in ve_after['events'] if e['candle_start_ms']<=40*60000]
    missing=[r for r in rows if r['second_start_ms']//60000 != 25]
    ve_missing=_ve(missing)
    assert not any(e['candle_start_ms']==40*60000 for e in ve_missing['events'])


def test_ve_minimum_coverage_and_multi_candle_confirmation():
    rows=_data()
    ve=_ve(rows,min_coverage_fraction=.9)
    assert ve['summary']['volume_exhaustion_events']==0
    assert ve['summary']['eligible_candles']==0
    two=_ve(rows,count_high_volume_in_last4=2)
    assert not any(e['candle_start_ms']==40*60000 for e in two['events'])


def test_nearest_candle_close_avoids_spurious_early_minute_match():
    rows=_data(minutes=62)
    ve=_ve(rows)
    anchor=next(e for e in ve['events'] if e['candle_start_ms']==40*60000)
    # AE near minute open is not matched by close tolerance 12s.
    early=_impulse(rows,40*60+10)
    late=_impulse(rows,40*60+49)
    a=_ae(rows,[early,late])
    five=compare_confluence(rows,ve,a,max_distance_seconds=12)
    match=next(c for c in five['cases'] if c['ve_event_id']==anchor['event_id'])
    assert match['ae_event_id']==late['event_id']
    assert five['summary']['matched_events']==1
    assert all(c['entry_reference'] is None or
               c['entry_reference']['entry_reference_available_wall_ns']>c['signal']['available_wall_ns']
               for c in five['cases'])


def test_opposite_side_kept_distinct_and_signal_not_backdated():
    rows=_data(minutes=62)
    ve=_ve(rows)
    event=next(e for e in ve['events'] if e['candle_start_ms']==40*60000)
    ae=_impulse(rows,41*60+10,side='SELL') # after VE candle closes; within 30s
    result=compare_confluence(rows,ve,_ae(rows,[ae]),max_distance_seconds=30)
    c=next(c for c in result['cases'] if c['ve_event_id']==event['event_id'])
    assert c['cohort']=='CONFLUENCE_OPPOSITE_SIDE'
    assert c['signal']['available_wall_ns']==max(event['available_wall_ns'],
                                                  ae['impulse']['available_wall_ns'])
    assert c['hypothesis_direction']=='SHORT'  # VE side is anchor, not hidden opposite
    assert c['entry_reference']['entry_reference_available_wall_ns'] > c['signal']['available_wall_ns']
    assert c['state']=='REFERENCE_ONLY_NOT_FILL'


def test_no_cross_segment_match_and_keep_all_unmatched():
    first=_data(minutes=48,seg=1,base_sec=0)
    second=_data(minutes=48,seg=2,base_sec=3600)
    rows=first+second
    ve=_ve(rows)
    ae=_ae(rows,[_impulse(second,3600+40*60+49,seg=2)])
    result=compare_confluence(rows,ve,ae,max_distance_seconds=60)
    assert all(x['segment_id'] == int(x['ve_event_id'].split(':')[0])
               for x in result['cases'] if x['ve_event_id'])
    assert result['summary']['ve_events'] == len(ve['events'])
    assert result['summary']['ae_events']==1
    assert len(result['cases']) == len(ve['events']) + 1 - result['summary']['matched_events']


def test_source_mismatch_is_rejected_without_overwriting():
    rows=_data()
    ve=_ve(rows)
    fake=_ae(rows,[])
    broken=deepcopy(rows)
    broken[2]['close']='60'
    broken[2]['low']='60'
    with pytest.raises(DiscoveryError,match='same Replay'):
        compare_confluence(broken,ve,fake)
    original=json.dumps(rows,sort_keys=True)
    first=compare_confluence(rows,ve,fake)
    second=compare_confluence(rows,ve,fake)
    assert first==second
    assert json.dumps(rows,sort_keys=True)==original


def test_missing_raw_refuses_persistence(tmp_path):
    rows=_data()
    ve=_ve(rows)
    ae=_ae(rows,[])
    study=compare_confluence(rows,ve,ae)
    replay=tmp_path/'source.jsonl'; replay.write_text('synthetic')
    summ=tmp_path/'summary.json';summ.write_text('{}')
    session={'buckets':rows,'tape_path':str(tmp_path/'absent.sqlite3'),
             'replay_path':str(replay),'summary_path':str(summ),
             'tape':'absent.sqlite3','summary':{
                'replay_jsonl_sha256':hashlib.sha256(b'synthetic').hexdigest(),
                'buckets_emitted':len(rows),'source_tape_rows':len(rows),
                'source_tape_digest_sha256':'deadbeef'}}
    with pytest.raises(Exception):
        save_confluence_run(tmp_path,session,ve,ae,study,verify_raw=True)
    assert not (tmp_path/'reports').exists()


def test_five_minute_candles_rsi_volume_have_correct_availability():
    rows=_data(minutes=185)
    for row in rows:
        minute=row['second_start_ms']//60000
        if 165<=minute<=169:
            row['buy_qty']='9'
            row['sell_qty']='1'
            row['delta_qty']='8'
    ve=_ve(rows,tf=5)
    assert ve['summary']['eligible_candles']>0
    event=next(e for e in ve['events'] if e['candle_start_ms']==165*60_000)
    assert event['timeframe_minutes']==5
    assert event['available_ms']>event['candle_end_ms_exclusive']
    assert event['volume_ratio_prior_candles']>=2
    assert event['seconds_observed']==250
    assert event['seconds_coverage_fraction']==pytest.approx(250/300)


def test_outcome_censors_unobserved_seconds_and_investigation_is_persistable(tmp_path):
    rows=_data(minutes=50)
    ve=_ve(rows)
    ae=_ae(rows,[])
    study=compare_confluence(rows,ve,ae)
    assert any(c['outcomes'].get('180s',{}).get('state')=='CENSORED_OBSERVATION_GAP'
               for c in study['cases'])
    # An in-memory synthetic test can persist with verification explicitly off;
    # actual UI ALWAYS requires verify_raw=True and real original SQLite.
    source=Path(__file__).resolve().parents[1]
    import shutil
    paths=[
        'research/micro_flow_v3/volume_aggression_confluence.py',
        'research/micro_flow_v3/aggression_exhaustion_discovery.py',
        'research/micro_flow_v3/swing_sweep_discovery.py',
        'research/micro_flow_v3/sweep_entry_research.py',
        'research/micro_flow_v3/replay.py',
        'research/micro_flow_v3/tape.py',
        'dashboard/micro_flow_v3_confluence_tab.py',
    ]
    for name in paths:
        dest=tmp_path/name
        dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(source/name,dest)
    replay=tmp_path/'source.jsonl'
    replay.write_text('synthetic')
    summary_path=tmp_path/'summary.json';summary_path.write_text('{}')
    session={'buckets':rows,'tape_path':str(tmp_path/'does-not-exist.sqlite3'),
             'replay_path':str(replay),'summary_path':str(summary_path),
             'tape':'synthetic.sqlite3','summary':{
                'replay_jsonl_sha256':hashlib.sha256(b'synthetic').hexdigest(),
                'buckets_emitted':len(rows),'source_tape_rows':len(rows),
                'source_tape_digest_sha256':'FAKE_FOR_TEST_ONLY'}}
    output=save_confluence_run(tmp_path,session,ve,ae,study,verify_raw=False)
    manifest=json.loads((output/'manifest.json').read_text())
    assert manifest['research_role']=='EXPLORATORY_NOT_OUT_OF_SAMPLE'
    assert manifest['source_sqlite_verification']=='NOT_REVERIFIED'
    assert manifest['source_buckets_sha256']==study['source_buckets_sha256']
    for artifact,expected in manifest['artifacts_sha256'].items():
        assert hashlib.sha256((output/artifact).read_bytes()).hexdigest()==expected
    assert replay.read_text()=='synthetic'
    bad=deepcopy(study)
    bad['cases'][0]['cohort']='MANIPULATED'
    with pytest.raises(DiscoveryError,match='inputs changed'):
        save_confluence_run(tmp_path,session,ve,ae,bad,verify_raw=False)
