"""Run python3 -m unittest -v dashboard.views.test_hll_v282_pre_cohort from project root."""
import unittest
from pathlib import Path
import tempfile

import numpy as np
import pandas as pd

try:
    from dashboard.views.hll_v282_pre_cohort import (
        join_attempts,create_cohort,outcome_class,parse_ts,TF,
    )
    from dashboard.views.hll_v282_pre_followup_view import filtered,load_cohort,make_chart
except ModuleNotFoundError:
    from hll_v282_pre_cohort import (
        join_attempts,create_cohort,outcome_class,parse_ts,TF,
    )
    from hll_v282_pre_followup_view import filtered,load_cohort,make_chart


class V282PreCohortTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.dir=self.root/'candles'
        self.dir.mkdir()
        t=1_780_000_000_000
        self.start=t
        # 160 30m candles, deterministic synthetic series
        k=np.arange(160)
        lows=95+0.01*k+np.sin(k*1.3)*0.4
        d=pd.DataFrame(dict(timestamp=t+k*TF,open=lows+0.30,
                            low=lows, high=lows+1.4,close=lows+0.7))
        d.to_csv(self.dir/'TESTUSDT_30m.csv',index=False)
        self.attempt=int(t+120*TF)  # breakout closes at 120th 30m position
        self.flip=self.attempt+5*TF
        self.attempts=pd.DataFrame([
            dict(attempt_id='a',symbol='TESTUSDT',known_closed_ts=self.attempt,
                 zone_low=96.,zone_high=96.5,atr_pre=1.,level_id='L1'),
            dict(attempt_id='b',symbol='TESTUSDT',known_closed_ts=self.attempt+TF,
                 zone_low=96.,zone_high=96.5,atr_pre=1.,level_id='L2'),
            dict(attempt_id='c',symbol='TESTUSDT',known_closed_ts=self.attempt+2*TF,
                 zone_low=96.,zone_high=96.5,atr_pre=1.,level_id='L3'),
        ])
        self.outcomes=pd.DataFrame([
            dict(attempt_id='a',status='FLIP_SUSTAINED_4B',
                 defense_closed_ts=self.flip,retest_closed_ts=self.attempt+2*TF,failed_closed_ts=np.nan),
            dict(attempt_id='b',status='RETEST_FAILED',
                 defense_closed_ts=np.nan,retest_closed_ts=self.attempt+2*TF,
                 failed_closed_ts=self.attempt+3*TF),
            dict(attempt_id='c',status='CENSORED_RETEST',
                 defense_closed_ts=np.nan,retest_closed_ts=self.attempt+3*TF,
                 failed_closed_ts=np.nan),
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def make(self):
        return create_cohort(join_attempts(self.attempts,self.outcomes),self.dir)

    def test_three_original_attempts_do_not_disappear(self):
        x=self.make()
        self.assertEqual(set(x.attempt_id),{'a','b','c'})
        self.assertEqual(x.post_outcome.tolist(),['FLIP_CONFIRMADO','SIN_FLIP_EN_VENTANA','INDETERMINADO'])
        self.assertEqual(len(filtered(x,48)),3)
        self.assertEqual(len(filtered(x,48,state='FLIP_CONFIRMADO')),1)
        self.assertEqual(len(filtered(x,48,state='INDETERMINADO')),1)
        self.assertEqual(int(x.iloc[0].flip_confirmed_closed_ts),self.flip)
        self.assertEqual(int(x.iloc[0].pre_closed_ts),self.attempt-TF)

    def test_frozen_pre_geometry_unaffected_by_future(self):
        one=self.make()
        c=pd.read_csv(self.dir/'TESTUSDT_30m.csv')
        mask=c.timestamp>=self.attempt
        c.loc[mask,'open']+=3
        c.loc[mask,'close']+=3
        c.loc[mask,'low']+=3
        c.loc[mask,'high']+=3
        c.to_csv(self.dir/'TESTUSDT_30m.csv',index=False)
        two=self.make()
        for f in ['pre_pattern_24','pre_pattern_48','pre_pattern_96','pre48_pivot_lows_json']:
            self.assertEqual(one.loc[0,f],two.loc[0,f])

    def test_missing_and_duplicate_ids_fail(self):
        with self.assertRaisesRegex(ValueError,'Duplicate'):
            join_attempts(pd.concat([self.attempts,self.attempts.iloc[:1]]),self.outcomes)
        with self.assertRaisesRegex(ValueError,'incomplete'):
            join_attempts(self.attempts,self.outcomes.iloc[:2])

    def test_outcome_states_are_not_synthetic_failures(self):
        self.assertEqual(outcome_class('FLIP_CONFIRMED_LATER_LOST',self.flip),'FLIP_CONFIRMADO')
        self.assertEqual(outcome_class('RETEST_FAILED',None),'SIN_FLIP_EN_VENTANA')
        self.assertEqual(outcome_class('CENSORED_ACCEPTANCE',None),'INDETERMINADO')
        self.assertEqual(outcome_class('RETEST_UNRESOLVED',None),'INDETERMINADO')
        self.assertEqual(outcome_class('UNRECOGNIZED',None),'INDETERMINADO')
        self.assertIsNone(parse_ts(np.nan))

    def test_plot_does_not_reveal_flip_in_pre_mode(self):
        x=self.make()
        bars=pd.read_csv(self.dir/'TESTUSDT_30m.csv')
        fig=make_chart(x.iloc[0],bars,48,show_post=False)
        self.assertTrue(any(t.name=='Mínimos confirmados en PRE' for t in fig.data))
        self.assertFalse(any('FLIP confirmado' in str(t.name) for t in fig.data))
        self.assertLessEqual(max(int(pd.Timestamp(s).timestamp()*1000) for s in fig.data[0].x),
                             int(x.iloc[0].pre_closed_ts)-TF)
        fig2=make_chart(x.iloc[0],bars,48,show_post=True)
        self.assertTrue(any('FLIP confirmado' in str(t.name) for t in fig2.data))

    def test_invalid_flip_time_fails(self):
        self.outcomes.loc[0,'defense_closed_ts']=self.attempt
        with self.assertRaisesRegex(ValueError,'inconsistent flip'):
            self.make()

    def test_view_source_schema(self):
        x=self.make()
        folder=self.root/'out';folder.mkdir()
        x.to_csv(folder/'pre_cohort.csv',index=False)
        loaded=load_cohort(folder)
        self.assertEqual(len(loaded),3)


if __name__=='__main__':unittest.main()
