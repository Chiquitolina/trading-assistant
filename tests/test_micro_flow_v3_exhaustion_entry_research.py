"""Causal/fidelity regression tests for research-only exhaustion entry A/B."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from research.micro_flow_v3.aggression_exhaustion_discovery import discover_exhaustion
from research.micro_flow_v3.exhaustion_entry_research import (
    evaluate_exhaustion_entries, save_exhaustion_entry_run, _known_swing,
)
from research.micro_flow_v3.swing_sweep_discovery import DiscoveryError
from tests.test_micro_flow_v3_sweep_entry_exhaustion import _pulses


def _swing(rows, *, side='LOW', level=99.9, segment=1, index=38):
    row=rows[index]
    return {'swing_id':f'{segment}:1m:3x3:{side}:0',
        'segment_id':segment, 'timeframe_minutes':1,'bars':3,
        'side':side,'level':level,'pivot_ms':0,
        'confirmed_available_wall_ns':row['available_wall_ns'],
        'confirmed_available_ms':row['available_ms']}


def _run(rows=None, swings=None, **kwargs):
    rows=_pulses() if rows is None else rows
    ex=discover_exhaustion(rows,followup_seconds=80)
    if swings is None:
        swings=[_swing(rows)]
    result=evaluate_exhaustion_entries(rows,ex,swings,**kwargs)
    impulse=next(e for e in ex['events'] if e['pressure_side']=='BUY')
    cases={e['variant']:e for e in result['entries'] if e['event_id']==impulse['event_id']}
    return ex,result,cases


def test_every_impulse_in_both_variants_with_explicit_failures_and_references():
    ex,result,cases=_run()
    assert len(result['entries'])==2*len(ex['events'])
    assert result['summary']['rows']==len(result['entries'])
    assert cases['FAILURE']['signal_state']=='AVAILABLE'
    assert cases['FAILURE_SWING_BREAK']['signal_state']=='AVAILABLE'
    assert cases['FAILURE_SWING_BREAK']['preknown_swing']['level']==99.9
    assert cases['FAILURE_SWING_BREAK']['signal_available_wall_ns'] > cases['FAILURE']['signal_available_wall_ns']
    for c in cases.values():
        assert c['entry_reference']['entry_reference_available_wall_ns'] > c['signal_available_wall_ns']
        assert c['entry_reference']['entry_reference_kind'].endswith('NOT_EXECUTABLE_FILL')
        assert c['trade_simulated'] is False
        assert c['outcomes']['60s']['state']=='OBSERVED_PROXY'
        assert c['outcomes']['1800s']['state'].startswith('CENSORED')
        h=c['outcomes']['60s']
        assert h['net_after_illustrative_cost_pct']==pytest.approx(h['return_directional_pct']-.12)
        assert h['mfe_directional_pct']>=0 and h['mae_directional_pct']<=0
    assert result['summary']['paired']['60s']['paired_observed_impulses']>=1


def test_no_swing_preserves_full_cohort_but_suppresses_structural_entry():
    ex,result,cases=_run(swings=[])
    assert len(result['entries'])==2*len(ex['events'])
    assert cases['FAILURE_SWING_BREAK']['signal_state']=='NO_PREKNOWN_SWING'
    assert cases['FAILURE_SWING_BREAK']['entry_reference'] is None
    assert cases['FAILURE']['entry_reference'] is not None


def test_wrong_side_future_and_unconfirmed_swings_never_selected():
    rows=_pulses()
    ex=discover_exhaustion(rows,followup_seconds=80)
    impulse=next(e for e in ex['events'] if e['pressure_side']=='BUY')
    valid=_swing(rows)
    future=_swing(rows,index=70)
    wrong=_swing(rows,side='HIGH',level=100.5)
    wrong_price=_swing(rows,level=100.5)
    assert _known_swing(impulse,[future,wrong,wrong_price],swing_max_age_minutes=120) is None
    assert _known_swing(impulse,[valid,future,wrong],swing_max_age_minutes=120)['swing_id']==valid['swing_id']


def test_structure_selection_is_not_based_on_future_outcome():
    rows=_pulses()
    ex,r1,c1=_run(rows)
    altered=deepcopy(rows)
    for row in altered:
        if row['second_start_ms']//1000 > 110:
            row['open']=row['close']=row['high']=row['low']='110'
    ex2,r2,c2=_run(altered)
    for v in ('FAILURE','FAILURE_SWING_BREAK'):
        assert c1[v]['signal_available_wall_ns']==c2[v]['signal_available_wall_ns']
        assert c1[v]['preknown_swing']==c2[v]['preknown_swing']
    assert r1['source_buckets_sha256']!=r2['source_buckets_sha256']


def test_gap_after_failure_censors_break_instead_of_searching_further():
    rows=[r for r in _pulses() if r['second_start_ms']//1000 not in (57,58,59,60)]
    # Deliberately put structural threshold much further down after the gap.
    for row in rows:
        if row['second_start_ms']//1000 < 64 and row['second_start_ms']//1000 >= 56:
            row['open']=row['close']=row['high']=row['low']='99.96'
        if row['second_start_ms']//1000 >= 64:
            row['open']=row['close']=row['high']=row['low']='99.8'
    ex,result,cases=_run(rows,swings=[_swing(rows)])
    assert cases['FAILURE_SWING_BREAK']['signal_state']=='CENSORED_OBSERVATION_GAP'
    assert cases['FAILURE_SWING_BREAK']['entry_reference'] is None


def test_source_replay_mismatch_or_modified_event_rejected():
    rows=_pulses()
    ex=discover_exhaustion(rows,followup_seconds=80)
    result=evaluate_exhaustion_entries(rows,ex,[_swing(rows)])
    broken=deepcopy(rows)
    broken[90]['close']='90'
    broken[90]['low']='90'
    with pytest.raises(DiscoveryError,match='Replay changed'):
        evaluate_exhaustion_entries(broken,ex,[_swing(rows)])
    broken_ex=deepcopy(ex)
    broken_ex['events'][0]['event_id']='FORGED'
    with pytest.raises(DiscoveryError,match='input cohort'):
        save_exhaustion_entry_run(Path('/unused'),{},broken_ex,[_swing(rows)],result,
                                  verify_raw=False)


def test_replay_is_never_mutated_and_deterministic_for_fixed_inputs():
    rows=_pulses()
    frozen=json.dumps(rows,sort_keys=True)
    a,ra,_=_run(rows)
    b,rb,_=_run(rows)
    assert json.dumps(ra,sort_keys=True)==json.dumps(rb,sort_keys=True)
    assert frozen==json.dumps(rows,sort_keys=True)
    assert a['source_buckets_sha256']==b['source_buckets_sha256']


def test_save_refuses_missing_raw_and_creates_immutable_auditable_run(tmp_path):
    rows=_pulses()
    ex,result,_=_run(rows)
    swing=[_swing(rows)]
    replay=tmp_path/'replay.jsonl'
    replay.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    summary=tmp_path/'summary.json'
    summary.write_text(json.dumps({'replay_jsonl_sha256':hashlib.sha256(replay.read_bytes()).hexdigest(),
        'source_tape_rows':900,'source_tape_digest_sha256':'fiction',
        'buckets_emitted':len(rows)}))
    session={'replay_path':str(replay),'summary_path':str(summary),
        'tape_path':str(tmp_path/'missing.sqlite3'),
        'buckets':rows,'summary':json.loads(summary.read_text())}
    with pytest.raises(Exception):
        save_exhaustion_entry_run(tmp_path,session,ex,swing,result,verify_raw=True)
    assert not (tmp_path/'reports').exists()
    project=Path(__file__).resolve().parents[1]
    for name in ('research/micro_flow_v3/exhaustion_entry_research.py',
                'research/micro_flow_v3/aggression_exhaustion_discovery.py',
                'research/micro_flow_v3/aggression_features.py',
                'research/micro_flow_v3/sweep_entry_research.py',
                'research/micro_flow_v3/swing_sweep_discovery.py',
                'research/micro_flow_v3/replay.py',
                'research/micro_flow_v3/tape.py',
                'engine/live/research/swing_detector.py'):
        dest=tmp_path/name
        dest.parent.mkdir(parents=True,exist_ok=True)
        dest.write_bytes((project/name).read_bytes())
    output=save_exhaustion_entry_run(tmp_path,session,ex,swing,result,verify_raw=False)
    m=json.loads((output/'manifest.json').read_text())
    assert m['source_sqlite_verification']=='NOT_REVERIFIED'
    assert m['source_preknown_swings_sha256']==result['source_preknown_swings_sha256']
    assert m['research_role']=='EXPLORATORY_NOT_OOS_VALIDATION'
    for name,digest in m['artifacts_sha256'].items():
        assert hashlib.sha256((output/name).read_bytes()).hexdigest()==digest
    assert len((output/'entries.jsonl').read_text().splitlines())==len(result['entries'])
    with pytest.raises(FileExistsError):
        output.mkdir(exist_ok=False)


def test_sell_impulse_mirror_generates_long_only_after_known_high_break():
    rows=deepcopy(_pulses())
    for row in rows:
        old={'open':float(row['open']), 'high':float(row['high']),
             'low':float(row['low']), 'close':float(row['close'])}
        row['open']=str(200-old['open'])
        row['close']=str(200-old['close'])
        row['high']=str(200-old['low'])
        row['low']=str(200-old['high'])
        row['buy_qty'],row['sell_qty']=row['sell_qty'],row['buy_qty']
        row['delta_qty']=str(float(row['buy_qty'])-float(row['sell_qty']))
    ex=discover_exhaustion(rows,followup_seconds=80)
    known=_swing(rows,side='HIGH',level=100.1)
    study=evaluate_exhaustion_entries(rows,ex,[known])
    sell=next(e for e in ex['events'] if e['pressure_side']=='SELL')
    candidates=[c for c in study['entries'] if c['event_id']==sell['event_id']]
    assert {c['hypothesis_direction'] for c in candidates}=={'LONG'}
    assert all(c['signal_state']=='AVAILABLE' for c in candidates)
    assert all(c['outcomes']['60s']['state']=='OBSERVED_PROXY' for c in candidates)


def test_structural_signal_requires_swing_age_and_sufficient_break():
    rows=_pulses()
    ex=discover_exhaustion(rows,followup_seconds=80)
    future_untouched=evaluate_exhaustion_entries(rows,ex,[_swing(rows,level=99.7)])
    buy=next(e for e in ex['events'] if e['pressure_side']=='BUY')
    case=next(c for c in future_untouched['entries'] if c['event_id']==buy['event_id']
              and c['variant']=='FAILURE_SWING_BREAK')
    assert case['signal_state']=='NO_BREAK_WITHIN_FOLLOWUP' or case['signal_state']=='CENSORED_SESSION_END'
    assert case['entry_reference'] is None
    # Since the only level is too old under 1-minute age, B has no eligible swing.
    age_limited=evaluate_exhaustion_entries(rows,ex,[_swing(rows)],
                                             swing_max_age_minutes=1)
    case2=next(c for c in age_limited['entries'] if c['event_id']==buy['event_id']
               and c['variant']=='FAILURE_SWING_BREAK')
    # impulse ~s49 and swing at s38: still younger than one minute in this fixture.
    assert case2['preknown_swing'] is not None
