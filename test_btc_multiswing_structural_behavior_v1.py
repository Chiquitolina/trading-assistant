#!/usr/bin/env python3
"""Offline/casuality regression for Multi-Swing Structural Behavior 15m V1."""
import unittest
import json
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

import btc_multiswing_structural_behavior_v1 as b

M=b.M
TF=b.TF


def bars(closes, *, begin=0, highs=None, lows=None):
    rows=[];prior=float(closes[0])
    for i,c in enumerate(closes):
        c=float(c)
        hi=max(c,prior) if highs is None else float(highs[i])
        lo=min(c,prior) if lows is None else float(lows[i])
        rows.append({'timestamp':begin+i*TF,'open':prior,
                     'high':max(hi,prior,c),'low':min(lo,prior,c),'close':c})
        prior=c
    return pd.DataFrame(rows)


def trace(closes, approach='BELOW', side='HIGH', highs=None,lows=None, horizon=None):
    f=bars(closes,highs=highs,lows=lows)
    return b.analyze_15m_sequence(
        f,event_id='msv2_x|V1|0',region_id='msv2_x',visit_number=1,
        touch_open=0,source_end=len(f)*TF,
        side=side,approach=approach,band_low=100,band_high=101,
        pre_close=99.7 if approach=='BELOW' else 101.3,
        horizon_bars=horizon or len(f))


class BehaviorTransitions(unittest.TestCase):
    def test_high_from_below_defends_with_two_confirmed_closes(self):
        trans,out=trace([99.6,99.4,99.1])
        self.assertEqual(out['first_resolution'],'DEFENSE_2_CLOSES_WITH_DEPARTURE')
        self.assertEqual(out['first_defense_ts'],2*TF)
        self.assertEqual(out['breakout_count'],0)
        self.assertTrue((trans.confirmed_ts>=TF).all())

    def test_high_from_below_breaks_only_after_second_close(self):
        trans,out=trace([100.8,101.3,101.6])
        self.assertEqual(out['first_breakout_ts'],3*TF)
        self.assertEqual(out['first_resolution'],'BREAKOUT_2_CLOSES')
        self.assertFalse(((trans.event_type=='BREAKOUT_2_CLOSES') &
                          (trans.confirmed_ts<3*TF)).any())

    def test_low_from_above_uses_opposite_orientation(self):
        trans,out=trace([101.6,99.8,99.3],approach='ABOVE',side='LOW')
        self.assertEqual(out['first_breakout_ts'],3*TF)
        self.assertEqual(out['first_resolution'],'BREAKOUT_2_CLOSES')

    def test_failed_breakout_requires_two_original_side_closes(self):
        trans,out=trace([101.4,101.7,100.5,99.8,99.7])
        self.assertEqual(out['first_breakout_ts'],2*TF)
        self.assertEqual(out['first_failed_breakout_ts'],5*TF)
        self.assertEqual(out['first_reentry_ts'],4*TF)

    def test_one_original_side_close_does_not_confirm_failed_breakout(self):
        trans,out=trace([101.5,101.5,99.8,101.7])
        self.assertIsNone(out['first_failed_breakout_ts'])

    def test_retest_after_clean_away_bar_then_two_closes(self):
        # i3 touches band again AFTER one fully outside far-side candle.
        closes=[101.7,102.2,103.5,100.8,101.8,102.2]
        highs=[101.8,102.3,103.5,102.1,101.9,102.3]
        lows=[99.9,101.2,102.3,100.6,100.7,101.4]
        trans,out=trace(closes,highs=highs,lows=lows)
        self.assertEqual(out['first_breakout_ts'],2*TF)
        self.assertEqual(out['first_retest_touch_ts'],4*TF)
        self.assertEqual(out['first_retest_hold_ts'],6*TF)
        touch=trans[trans.event_type.eq('RETEST_TOUCH_15M')].iloc[0]
        self.assertEqual(touch.ambiguity_note,'15M_OHLC_INTRABAR_ORDER_UNKNOWN')

    def test_no_retest_same_breakout_bar(self):
        trans,out=trace([101.3,101.6,101.8])
        self.assertIsNone(out['first_retest_touch_ts'])

    def test_wick_reversal_is_descriptive_after_completed_bar(self):
        trans,out=trace([99.7,99.6,99.3],
                        highs=[101.3,101.7,100.0],
                        lows=[99.6,99.4,99.1])
        self.assertEqual(out['wick_reversal_count'],2)
        self.assertEqual(out['first_wick_reversal_ts'],TF)
        self.assertEqual(trans.loc[trans.event_type.eq('WICK_REVERSAL_15M_DESCRIPTIVE'),
                                   'ambiguity_note'].iloc[0],
                         '15M_OHLC_INTRABAR_ORDER_UNKNOWN')

    def test_wick_inside_initial_partial_15m_candle_is_not_claimed(self):
        f=bars([99.3,99.1], highs=[101.4,100], lows=[99.2,98.9])
        trans,out=b.analyze_15m_sequence(f,event_id='x',region_id='x',visit_number=1,
            touch_open=4*M,source_end=2*TF,side='HIGH',approach='BELOW',
            band_low=100,band_high=101,pre_close=99.2)
        self.assertEqual(out['first_wick_reversal_ts'],None)

    def test_inside_three_closes_not_assumed_breakout(self):
        trans,out=trace([100.3,100.5,100.4])
        self.assertEqual(out['inside_acceptance_count'],1)
        self.assertEqual(out['first_resolution'],'UNRESOLVED')

    def test_unconventional_approach_not_rated_as_breakout(self):
        trans,out=trace([99.8,99.3,99.1],approach='ABOVE')
        self.assertEqual(out['first_resolution'],'NONCONVENTIONAL_APPROACH_NOT_RATED')
        self.assertEqual(out['breakout_count'],0)

    def test_censoring_is_not_failure(self):
        trans,out=trace([100.4,100.5,100.4],horizon=7)
        self.assertEqual(out['horizon_status'],'FREEZE_CENSORED')
        self.assertIsNone(out['first_failed_breakout_ts'])
        self.assertEqual(out['first_resolution'],'UNRESOLVED')

    def test_invalid_15m_gap_refused(self):
        f=bars([99,99,99]);f.loc[1,'timestamp']+=M
        with self.assertRaisesRegex(ValueError,'NONCONTIGUOUS'):
            b.analyze_15m_sequence(f,event_id='x',region_id='x',visit_number=1,
                touch_open=0,source_end=3*TF,side='HIGH',approach='BELOW',
                band_low=100,band_high=101,pre_close=99)

    def test_invalid_ohlc_refused(self):
        f=bars([99.0,99.1,99.2]);f.loc[1,'high']=98.0
        with self.assertRaisesRegex(ValueError,'INVALID_15M_OHLC'):
            b.analyze_15m_sequence(f,event_id='x',region_id='x',visit_number=1,
                touch_open=0,source_end=3*TF,side='HIGH',approach='BELOW',
                band_low=100,band_high=101,pre_close=99)


class DatasetGuards(unittest.TestCase):
    # Use 4 hours of continuous 1m candles; 15m bars are truly aggregated.
    def make_data(self, *,touch=True, stage_shift=0):
        size=15*16
        close=np.full(size,99.0)
        close[46:]=99.2
        high=close+0.1
        low=close-0.1
        if touch:high[45]=100.4
        full=pd.DataFrame({'timestamp':np.arange(size)*M,
              'open':close,'high':high,'low':low,'close':close})
        regions=pd.DataFrame([{'region_id':'msv2_x','side':'HIGH',
            'birth_known_ts':30*M,'band_low_birth':100.,'band_high_birth':101.}])
        visits=pd.DataFrame([{'region_id':'msv2_x','visit_number':1,
            'first_touch_ts':45*M,'end_known_ts':None}])
        stages=pd.DataFrame([{'region_id':'msv2_x','side':'HIGH',
            'known_from':(30+stage_shift)*M,'known_until':size*M+TF,
            'independent_extrema':2,'band_low':100.,'band_high':101.}])
        return full,regions,visits,stages

    def test_all_parent_visits_retained_and_15m_events_after_pre(self):
        a=self.make_data()
        pre,tr,post,summary=b.build_behavior_dataset(*a,source_start=0,source_end=240*M,
                                                        horizon_bars=4)
        self.assertEqual(len(pre),len(post))
        self.assertEqual(pre.iloc[0].pre_stage_status,'PRE_STAGE_PRESENT_TOUCH_NOT_YET_OBSERVED')
        self.assertEqual(post.iloc[0].eligibility,'ELIGIBLE_CURRENT_STAGE_1M_TOUCH')
        self.assertTrue((tr.confirmed_ts>pre.iloc[0].pre_cutoff_ts).all())
        self.assertEqual(summary['eligible_current_stage_contacts'],1)
        self.assertEqual(int(post.iloc[0].first_15m_confirmed_ts),60*M)
        self.assertEqual(int(pre.iloc[0].pre_last_15m_close_ts),45*M)
        self.assertTrue(int(pre.iloc[0].stage_known_from)<=45*M)

    def test_birth_contact_not_current_stage_not_eligible(self):
        full,r,v,s=self.make_data(touch=True)
        s.loc[0,'band_low']=102.;s.loc[0,'band_high']=103.
        pre,tr,post,summary=b.build_behavior_dataset(full,r,v,s,0,240*M)
        self.assertEqual(pre.iloc[0].pre_stage_status,'PRE_STAGE_PRESENT_TOUCH_NOT_YET_OBSERVED')
        self.assertEqual(post.iloc[0].eligibility,'BIRTH_BAND_TOUCH_NOT_CURRENT_STAGE_TOUCH')
        self.assertTrue(tr.empty)
        self.assertEqual(post.iloc[0].horizon_status,'NOT_ELIGIBLE')
        self.assertEqual(summary['excluded_birth_stage_contact_mismatch'],1)

    def test_no_stage_asof_pre_not_eligible(self):
        a=self.make_data(stage_shift=30)
        pre,tr,post,summary=b.build_behavior_dataset(*a,0,240*M)
        self.assertEqual(pre.iloc[0].pre_stage_status,'NO_GEOMETRIC_STAGE_AT_PRE')
        self.assertTrue(tr.empty)

    def test_future_1m_prices_do_not_change_pre_record(self):
        full,r,v,s=self.make_data()
        pre1,tr1,post1,_=b.build_behavior_dataset(full,r,v,s,0,240*M,horizon_bars=4)
        revised=full.copy()
        revised.loc[revised.timestamp>=60*M,['open','high','low','close']]=99.6
        pre2,tr2,post2,_=b.build_behavior_dataset(revised,r,v,s,0,240*M,horizon_bars=4)
        pd.testing.assert_frame_equal(pre1,pre2)

    def test_first_touch_bar_high_cannot_appear_in_pre(self):
        full,r,v,s=self.make_data()
        a=b.build_behavior_dataset(full,r,v,s,0,240*M,horizon_bars=4)
        full2=full.copy();full2.loc[45,'high']=105.5
        z=b.build_behavior_dataset(full2,r,v,s,0,240*M,horizon_bars=4)
        pd.testing.assert_frame_equal(a[0],z[0])
        self.assertEqual(a[2].iloc[0].eligibility,z[2].iloc[0].eligibility)
        self.assertNotIn('eligibility',a[0].columns)

    def test_contact_result_1m_can_change_without_changing_pre(self):
        full,r,v,s=self.make_data()
        pre_a,_,post_a,_=b.build_behavior_dataset(full,r,v,s,0,240*M,horizon_bars=4)
        altered=full.copy()
        altered.loc[45,'high']=99.1  # no longer touches PRE-stage band
        pre_b,_,post_b,_=b.build_behavior_dataset(altered,r,v,s,0,240*M,horizon_bars=4)
        pd.testing.assert_frame_equal(pre_a,pre_b)
        self.assertNotEqual(post_a.iloc[0].eligibility,post_b.iloc[0].eligibility)

    def test_next_visit_overlap_only_post(self):
        full,r,v,s=self.make_data()
        v=pd.concat([v,pd.DataFrame([{'region_id':'msv2_x','visit_number':2,
                         'first_touch_ts':70*M,'end_known_ts':None}])],ignore_index=True)
        full.loc[70,'high']=100.4
        pre,tr,post,_=b.build_behavior_dataset(full,r,v,s,0,240*M,horizon_bars=12)
        self.assertNotIn('overlaps_next_birth_visit_in_horizon',pre.columns)
        first=post.loc[post.visit_number.eq(1)].iloc[0]
        self.assertTrue(bool(first.overlaps_next_birth_visit_in_horizon))

    def test_immutable_manifest_hashes_outputs_and_refuses_overwrite(self):
        full,r,v,s=self.make_data()
        pre,tr,post,summary=b.build_behavior_dataset(full,r,v,s,0,240*M,horizon_bars=4)
        with tempfile.TemporaryDirectory() as tmp:
            parent=Path(tmp)/'parent';parent.mkdir()
            (parent/'manifest.json').write_text('"test"',encoding='utf-8')
            dest=Path(tmp)/'reports'/'v1'
            source={'start':0,'end':240*M,'source_sha256':'fixture',
                    'catalog_sha256':'fixture','source_hll_parity':'synthetic'}
            b._write_immutable(dest,pre,tr,post,summary,source,{'config':{}},parent)
            manifest=json.loads((dest/'manifest.json').read_text(encoding='utf-8'))
            self.assertTrue(manifest['immutable_run'])
            for name,sha in manifest['files_sha256'].items():
                self.assertEqual(b.digest(dest/name),sha)
            with self.assertRaises(FileExistsError):
                b._write_immutable(dest,pre,tr,post,summary,source,{'config':{}},parent)

    def test_parent_real_fixture_cardinality_if_available(self):
        parent=Path(__file__).parent/'fixture_v24a'
        if not (parent/'manifest.json').is_file():self.skipTest('fixture optional')
        from btc_multiswing_arrival_lab_v1 import _parent_run
        regions,visits,manifest,_=_parent_run(parent)
        self.assertEqual(len(regions),216)
        self.assertEqual(len(visits),4565)
        self.assertTrue(manifest['immutable_run'])


if __name__=='__main__':
    unittest.main()
