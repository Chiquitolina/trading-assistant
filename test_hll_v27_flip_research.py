import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parent))
from hll_v27_flip_research import (_clock_candidates,structural_stops,coverage_and_path,
                                   simulate_path,analyze_symbol,_summarize,TF,M)


def candle_series(t0,n):
    return pd.DataFrame({'timestamp':[t0+i*M for i in range(n)],
        'open':[100.0]*n,'high':[100.25]*n,'low':[99.75]*n,'close':[100.0]*n})

class V27Tests(unittest.TestCase):
    def setUp(self):
        self.known=10*TF
        self.minute=candle_series(self.known+M,60)
        self.attempt=pd.DataFrame([dict(attempt_id='X',level_id='L',symbol='A',
            known_closed_ts=3*TF,zone_low=90.,zone_high=95.,atr_pre=2.,
            prior_independent_touches=2,prior_confirmed_rejections=1,
            prior_failed_breakouts=0,pressure_build_up=1,volume_ratio_pre20=1.5,
            next_resistance_distance_atr=2.0)])
        self.outcome=pd.DataFrame([dict(attempt_id='X',status='FLIP_SUSTAINED_4B',
            retest_closed_ts=6*TF,defense_closed_ts=8*TF,
            flip_sustained_closed_ts=12*TF)])
    def test_post_clocks_known_later_than_break(self):
        c=_clock_candidates(self.attempt,self.outcome)
        self.assertEqual(len(c),2)
        self.assertEqual(c.known_closed_ts.tolist(),[8*TF,12*TF])
        self.assertTrue(c.entry_ts.eq(c.known_closed_ts+M).all())
    def test_confirmed_later_lost_still_eligible_early(self):
        self.outcome.loc[0,'status']='FLIP_CONFIRMED_LATER_LOST'
        self.outcome.loc[0,'flip_sustained_closed_ts']=np.nan
        c=_clock_candidates(self.attempt,self.outcome)
        self.assertEqual(c.entry_type.tolist(),['FLIP_CONFIRMED'])
    def test_no_flip_no_signal(self):
        self.outcome.loc[0,'status']='RETEST_FAILED'
        self.outcome.loc[0,'defense_closed_ts']=np.nan
        self.outcome.loc[0,'flip_sustained_closed_ts']=np.nan
        self.assertTrue(_clock_candidates(self.attempt,self.outcome).empty)
    def test_wrong_sustained_clock_fails(self):
        self.outcome.loc[0,'flip_sustained_closed_ts']=11*TF
        with self.assertRaises(ValueError):_clock_candidates(self.attempt,self.outcome)
    def test_minute_entry_delayed_one_minute(self):
        self.assertEqual(coverage_and_path(self.minute,self.known,60)[0],'COMPLETE_1M')
        self.assertEqual(coverage_and_path(self.minute,self.known,61)[0],'CENSORED_1M_TAIL')
        self.assertEqual(coverage_and_path(self.minute,self.known-M,1)[0],'NO_ENTRY_1M')
    def test_missing_middle_bar_censored(self):
        d=self.minute.drop(index=[4]).reset_index(drop=True)
        self.assertEqual(coverage_and_path(d,self.known,30)[0],'GAP_1M')
    def test_tp_first(self):
        d=self.minute.iloc[:6].copy();d.loc[2,'high']=102
        x=simulate_path(d,1.0,98.0)
        self.assertEqual(x['outcome'],'TP_FIRST')
        self.assertEqual(x['bars_to_exit'],3)
        self.assertAlmostEqual(x['return_after_fee_pct'],.9)
    def test_sl_first(self):
        d=self.minute.iloc[:6].copy();d.loc[1,'low']=97
        x=simulate_path(d,2.0,98)
        self.assertEqual(x['outcome'],'SL_FIRST')
        self.assertAlmostEqual(x['return_after_fee_pct'],-2.1)
    def test_both_same_1m_sl_worst(self):
        d=self.minute.iloc[:6].copy();d.loc[1,'low']=97;d.loc[1,'high']=105
        x=simulate_path(d,2.0,98)
        self.assertEqual(x['outcome'],'BOTH_SAME_1M_SL_ASSUMED')
        self.assertAlmostEqual(x['return_after_fee_pct'],-2.1)
    def test_time_exit(self):
        x=simulate_path(self.minute.iloc[:4],3.0,97)
        self.assertEqual(x['outcome'],'TIME_EXIT')
        self.assertEqual(x['bars_to_exit'],4)
    def test_gap_through_stop_fills_at_worse_open(self):
        d=self.minute.iloc[:4].copy()
        d.loc[1,['open','high','low','close']]=[95,96,94,95]
        x=simulate_path(d,2,98)
        self.assertEqual(x['outcome'],'SL_FIRST')
        self.assertAlmostEqual(x['return_after_fee_pct'],-5.1)
    def test_gap_over_tp_caps_at_goal(self):
        d=self.minute.iloc[:4].copy()
        d.loc[1,['open','high','low','close']]=[105,106,104,105]
        x=simulate_path(d,2,98)
        self.assertEqual(x['outcome'],'TP_FIRST')
        self.assertAlmostEqual(x['return_after_fee_pct'],1.9)
    def test_stop_requires_below_entry(self):
        x=simulate_path(self.minute.iloc[:4],3.0,101)
        self.assertEqual(x['outcome'],'INVALID_STOP')
    def test_retest_stop_only_known_bars(self):
        t=4*TF
        bars=pd.DataFrame(dict(timestamp=[t+i*TF for i in range(9)],
            low=[95,92,94,91,96,94,99,98,1]))
        # retest 7*TF (bar indexed 2), entry 10*TF (bar indexed 5)
        event=SimpleNamespace(known_closed_ts=10*TF,retest_closed_ts=7*TF,
                              atr_pre=2.,zone_low=90.)
        zone,retest,state=structural_stops(event,bars)
        self.assertEqual(state,'OK');self.assertAlmostEqual(zone,89.8)
        self.assertAlmostEqual(retest,90.8) # minimum of bars close <= clock, from retest
        bars.loc[8,'low']=-100
        self.assertEqual(structural_stops(event,bars),(zone,retest,state))
    def test_gaps_30m_invalidate_stop(self):
        bars=pd.DataFrame(dict(timestamp=[6*TF,8*TF],low=[92,93]))
        row=SimpleNamespace(known_closed_ts=9*TF,retest_closed_ts=7*TF,
                            atr_pre=2.,zone_low=90.)
        self.assertEqual(structural_stops(row,bars)[2],'GAP_30M')
    def test_duplicate_summary_not_counted_twice(self):
        s=pd.DataFrame([dict(entry_type='FLIP_CONFIRMED',horizon_min=60,tp_pct=1,
                              stop_type='FIXED_2P0',attempt_id='X',symbol='A',
                              path_state='COMPLETE_1M',outcome='TP_FIRST',
                              return_after_fee_pct=.9,stop_risk_pct=2)])
        self.assertEqual(int(_summarize(s).iloc[0]['n']),1)

if __name__=='__main__':unittest.main()
