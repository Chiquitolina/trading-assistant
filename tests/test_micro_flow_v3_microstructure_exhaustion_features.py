"""Causal, raw-footprint and persistence safeguards for shared V3 features."""
from __future__ import annotations
import json
from pathlib import Path
import sqlite3
import pytest

from research.micro_flow_v3.microstructure_exhaustion_features import (
    _hash_json, build_exhaustion_feature_catalog, read_footprint_from_closed_raw,
    save_feature_run,
)
from research.micro_flow_v3.swing_sweep_discovery import DiscoveryError, validate_buckets
from research.micro_flow_v3.tape import TapeStore, replay_digest

BASE = 1792371336000


def rows(n=70, *, gap_at=None, segment_at=None):
    result=[]
    cvd=0.
    for i in range(n):
        sec=i if i < (gap_at if gap_at is not None else 10**9) else i+7
        seg=1 if segment_at is None or i < segment_at else 2
        # independent causality boundary; no artificially continuous segment content
        price=2400+(i*0.02)
        buy=2.0 if i%2==0 else 1.0
        sell=1.0
        cvd+=buy-sell
        t=BASE+sec*1000
        result.append({'event_type':'BUCKET_1S','symbol':'ETHUSDT',
            'segment_id':seg,'second_start_ms':t,'second_end_ms_exclusive':t+1000,
            'available_wall_ns':(t+1100)*1_000_000, 'available_ms':t+1100,
            'first_seq':i+1,'last_seq':i+1,'first_agg_id':100+i,'last_agg_id':100+i,
            'open':str(price),'high':str(price+.05),'low':str(price-.05),
            'close':str(price+.01),'buy_qty':str(buy),'sell_qty':str(sell),
            'delta_qty':str(buy-sell),'cvd_qty':str(cvd),'trades':1})
    return result


def cohort(rs,indices=(48,)):
    rs=validate_buckets(rs)
    events=[]
    for ix in indices:
        r=rs[ix]
        events.append({'event_id':f'1:EXHAUST:BUY:{r["second_start_ms"]}',
            'segment_id':r['segment_id'],'pressure_side':'BUY',
            'hypothesis_direction':'SHORT',
            'impulse':{'observed_second_start_ms':r['second_start_ms'],
                'available_wall_ns':r['available_wall_ns'],
                'last_seq':r['last_seq'],'first_seq':r['first_seq'],
                'observed_close':r['close']}})
    return {'schema':'synthetic-AE-v1','parameters':{'test':True},
            'source_buckets_sha256':_hash_json(rs),'events':events}


def test_catalog_every_impulse_and_windows():
    rs=rows()
    a=cohort(rs,(48,59))
    cat=build_exhaustion_feature_catalog(rs,a)
    assert cat['summary']['impulses']==2
    assert len(cat['features'])==2
    for entry in cat['features']:
        assert set(entry['windows'])=={'5s','15s','30s','60s'}
        for window in entry['windows'].values():
            assert window['available_wall_ns']==entry['signal_available_wall_ns']
            if window['state']=='OBSERVED':
                assert window['last_seq']==entry['signal_last_seq']
                assert 0 < window['coverage_fraction'] <= 1


def test_future_changes_do_not_change_past_features():
    original=rows(70)
    a=cohort(original,(35,))
    orig=build_exhaustion_feature_catalog(original,a)
    altered=rows(90)
    for item in altered[36:]:
        item['close']='3000'  # later observation not available at impulse
        item['high']='3001'
        item['low']='2390'
    b=cohort(altered,(35,))
    new=build_exhaustion_feature_catalog(altered,b)
    assert orig['features']==new['features']


def test_no_cross_segment_future_or_history():
    r=rows(70,segment_at=40)
    a=cohort(r,(45,))
    cat=build_exhaustion_feature_catalog(r,a)
    win=cat['features'][0]['windows']['15s']
    assert win['state']=='INSUFFICIENT_OBSERVED_COVERAGE'
    assert win['first_seq']==41


def test_missing_seconds_never_forward_filled():
    r=rows(70,gap_at=45)
    a=cohort(r,(48,))
    cat=build_exhaustion_feature_catalog(r,a,min_coverage=.40)
    feature=cat['features'][0]['windows']['15s']
    assert feature['state']=='CENSORED_OBSERVATION_GAP'


def test_wrong_replay_rejected():
    r=rows(70)
    a=cohort(r,(40,))
    r[-1]['buy_qty']='700'
    with pytest.raises(DiscoveryError,match='different Replay'):
        build_exhaustion_feature_catalog(r,a)


def test_unpublished_or_fabricated_impulse_rejected():
    r=rows(70)
    a=cohort(r,(40,))
    a['events'][0]['impulse']['available_wall_ns']+=10**9
    with pytest.raises(DiscoveryError,match='published data differs'):
        build_exhaustion_feature_catalog(r,a)


def test_repeated_id_rejected():
    r=rows(70)
    a=cohort(r,(35,))
    a['events'].append(dict(a['events'][0]))
    with pytest.raises(DiscoveryError,match='Duplicate'):
        build_exhaustion_feature_catalog(r,a)


def test_prior_second_rejection_cannot_use_same_second_extreme():
    r=rows(50)
    for q in r:
        q['open']='2400';q['high']='2400';q['low']='2399';q['close']='2400'
    r[39]['high']='2420'  # extreme in SAME final second
    a=cohort(r,(39,))
    feat=build_exhaustion_feature_catalog(r,a)['features'][0]['windows']['5s']
    assert feat['prior_second_extreme_price']==2400
    assert feat['rejection_after_prior_extreme_observed'] is False


def test_prior_second_extreme_followed_by_retreat_can_be_observed():
    r=rows(50)
    for q in r:
        q['open']='2400';q['high']='2400.01';q['low']='2399.99';q['close']='2400'
    r[37]['high']='2405'
    a=cohort(r,(39,))
    feat=build_exhaustion_feature_catalog(r,a)['features'][0]['windows']['5s']
    assert feat['rejection_after_prior_extreme_observed'] is True
    assert feat['prior_extreme_second_start_ms']==r[37]['second_start_ms']


def test_input_validation():
    r=rows()
    a=cohort(r,(50,))
    with pytest.raises(DiscoveryError):
        build_exhaustion_feature_catalog(r,a,windows_seconds=(5,5))
    with pytest.raises(DiscoveryError):
        build_exhaustion_feature_catalog(r,a,min_coverage=0)


def test_footprint_raw_only_verified_closed_and_integrity(tmp_path):
    tape=tmp_path/'ETHUSDT.sqlite3'
    store=TapeStore(tape,batch_size=1)
    for i in range(20):
        p={'e':'aggTrade','s':'ETHUSDT','a':101+i,'T':BASE+i*1000,'E':BASE+i*1000,
           'p':('2400' if i%2 else '2400.10'),'q':'2.0','m':bool(i%2)}
        store.ingest(json.dumps(p),wall_ns=(BASE+i*1000+100)*1_000_000,
                     mono_ns=100000+i*1000000)
    store.marker('PROCESS_STOP',wall_ns=(BASE+21_000)*1_000_000,mono_ns=123123123)
    store.close()
    count,digest=replay_digest(tape)
    assert count==21
    event={'event_id':'faux','signal_available_wall_ns':(BASE+20_100)*1_000_000,
           'windows':{'15s':{'state':'OBSERVED', 'first_seq':6,'last_seq':20,
               'buy_qty':14.0,'sell_qty':16.0}}}
    ses={'tape_path':str(tape),'tape':'ETHUSDT.sqlite3',
         'summary':{'source_tape_rows':count,'source_tape_digest_sha256':digest}}
    foot=read_footprint_from_closed_raw(ses,event,window_seconds=15)
    assert foot['trade_rows']==15
    assert foot['levels_total']==2
    assert sum(float(v['buy_qty']) for v in foot['levels'])==14.
    assert sum(float(v['sell_qty']) for v in foot['levels'])==16.
    con=sqlite3.connect(str(tape))
    con.execute('UPDATE tape SET quantity=? WHERE seq=7',('999',))
    con.commit()
    con.close()
    with pytest.raises(DiscoveryError,match='does not match'):
        read_footprint_from_closed_raw(ses,event,window_seconds=15)


def test_footprint_missing_raw_no_fallback(tmp_path):
    ses={'tape_path':str(tmp_path/'missing.sqlite3'),'tape':'missing.sqlite3'}
    with pytest.raises(DiscoveryError,match='unavailable'):
        read_footprint_from_closed_raw(ses,{'windows':{'15s':{'state':'OBSERVED'}}},window_seconds=15)


def test_quote_impact_from_observed_replay_not_fabricated():
    rs=rows(70)
    event=cohort(rs,(45,))
    missing=build_exhaustion_feature_catalog(rs,event)['features'][0]['windows']['5s']
    assert missing['gross_quote_usdt'] is None
    assert missing['signed_impact_bps_per_100k_usdt_executed'] is None
    for r in rs:
        r['buy_quote']=str(float(r['buy_qty'])*2400)
        r['sell_quote']=str(float(r['sell_qty'])*2400)
    event=cohort(rs,(45,))
    feature=build_exhaustion_feature_catalog(rs,event)['features'][0]['windows']['5s']
    assert feature['gross_quote_usdt'] > 0
    assert feature['signed_impact_bps_per_100k_usdt_executed'] is not None


def test_save_refuses_missing_raw_without_creating_run(tmp_path):
    r=rows()
    a=cohort(r,(45,))
    catalog=build_exhaustion_feature_catalog(r,a)
    replay=tmp_path/'replay.jsonl'
    replay.write_text(''.join(json.dumps(item)+'\n' for item in r))
    summary=tmp_path/'summary.json'
    summary.write_text('{}')
    session={'buckets':r,'tape_path':str(tmp_path/'missing.sqlite3'),
        'tape':'missing.sqlite3','replay_path':str(replay),'summary_path':str(summary),
        'summary':{'symbol':'ETHUSDT','replay_jsonl_sha256':__import__('hashlib').sha256(replay.read_bytes()).hexdigest(),
        'source_tape_rows':0,'source_tape_digest_sha256':'not-the-real-digest'}}
    with pytest.raises((sqlite3.OperationalError, DiscoveryError)):
        save_feature_run(tmp_path,session,a,catalog)
    assert not (tmp_path/'reports').exists()


def test_save_success_creates_verifiable_artifacts_and_manifest(tmp_path):
    from research.micro_flow_v3.swing_sweep_discovery import file_sha256
    rs=rows(70)
    a=cohort(rs,(45,55))
    cat=build_exhaustion_feature_catalog(rs,a)
    tape=tmp_path/'src.sqlite3'
    store=TapeStore(tape,batch_size=1)
    for i in range(70):
        trade={'e':'aggTrade','s':'ETHUSDT','a':500+i,'T':BASE+i*1000,
            'E':BASE+i*1000,'p':'2400','q':'1.0','m':False}
        store.ingest(json.dumps(trade),wall_ns=(BASE+i*1000+100)*1_000_000,
                     mono_ns=101000000+i*1000000)
    store.marker('PROCESS_STOP',wall_ns=(BASE+71_000)*1_000_000,mono_ns=987654321)
    store.close()
    count,digest=replay_digest(tape)
    rep=tmp_path/'example_1s.jsonl'
    rep.write_text(''.join(json.dumps(x)+'\n' for x in rs),encoding='utf8')
    summ=tmp_path/'summary.json'
    summ.write_text('example')
    for name in ('aggression_exhaustion_discovery.py','aggression_features.py','replay.py'):
        folder=tmp_path/'research'/'micro_flow_v3'
        folder.mkdir(parents=True,exist_ok=True)
        folder.joinpath(name).write_text('source for test')
    session={'tape_path':str(tape),'tape':tape.name,
       'summary':{'symbol':'ETHUSDT','replay_jsonl_sha256':file_sha256(rep),
           'source_tape_rows':count,'source_tape_digest_sha256':digest},
       'summary_path':str(summ),'replay_path':str(rep),'buckets':rs}
    target=save_feature_run(tmp_path,session,a,cat)
    saved=json.loads((target/'manifest.json').read_text())
    assert saved['raw_logical_sha256']==digest
    assert saved['artifacts_sha256']['impulse_features.jsonl']==file_sha256(target/'impulse_features.jsonl')
    assert len((target/'impulse_features.jsonl').read_text().splitlines())==2
    assert saved['research_contract']['outcomes'].startswith('none')


def test_nonfinite_quote_is_not_silently_recorded():
    rs=rows(70)
    for r in rs:
        r['buy_quote']='200'
        r['sell_quote']='100'
    rs[43]['buy_quote']='nan'
    event=cohort(rs,(45,))
    with pytest.raises(DiscoveryError,match='Invalid executed USDT'):
        build_exhaustion_feature_catalog(rs,event)
