import tempfile
import unittest
from pathlib import Path
import pandas as pd

from dashboard.views.hll_v295_net_matrix import (
    first_touch_net, aggregate_net_matrix, first_attempt_ids,
)

M=60_000


class V295NetMatrixTests(unittest.TestCase):
    def test_net_tp_first_clock(self):
        t=1_800_000
        x=pd.DataFrame([dict(timestamp=t,open=100,high=100.5,low=99.7,close=100.1),
                        dict(timestamp=t+M,open=100.1,high=102.1,low=99.8,close=101)])
        r=first_touch_net(x,100,'CENSORED_TAIL',t,0.10,0.05)
        a=r[(r.tp_pct==2)&(r.sl_pct==1)].iloc[0]
        self.assertEqual(a.result,'TP_FIRST')
        self.assertEqual(a.first_hit_min,1)
        self.assertEqual(a.first_hit_minute_utc,pd.Timestamp(t+M,unit='ms',tz='UTC').isoformat())
        self.assertAlmostEqual(a.net_pct,1.85)

    def test_net_sl_first(self):
        t=120_000_000
        x=pd.DataFrame([dict(timestamp=t,open=100,high=100.2,low=98.7,close=99)])
        r=first_touch_net(x,100,'CENSORED_GAP',t,0.10,0.10)
        a=r[(r.tp_pct==2)&(r.sl_pct==1)].iloc[0]
        self.assertEqual(a.result,'SL_FIRST')
        self.assertAlmostEqual(a.net_pct,-1.2)

    def test_ambiguous_cannot_be_assigned_net(self):
        t=120_000_000
        x=pd.DataFrame([dict(timestamp=t,open=100,high=104,low=96,close=99)])
        r=first_touch_net(x,100,'COMPLETE_1M',t)
        a=r[(r.tp_pct==3)&(r.sl_pct==3)].iloc[0]
        self.assertEqual(a.result,'AMBIGUOUS_1M')
        self.assertTrue(pd.isna(a.net_pct))

    def test_timeout_close_and_fee(self):
        t=120_000_000
        x=pd.DataFrame([dict(timestamp=t,open=100,high=100.4,low=99.8,close=100.7)])
        r=first_touch_net(x,100,'COMPLETE_1M',t,0.15)
        a=r[(r.tp_pct==1)&(r.sl_pct==1)].iloc[0]
        self.assertEqual(a.result,'TIMEOUT')
        self.assertAlmostEqual(a.net_pct,.55)
        self.assertIsNone(a.first_hit_minute_utc)

    def test_gap_unresolved_is_censored(self):
        t=120_000_000
        x=pd.DataFrame([dict(timestamp=t,open=100,high=100.4,low=99.8,close=100.2)])
        r=first_touch_net(x,100,'CENSORED_GAP',t,.1)
        a=r[(r.tp_pct==1)&(r.sl_pct==1)].iloc[0]
        self.assertEqual(a.result,'CENSORED_GAP')
        self.assertTrue(pd.isna(a.net_pct))

    def test_first_attempt_global_before_filter(self):
        cohort=pd.DataFrame([dict(attempt_id='early',symbol='X',level_id='L',breakout_closed_ts=1),
                             dict(attempt_id='late',symbol='X',level_id='L',breakout_closed_ts=2),
                             dict(attempt_id='other_sym',symbol='Y',level_id='L',breakout_closed_ts=3)])
        self.assertEqual(first_attempt_ids(cohort),{'early','other_sym'})

    def test_aggregated_routes_separate_and_known_denominators(self):
        t=120_000_000
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)
            candles=[]
            for i in range(120):
                candles.append(dict(timestamp=t+i*M,open=100,high=102.0 if i==1 else 100.5,
                                    low=99.5,close=100.5))
            for i in range(120):
                candles.append(dict(timestamp=t+120*M+i*M,open=100,high=100.3,low=98 if i==1 else 99.5,
                                    close=100))
            pd.DataFrame(candles).to_csv(folder/'X_1m.csv',index=False)
            catalogue=pd.DataFrame([
                dict(attempt_id='A',symbol='X',route='FLIP_1M',status='ENTRY_VERIFIED',
                     entry_ts=t,entry_price=100),
                dict(attempt_id='A',symbol='X',route='V29_CONTINUATION_30M',status='ENTRY_VERIFIED',
                     entry_ts=t+120*M,entry_price=100),
                dict(attempt_id='B',symbol='X',route='FLIP_1M',status='SIGNAL_OPEN_MISSING',
                     entry_ts=t,entry_price=None),
            ])
            r=aggregate_net_matrix(catalogue,folder,120,0.1,0.0)
            self.assertEqual(len(r),50)
            flip=r[(r.route=='FLIP_1M')&(r.tp_pct==1)&(r.sl_pct==2)].iloc[0]
            cont=r[(r.route=='V29_CONTINUATION_30M')&(r.tp_pct==1)&(r.sl_pct==2)].iloc[0]
            self.assertEqual(flip.entries,1)
            self.assertEqual(flip.TP_FIRST,1)
            self.assertAlmostEqual(flip.net_mean_pct,.9)
            self.assertEqual(cont.SL_FIRST,1)
            self.assertAlmostEqual(cont.net_mean_pct,-2.1)

    def test_ui_has_net_controls_and_gate(self):
        import inspect
        from dashboard.views.hll_v282_pre_followup_view import render_hll_v282_pre_followup
        src=inspect.getsource(render_hll_v282_pre_followup)
        self.assertIn('fee_pct',src)
        self.assertIn('slippage_pct',src)
        self.assertIn('first_attempt_ids(df)',src)
        self.assertIn("if show_post and not blind and references:",src)
        self.assertIn('aggregate_net_matrix(',src)


if __name__=='__main__':
    unittest.main()
