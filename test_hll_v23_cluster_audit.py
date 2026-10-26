import unittest
import pandas as pd
from hll_v23_cluster_audit import check_and_dedup, differences, describe, day_bootstrap


def mk(event, sym='AAA', ts=100, level='L1', volume=2.5, outcome='TP_FIRST', net=1.9, day='2026-10-20', range_atr=1.0):
    return dict(stage='SUPPORT_CROSS_DOWN',symbol=sym,known_closed_ts=ts,level_id=level,
         event_id=event,entry_ts=ts+60000,entry_price=10., direction='SHORT',
         outcome=outcome,return_after_fee_pct=net,vol_group='GE_2' if volume>=2 else 'LT_2',
         day_utc=day,range_atr=range_atr)

class ClusterAuditTests(unittest.TestCase):
    def test_same_candle_deduplicates_without_reading_outcome(self):
        x=pd.DataFrame([mk('b',level='Z'),mk('a',level='A')])
        y=check_and_dedup(x)
        self.assertEqual(len(y),1)
        self.assertEqual(y.level_id.iloc[0],'A')
    def test_conflicting_same_candle_aborts(self):
        x=pd.DataFrame([mk('a'),mk('b',level='L2',outcome='SL_FIRST',net=-1.6)])
        with self.assertRaisesRegex(ValueError,'Same-candle different'):
            check_and_dedup(x)
    def test_only_control_comparisons_with_both_volumes(self):
        x=pd.DataFrame([mk('a'),mk('b',ts=200,volume=1.1,outcome='SL_FIRST',net=-1.6),
                        mk('c',ts=300,volume=1.1,outcome='TIME_EXIT',net=-0.1)])
        d=differences(x)
        self.assertAlmostEqual(d['d_TP_percentage_points'],100.)
        self.assertAlmostEqual(d['d_mean_net_pct'],2.75)
    def test_all_denominator_includes_timeouts(self):
        x=pd.DataFrame([mk('a'),mk('b',ts=200,outcome='TIME_EXIT',net=-0.1)])
        self.assertAlmostEqual(describe(x)['TP_all_pct'],50.)
    def test_bootstrap_by_day_returns_finite_interval(self):
        x=[]
        for i in range(6):
            d=f'2026-10-{i+1:02d}'
            x.extend([mk(f'H{i}',ts=i*1000+100,day=d),mk(f'L{i}',ts=i*1000+200,day=d,volume=1.)])
        o=day_bootstrap(pd.DataFrame(x),draws=40)
        self.assertEqual(o['days'],6)
        self.assertIsNotNone(o['tp_difference_pp'])

if __name__=='__main__': unittest.main()
