import unittest
import pandas as pd
from horizontal_level_retest_lab_v15 import find_defended_retest, describe, TF, M, RETEST_BARS

T = 1750000000000 // TF * TF

def bars(rows):
    return pd.DataFrame([dict(timestamp=T+i*TF,open=o,high=h,low=l,close=c,volume=1,quoteVolume=1)
                         for i,(o,h,l,c) in enumerate(rows)])

def signal(close=102.0):
    return dict(signal_closed_ts=T+TF,zone_high=101.0,zone_low=99.0)

class RetestResearchTests(unittest.TestCase):
    def test_confirmed_first_retest_after_breakout(self):
        b=bars([(100,103,99,102),(102,103,100.5,102.5)])
        trigger,state=find_defended_retest(signal(),b,T+10*TF)
        self.assertEqual(state,'CONFIRMED')
        self.assertEqual(trigger['signal_closed_ts'],T+2*TF)
        self.assertEqual(trigger['entry_delay_min'],30)

    def test_break_under_zone_not_eligible(self):
        b=bars([(100,101,99,100.5),(100.5,103,100,102)])
        trigger,state=find_defended_retest(signal(),b,T+10*TF)
        self.assertIsNone(trigger)
        self.assertEqual(state,'CROSS_NOT_ABOVE_ZONE')

    def test_touch_without_bullish_close_not_counted(self):
        b=bars([(100,103,99,102),(102,102.5,100.2,101.6),(101.6,104,100.1,103)])
        trigger,state=find_defended_retest(signal(),b,T+10*TF)
        self.assertIsNone(trigger)
        self.assertEqual(state,'TOUCH_NO_DEFENCE')

    def test_invalidated_before_retest(self):
        b=bars([(100,103,99,102),(101,101.5,95,98),(98,103,96,102)])
        trigger,state=find_defended_retest(signal(),b,T+10*TF)
        self.assertEqual(state,'INVALIDATED')
        self.assertIsNone(trigger)

    def test_no_gap_imputation(self):
        b=bars([(100,103,99,102),(102,104,102,103),(103,105,100,104)])
        b.loc[1,'timestamp']+=TF
        trigger,state=find_defended_retest(signal(),b,T+10*TF)
        self.assertIsNone(trigger)
        self.assertEqual(state,'GAP_30M')

    def test_requires_close_before_cutoff(self):
        b=bars([(100,103,99,102),(102,103,100.4,102.5)])
        trigger,state=find_defended_retest(signal(),b,T+2*TF)
        self.assertIsNone(trigger)
        self.assertEqual(state,'CUTOFF')

    def test_retest_window_expiration(self):
        b=bars([(100,103,99,102)]+[(103,106,102,104)]*(RETEST_BARS+1))
        trigger,state=find_defended_retest(signal(),b,T+20*TF)
        self.assertIsNone(trigger)
        self.assertEqual(state,'NO_RETEST_WITHIN_WINDOW')

    def test_immediately_bullish_above_level_without_touch_not_signal(self):
        b=bars([(100,103,99,102),(102,105,101.4,104)])
        trigger,state=find_defended_retest(signal(),b,T+10*TF)
        self.assertIsNone(trigger)
        self.assertEqual(state,'NO_RETEST_WITHIN_WINDOW')

    def test_no_rows_summary(self):
        self.assertEqual(describe(pd.DataFrame())['N'],0)

if __name__=='__main__': unittest.main()
