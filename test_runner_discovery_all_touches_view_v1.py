"""Pure UI aggregation checks; no Streamlit, DB or LIVE needed."""
import unittest
import pandas as pd
from dashboard.views.reaction_lab_runner_discovery_all_touches_v1_view import (
    _select_outcome, make_summary,
)


class AllFirstTouchesViewTests(unittest.TestCase):
    def test_horizon_censoring_and_first_hit(self):
        self.assertEqual(_select_outcome('TP_FIRST',201,180),'NO_HIT')
        self.assertEqual(_select_outcome('SL_FIRST',3,180),'SL_FIRST')
        self.assertEqual(_select_outcome('AMBIGUOUS',1,180),'AMBIGUOUS')
        self.assertEqual(_select_outcome('NO_HIT',None,180),'NO_HIT')

    def test_denominators_by_status_and_180m(self):
        frame=pd.DataFrame([
            dict(first_touch_status='REACTION',path_quality='CONTIGUOUS_VALID',path_count=360,
                 retest_close=100,next_open=100,retest_time_to_run_2pct=100,retest_tp2_result='TP_FIRST',retest_tp2_minute=100),
            dict(first_touch_status='TOUCH_FAILED',path_quality='CONTIGUOUS_VALID',path_count=360,
                 retest_close=100,next_open=100,retest_time_to_run_2pct=125,retest_tp2_result='SL_FIRST',retest_tp2_minute=15),
            dict(first_touch_status='TOUCH_FAILED',path_quality='NO_CONTIGUOUS_PATH',path_count=0,
                 retest_close=100,next_open=None,retest_time_to_run_2pct=None,retest_tp2_result=None,retest_tp2_minute=None),
            dict(first_touch_status='TOUCH_INDECISIVE',path_quality='CONTIGUOUS_VALID',path_count=120,
                 retest_close=100,next_open=100,retest_time_to_run_2pct=30,retest_tp2_result='TP_FIRST',retest_tp2_minute=30),
        ])
        table=make_summary(frame,horizon=180,tp=2,basis='retest').set_index('Clasificación inicial')
        self.assertEqual(table.loc['REACTION','Con cobertura'],1)
        self.assertEqual(table.loc['TOUCH_FAILED','Con cobertura'],1)
        self.assertEqual(table.loc['TOUCH_FAILED','Sin cobertura'],1)
        self.assertEqual(table.loc['TOUCH_FAILED','Runners +2%'],1)
        self.assertEqual(table.loc['TOUCH_FAILED','TP antes de SL −1%'],0)
        self.assertEqual(table.loc['TOUCH_INDECISIVE','Con cobertura'],0)
        table15=make_summary(frame,horizon=15,tp=2,basis='retest').set_index('Clasificación inicial')
        self.assertEqual(table15.loc['TOUCH_INDECISIVE','Con cobertura'],1)
        self.assertEqual(table15.loc['TOUCH_INDECISIVE','Runners +2%'],0)

if __name__=='__main__':unittest.main()
