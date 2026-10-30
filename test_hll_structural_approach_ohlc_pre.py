#!/usr/bin/env python3
"""Unit tests for HLL OHLC PRE V2; no exchange/network access."""
import csv
import importlib.util
import pathlib
import tempfile
import unittest
from datetime import datetime, timezone

P=pathlib.Path(__file__).with_name('hll_structural_approach_ohlc_pre.py')
S=importlib.util.spec_from_file_location('ohlc',P)
m=importlib.util.module_from_spec(S);S.loader.exec_module(m)
BASE=m.parse_time('2026-09-18T07:00:00+00:00')

def bar(t, close=100, low=None, high=None, op=None):
    op=close if op is None else op
    low=min(op,close) - 0.1 if low is None else low
    high=max(op,close) + 0.1 if high is None else high
    return {'open_ts':t,'close_ts':t+m.BAR_MS,'open':op,'high':high,'low':low,'close':close}

def rows(n=96, pre=BASE):
    return {pre-n*m.BAR_MS+i*m.BAR_MS:bar(pre-n*m.BAR_MS+i*m.BAR_MS,100+i*0.1) for i in range(n)}

def summaries(**kwargs):
    return [dict(attempt_id='a',symbol='TESTUSDT',pre_closed_ts=str(BASE),threshold_atr='1.0',
                 latest_chain_h2_distance_zone_atr='-0.5', **kwargs)]

class OHLCPreTests(unittest.TestCase):
    def test_time_ms(self):self.assertEqual(m.parse_time(str(BASE)),BASE)
    def test_time_seconds(self):self.assertEqual(m.parse_time(str(BASE/1000)),BASE)
    def test_time_iso_utc(self):self.assertEqual(m.parse_time('2026-09-18 07:00:00'),BASE)
    def test_time_invalid(self):
        with self.assertRaises(ValueError):m.parse_time('hello')
    def test_time_unit_invalid(self):
        with self.assertRaises(ValueError):m.parse_time('300')
    def test_schema_explicit_open(self):
        schema=m.schema_for(['open_time','open','high','low','close','close_time'])
        self.assertEqual(schema['time_kind'],'open')
        self.assertEqual(schema['time_col'],'open_time')
    def test_schema_only_close(self):self.assertEqual(m.schema_for(['close_time','o','h','l','c'])['time_kind'],'close')
    def test_schema_ambiguous_fails_closed(self):
        with self.assertRaisesRegex(ValueError,'Ambiguous timestamp'):
            m.schema_for(['timestamp','open','high','low','close'])
    def test_schema_ambiguous_override(self):
        self.assertEqual(m.schema_for(['timestamp','open','high','low','close'],'open')['time_kind'],'open')
    def test_missing_ohlc_column_fails(self):
        with self.assertRaisesRegex(ValueError,'OHLC columns absent'):
            m.schema_for(['open_time','open','high','low'])
    def test_normalize_close_time_binance_minus_one_ms(self):
        schema=m.schema_for(['close_time','open','high','low','close'])
        c=m.decode_candle(dict(close_time=str(BASE-1),open='100',high='102',low='99',close='101'),schema)
        self.assertEqual(c['open_ts'],BASE-m.BAR_MS)
    def test_duplicate_timestamp(self):
        with tempfile.TemporaryDirectory() as d:
            p=pathlib.Path(d)/'sample.csv'
            p.write_text('open_time,open,high,low,close\n'+f'{BASE},100,102,99,101\n'*2)
            with self.assertRaisesRegex(ValueError,'Duplicate open_ts'):m.load_candles(p)
    def test_bad_ohlc_fails(self):
        s=m.schema_for(['open_time','open','high','low','close'])
        with self.assertRaisesRegex(ValueError,'Inconsistent OHLC'):
            m.decode_candle(dict(open_time=str(BASE),open='100',high='99',low='98',close='101'),s)
    def test_timestamp_alignment_fails(self):
        s=m.schema_for(['open_time','open','high','low','close'])
        with self.assertRaisesRegex(ValueError,'Non-aligned'):
            m.decode_candle(dict(open_time=str(BASE+1),open='100',high='101',low='99',close='100'),s)
    def test_complete_windows(self):
        result,overview=m.analyze(summaries(),{'TESTUSDT':rows()})
        self.assertEqual(len(result),4)
        self.assertTrue(all(r['status']=='COMPLETE_PRE' for r in result))
        self.assertEqual(result[0]['last_candle_close_ts'],BASE)
        self.assertEqual(overview[0]['status_96'],'COMPLETE_PRE')
    def test_missing_gap_is_not_filled(self):
        cs=rows();del cs[BASE-8*m.BAR_MS]
        result,_=m.analyze(summaries(),{'TESTUSDT':cs})
        self.assertEqual([r['status'] for r in result],['INSUFFICIENT_OR_GAPPED_OHLC']*4)
        self.assertEqual(result[0]['bars_found'],11)
        self.assertNotIn('close_path_efficiency',result[0])
    def test_future_candles_not_used(self):
        cs=rows();base=m.analyze(summaries(),{'TESTUSDT':cs})
        cs[BASE]=bar(BASE,close=999999)
        cs[BASE+m.BAR_MS]=bar(BASE+m.BAR_MS,close=1)
        self.assertEqual(base,m.analyze(summaries(),{'TESTUSDT':cs}))
    def test_current_forming_candle_excluded(self):
        cs=rows()
        cs[BASE-m.BAR_MS]=bar(BASE-m.BAR_MS,close=120)
        base=m.analyze(summaries(),{'TESTUSDT':cs})
        cs[BASE]=bar(BASE,close=0.001)
        self.assertEqual(base,m.analyze(summaries(),{'TESTUSDT':cs}))
    def test_no_labels_change_result(self):
        c=rows()
        a=m.analyze(summaries(),{'TESTUSDT':c})
        b=m.analyze([dict(summaries()[0],visual_label='BAD')],{'TESTUSDT':c})
        self.assertEqual(a,b)
    def test_input_order_invariant(self):
        cs=rows()
        one=summaries()+[dict(summaries()[0],threshold_atr='0.5')]
        self.assertEqual(m.analyze(one,{'TESTUSDT':cs}),m.analyze(list(reversed(one)),{'TESTUSDT':cs}))
    def test_duplicate_attempt_with_different_pre(self):
        with self.assertRaisesRegex(ValueError,'Inconsistent attempt'):
            m.get_attempts(summaries()+[dict(summaries()[0],pre_closed_ts=str(BASE-m.BAR_MS))])
    def test_no_ohlc_symbol(self):
        out,_=m.analyze(summaries(),{})
        self.assertEqual(out[0]['status'],'MISSING_SYMBOL')
    def test_close_path_efficiency_formula(self):
        pre=BASE
        closes=[100,102,101,103]
        candles=[bar(pre-(4-i)*m.BAR_MS,close=v) for i,v in enumerate(closes)]
        v=m.compute_metrics(candles)
        self.assertAlmostEqual(v['close_path_efficiency'],3/5)
        self.assertAlmostEqual(v['signed_upward_efficiency'],3/5)
        self.assertAlmostEqual(v['positive_close_steps_share'],2/3)
    def test_overlap_100_percent_for_identical_ranges(self):
        cs=[bar(BASE-i*m.BAR_MS,100,low=99,high=101) for i in (3,2,1)]
        self.assertAlmostEqual(m.compute_metrics(cs)['consecutive_range_overlap_mean'],1)
    def test_unique_level_not_triplicated_by_scales(self):
        base=summaries()
        r,_=m.analyze(base+[dict(base[0],threshold_atr='0.5'),dict(base[0],threshold_atr='1.5')],{'TESTUSDT':rows()})
        self.assertEqual(len(r),4)
    def test_writes_columns_and_no_lookahead(self):
        with tempfile.TemporaryDirectory() as d:
            p=pathlib.Path(d)/'out.csv';rs,_=m.analyze(summaries(),{'TESTUSDT':rows()})
            m.write_csv(p,rs,m.OUT_FIELDS)
            with p.open() as f: r=list(csv.DictReader(f))
            self.assertEqual(len(r),4)
            self.assertEqual(int(r[-1]['last_candle_close_ts']),BASE)
    def test_known_real_summary_metadata(self):
        p=pathlib.Path('/mnt/data/hll_approach_result/approach_summary_pre.csv')
        if p.is_file():
            a,c=m.get_attempts(m.read_csv(p))
            self.assertEqual(len(a),50)
            self.assertEqual(len(c),50)

if __name__=='__main__':unittest.main()
