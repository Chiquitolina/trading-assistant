"""Pruebas aisladas; no requieren reportes 290 ni conexión a Binance."""
import csv
import json
import math
import tempfile
import unittest
from pathlib import Path

import hll_structural_discovery_v1 as d

BASE=1_787_184_000_000  # 2026-08-20 00:00 UTC, alineado a 30m.


def sample(n=110, future=0):
    result={}
    for i in range(n+future):
        ts=BASE+i*d.TF
        val=100.0+0.12*i+3*math.sin(i*0.32)
        result[ts]=dict(open_ts=ts,close_ts=ts+d.TF,open=val,
                        high=val+1.0,low=val-1.0,close=val)
    return result


def record(pre=None, coverage=True, aid='attempt_1'):
    pre=pre or BASE+106*d.TF
    return dict(attempt_id=aid,symbol='TESTUSDT',level_id='level_1',
        pre_closed_ts=pre,breakout_closed_ts=pre+d.TF,
        zone_low=112.5,zone_high=113.5,atr_pre=1.1,
        pre_pattern_24='OTHER',pre_pattern_48='ASCENDING_CLEAN',
        pre_pattern_96='OTHER',pre_96_coverage=coverage)


class StructuralDiscoveryTests(unittest.TestCase):
    def test_exact_cutoff_not_post(self):
        pre=BASE+106*d.TF
        m=record(pre)
        a,sc=d.attempt_result(m,sample(110))
        b,sc2=d.attempt_result(m,sample(110,future=60))
        self.assertEqual(a,b)
        self.assertEqual(sc,sc2)
        self.assertEqual(len(sc),3)
        self.assertEqual(a['status'],'COMPLETE_PRE')
        self.assertNotIn('post_outcome',a)
        self.assertTrue(all(s['pre_closed_ts']==pre for s in sc))
    def test_gap_not_interpolated(self):
        m=record(coverage=False)
        candles=sample(110)
        del candles[BASE+55*d.TF]
        x,sc=d.attempt_result(m,candles)
        self.assertEqual(x['status'],'INCOMPLETE_PRE_96')
        self.assertEqual(sc,[])
        self.assertEqual(x['pre_bars_found'],95)
    def test_coverage_mismatch_aborts(self):
        candles=sample(110)
        del candles[BASE+55*d.TF]
        with self.assertRaisesRegex(ValueError,'coverage contradicts'):
            d.attempt_result(record(coverage=True),candles)
    def test_cannot_read_unexpected_post_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            r=record();r.update(pre_96_coverage='True',post_outcome='FAILED_BREAKOUT',v26_status='POST')
            p=Path(temp)/'pre_cohort.csv';m=p.with_name('manifest.json')
            with p.open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=list(r));w.writeheader();w.writerow(r)
            m.write_text(json.dumps({'version':'HLL_V282_PRE_COHORT','mode':'OFFLINE_READ_ONLY',
                          'symbols':1,'original_attempts':1}))
            rows,_=d.read_cohort(p,m,enforce_290=False)
            self.assertNotIn('post_outcome',rows[0]);self.assertNotIn('v26_status',rows[0])
            self.assertEqual(len(rows),1)
    def test_duplicate_id_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            r=record();r.update(pre_96_coverage='True')
            p=Path(temp)/'pre_cohort.csv';m=p.with_name('manifest.json')
            with p.open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=list(r));w.writeheader();w.writerow(r);w.writerow(r)
            m.write_text(json.dumps({'version':'HLL_V282_PRE_COHORT','mode':'OFFLINE_READ_ONLY',
                          'symbols':1,'original_attempts':2}))
            with self.assertRaisesRegex(ValueError,'duplicado'):
                d.read_cohort(p,m,enforce_290=False)
    def test_no_overwrite_and_all_attempts_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);can=root/'candles';can.mkdir()
            vals=sample(110)
            with (can/'TESTUSDT_30m.csv').open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=['timestamp','open','high','low','close']);w.writeheader()
                for b in vals.values():w.writerow({'timestamp':b['open_ts'],**{k:b[k] for k in ('open','high','low','close')}})
            r=record(); r['pre_96_coverage']=True
            cohort=root/'src.csv';cohort.write_text('test')
            output=root/'generated'
            m=d.run([r],cohort,{},can,output)
            self.assertEqual(m['attempts'],1)
            self.assertEqual(m['scale_rows'],3)
            with self.assertRaises(FileExistsError):
                d.run([r],cohort,{},can,output)
            self.assertEqual(len((output/'attempts_pre.csv').read_text().splitlines()),2)
    def test_plan_is_read_only(self):
        r=record()
        with tempfile.TemporaryDirectory() as temp:
            report=d.planned([r],Path(temp))
            self.assertEqual(report['missing_candle_symbols_count'],1)
            self.assertTrue(report['sample_is_conditioned_on_known_cross_up'])

if __name__=='__main__':unittest.main()
