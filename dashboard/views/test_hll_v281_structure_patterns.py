#!/usr/bin/env python3
"""V2.8.1 regression: deterministic morphology + causal timestamp rules."""
import unittest
import json
from types import SimpleNamespace
import numpy as np
import pandas as pd
import hll_v281_structure_patterns as m


def candles(n=180,trend=.015):
    base=1_780_000_000_000
    base=base//m.TF*m.TF
    x=np.arange(n)
    cl=100+x*trend+.15*np.sin(x*2*np.pi/8)
    return pd.DataFrame(dict(timestamp=base+x*m.TF,open=cl,
        high=cl+.15, low=cl-.15,close=cl))


def f(**kwargs):
    d=dict(coverage=True,window_bars=48,pivots=4,last3_rising=0,last3_falling=0,
       zone_center_crosses=0,full_zone_crosses=0,zone_overlap_bars=0,
       directional_efficiency=.3,net_move_atr=.2)
    d.update(kwargs)
    return d


class TaxonomyTests(unittest.TestCase):
    def test_higher_lows_are_not_enough(self):
        self.assertEqual(m.classify_structure(f(last3_rising=1,zone_center_crosses=4)),
                         'ASCENDING_CHOPPY')
        self.assertEqual(m.classify_structure(f(last3_rising=1,full_zone_crosses=1)),
                         'ASCENDING_CHOPPY')
        self.assertEqual(m.classify_structure(f(last3_rising=1,zone_center_crosses=1)),
                         'ASCENDING_CLEAN')

    def test_shape_categories(self):
        self.assertEqual(m.classify_structure(f(zone_center_crosses=5)), 'MULTI_CROSS_RANGE')
        self.assertEqual(m.classify_structure(f(zone_overlap_bars=14)), 'HORIZONTAL_BASE')
        self.assertEqual(m.classify_structure(f(pivots=1,net_move_atr=1.3,
                    directional_efficiency=.75,zone_overlap_bars=1)), 'IMPULSIVE_APPROACH')
        self.assertEqual(m.classify_structure(f(last3_falling=1)), 'DESCENDING_LOW_SEQUENCE')
        self.assertEqual(m.classify_structure(f(coverage=False)), 'UNKNOWN')

    def test_full_zone_cross_is_not_center_chop(self):
        c=candles(60,trend=0)
        c.loc[28:51,'close']=[100.5+(.02 if i%2 else -.02) for i in range(24)]
        v=m.window_features(c,m.pivot_lows_known(c),int(c.timestamp.iloc[51])+m.TF,
                 24,100.45,100.55,.25)
        self.assertGreater(v['zone_center_crosses'],0)
        self.assertEqual(v['full_zone_crosses'],0)

    def test_strict_2x2_pivot_clock(self):
        c=candles(50)
        c.loc[35,'low']=10.0
        p=m.pivot_lows_known(c)
        pivot=next(x for x in p if x[0]==35)
        self.assertEqual(pivot[1],int(c.timestamp.iloc[37])+m.TF)
        before=m.window_features(c,p,int(c.timestamp.iloc[36])+m.TF,24,99,101,.5)
        after=m.window_features(c,p,int(c.timestamp.iloc[37])+m.TF,24,99,101,.5)
        self.assertNotIn(35,[x['open_ts'] for x in json.loads(before['pivot_lows_json'])])
        self.assertIn(35,[int((x['open_ts']-c.timestamp.iloc[0])//m.TF) for x in json.loads(after['pivot_lows_json'])])

    def test_future_changes_cannot_change_features(self):
        c=candles()
        cutoff=int(c.timestamp.iloc[95])+m.TF
        first=m.window_features(c,m.pivot_lows_known(c),cutoff,48,100,102,.5)
        changed=c.copy()
        changed.loc[100:,'low']*=.5
        changed.loc[100:,'high']*=1.5
        changed.loc[100:,'close']*=1.2
        second=m.window_features(changed,m.pivot_lows_known(changed),cutoff,48,100,102,.5)
        self.assertEqual(first,second)

    def test_gap_is_unknown_not_absent(self):
        c=candles()
        c=c.drop(index=90).reset_index(drop=True)
        f=m.window_features(c,m.pivot_lows_known(c),int(c.timestamp.iloc[100])+m.TF,
                            24,100,102,.5)
        self.assertFalse(f['coverage'])
        self.assertEqual(m.classify_structure(f),'UNKNOWN')

    def test_missing_96_not_assumed_empty(self):
        c=candles(90)
        f=m.window_features(c,m.pivot_lows_known(c),int(c.timestamp.iloc[85])+m.TF,
                            96,100,102,.5)
        self.assertFalse(f['coverage'])
        self.assertTrue(np.isnan(f['pivots']))

    def test_phase_pre_clock_excludes_breakout(self):
        c=candles()
        cut=int(c.timestamp.iloc[90])+m.TF
        pre=m.window_features(c,m.pivot_lows_known(c),cut-m.TF,24,100,102,.5)
        changed=c.copy();changed.loc[90,'close']=110.;changed.loc[90,'high']=110.
        pre2=m.window_features(changed,m.pivot_lows_known(changed),cut-m.TF,24,100,102,.5)
        self.assertEqual(pre,pre2)

    def test_defense_separate_from_pre_structure(self):
        normal=dict(retest_low_below_zone_high_atr=.2,signal_distance_from_zone_low_atr=1.,
          retest_to_signal_closes_inside_zone=0,retest_to_signal_closes_below_zone=0,
          retest_to_signal_zone_overlaps=1)
        self.assertEqual(m.classify_defense(normal),'CLEAN_DEFENSE')
        self.assertEqual(m.classify_defense({**normal,'retest_to_signal_closes_inside_zone':3}),
                         'CHOPPY_DEFENSE')
        self.assertEqual(m.classify_defense({**normal,'retest_low_below_zone_high_atr':.7}),
                         'DEEP_RETEST')

    def test_outcomes_joined_only_after_structure(self):
        tax=pd.DataFrame([dict(attempt_id='id1',symbol='AAA',entry_type='FLIP_CONFIRMED',
            **{f'{phase}_pattern_{w}':'ASCENDING_CLEAN' for phase in ('pre','flip') for w in m.WINDOWS},
            defense_pattern='CLEAN_DEFENSE')])
        clock=1_780_000_000_000
        evaluation=pd.DataFrame([dict(attempt_id='id1',symbol='AAA',entry_type='FLIP_CONFIRMED',
          entry_ts=clock, horizon_min=180,path_state='COMPLETE_1M',outcome='TP_FIRST',
          return_after_fee_pct=1.9)])
        result=m.summarize(tax,evaluation)
        self.assertEqual(len(result),7)
        self.assertTrue((result.mean_net_pct==1.9).all())
        self.assertTrue((result.n==1).all())

    def test_no_pnl_used_in_taxonomy(self):
        a=f(last3_rising=1,return_after_fee_pct=-1)
        b=f(last3_rising=1,return_after_fee_pct=+2)
        self.assertEqual(m.classify_structure(a),m.classify_structure(b))

if __name__=='__main__':unittest.main()
