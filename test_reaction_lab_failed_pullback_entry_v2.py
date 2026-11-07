"""Causal boundary and conservative-fill tests for FAILED sweep V2."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import reaction_lab_failed_pullback_entry_v2 as v2
from dashboard.views.reaction_lab_runner_discovery_all_touches_v1_view import plot_all_first_touch_case
from dashboard.views.reaction_lab_failed_pullback_entry_v2_view import plot_sweep_episode

M=60000


def candle(i,op,hi,lo,cl):
    assert hi>=max(op,cl)>=lo and hi>=lo
    return (10*M+i*M,op,hi,lo,cl)


def bars():
    seq=[candle(i,100,100.2,99.8,100) for i in range(260)]
    seq[0]=candle(0,100,100.3,97.8,99.7)  # sweep > 2% adverse, recovery above 98% threshold
    seq[1]=candle(1,99.7,100,99.2,99.9)
    seq[2]=candle(2,99.9,101.7,99.8,101.2) # reclaim original swing=101 +.05%
    seq[3]=candle(3,101.2,101.8,101,101.6)
    return seq


def base(eligible=True):
    return dict(touch_key='key',date_utc='2026-11-01',symbol='TESTUSDT',
        side='LONG',observation_type='RECOVERED_WINDOW',retest_ts=9*M,
        known_ts=10*M,path_source='HISTORICAL_BINANCE_V11',
        path_count=260,metadata_state='VALID_FAILED',
        eligible_common_180m=eligible,retest_close=100.,swing_price=101.)


class SweepLogic(unittest.TestCase):
    def test_depth_grid(self):
        self.assertEqual(v2.parse_depths('2,1,1.5,1'),(1.,1.5,2.))
        for bad in ('','0,1','foo','12'):
            with self.subTest(bad=bad),self.assertRaises(ValueError):
                v2.parse_depths(bad)

    def test_original_direction_adverse_sweep(self):
        self.assertAlmostEqual(v2.sweep_level(100,'LONG',2),98)
        self.assertAlmostEqual(v2.sweep_level(100,'SHORT',2),102)
        self.assertTrue(v2.reached(candle(0,100,103,97,102),'LONG',98))
        self.assertTrue(v2.reached(candle(0,100,103,97,102),'SHORT',102))

    def test_rebound_enters_next_open_not_sweep_wick(self):
        found=v2.first_signal(bars(),'LONG',100,101,2,'SWEEP_REBOUND',60)
        self.assertEqual(found[:2],(2,0))
        self.assertEqual(found[3],'NEXT_OPEN_CONFIRMED')
        self.assertEqual(found[2],bars()[2][1])

    def test_reclaim_requires_sweep_first(self):
        seq=bars()
        seq[0]=candle(0,100,101.6,99.2,101.2) # initial reclaim but NOT sweep
        seq[1]=candle(1,101.2,102,98.0,100.0) # sweep, no reclaim
        seq[2]=candle(2,100,101.6,99.5,101.2)
        found=v2.first_signal(seq,'LONG',100,101,2,'SWEEP_RECLAIM_LEVEL',60)
        self.assertEqual(found[:2],(3,1))

    def test_reclaim_2_closes_post_sweep(self):
        seq=bars()
        found=v2.first_signal(seq,'LONG',100,101,2,'SWEEP_RECLAIM_2_CLOSES',60)
        self.assertEqual(found[:2],(4,0))

    def test_no_sweep_no_signal(self):
        seq=[candle(i,100,101.5,99.5,101.1) for i in range(250)]
        for mode in v2.MODES[1:]:
            with self.subTest(mode=mode):
                self.assertIsNone(v2.first_signal(seq,'LONG',100,101,2,mode,60))

    def test_short_rebound_confirmed_after_adverse_upward_sweep(self):
        seq=[candle(i,100,100.3,99.7,100) for i in range(250)]
        seq[0]=candle(0,100,102.3,99.5,101.0)
        seq[1]=candle(1,101.,101.1,99.2,99.6)
        found=v2.first_signal(seq,'SHORT',100,99,2,'SWEEP_REBOUND',60)
        self.assertEqual(found[:2],(2,0))
        self.assertEqual(found[3],'NEXT_OPEN_CONFIRMED')

    def test_not_use_future_bar_to_signal(self):
        seq=bars()
        truncated=seq[:3]
        # bar 2 closes a reclaim but no bar 3 OPEN to enter
        self.assertIsNone(v2.first_signal(truncated,'LONG',100,101,2,'SWEEP_RECLAIM_LEVEL',60))
        self.assertEqual(v2.first_signal(seq,'LONG',100,101,2,'SWEEP_RECLAIM_LEVEL',60)[0],3)

    def test_limit_intra_candle_is_not_credited_tp(self):
        seq=[candle(i,100,100.1,99.9,100) for i in range(240)]
        seq[0]=candle(0,100,103,97.5,99) # LIMIT filled @ 98, TP also touched but order unknowable
        found=v2.first_signal(seq,'LONG',100,101,2,'SWEEP_LIMIT',60)
        self.assertEqual(found[3],'INTRABAR_LIMIT')
        state,minute,gross,unknown=v2.entry_bar_outcome(seq,0,98,'LONG',2,1,180)
        self.assertEqual(state,'FILL_BAR_TP_UNORDERED')
        self.assertEqual((minute,gross,unknown),(1,-1,True))

    def test_same_bar_both_bands_ambiguous_conservative(self):
        seq=[candle(i,100,100.1,99.9,100) for i in range(240)]
        seq[0]=candle(0,100,102.5,98.8,100)
        result=v2.evaluate(base(),seq,'IMMEDIATE_ORIGINAL',1,2,sl=1)
        self.assertEqual(result['outcome'],'SL_AMBIGUOUS')
        self.assertLess(result['net_pct'],0)
        self.assertGreater(result['net_best_pct'],0)

    def test_respects_configurable_stop(self):
        seq=[candle(i,100,100.1,99.3,99.9) for i in range(240)]
        one=v2.evaluate(base(),seq,'IMMEDIATE_ORIGINAL',1,2,sl=.5)
        two=v2.evaluate(base(),seq,'IMMEDIATE_ORIGINAL',1,2,sl=1)
        self.assertEqual(one['outcome'],'SL_FIRST')
        self.assertEqual(two['outcome'],'TIME_EXIT')

    def test_stop_preset_and_custom_report_paths(self):
        self.assertEqual(v2.SUGGESTED_SL_PCTS,(1.,3.,4.,5.))
        root=Path('/tmp/pullback-sl-config-test')
        self.assertEqual(v2.output_dir_for_sl(root,1),root/v2.OUTPUT)
        paths=[v2.output_dir_for_sl(root,s) for s in (1,3,4,5,2.5)]
        self.assertEqual(len(set(paths)),len(paths))
        self.assertEqual(v2.output_dir_for_sl(root,3),root/v2.OUTPUT/'sl_3pct')
        self.assertEqual(v2.output_dir_for_sl(root,4),root/v2.OUTPUT/'sl_4pct')
        self.assertEqual(v2.output_dir_for_sl(root,5),root/v2.OUTPUT/'sl_5pct')
        self.assertEqual(v2.output_dir_for_sl(root,2.5),root/v2.OUTPUT/'sl_2p5pct')

    def test_actual_stop_changes_trade_outcome_on_same_bars(self):
        seq=[candle(i,100,100.25,99.75,100) for i in range(260)]
        seq[2]=candle(2,100,100.2,98.1,99.3)  # SL 1%, but not 3%, 4%, 5%
        for sl in (1,3,4,5):
            result=v2.evaluate(base(),seq,'IMMEDIATE_ORIGINAL',2,2,sl=sl)
            self.assertEqual(result['sl_pct'],sl)
            self.assertEqual(result['outcome'],'SL_FIRST' if sl==1 else 'TIME_EXIT')
            self.assertIn('net_pct',result)

    def test_missing_horizon_not_negative_trade(self):
        result=v2.evaluate(base(False),[], 'SWEEP_REBOUND',1.,2)
        self.assertEqual((result['signal_state'],result['outcome']),
                         ('CENSORED_COMMON_HORIZON','CENSORED'))

    def test_no_signal_counted_zero_exposure(self):
        seq=[candle(i,100,100.2,99.8,100) for i in range(260)]
        event=base();row=v2.evaluate(event,seq,'SWEEP_REBOUND',2,2)
        self.assertEqual((row['signal_state'],row['outcome']),('NO_SIGNAL','NO_TRADE'))
        summ=v2.summarize([row],[event],'ALL','ALL','SWEEP_REBOUND',2,2)
        self.assertEqual(summ['no_signal_n'],1)
        self.assertEqual(summ['signal_n'],0)
        self.assertIsNone(summ['net_mean_per_signal_pct'])

    def test_common_denominator_same_for_all_modes(self):
        seq=bars();event=base()
        outcomes=[v2.evaluate(event,seq,mode,2,2) for mode in v2.MODES]
        self.assertTrue(all(row['cohort_eligible'] for row in outcomes))

    def test_chart_renders_post_and_tp_sl(self):
        seq=bars();ev=base()
        outcome=v2.evaluate(ev,seq,'SWEEP_REBOUND',2,2)
        fig=plot_sweep_episode(seq,ev,outcome,180)
        self.assertTrue(len(fig.data)>=2)
        self.assertTrue(any('SL' in str(a.text) for a in fig.layout.annotations))
        rr={'side':'LONG','first_touch_status':'TOUCH_FAILED','retest_close':100}
        fig2=plot_all_first_touch_case(seq,rr,tp=2,sl=1,n=180,level=101)
        self.assertTrue(len(fig2.data)>0)

    def test_full_pilot_106_events(self):
        root=Path(__file__).resolve().parent
        sample=root/'reports/reaction_lab/failed_reclaim_audit_sample_v1/journal_metadata_sample.csv'
        hist=root/'reports/reaction_lab/first_touch_historical_recovery_v11'
        if not sample.exists():self.skipTest('Pilot files not installed on VPS (expected)')
        with tempfile.TemporaryDirectory() as td:
            result=v2.build(root,sample_metadata=sample,recovery_root=hist,
                            depths=(1,2),sl_pct=1,out=td)
            self.assertEqual(result['cohort_total'],106)
            self.assertEqual(result['coverage_common'],106)
            self.assertEqual(result['settings']['sl_pct'],1)
            self.assertEqual(result['settings']['depths_pct'],[1,2])


if __name__=='__main__':unittest.main()
