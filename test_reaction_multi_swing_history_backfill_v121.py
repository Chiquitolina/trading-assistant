import csv
import json
import tempfile
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
import zlib

from reaction_multi_swing_outcome_join_v12 import BAR, MINUTE
import reaction_multi_swing_history_backfill_v121 as m

BASE=1700000040000  # one-minute aligned? normalized below
BASE=(BASE//MINUTE)*MINUTE


def event(key='BTCUSDT|LONG|X', start=BASE, side='LONG', swing=100.0):
    return {'touch_key':key,'symbol':'BTCUSDT','side':side,'swing_price':str(swing),
            'retest_ts':str(start-MINUTE),'known_ts':str(start),
            'original_status':'REACTION','v11_state':'V11_OK'}


def kl(ts,price=100.,hi=100.1,lo=99.99,close=100.):
    return [ts,str(price),str(hi),str(lo),str(close),'0',ts+59999,'0','0','0','0','0']


class Backfill121(unittest.TestCase):
    def test_merge_overlapping_intervals(self):
        feats=[event('a'),event('b', BASE+MINUTE),event('c',BASE+120*MINUTE)]
        got=m.grouped_intervals(feats)['BTCUSDT']
        self.assertEqual(len(got),2)
        self.assertEqual(got[0],(BASE,BASE+60*MINUTE))

    def test_plan_is_read_only_no_network(self):
        with patch.object(m,'load_features',return_value=([event()],{'structural_cutoff':'retest_ts (PRE_TOUCH)'})),\
             patch.object(m,'http_fetch',side_effect=AssertionError('network used')):
            self.assertEqual(m.main(['--features','dummy.csv','--plan']),0)

    def test_future_pivot_must_not_be_used(self):
        e=event()
        e['known_ts']=str(BASE+MINUTE)
        with self.assertRaises(ValueError):m.check_features([e])

    def test_reject_partial_or_bad_ohlc(self):
        with self.assertRaises(ValueError):m.validate_kline('BTCUSDT',kl(BASE,100,99,99.9,100),BASE,BASE,BASE)
        with self.assertRaises(ValueError):m.validate_kline('BTCUSDT',kl(BASE+MINUTE),BASE,BASE+MINUTE,BASE)

    def test_chunk_request_keeps_exact_1m(self):
        rows=[event()]
        calls=[]
        def get(sym,a,b,timeout=0):
            calls.append((a,b))
            return [kl(ts) for ts in range(a,b+1,MINUTE)]
        data,queries,cutoff=m.fetch_all(m.grouped_intervals(rows),fetcher=get,pause=0,max_closed_ts=BASE+100*MINUTE)
        self.assertEqual(len(calls),1)
        self.assertEqual(len(data['BTCUSDT']),60)
        self.assertEqual(len(queries),1)
        self.assertEqual(m.contiguous_path(rows[0],data)[1],None)

    def test_gap_censors_and_never_bridges(self):
        bar={BASE+i*MINUTE:(BASE+i*MINUTE,100,100.1,99.99,100)
             for i in range(60) if i!=12}
        bars,missing=m.contiguous_path(event(),{'BTCUSDT':bar})
        self.assertEqual(len(bars),12)
        self.assertEqual(missing,BASE+12*MINUTE)
        out,_=m.build_output([event()],{'BTCUSDT':bar})
        self.assertEqual(out[0]['v12_defense_30m'],'CENSORED')
        self.assertEqual(out[0]['v12_opposite_break_60m'],'CENSORED')

    def test_missing_start_is_not_zero_confluence(self):
        f=event();out,_=m.build_output([f],{})
        self.assertEqual(out[0]['v12_quality_state'],'NOT_COVERED')
        self.assertEqual(out[0]['v12_failure_reason'],'HISTORICAL_1M_START_MISSING')
        self.assertEqual(out[0]['v121_data_quality'],'NO_START_BAR')
        self.assertEqual(out[0]['v11_state'],f['v11_state'])

    def test_same_bar_both_barriers_ambiguous(self):
        bars={BASE+i*MINUTE:(BASE+i*MINUTE,100,101 if i==0 else 100.1,99 if i==0 else 99.9,100)
              for i in range(60)}
        out,_=m.build_output([event()],{'BTCUSDT':bars})
        self.assertEqual(out[0]['v12_defense_30m'],'AMBIGUOUS_SAME_BAR')

    def test_timestamp_when_opposite_break(self):
        bars={BASE+i*MINUTE:(BASE+i*MINUTE,100,100.2,99.7,99.8 if i in (3,4) else 100)
              for i in range(60)}
        out,_=m.build_output([event()],{'BTCUSDT':bars})
        self.assertEqual(out[0]['v12_opposite_break_60m'],'OPPOSITE_CONFIRMED')
        self.assertEqual(out[0]['v12_opposite_break_confirmed_ts'],BASE+5*MINUTE)

    def test_audit_parity_read_only_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'paths.sqlite3'
            with sqlite3.connect(db) as c:
                c.execute('CREATE TABLE paths (touch_key TEXT, entry_ts INTEGER, count INTEGER, path_blob BLOB)')
                c.execute('INSERT INTO paths VALUES (?,?,?,?)',('key',BASE,2,zlib.compress(BAR.pack(BASE,100,100.1,99.9,100)+BAR.pack(BASE+MINUTE,100,100.1,99.9,100))))
            f=event('key')
            data={'BTCUSDT':{BASE:(BASE,100,100.1,99.9,100),BASE+MINUTE:(BASE+MINUTE,100,100.1,99.9,100)}}
            result=m.parity_with_existing([f],data,db)
            self.assertEqual(result['matching_bars'],2)
            self.assertEqual(result.get('ohlc_mismatches',0),0)
            self.assertEqual(result['events_found'],1)

    def test_parity_ignores_collector_tail_beyond_60m(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'paths.sqlite3'
            old=[(BASE+i*MINUTE,100,100.1,99.9,100) for i in range(61)]
            with sqlite3.connect(db) as c:
                c.execute('CREATE TABLE paths (touch_key TEXT, entry_ts INTEGER, count INTEGER, path_blob BLOB)')
                c.execute('INSERT INTO paths VALUES (?,?,?,?)',
                          ('key',BASE,61,zlib.compress(b''.join(BAR.pack(*x) for x in old))))
            f=event('key')
            data={'BTCUSDT':{x[0]:x for x in old[:60]}}
            result=m.parity_with_existing([f],data,db)
            self.assertEqual(result['matching_bars'],60)
            self.assertEqual(result.get('old_bars_absent_from_download',0),0)

    def test_closure_validation_never_uses_current_open_bar(self):
        def fetcher(symbol,a,b,timeout=0):
            return [kl(BASE)]
        # The event remains unobservable at this cutoff.
        data,_,_=m.fetch_all({'BTCUSDT':[(BASE,BASE+MINUTE)]},fetcher=fetcher,
                             pause=0,max_closed_ts=BASE-MINUTE)
        self.assertEqual(data['BTCUSDT'],{})

    def test_full_feature_fields_immutable_and_output_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            f=event();f['experimental_pre']='untouched'
            bars={'BTCUSDT':{BASE+i*MINUTE:(BASE+i*MINUTE,100,100.2,99.9,100.1) for i in range(60)}}
            outputs,_=m.build_output([f],bars)
            self.assertEqual(outputs[0]['experimental_pre'],'untouched')
            source=p/'features.csv';source.write_text('placeholder')
            (p/'manifest.json').write_text('{}')
            result=m.write_results(p/'output',outputs,bars,[],features_path=source,
                                   feature_manifest={'structural_cutoff':'retest_ts (PRE_TOUCH)'},
                                   code_path=Path(m.__file__),parity={'db_found':False},cutoff_ts=BASE+100*MINUTE)
            self.assertTrue(Path(result['csv']).is_file())
            self.assertTrue((p/'output'/'historical_1m.csv.gz').is_file())
            with self.assertRaises(FileExistsError):
                m.write_results(p/'output',outputs,bars,[],features_path=source,
                                feature_manifest={'structural_cutoff':'retest_ts (PRE_TOUCH)'},
                                code_path=Path(m.__file__),parity={},cutoff_ts=BASE)


if __name__=='__main__':unittest.main()
