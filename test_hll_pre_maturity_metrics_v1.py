"""Tests for PRE compression metrics; no VPS data or live services needed."""
import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path

import hll_pre_maturity_metrics_v1 as m


def candles(n=288, pre=1800000000000, *, varying=True):
    assert pre % m.TF == 0
    result={}
    for i in range(n):
        amp=(0.30*(1-i/(n*1.2)) if varying else 0.15)
        center=12.0+0.3*i/n
        bar={'open_ts':pre-(n-i)*m.TF,
             'close_ts':pre-(n-i-1)*m.TF,
             'open':center,'close':center,
             'high':center+amp,'low':center-amp}
        result[bar['open_ts']]=bar
    return result


def example(pre=1800000000000, attempt_id='a', eid='e', first=1):
    return dict(attempt_id=attempt_id,symbol='TESTUSDT',level_id='LEVEL',
        pre_closed_ts=str(pre),zone_low='12.2',zone_high='12.35',atr_pre='0.5',
        status='COMPLETE_PRE',family_1p0='ASCENSO_ESCALONADO',
        scale_agreement='IGUAL_3_ATR',episode_id=eid,
        episode_ordinal=1 if first else 2,is_first_in_episode=int(first))


class MaturityMetricsTests(unittest.TestCase):
    def setUp(self):
        self.pre=1800000000000
        self.bars=candles(pre=self.pre)
        self.r=example(pre=self.pre)

    def test_contiguous_pre_only_and_future_invariant(self):
        x=m.attempt_metrics(self.r,self.bars)
        future=copy.deepcopy(self.bars)
        future[self.pre]=dict(self.bars[self.pre-m.TF],open_ts=self.pre,close_ts=self.pre+m.TF,
                              close=100,open=100,high=101,low=99)
        y=m.attempt_metrics(self.r,future)
        self.assertEqual(x,y)
        self.assertEqual(x['status_288'],'COMPLETE_PRE_288')
        self.assertEqual(x['pre_288_bars_found'],288)

    def test_window_compression_detects_shrinking_range(self):
        x=m.attempt_metrics(self.r,self.bars)
        self.assertLess(x['compression_24_recent_vs_early'],1)
        self.assertLess(x['compression_48_recent_vs_first'],1)
        self.assertGreaterEqual(x['trough_progress_steps_last3'],0)
        self.assertLessEqual(x['trough_progress_steps_last3'],2)
        self.assertTrue(0<=x['near_zone_share_48_half_atr']<=1)

    def test_flat_range_not_improvement(self):
        cs=candles(pre=self.pre,varying=False)
        x=m.attempt_metrics(self.r,cs)
        self.assertAlmostEqual(x['compression_24_recent_vs_early'],1,places=1)

    def test_incomplete_288_is_null_not_zero(self):
        cs={k:v for k,v in self.bars.items() if k>=self.pre-96*m.TF}
        x=m.attempt_metrics(self.r,cs)
        self.assertEqual(x['status_96'],'COMPLETE_PRE')
        self.assertEqual(x['status_288'],'INCOMPLETE_PRE_288')
        self.assertNotIn('near_zone_share_288_half_atr',x)
        self.assertNotIn('compression_72_recent_vs_first_288',x)

    def test_gap_in_96_rejected_when_discovery_says_complete(self):
        cs=self.bars.copy();del cs[self.pre-3*m.TF]
        with self.assertRaisesRegex(ValueError,'Cobertura real'):
            m.attempt_metrics(self.r,cs)

    def test_incomplete_96_preserves_attempt(self):
        r={**self.r,'status':'INCOMPLETE_PRE_96'}
        x=m.attempt_metrics(r,{})
        self.assertEqual(x['status_96'],'INCOMPLETE_PRE_96')
        self.assertNotIn('amplitude_24_recent_atr',x)

    def test_288_snapshot_must_end_in_same_96(self):
        full=m.bar_window(self.bars,self.pre,288)
        core=m.bar_window(self.bars,self.pre,96)
        core2=copy.deepcopy(core)
        core2[-1]['close']+=.01
        with self.assertRaisesRegex(ValueError,'no coincide'):
            m.metric_snapshot(core2,full,12.2,12.35,.5)

    def test_no_zero_ratio_division(self):
        self.assertIsNone(m.safe_ratio(1,0))

    def test_episode_first_export_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            candles_dir=root/'candles';candles_dir.mkdir()
            # CSV candle reader is mocked; tested above with real typed PRE bars.
            old=m.ohlc.load_candles
            m.ohlc.load_candles=lambda *a,**kw:(self.bars,None)
            try:
                (candles_dir/'TESTUSDT_30m.csv').write_text('data')
                out=root/'results'
                rows=[self.r,{**self.r,'attempt_id':'b','episode_ordinal':2,
                              'is_first_in_episode':0}]
                report=m.write_rows(out,rows,{}, {'heuristic_episodes_gap24_cap96':1},
                                    {'discovery_attempts':'hash1','episode_map':'hash2'},candles_dir)
                self.assertEqual(report['attempts'],2)
                self.assertEqual(report['episodes'],1)
                with (out/'episode_first_maturity_pre.csv').open() as f:
                    first=list(csv.DictReader(f))
                self.assertEqual(len(first),1)
                self.assertEqual(first[0]['attempt_id'],'a')
                self.assertEqual(first[0]['first_attempt_id'],'a')
                with self.assertRaises(FileExistsError):
                    m.write_rows(out,rows,{}, {},{},candles_dir)
            finally:
                m.ohlc.load_candles=old

    def test_label_irrelevant_to_pre_metrics(self):
        a=m.metric_snapshot(m.bar_window(self.bars,self.pre,96),
                            m.bar_window(self.bars,self.pre,288),12.2,12.35,.5)
        b=m.metric_snapshot(m.bar_window(self.bars,self.pre,96),
                            m.bar_window(self.bars,self.pre,288),12.2,12.35,.5)
        self.assertEqual(a,b)


if __name__=='__main__':unittest.main()
