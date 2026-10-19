"""Causal research invariant tests: no intra-second ordering / no source edits."""
import hashlib
import json
from pathlib import Path

import pytest

from research.micro_flow_v3.aggression_features import effort_snapshot
from research.micro_flow_v3.aggression_exhaustion_discovery import discover_exhaustion
from research.micro_flow_v3.sweep_entry_research import (
    analyze_sweep_entries, save_entry_research_run,
)
from research.micro_flow_v3.swing_sweep_discovery import DiscoveryError, discover


def _row(s, price, *, op=None, buy=5., sell=5., segment=1, high=None, low=None):
    start=s*1000
    op=price if op is None else op
    hi=max(op,price) if high is None else high
    lo=min(op,price) if low is None else low
    return {
        'event_type':'BUCKET_1S','symbol':'ETHUSDT','segment_id':segment,
        'second_start_ms':start,'second_end_ms_exclusive':start+1000,
        'available_wall_ns':(start+1200)*1_000_000,'available_ms':start+1200,
        'first_seq':segment*100000+s*2,'last_seq':segment*100000+s*2+1,
        'trades':4,'open':str(op),'close':str(price),
        'high':str(hi),'low':str(lo),
        'buy_qty':str(buy),'sell_qty':str(sell),
        'delta_qty':str(buy-sell),'cvd_qty':'0',
    }


def _pulses():
    rows=[]
    for s in range(160):
        price=100.
        op=100.
        buy,sell=5.,5.
        if 45<=s<=49:
            op=100. + (s-45)*.1
            price=op+.1
            buy,sell=100.,1.
        elif 50<=s<=55:
            op=price=100.5
            buy,sell=85.,1.
        elif s>=56:
            op=price=99.8
        rows.append(_row(s,price,op=op,buy=buy,sell=sell))
    return rows


def test_impulse_then_weakening_then_failure_are_causal_and_independent():
    rows=_pulses()
    result=discover_exhaustion(rows,relative_volume=2.0,min_imbalance=.7,
                               cooldown_seconds=60,followup_seconds=70)
    b=next(e for e in result['events'] if e['pressure_side']=='BUY')
    assert b['impulse']['available_ms'] < b['weakening']['available_ms']
    assert b['impulse']['available_ms'] < b['failure']['available_ms']
    assert b['entry_reference']['entry_reference_second_start_ms']*1_000_000 > b['failure']['available_wall_ns']
    assert b['entry_state']=='OBSERVED_REFERENCE_NOT_FILL'
    assert b['hypothesis_direction']=='SHORT'
    assert b['outcomes']['30s']['state']=='OBSERVED_PROXY'
    assert b['impulse_features']['first_seq']<b['impulse_features']['last_seq']
    assert b['impulse_features']['baseline_buckets']==30


def test_impulse_detection_does_not_depend_on_future_price():
    rows=_pulses()
    a=discover_exhaustion(rows[:55])
    b=discover_exhaustion(rows)
    a_ids={r['event_id']:r['impulse'] for r in a['events']}
    b_ids={r['event_id']:r['impulse'] for r in b['events']}
    assert all(b_ids[k]==v for k,v in a_ids.items())
    assert a['events'][0]['impulse_features']==b['events'][0]['impulse_features']


def test_same_second_low_does_not_prove_failure():
    rows=_pulses()[:56]
    # The intrasecond low cannot be ordered after the impulse without ticks.
    rows[49]['low']='80'
    events=discover_exhaustion(rows,followup_seconds=100)['events']
    assert all(e['failure'] is None for e in events if e['pressure_side']=='BUY')


def test_followup_gap_is_explicitly_censored():
    rows=[r for r in _pulses() if not 56<=r['second_start_ms']//1000<=65]
    e=next(e for e in discover_exhaustion(rows,followup_seconds=100)['events']
           if e['pressure_side']=='BUY')
    assert e['followup_state']=='CENSORED_OBSERVATION_GAP'
    assert e['failure'] is None


def test_no_cross_segment_and_separate_ids():
    original=_pulses()
    second=[_row(s+1000,float(r['close']),buy=float(r['buy_qty']),
                 sell=float(r['sell_qty']),segment=2)
            for s,r in enumerate(original)]
    result=discover_exhaustion(original+second)
    assert {e['segment_id'] for e in result['events']}=={1,2}
    assert len({e['event_id'] for e in result['events']})==len(result['events'])
    assert all(e['impulse']['first_seq']//100000 == e['segment_id'] for e in result['events'])


def test_shared_feature_engine_strict_prior_baseline():
    rows=_pulses()
    starts=[r['second_start_ms'] for r in rows]
    shot=effort_snapshot(rows,starts,49)
    assert shot['relative_volume']>2.0
    assert shot['baseline_mean_qty_per_observed_second']==10.
    assert shot['available_wall_ns']==rows[49]['available_wall_ns']
    assert shot['pressure_side']=='BUY'
    # No synthetic seconds in a missing part of the window.
    broken=rows[:45]+rows[48:]
    ts=[r['second_start_ms'] for r in broken]
    assert effort_snapshot(broken,ts,46,max_observation_gap_seconds=2) is None


def test_reclaim_retest_policies_keep_full_cohort_and_no_fill():
    from tests.test_micro_flow_v3_swing_sweep_discovery import _sequence
    rows=_sequence()
    discovery=discover(rows)
    research=analyze_sweep_entries(rows,discovery)
    assert len(research['entries'])==2*len(discovery['events'])
    for case in research['entries']:
        if case['entry_reference']:
            assert 'NOT_EXECUTABLE_FILL' in case['entry_reference']['entry_reference_kind']
            assert case['entry_reference']['entry_reference_available_wall_ns'] > case['signal_available_wall_ns']
    assert {e['variant'] for e in research['entries']}=={'RECLAIM','RETEST'}


def test_source_buckets_unchanged_and_deterministic():
    rows=_pulses()
    before=json.dumps(rows,sort_keys=True)
    first=discover_exhaustion(rows)
    second=discover_exhaustion(rows)
    assert json.dumps(first,sort_keys=True)==json.dumps(second,sort_keys=True)
    assert json.dumps(rows,sort_keys=True)==before


def test_immutable_run_has_all_artifact_hashes_without_raw(tmp_path):
    from tests.test_micro_flow_v3_swing_sweep_discovery import _sequence
    rows=_sequence()
    discovery=discover(rows)
    research=analyze_sweep_entries(rows,discovery)
    exhaustion=discover_exhaustion(rows)
    project=Path(__file__).resolve().parents[1]
    needed=('research/micro_flow_v3/swing_sweep_discovery.py',
            'research/micro_flow_v3/sweep_entry_research.py',
            'research/micro_flow_v3/aggression_features.py',
            'research/micro_flow_v3/aggression_exhaustion_discovery.py',
            'research/micro_flow_v3/replay.py',
            'research/micro_flow_v3/tape.py',
            'engine/live/research/swing_detector.py')
    for name in needed:
        dest=tmp_path/name
        dest.parent.mkdir(parents=True,exist_ok=True)
        dest.write_bytes((project/name).read_bytes())
    replay=tmp_path/'replay.jsonl'
    replay.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    summary=tmp_path/'summary.json'
    digest=hashlib.sha256(replay.read_bytes()).hexdigest()
    summary.write_text(json.dumps({'replay_jsonl_sha256':digest,
       'source_tape_rows':len(rows),'source_tape_digest_sha256':'synthetic',
       'buckets_emitted':len(rows)}))
    session={'replay_path':str(replay),'summary_path':str(summary),
             'tape_path':str(tmp_path/'no_raw.sqlite3'),'tape':'synthetic.sqlite3',
             'summary':json.loads(summary.read_text()),'buckets':rows}
    path=save_entry_research_run(tmp_path,session,discovery,research,
                                  exhaustion=exhaustion,verify_raw=False)
    manifest=json.loads((path/'manifest.json').read_text())
    assert manifest['raw_status_at_save']=='NOT_REVERIFIED'
    assert manifest['exhaustion_summary']==exhaustion['summary']
    assert all(hashlib.sha256((path/f).read_bytes()).hexdigest()==digest
               for f,digest in manifest['artifacts_sha256'].items())
    assert len(list(path.glob('*.jsonl')))==6
    path2=save_entry_research_run(tmp_path,session,discovery,research,
                                  exhaustion=exhaustion,verify_raw=False)
    assert path2!=path
    # Mismatched independent sample must never be misattributed to source Replay.
    wrong=dict(exhaustion,source_buckets_sha256='0'*64)
    with pytest.raises(DiscoveryError,match='Exhaustion/source Replay buckets mismatch'):
        save_entry_research_run(tmp_path,session,discovery,research,exhaustion=wrong,verify_raw=False)
    replay.write_text('tampered')
    with pytest.raises(DiscoveryError,match='Replay digest'):
        save_entry_research_run(tmp_path,session,discovery,research,
                                exhaustion=exhaustion,verify_raw=False)
