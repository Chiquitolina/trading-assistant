"""Causal and lifecycle tests for V1.2. No external network / LIVE imports."""
import copy
import unittest
import pandas as pd

from horizontal_breakout_lifecycle_v12 import (LifecycleConfig, scan_symbol, strength_on_close)
from test_horizontal_breakout_lab import sample_series, minutes


class LifecycleV12Tests(unittest.TestCase):
    def setUp(self):
        self.cfg=LifecycleConfig()
        self.thirty=sample_series(110)
        self.thirty.loc[63,'volume']=600
        self.minute=minutes()

    def scan(self, bars=None, one=None):
        lv,att,out,te,d=scan_symbol('TESTUSDT',bars if bars is not None else self.thirty,
                           one if one is not None else self.minute,self.cfg)
        return lv,att,out,te,d

    def test_group_one_level_successful_cross(self):
        levels,attempts,outcomes,tests,diagnostic=self.scan()
        self.assertEqual(len(levels),1)
        self.assertEqual(len(attempts),1)
        self.assertEqual(levels[0]['level_id'],attempts[0]['level_id'])
        self.assertEqual(levels[0]['end_state'],'ACCEPTED_UP_AFTER_STRONG')
        self.assertGreater(attempts[0]['signal_closed_ts'],levels[0]['formation_closed_ts'])
        self.assertEqual(outcomes[0]['entry_ts'],attempts[0]['signal_closed_ts'])

    def test_strong_requires_all_pre_factors(self):
        _,attempts,_,_,_=self.scan()
        a=attempts[0]
        self.assertEqual(a['strength_class'],'STRONG_BREAKOUT')
        self.assertGreaterEqual(a['volume_ratio20'],self.cfg.min_volume_ratio)
        self.assertGreaterEqual(a['body_atr'],self.cfg.min_body_atr)
        self.assertGreaterEqual(a['close_position'],self.cfg.min_close_position)
        self.assertTrue(a['zone_cleared'])
        self.assertTrue(a['new_high_lookback'])
        self.assertEqual(a['strength_checks_passed'],5)

    def test_weak_without_volume_still_remains_attempt(self):
        bars=self.thirty.copy()
        bars.loc[63,'volume']=105
        _,attempts,_,_,_=self.scan(bars)
        self.assertEqual(len(attempts),1)
        self.assertEqual(attempts[0]['strength_class'],'BREAKOUT_ATTEMPT')
        self.assertLess(attempts[0]['volume_ratio20'],2)

    def test_same_level_records_failed_and_strong_later(self):
        bars=self.thirty.copy()
        bars[['open','high','low','close','volume']]=bars[['open','high','low','close','volume']].astype(float)
        bars.loc[63,['open','high','low','close','volume']]=[100,103,99,102.4,110]
        bars.loc[64,['open','high','low','close','volume']]=[102.4,102.5,99,100,105]
        bars.loc[65,['open','high','low','close','volume']]=[100,106,99,105,700]
        levels,attempts,_,_,_=self.scan(bars)
        self.assertEqual(len(attempts),2)
        self.assertEqual(len(set(a['level_id'] for a in attempts)),1)
        self.assertEqual([a['attempt_number'] for a in attempts],[1,2])
        self.assertEqual(attempts[0]['strength_class'],'BREAKOUT_ATTEMPT')
        self.assertEqual(attempts[1]['strength_class'],'STRONG_BREAKOUT')

    def test_post_changes_do_not_modify_first_strength(self):
        _,a,_,_,_=self.scan()
        bars=self.thirty.copy()
        # Change only candles after the first signal; POST may change,
        # the first candle strength and frozen level cannot.
        bars.loc[64:,'open']=100
        bars.loc[64:,'close']=100
        bars.loc[64:,'high']=101
        bars.loc[64:,'low']=98
        _,b,_,_,_=self.scan(bars)
        cols=['level_id','signal_closed_ts','volume_ratio20','body_atr',
              'close_position','strength_class','strength_checks_passed']
        self.assertEqual({k:a[0][k] for k in cols},{k:b[0][k] for k in cols})
        self.assertNotEqual(a[0]['post_label'],b[0]['post_label'])

    def test_broken_htf_history_not_counted_as_no_hit(self):
        bars=self.thirty.drop(index=[64]).reset_index(drop=True)
        levels,attempts,_,_,_=self.scan(bars)
        self.assertEqual(attempts[0]['post_label'],'POST_CENSORED')
        self.assertTrue(any(x['end_state']=='GAP_30M' for x in levels))

    def test_missing_1m_fill_is_censored(self):
        levels,attempts,outcomes,_,_=self.scan(one=self.minute.iloc[:100])
        self.assertEqual(len(attempts),1)
        self.assertEqual(outcomes[0]['path_state'],'NO_FILL_1M')
        self.assertEqual(outcomes[0]['tp2_h360'],'CENSORED')

    def test_without_pre_20_volume_cannot_claim_strong(self):
        level=dict(atr_pre=3.,level=102.,zone_high=102.5)
        bars=self.thirty.iloc[50:64].reset_index(drop=True)
        r=strength_on_close(bars,len(bars)-1,level,self.cfg)
        self.assertEqual(r['strength_class'],'UNKNOWN_STRENGTH')
        self.assertIsNone(r['volume_ratio20'])

    def test_weak_holds_do_not_retire_level_before_later_strong(self):
        bars=self.thirty.copy()
        bars[['open','high','low','close','volume']]=bars[['open','high','low','close','volume']].astype(float)
        # Two modest closes above the ceiling: still the same ceiling.
        for index in (63,64):
            bars.loc[index,['open','high','low','close','volume']]=[102.1,103,101.8,102.4,100]
        # A new local breakout follows a few 30m bars later.
        bars.loc[65,['open','high','low','close','volume']]=[102.4,106,102.0,105,650]
        levels,attempts,_,_,_=self.scan(bars)
        self.assertEqual(len(set(a['level_id'] for a in attempts)),1)
        self.assertTrue(any(a['strength_class']=='STRONG_BREAKOUT' for a in attempts))
        self.assertTrue(any(a['trigger_kind']=='EXPANSION_CONTINUATION' for a in attempts))

    def test_level_freeze_precedes_all_attempts(self):
        levels,attempts,_,_,_=self.scan()
        for x in attempts:
            level=next(l for l in levels if l['level_id']==x['level_id'])
            self.assertLess(level['formation_closed_ts'],x['signal_closed_ts'])

    def test_level_tests_keep_same_level_id(self):
        levels,attempts,outcomes,tests,_=self.scan()
        self.assertTrue(all(t['level_id']==levels[0]['level_id'] for t in tests))
        self.assertTrue(all(t['event_type']=='LEVEL_TEST' for t in tests))
        self.assertEqual(levels[0]['level_tests_post'],len(tests))

    def test_no_duplicate_attacks_while_above(self):
        bars=self.thirty.copy()
        bars.loc[64,'open']=104
        bars.loc[64,'high']=106
        bars.loc[64,'low']=103
        bars.loc[64,'close']=105
        bars.loc[64,'volume']=600
        _,attempts,_,_,_=self.scan(bars)
        self.assertEqual(len(attempts),1)


if __name__=='__main__': unittest.main()
