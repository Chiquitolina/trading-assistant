#!/usr/bin/env python3
"""V2.8 causal structural tests, no network or trading services."""
import json
import unittest
from types import SimpleNamespace

import numpy as np
import pandas as pd
from pandas.testing import assert_series_equal

import hll_v28_structure_quality as m


def candles(n=90):
    t=1_780_000_000_000
    t=(t//m.TF)*m.TF
    highs=[];lows=[];closes=[]
    for i in range(n):
        mid=100+.04*i
        # Emulate consolidation / repeated small pullbacks around a resistance
        v=mid+.1*np.sin(i*np.pi/3)
        closes.append(v);highs.append(v+.23);lows.append(v-.23)
    return pd.DataFrame(dict(timestamp=[t+i*m.TF for i in range(n)],
         open=closes,high=highs,low=lows,close=closes))


def episode(c,breakout_idx=60,signal_idx=72,retest_idx=69):
    attempt=SimpleNamespace(attempt_id='test1',symbol='AAAUSDT',level_id='lv',
        known_closed_ts=int(c.timestamp.iloc[breakout_idx])+m.TF,
        atr_pre=.8,zone_low=100,zone_high=101,
        prior_independent_touches=2,pressure_build_up=0,
        volume_ratio_pre20=1.2,next_resistance_distance_atr=2.1,
        breakout_body_atr=.7,breakout_close_beyond_high_atr=.4)
    entry=SimpleNamespace(entry_type='FLIP_CONFIRMED',
        known_closed_ts=int(c.timestamp.iloc[signal_idx])+m.TF,
        retest_closed_ts=int(c.timestamp.iloc[retest_idx])+m.TF,
        entry_ts=int(c.timestamp.iloc[signal_idx])+m.TF+60000,
        v26_status='FLIP_SUSTAINED_4B')
    return attempt,entry


class StructureQualityTests(unittest.TestCase):
    def setUp(self):
        self.c=candles()
        self.a,self.e=episode(self.c)

    def _make(self,c):
        piv=m.confirmed_pivot_lows(c.low.to_numpy(),c.timestamp.to_numpy())
        return m.structure_at_clock(c,piv,self.a,self.e)

    def test_no_future_changes_affect_signal_features(self):
        before=self._make(self.c)
        future=self.c.copy()
        future.loc[future.index>72,'low']*=.65
        future.loc[future.index>72,'high']*=1.45
        future.loc[future.index>72,'close']*=1.15
        future.loc[future.index>72,'open']*=1.15
        after=self._make(future)
        self.assertEqual(before,after)

    def test_signal_change_does_not_change_pre_features(self):
        before=self._make(self.c)
        changed=self.c.copy()
        changed.loc[60:72,'low']-=.10
        changed.loc[60:72,'high']+=.10
        changed.loc[60:72,'close']+=.10
        after=self._make(changed)
        pre=[k for k in before if k.startswith('pre')]
        for key in pre:
            x,y=before[key],after[key]
            self.assertTrue((pd.isna(x) and pd.isna(y)) or x==y,key)

    def test_not_yet_confirmed_low_is_excluded(self):
        c=candles(11)
        c['low']=9.0
        c.loc[7,'low']=5.0
        c.loc[5:6,'low']=9.0
        c.loc[8:10,'low']=8.0
        piv=m.confirmed_pivot_lows(c.low,c.timestamp)
        p=[p for p in piv if p[0]==7]
        self.assertEqual(len(p),1)
        self.assertEqual(p[0][1],int(c.timestamp.iloc[9])+m.TF)
        self.assertNotIn(p[0],m._pivots_known(piv,0,8,int(c.timestamp.iloc[8])+m.TF))
        self.assertIn(p[0],m._pivots_known(piv,0,9,int(c.timestamp.iloc[9])+m.TF))

    def test_rising_3_lows(self):
        c=candles(20)
        c['low']=10.
        c.loc[[4,9,14],'low']=[1.,2.,3.]
        piv=m.confirmed_pivot_lows(c.low,c.timestamp)
        f,_,ok=m._pivot_factors(c,piv,19,int(c.timestamp.iloc[19])+m.TF,.5,20,'pre24')
        self.assertTrue(ok)
        self.assertEqual(f['pre24_hl_last3'],1)
        self.assertAlmostEqual(f['pre24_low_progression_atr'],4.)

    def test_missing_context_is_unknown_not_false(self):
        c=candles(30)
        vals,_,ok=m._pivot_factors(c,[],20,int(c.timestamp.iloc[20])+m.TF,1,48,'pre48')
        self.assertFalse(ok)
        self.assertTrue(np.isnan(vals['pre48_hl_last3']))

    def test_gapped_pre_candles_are_unknown(self):
        bad=self.c.drop(index=[33]).reset_index(drop=True)
        # Signal still at the same timestamp, but its previous 24 candle
        # window now contains a hole.
        v=self._make(bad)
        self.assertEqual(v['pre24_coverage'],1) # missing candle is older than last 24
        bad2=self.c.drop(index=[57]).reset_index(drop=True)
        v=self._make(bad2)
        self.assertEqual(v['pre24_coverage'],0)
        self.assertTrue(np.isnan(v['pre24_close_center_crossings']))

    def test_missing_breakout_to_entry_bar_raises(self):
        bad=self.c.drop(index=[67]).reset_index(drop=True)
        with self.assertRaisesRegex(ValueError,'Gap'):
            self._make(bad)

    def test_retest_after_flip_raises(self):
        wrong=SimpleNamespace(**vars(self.e))
        wrong.retest_closed_ts=self.e.known_closed_ts+m.TF
        piv=m.confirmed_pivot_lows(self.c.low,self.c.timestamp)
        with self.assertRaisesRegex(ValueError,'Invalid retest'):
            m.structure_at_clock(self.c,piv,self.a,wrong)

    def test_zone_center_cross_counter(self):
        v=self._make(self.c)
        self.assertGreaterEqual(v['pre24_close_center_crossings'],0)
        self.assertGreaterEqual(v['breakout_to_signal_center_crossings'],0)

    def test_signal_pivots_are_all_confirmed(self):
        v=self._make(self.c)
        for p in json.loads(v['known_pivots_signal48_json']):
            self.assertLessEqual(p['confirmed_close_ts'],self.e.known_closed_ts)

    def test_feature_buckets_preserve_unknown(self):
        f=pd.DataFrame({'pre24_hl_last3':[np.nan,0,1],
           'breakout_to_signal_center_crossings':[np.nan,0,4]})
        self.assertEqual(list(m.factor_bucket(f,'pre24_hl_last3')),
                         ['UNKNOWN','NO','YES'])
        self.assertEqual(list(m.factor_bucket(f,'breakout_to_signal_center_crossings')),
                         ['UNKNOWN','0','3+'])

    def test_buckets_are_prespecified_not_fit_on_future_returns(self):
        f=pd.DataFrame({'breakout_volume_ratio':[0.5,1.6,3.0]})
        self.assertEqual(list(m.factor_bucket(f,'breakout_volume_ratio')),
                         ['<=1x','1-2x','>2x'])

    def test_matched_input_and_output_clocks(self):
        v=self._make(self.c)
        self.assertEqual(v['known_closed_ts'],self.e.known_closed_ts)
        self.assertEqual(v['breakout_closed_ts'],self.a.known_closed_ts)
        self.assertEqual(v['bars_breakout_to_signal'],12)
        self.assertEqual(v['bars_retest_to_signal'],3)

    def test_causal_structure_does_not_contain_profit_outcome(self):
        v=self._make(self.c)
        for forbidden in ['tp','sl','outcome','return_after_fee_pct','mfe_to_exit_pct']:
            self.assertNotIn(forbidden,v)

    def test_result_groups_separate_period_and_horizon(self):
        base=dict(symbol='AAAUSDT',entry_ts=1_780_000_000_000,
                  path_state='COMPLETE_1M',outcome='TP_FIRST',return_after_fee_pct=1.9)
        rows=[]
        for h in [60,180]:
            rows.append({**base,'attempt_id':f'id{h}','horizon_min':h,
                **{f:1.0 for f in m.FACTORS}})
        f,_=m.factor_tables(pd.DataFrame(rows))
        self.assertEqual(set(f.horizon_min),{60,180})


if __name__=='__main__':unittest.main()
