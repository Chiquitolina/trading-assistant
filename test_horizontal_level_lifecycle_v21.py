import unittest
from pathlib import Path
import sys
import numpy as np
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parent))
from horizontal_breakout_lab import TF
from horizontal_level_lifecycle_v21 import Config, scan_symbol, _overlap_ratio

BASE=(1790000000000//TF)*TF


def bars(n=116,origin='HIGH',third=False,test_kind=None,outcome=None,gap=False):
    price=99. if origin=='HIGH' else 101.
    prices=np.full(n,price)
    opens=prices.copy()
    highs=np.full(n,price+0.2)
    lows=np.full(n,price-0.2)
    for j in ((40,60,80) if third else (40,60)):
        if j < n:
            if origin=='HIGH': highs[j]=101.
            else: lows[j]=99.
    if test_kind is not None and 74 < n:
        # Wicks first reach frozen zone from outside; signal known at 30m CLOSE.
        if origin=='HIGH': highs[74]=101.10; prices[74]=100.15
        else: lows[74]=98.90; prices[74]=99.85
    if outcome and 75 < n:
        if origin=='HIGH':
            prices[75]=98.5 if outcome=='defense' else 102.0
        else:
            prices[75]=101.5 if outcome=='defense' else 98.0
    highs=np.maximum(highs,np.maximum(opens,prices))
    lows=np.minimum(lows,np.minimum(opens,prices))
    times=BASE+np.arange(n,dtype=np.int64)*TF
    if gap:times[90:]+=TF
    return pd.DataFrame(dict(timestamp=times,open=opens,high=highs,
        low=lows,close=prices,volume=np.full(n,100.),quoteVolume=np.full(n,10000.)))


class LevelLifecycleV21(unittest.TestCase):
    def test_overlaps_by_zone_not_center(self):
        self.assertEqual(_overlap_ratio(0,2,1,3),.5)
        self.assertEqual(_overlap_ratio(0,2,5,7),0)
        self.assertAlmostEqual(_overlap_ratio(0,10,1,2),1.)

    def test_suppress_duplicate_at_new_pivot(self):
        f,events,ends,report=scan_symbol('DEMO',bars(third=True))
        highs=[x for x in f if x['origin']=='HIGH']
        self.assertEqual(len(highs),1)
        self.assertGreaterEqual(report['suppressed_overlaps'],1)
        self.assertTrue(any(x['event_type']=='LEVEL_RECONFIRMED' for x in events))

    def test_tests_are_independent_and_later_defended(self):
        f,e,ends,r=scan_symbol('DEMO',bars(test_kind='test',outcome='defense'))
        ts=[x for x in e if x['event_type']=='TEST_FROM_BELOW' and x['origin']=='HIGH']
        defense=[x for x in e if x['event_type']=='DEFENSE_CONFIRMED' and x['origin']=='HIGH']
        self.assertEqual(len(ts),1)
        self.assertEqual(len(defense),1)
        self.assertGreater(defense[0]['observed_closed_ts'],ts[0]['observed_closed_ts'])
        self.assertEqual(defense[0]['test_id'],ts[0]['test_id'])
        self.assertEqual(r['visits'],1)

    def test_loss_not_defense_and_side_symmetry(self):
        for origin,kind in [('HIGH','TEST_FROM_BELOW'),('LOW','TEST_FROM_ABOVE')]:
            _,e,_,r=scan_symbol('DEMO',bars(origin=origin,test_kind='test',outcome='loss'))
            ts=[x for x in e if x['event_type']==kind and x['origin']==origin]
            loss=[x for x in e if x['event_type']=='LOSS_CONFIRMED' and x['origin']==origin]
            self.assertEqual(len(ts),1)
            self.assertEqual(len(loss),1)
            self.assertEqual(r['defenses'],0)

    def test_prefix_invariance_no_future_repainting(self):
        b=bars(n=118,test_kind='test',outcome='defense',third=True)
        earlyf,earlyev,_,_=scan_symbol('DEMO',b.iloc[:95])
        b2=b.copy()
        b2.loc[99:,'open']*=1.03
        b2.loc[99:,'high']*=1.05
        b2.loc[99:,'low']*=1.02
        b2.loc[99:,'close']*=1.03
        fullf,fullev,_,_=scan_symbol('DEMO',b2)
        self.assertEqual(earlyf,[x for x in fullf if x['formation_closed_ts']<=BASE+95*TF])
        self.assertEqual(earlyev,[x for x in fullev if x['observed_closed_ts']<=BASE+95*TF])

    def test_gap_not_spanned(self):
        f,e,ends,r=scan_symbol('DEMO',bars(gap=True))
        self.assertEqual(r['gaps30'],1)
        self.assertTrue(any(x['end_state']=='GAP_CENSORED' for x in ends))
        self.assertFalse(any(x['formation_closed_ts']>BASE+90*TF for x in f))

    def test_no_late_result_at_formation(self):
        form,events,ends,report=scan_symbol('DEMO',bars(test_kind='test',outcome='defense'))
        first=min(x['formation_closed_ts'] for x in form)
        self.assertTrue(all(x['observed_closed_ts']>first for x in events))
        self.assertEqual(report['status'],'COMPLETE')

    def test_wide_zone_is_quality_flag_not_selected_by_pnl(self):
        f,_,_,r=scan_symbol('DEMO',bars(),Config(wide_zone_pct=.001))
        self.assertTrue(f)
        self.assertTrue(all(x['quality_flag']=='WIDE_ZONE_REVIEW' for x in f))
        self.assertEqual(r['wide_zones'],len(f))

if __name__=='__main__':unittest.main()
