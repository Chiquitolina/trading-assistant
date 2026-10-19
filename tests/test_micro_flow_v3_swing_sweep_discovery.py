"""No lookahead, provenance and session-boundary invariants for sweep discovery."""
import hashlib
import json
from pathlib import Path

import pytest

from research.micro_flow_v3.swing_sweep_discovery import (
    DiscoveryError, validate_buckets, build_observed_candles,
    detect_causal_swings, discover, save_immutable_run,
)


def _bucket(sec, price, high=None, low=None, seg=1, avail_delay_ms=1200):
    start = sec * 1000
    return {
        'event_type':'BUCKET_1S', 'symbol':'ETHUSDT', 'segment_id':seg,
        'second_start_ms':start,'second_end_ms_exclusive':start+1000,
        'available_wall_ns':(start+avail_delay_ms)*1000000,
        'available_ms':start+avail_delay_ms,
        'first_seq':seg*1000000+sec*2,'last_seq':seg*1000000+sec*2+1,
        'trades':2,'open':str(price),'high':str(high if high is not None else price),
        'low':str(low if low is not None else price),'close':str(price),
        'buy_qty':'5','sell_qty':'10','delta_qty':'-5','cvd_qty':'-5',
    }


def _sequence(seg=1, shift=0):
    """Pivot HIGH at minute 4, confirmed after m7, swept at m9."""
    highs = [100,101,102,103,110,106,105,104,108,112,109.8,109.0,109.99,105]
    closes=[99,100,101,102,106,103,102,101,107,111,109.2,108,109,104]
    data=[]
    for minute,(high,close) in enumerate(zip(highs,closes)):
        # Window always has 2 observed seconds, never synthetic zero-volume.
        base=shift+minute*60
        low=min(close-1,99)
        data.append(_bucket(base+1,close,high,low,seg))
        data.append(_bucket(base+50,close,high,low,seg))
    return data


def test_causal_swing_confirmed_only_after_right_candle_published():
    rows=validate_buckets(_sequence())
    candles=build_observed_candles(rows,1)
    swings=detect_causal_swings(candles,3)
    high=next(s for s in swings if s['side']=='HIGH' and s['pivot_ms']==240000)
    assert high['confirm_candle_start_ms']==420000
    # m6 ends 420, but availability only after publication of m7's 1s bucket.
    assert high['confirmed_available_ms'] > 480000
    assert high['confirmed_available_wall_ns'] == next(
        c['available_wall_ns'] for c in candles if c['timestamp']==420000)
    # Original SwingDetector's confirmed_timestamp is the START of m6; must not use it.
    assert high['confirmed_available_ms'] > high['confirm_candle_start_ms']


def test_sweep_reclaim_departure_retest_are_strictly_ordered():
    rows=_sequence()
    output=discover(rows,timeframe_minutes=1,swing_bars=3)
    high=next(e for e in output['events'] if e['swing_side']=='HIGH' and e['pivot_ms']==240000)
    assert high['sweep']['observed_second_start_ms']>=541000
    assert high['swing_confirm_available_wall_ns'] < high['sweep']['available_wall_ns']
    assert high['reclaim'] is not None
    assert high['departure'] is not None
    assert high['retest'] is not None
    assert high['status']=='RETEST_OBSERVED'
    stamps=[high[k]['available_wall_ns'] for k in ('sweep','reclaim','departure','retest')]
    assert stamps==sorted(stamps) and len(set(stamps))==4
    assert high['hypothesis_direction']=='SHORT'
    assert high['sweep']['first_seq'] in [r['first_seq'] for r in rows]


def test_never_infer_reclaim_order_within_sweep_second():
    rows=_sequence()
    # Sweep crosses above level, yet closes below in SAME second.
    for r in rows:
        if r['second_start_ms'] == 541000:
            r['close']='109'
    e=next(e for e in discover(rows)['events'] if e['pivot_ms']==240000 and e['swing_side']=='HIGH')
    assert e['reclaim']['observed_second_start_ms']>e['sweep']['observed_second_start_ms']


def test_gap_minutes_do_not_support_pivot_across_missing_minute():
    rows=[r for r in _sequence() if r['second_start_ms']//60000 != 6]
    out=discover(rows)
    assert not any(e['pivot_ms']==240000 for e in out['events'])
    assert not any(s['pivot_ms']==240000 for s in out['swings'])


def test_segment_boundaries_are_not_joined():
    a=_sequence(seg=1)
    # Another session is another causal segment; no shared swing/pressure windows.
    b=_sequence(seg=2,shift=1200)
    out=discover(a+b)
    assert out['summary']['sweeps'] >= 2
    assert len({e['segment_id'] for e in out['events']})==2
    assert all(e['swing_id'].split(':')[0] == str(e['segment_id']) for e in out['events'])


def test_reject_publication_before_end_and_non_monotonic_buckets():
    rows=_sequence()
    broken=[dict(r) for r in rows]
    broken[5]['available_wall_ns']=broken[5]['second_end_ms_exclusive']*1000000-1
    with pytest.raises(DiscoveryError): discover(broken)
    with pytest.raises(DiscoveryError): discover(rows[:5]+rows[:5])


def test_incomplete_first_last_windows_and_no_fabricated_seconds():
    candles=build_observed_candles(validate_buckets(_sequence()),1)
    starts={c['timestamp'] for c in candles}
    assert 0 not in starts
    assert max(starts)==720000  # m12 full; m13 has no later observation
    assert all(c['seconds_observed']==2 for c in candles)
    assert all(c['seconds_in_window']==60 for c in candles)
    assert all(c['available_ms']>c['end_ms_exclusive'] for c in candles)


def test_5m_works_without_synthetic_bars():
    rows=[]
    for minute in range(75):
        level=100+((minute//5)%8)
        if minute==62:
            level=115
        rows.extend([_bucket(minute*60+1,level),_bucket(minute*60+50,level)])
    out=discover(rows,timeframe_minutes=5,swing_bars=2)
    assert out['summary']['candles']>10
    assert all(c['timeframe_minutes']==5 for c in out['candles'])


def test_persist_run_with_hashes_never_overwrites(tmp_path):
    rows=_sequence()
    out=discover(rows)
    root=tmp_path
    source_root=Path(__file__).resolve().parents[1]
    for filename in ('engine/live/research/swing_detector.py',
                     'research/micro_flow_v3/replay.py',
                     'research/micro_flow_v3/tape.py'):
        dest=root/filename
        dest.parent.mkdir(parents=True,exist_ok=True)
        dest.write_bytes((source_root/filename).read_bytes())
    replay=root/'replay.jsonl'
    replay.write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf8')
    summary=root/'summary.json'
    digest=hashlib.sha256(replay.read_bytes()).hexdigest()
    summary_obj={'replay_jsonl_sha256':digest,'source_tape_digest_sha256':'from_prior_verification',
                 'source_tape_rows':len(rows),'buckets_emitted':len(rows)}
    summary.write_text(json.dumps(summary_obj))
    session={'replay_path':str(replay),'summary_path':str(summary),
             'tape_path':'raw.sqlite3','tape':'synthetic.sqlite3','summary':summary_obj}
    path=save_immutable_run(root,session,out,verify_raw=False)
    assert (path/'manifest.json').exists()
    mf=json.loads((path/'manifest.json').read_text())
    assert mf['raw_status_at_save']=='NOT_REVERIFIED'
    assert mf['parameters']['swing_bars']==3
    assert (path/'events.jsonl').read_text().count('\n')==len(out['events'])
    assert (path/'manifest.json').exists()
    path2=save_immutable_run(root,session,out,verify_raw=False)
    assert path2 != path
    assert (path/'manifest.json').read_text()==(path/'manifest.json').read_text()
    assert replay.read_bytes().startswith(b'{')


def test_tampered_replay_does_not_create_manifest(tmp_path):
    rows=_sequence()
    out=discover(rows)
    replay=tmp_path/'replay.jsonl'
    replay.write_text('invalid',encoding='utf8')
    summary=tmp_path/'summary.json'
    summary.write_text('')
    sess={'replay_path':str(replay),'summary_path':str(summary),
          'tape_path':'absent.sqlite3','tape':'lost.sqlite3',
          'summary':{'replay_jsonl_sha256':'bad','buckets_emitted':len(rows)}}
    with pytest.raises(DiscoveryError,match='digest'):
        save_immutable_run(tmp_path,sess,out,verify_raw=True)
    assert not (tmp_path/'reports/micro_flow_v3/swing_sweep_discovery/runs').exists()


def test_discovery_deterministic_and_input_unmodified():
    rows=_sequence()
    baseline=json.dumps(rows,sort_keys=True)
    a=discover(rows,timeframe_minutes=1,swing_bars=3)
    b=discover(rows,timeframe_minutes=1,swing_bars=3)
    assert json.dumps(a,sort_keys=True)==json.dumps(b,sort_keys=True)
    assert json.dumps(rows,sort_keys=True)==baseline
