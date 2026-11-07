"""Causal order and stop-geometry regression tests for FAILED V3."""
from __future__ import annotations
import csv
from pathlib import Path
import tempfile
import unittest

import reaction_lab_failed_reversal_retest_v3 as v3
import reaction_lab_failed_pullback_entry_v2 as v2
from dashboard.views.reaction_lab_failed_reversal_retest_v3_view import plot_reversal_retest

M=60_000


def b(i,op,hi,lo,cl):
    assert hi>=max(op,cl)>=min(op,cl)>=lo
    return (10*M+i*M,op,hi,lo,cl)


def long_bars():
    seq=[b(i,100,100.2,99.8,100) for i in range(260)]
    seq[0]=b(0,100,100.2,98.8,99.0)  # sweep >=1%
    seq[1]=b(1,99,99.5,98.9,99.2)   # rebound but NOT prior high breakout
    seq[2]=b(2,99.2,100.1,99.,99.8) # close 99.8 > prior high 99.5
    seq[3]=b(3,99.8,100.1,99.6,99.9)  # retest 25% 99.55 NOT filled
    seq[4]=b(4,99.9,100.,99.45,99.6) # retest 25% filled 99.55
    seq[5]=b(5,99.6,99.7,99.25,99.5) # retest 50% filled 99.30
    seq[6]=b(6,99.5,99.6,99.0,99.4)  # retest 75% filled 99.05
    return seq


def short_bars():
    seq=[b(i,100,100.2,99.8,100) for i in range(260)]
    seq[0]=b(0,100,101.2,99.8,101.)
    seq[1]=b(1,101.,101.1,100.5,100.8)
    seq[2]=b(2,100.8,101.,99.9,100.2) # close 100.2 < prior low100.5
    seq[3]=b(3,100.2,100.5,100.1,100.3) # fills retest25 at 100.45
    return seq


def base(side='LONG',eligible=True):
    return dict(touch_key=f'key_{side}',date_utc='2026-11-01',symbol='TESTUSDT',side=side,
        observation_type='RECOVERED_WINDOW',retest_ts=9*M,known_ts=10*M,
        path_source='HISTORICAL_BINANCE_V11',path_count=260,
        metadata_state='VALID_FAILED',eligible_common_180m=eligible,
        retest_close=100.,swing_price=101 if side=='LONG' else 99)


class ReversalRetestV3(unittest.TestCase):
    def test_sweep_then_later_reversal_long(self):
        x=v3.find_sweep_reversal(long_bars(),'LONG',100,1)
        self.assertEqual((x['sweep_bar_index'],x['reversal_bar_index']),(0,2))
        self.assertEqual(x['sweep_extreme'],98.8)
        self.assertGreater(x['reversal_leg_pct'],.25)

    def test_symmetric_short(self):
        x=v3.find_sweep_reversal(short_bars(),'SHORT',100,1)
        self.assertEqual((x['sweep_bar_index'],x['reversal_bar_index']),(0,2))
        self.assertEqual(x['sweep_extreme'],101.2)
        self.assertAlmostEqual(v3.retest_limit(101.2,100.2,'SHORT',25),100.45)
        y=v3.evaluate(base('SHORT'),short_bars(),'RETEST_25',1,2,sl=3)
        self.assertEqual(y['entry_bar_index'],3)
        self.assertAlmostEqual(y['entry_price'],100.45)

    def test_first_pullback_levels_are_frozen_at_reversal(self):
        seq=long_bars()
        expected={25:(4,99.55),50:(5,99.30),75:(6,99.05)}
        for k,(ix,price) in expected.items():
            with self.subTest(k=k):
                y=v3.evaluate(base(),seq,f'RETEST_{k}',1,2,sl=3)
                self.assertEqual(y['entry_bar_index'],ix)
                self.assertAlmostEqual(y['entry_price'],price)
                self.assertEqual(y['reversal_bar_index'],2)
                self.assertGreater(ix,2)

    def test_wick_break_alone_does_not_confirm(self):
        seq=long_bars()
        seq[2]=b(2,99.2,100.1,99.,99.3)  # high breaks, close does not
        x=v3.find_sweep_reversal(seq[:3],'LONG',100,1)
        self.assertIsNone(x['reversal_bar_index'])

    def test_same_bar_sweep_and_break_does_not_confirm(self):
        seq=long_bars()
        seq[0]=b(0,100,102,98.5,101.5)
        x=v3.find_sweep_reversal(seq[:1],'LONG',100,1)
        self.assertEqual(x['sweep_bar_index'],0)
        self.assertIsNone(x['reversal_bar_index'])

    def test_future_changes_cannot_rewrite_reversal_price(self):
        seq=long_bars()
        one=v3.find_sweep_reversal(seq[:4],'LONG',100,1)
        seq[40]=b(40,100,110,80,90)
        two=v3.find_sweep_reversal(seq,'LONG',100,1)
        self.assertEqual(one['reversal_bar_index'],two['reversal_bar_index'])
        self.assertEqual(one['reversal_close'],two['reversal_close'])
        self.assertEqual(one['sweep_extreme'],two['sweep_extreme'])
        p1=v3.retest_limit(one['sweep_extreme'],one['reversal_close'],'LONG',50)
        p2=v3.retest_limit(two['sweep_extreme'],two['reversal_close'],'LONG',50)
        self.assertEqual(p1,p2)

    def test_no_sweep_no_entry(self):
        seq=[b(i,100,100.1,99.7,100) for i in range(260)]
        for mode in ('REVERSAL_OPEN','RETEST_25','RETEST_50','RETEST_75'):
            with self.subTest(mode=mode):
                y=v3.evaluate(base(),seq,mode,2,2,sl=3)
                self.assertEqual(y['signal_state'],'NO_SIGNAL')
                self.assertEqual(y['no_signal_reason'],'NO_SWEEP')

    def test_no_fill_is_zero_exposure_not_failure(self):
        seq=long_bars()
        for j in range(3,260):seq[j]=b(j,100.2,100.5,100.1,100.3)
        y=v3.evaluate(base(),seq,'RETEST_75',1,2,sl=3)
        self.assertEqual((y['signal_state'],y['no_signal_reason']),('NO_SIGNAL','NO_FILL'))
        self.assertIsNone(y['net_pct'])
        summ=v3.summarize([y],[base()],'ALL','ALL','RETEST_75',1,2,'pct',3)
        self.assertEqual((summ['no_signal_n'],summ['no_fill_n'],summ['signal_n']),(1,1,0))

    def test_censored_is_not_no_signal(self):
        y=v3.evaluate(base(eligible=False),[],'RETEST_50',1,2,sl=3)
        self.assertEqual(y['signal_state'],'CENSORED_COMMON_HORIZON')
        self.assertIsNone(y['net_pct'])

    def test_stop_structural_price_long_short(self):
        self.assertAlmostEqual(v3.stop_price_for(100,'LONG','structural',3,98,.1),97.902)
        self.assertAlmostEqual(v3.stop_price_for(100,'SHORT','structural',3,102,.1),102.102)
        self.assertAlmostEqual(v3.stop_price_for(100,'LONG','pct',3,98,.1),97.)

    def test_percent_sl_1_3_4_5_affects_actual_exit(self):
        seq=[b(i,100,100.1,99.6,100) for i in range(260)]
        seq[3]=b(3,100,100.1,98.1,99.5)
        for sl in (1,3,4,5):
            with self.subTest(sl=sl):
                y=v3.evaluate(base(),seq,'IMMEDIATE_ORIGINAL',1,2,sl=sl)
                # Bar 3 hits 1%, not 3%, 4%, 5%.
                self.assertEqual(y['outcome'],'SL_FIRST' if sl==1 else 'TIME_EXIT')

    def test_intrabar_fill_bar_tp_uncertain_has_real_future_lower_bound(self):
        seq=[b(i,100,100.2,99.8,100) for i in range(10)]
        seq[0]=b(0,100,103,98.5,100)  # LIMIT at99 fills, TP +2% also touched
        result=v3.price_stop_execute(seq,0,99,'LONG',2,96,5,intrabar_entry=True)
        self.assertEqual(result[0],'FILL_BAR_TP_UNORDERED')
        self.assertTrue(result[4])
        self.assertGreater(result[2],0) # future TIME_EXIT, not invented -SL
        self.assertAlmostEqual(result[3],2.)

    def test_open_entry_matches_v2_baseline_and_controls(self):
        seq=long_bars()
        for old,new in [('IMMEDIATE_ORIGINAL','IMMEDIATE_ORIGINAL'),
                        ('SWEEP_REBOUND','V2_SWEEP_REBOUND'),
                        ('SWEEP_RECLAIM_2_CLOSES','V2_RECLAIM_2_CLOSES')]:
            a=v2.evaluate(base(),seq,old,1,2,sl=3)
            b3=v3.evaluate(base(),seq,new,1,2,sl=3)
            self.assertEqual((a['signal_state'],a['outcome'],a['net_pct']),
                             (b3['signal_state'],b3['outcome'],b3['net_pct']))

    def test_structural_gap_does_not_disappear_as_no_signal(self):
        seq=long_bars()
        seq[3]=b(3,97,97.5,96.9,97.2)  # post-reversal OPEN below structural stop
        y=v3.evaluate(base(),seq,'RETEST_50',1,2,stop_mode='structural')
        self.assertEqual(y['signal_state'],'SIGNAL')
        self.assertEqual(y['outcome'],'STRUCT_STOP_GAP_AT_FILL')
        self.assertLess(y['net_pct'],0)

    def test_limit_open_better_than_order_price(self):
        seq=long_bars()
        seq[3]=b(3,99.1,99.4,99.0,99.3)  # opens through 50% limit 99.3
        result=v3.evaluate(base(),seq,'RETEST_50',1,2,sl=3)
        self.assertEqual(result['entry_bar_index'],3)
        self.assertEqual(result['entry_resolution'],'LIMIT_OPEN_BETTER')
        self.assertAlmostEqual(result['entry_price'],99.1)

    def test_intrabar_fill_tp_sl_same_candle_not_a_winner(self):
        seq=[b(i,100,100.1,99.9,100) for i in range(240)]
        seq[0]=b(0,100,103,95,100)
        state,minute,worst,best,uncertain=v3.price_stop_execute(
            seq,0,99,'LONG',2,96,180,intrabar_entry=True)
        self.assertEqual(state,'FILL_BAR_TP_UNORDERED')
        self.assertTrue(uncertain)
        self.assertLess(worst,0)
        self.assertEqual(best,2)

    def test_unfrozen_parameters_refused_to_prevent_training_drift(self):
        with self.assertRaisesRegex(ValueError,'frozen'):
            v3.build('.',depths=(1,1.5,2),sample_metadata='unused')

    def test_report_paths_do_not_overlap_v1_v2_or_each_other(self):
        root=Path('/tmp/failed_v3_path_unit')
        self.assertEqual(len({str(v3.config_dir(root,sl_pct=x)) for x in (1,3,4,5)}),4)
        self.assertNotEqual(v3.config_dir(root,stop_mode='structural'),
                            v3.config_dir(root,sl_pct=1))
        self.assertNotIn('failed_pullback_entry_v2',str(v3.config_dir(root)))

    def test_candle_plot(self):
        seq=long_bars();y=v3.evaluate(base(),seq,'RETEST_25',1,2,sl=3)
        fig=plot_reversal_retest(seq,base(),y,180)
        self.assertTrue(len(fig.data)>=3)
        self.assertTrue(any('SL real' in str(a.text) for a in fig.layout.annotations))

    def test_real_106_event_pilot_verified(self):
        root=Path(__file__).resolve().parent
        sample=root/'reports/reaction_lab/failed_reclaim_audit_sample_v1/journal_metadata_sample.csv'
        hist=root/'reports/reaction_lab/first_touch_historical_recovery_v11'
        if not sample.exists():self.skipTest('Sample not present; expected on VPS')
        with tempfile.TemporaryDirectory() as tmp:
            result=v3.build(root,sample_metadata=sample,recovery_root=hist,
                            depths=(1,2),stop_mode='pct',sl_pct=3,out=tmp)
            self.assertEqual(result['cohort_total'],106)
            self.assertEqual(result['coverage_common'],106)
            with (Path(tmp)/'event_outcomes.csv').open(newline='',encoding='utf-8') as f:
                records=list(csv.DictReader(f))
            self.assertEqual(len(records),106*2*len(v3.MODES)*len(v3.TARGETS))
            self.assertTrue(any(r['signal_state']=='NO_SIGNAL' for r in records))
            self.assertTrue(any(r['entry_resolution']=='LIMIT_INTRABAR' for r in records))
            # Verify controls against original V2 on ALL real sample paths.
            import reaction_lab_failed_reclaim_audit_v1 as v1
            journal=v1.read_sample_journal(sample)
            cases=0
            for row,bars in v1.sample_paths(journal,hist):
                event=v1.event_base(row,bars,journal.get(row['touch_key']),60,180)
                for old,new in [('IMMEDIATE_ORIGINAL','IMMEDIATE_ORIGINAL'),
                                ('SWEEP_REBOUND','V2_SWEEP_REBOUND'),
                                ('SWEEP_RECLAIM_2_CLOSES','V2_RECLAIM_2_CLOSES')]:
                    a=v2.evaluate(event,bars,old,1,2,sl=3)
                    b3=v3.evaluate(event,bars,new,1,2,sl=3)
                    self.assertEqual((a['signal_state'],a['outcome'],a['net_pct']),
                                     (b3['signal_state'],b3['outcome'],b3['net_pct']))
                    cases+=1
            self.assertEqual(cases,318)


if __name__=='__main__':unittest.main()
