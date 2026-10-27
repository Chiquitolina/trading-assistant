"""Regressions: no fake entries, no TP/SL without confirmation, causal first touches."""
from __future__ import annotations
import tempfile,unittest
from pathlib import Path
import pandas as pd
from dashboard.views.hll_v294_entry_matrix import (
 build_entry_catalogue,summarize_coverage,evaluate_grid,matrix_table,
 read_minute_window,aggregate_selection_matrix,reconcile_entries_with_catalogue
)

MS=60_000; TF=1_800_000

class HLLV294Tests(unittest.TestCase):
    def test_catalogue_only_confirmed_routes_and_count_unique_attempts(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); minutes=root/'m';candles=root/'c';minutes.mkdir();candles.mkdir()
            cohort=pd.DataFrame([
             dict(attempt_id='A',symbol='TEST',flip_confirmed_closed_ts=4*TF),
             dict(attempt_id='B',symbol='TEST',flip_confirmed_closed_ts=float('nan')),
             dict(attempt_id='C',symbol='TEST',flip_confirmed_closed_ts=float('nan')),
            ])
            data=[dict(timestamp=i*TF,open=100+i,high=103+i,low=99+i,close=101+i)
                  for i in range(2,12)]
            pd.DataFrame(data).to_csv(candles/'TEST_30m.csv',index=False)
            pd.DataFrame([dict(timestamp=4*TF+MS,open=120,high=122,low=119,close=121)]).to_csv(minutes/'TEST_1m.csv',index=False)
            events=pd.DataFrame([
                dict(attempt_id='A',branch='BREAKOUT_CONTINUATION',status='CONFIRMED',
                     confirmed_ts=7*TF,signal_open_ts=6*TF,signal_close=107, next_open_ts=7*TF,next_open=107),
                dict(attempt_id='B',branch='BREAKOUT_CONTINUATION',status='NOT_CONFIRMED',
                     confirmed_ts=float('nan'),signal_open_ts=float('nan'),signal_close=float('nan')),
                dict(attempt_id='C',branch='RECLAIM_CONTINUATION',status='CONFIRMED',
                     confirmed_ts=8*TF,signal_open_ts=7*TF,signal_close=108,next_open_ts=8*TF,next_open=108),
            ])
            # Signal candle at 6*TF has close 107 and at 7*TF has close 108.
            cat=build_entry_catalogue(cohort,events,candles,minutes)
            self.assertEqual(len(cat),3)
            self.assertEqual(cat.status.tolist(),['ENTRY_VERIFIED','ENTRY_VERIFIED','ENTRY_VERIFIED'])
            s=summarize_coverage(cohort,cat)
            self.assertEqual(s['with_entry'],2)
            self.assertEqual(s['without_entry'],1)
            self.assertEqual(s['no_signal'],1)
            self.assertEqual(s['FLIP_1M']['verified'],1)
            self.assertEqual(s['V29_CONTINUATION_30M']['verified'],1)
            self.assertEqual(s['V29_RECLAIM_30M']['verified'],1)

    def test_signal_without_open_is_not_entry(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);minutes=root/'m';candles=root/'c';minutes.mkdir();candles.mkdir()
            cohort=pd.DataFrame([dict(attempt_id='X',symbol='TEST',flip_confirmed_closed_ts=5*TF)])
            pd.DataFrame([dict(timestamp=5*TF+2*MS,open=99,high=100,low=98,close=99)]).to_csv(minutes/'TEST_1m.csv',index=False)
            cat=build_entry_catalogue(cohort,pd.DataFrame(),candles,minutes)
            self.assertEqual(cat.status.iloc[0],'SIGNAL_OPEN_MISSING')
            s=summarize_coverage(cohort,cat)
            self.assertEqual(s['without_entry'],1)
            self.assertEqual(s['confirmed_unverified'],1)

    def test_first_hit_tp_sl_ambiguous_and_no_hit(self):
        data=pd.DataFrame([
            dict(high=101.5,low=99.5),
            dict(high=103.2,low=97.9),
            dict(high=105.1,low=97.0),
        ])
        r=evaluate_grid(data,100,'COMPLETE_1M')
        self.assertEqual(r[(r.tp_pct==1)&(r.sl_pct==2)].iloc[0].result,'TP_FIRST')
        self.assertEqual(r[(r.tp_pct==4)&(r.sl_pct==3)].iloc[0].result,'AMBIGUOUS_1M')
        self.assertEqual(r[(r.tp_pct==5)&(r.sl_pct==2)].iloc[0].result,'SL_FIRST')
        self.assertEqual(matrix_table(r).shape,(5,5))

    def test_gap_before_hit_is_censored_but_after_hit_does_not_undo_result(self):
        data=pd.DataFrame([dict(high=101.2,low=99.8)])
        result=evaluate_grid(data,100,'CENSORED_GAP')
        self.assertEqual(result[(result.tp_pct==1)&(result.sl_pct==3)].iloc[0].result,'TP_FIRST')
        self.assertEqual(result[(result.tp_pct==5)&(result.sl_pct==3)].iloc[0].result,'CENSORED_GAP')

    def test_incomplete_path_not_timeout(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'s_1m.csv';t=60_000_000
            pd.DataFrame([dict(timestamp=t,open=100,high=100.2,low=99.8,close=100),
                          dict(timestamp=t+2*MS,open=100,high=100.3,low=99.7,close=100)]).to_csv(path,index=False)
            data,state=read_minute_window(path,t,120)
            self.assertEqual(state,'CENSORED_GAP')
            self.assertEqual(len(data),1)
            self.assertEqual(evaluate_grid(data,100,state).result.iloc[0],'CENSORED_GAP')

    def test_aggregate_displays_25_cells_without_dropping_uncertain_cases(self):
        with tempfile.TemporaryDirectory() as d:
            dir=Path(d)
            t=120_000_000
            prices=[dict(timestamp=t+i*MS,open=100,high=101.5 if i==1 else 100.2,
                         low=99.8,close=100) for i in range(120)]
            pd.DataFrame(prices).to_csv(dir/'TEST_1m.csv',index=False)
            cat=pd.DataFrame([dict(attempt_id='X',symbol='TEST',route='FLIP_1M',
                                   status='ENTRY_VERIFIED',entry_ts=t,entry_price=100)])
            result=aggregate_selection_matrix(cat,dir,120)
            self.assertEqual(len(result),25)
            a=result[(result.tp_pct==1)&(result.sl_pct==2)].iloc[0]
            self.assertEqual(a.TP_FIRST,1)
            b=result[(result.tp_pct==5)&(result.sl_pct==2)].iloc[0]
            self.assertEqual(b.TIMEOUT,1)

    def test_plotter_can_recover_verified_entry_but_never_breakout(self):
        catalogue=pd.DataFrame([dict(attempt_id='X',symbol='TEST',route='V29_CONTINUATION_30M',
                                     status='ENTRY_VERIFIED',entry_ts=36000000,entry_price=0.004741)])
        data=reconcile_entries_with_catalogue([],catalogue)
        self.assertEqual(len(data),1)
        self.assertEqual(data[0]['key'],'V29_CONTINUATION_30M')
        self.assertEqual(data[0]['price'],0.004741)
        invalid=pd.DataFrame([dict(attempt_id='X',symbol='TEST',route='V29_RECLAIM_30M',
                                      status='SIGNAL_OPEN_MISSING',entry_ts=36000000,entry_price=None)])
        self.assertEqual(reconcile_entries_with_catalogue([],invalid),[])

    def test_view_requires_verified_reference_for_guides(self):
        from dashboard.views.hll_v282_pre_followup_view import render_hll_v282_pre_followup
        import inspect
        src=inspect.getsource(render_hll_v282_pre_followup)
        self.assertIn("if e['key'] != 'BREAKOUT_30M'",src)
        self.assertIn("if show_post and not blind and references:",src)
        self.assertIn('aggregate_selection_matrix(',src)
        self.assertIn('load_catalogue_for_view(',src)

if __name__=='__main__':unittest.main()
