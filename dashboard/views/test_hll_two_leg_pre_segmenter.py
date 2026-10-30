#!/usr/bin/env python3
"""Offline research unit tests; run from VPS project root or anywhere."""
import json
import unittest

import numpy as np
import pandas as pd

try:
    from .hll_two_leg_pre_segmenter import (TF, prefix_candles, confirmed_pivot_highs,
         segment_one, select_candidates, SEED)
except ImportError:
    from hll_two_leg_pre_segmenter import (TF, prefix_candles, confirmed_pivot_highs,
         segment_one, select_candidates, SEED)


def example():
    ts=np.arange(96,dtype='int64')*TF
    bars=pd.DataFrame(dict(timestamp=ts,open=np.full(96,106.),
        high=np.full(96,107.),low=np.full(96,105.),close=np.full(96,106.)))
    for j,v in [(5,100),(10,101),(15,102),(24,104),(36,105),(51,106)]:
        bars.loc[j,'low']=v
    bars.loc[19,'high']=115.
    bars.loc[92,'high']=116.
    # Separating correction below the first peak (but not below HL).
    bars.loc[20:23,'high']=108.
    bars.loc[23,'low']=105.
    rows=[(5,100),(10,101),(15,102),(24,104),(36,105),(51,106)]
    pivots=[dict(open_ts=j*TF,known_closed_ts=(j+3)*TF,low=v)
            for j,v in rows]
    row=dict(pre_closed_ts=96*TF,initial_break_closed_ts=24*TF,
        new_hl_open_ts=24*TF,new_hl_known_closed_ts=27*TF,new_hl_low=104.,
        anchor_first_ts=5*TF,anchor_third_ts=15*TF,anchor_confirmed_ts=18*TF,
        atr_pre=1.0,zone_low=115.,zone_high=115.5,
        pre96_pivot_lows_json=json.dumps(pivots))
    return row,bars


class TwoLegCausalTests(unittest.TestCase):
    def test_timestamp_boundary_equal_break_hl_is_valid(self):
        row,bars=example()
        f,ext=segment_one(row,bars)
        self.assertTrue(f['break_at_hl_open'])
        self.assertEqual(len([p for p in ext if p['kind']=='CORRECTION_HL']),1)

    def test_different_break_before_hl_valid(self):
        row,bars=example()
        row['initial_break_closed_ts']=23*TF
        f,_=segment_one(row,bars)
        self.assertFalse(f['break_at_hl_open'])

    def test_no_post_candles_influence_pre(self):
        row,bars=example()
        original,_=segment_one(row,bars)
        future=bars.copy()
        future=pd.concat([future,pd.DataFrame(dict(timestamp=np.arange(96,140)*TF,
            open=100000.,high=200000.,low=100.,close=150000.))],ignore_index=True)
        updated,_=segment_one(row,future)
        self.assertEqual(set(original),set(updated))
        for key in original:
            if isinstance(original[key],float) and np.isnan(original[key]):
                self.assertTrue(np.isnan(updated[key]),key)
            else:
                self.assertEqual(original[key],updated[key],key)

    def test_future_known_hl_not_accepted(self):
        row,bars=example()
        row['new_hl_known_closed_ts']=97*TF
        with self.assertRaisesRegex(ValueError,'INVALID_CAUSAL_ORDER'):
            segment_one(row,bars)

    def test_reject_gap(self):
        row,bars=example()
        with self.assertRaisesRegex(ValueError,'GAP_30M_PRE'):
            segment_one(row,bars.drop(index=80))

    def test_reject_double_timestamp(self):
        row,bars=example()
        with self.assertRaisesRegex(ValueError,'GAP_30M_PRE'):
            segment_one(row,pd.concat([bars,bars.iloc[[25]]],ignore_index=True))

    def test_pivot_confirmation_not_early(self):
        row,bars=example()
        # Peak at index 92 confirmed when index 94 closed, at 95*TF.
        early=confirmed_pivot_highs(bars,94*TF)
        late=confirmed_pivot_highs(bars,96*TF)
        self.assertNotIn(92*TF,[p['open_ts'] for p in early])
        self.assertIn(92*TF,[p['open_ts'] for p in late])

    def test_unconfirmed_pivot_lows_not_counted(self):
        row,bars=example()
        pivots=json.loads(row['pre96_pivot_lows_json'])
        pivots.append(dict(open_ts=87*TF,known_closed_ts=100*TF,low=107.))
        row['pre96_pivot_lows_json']=json.dumps(pivots)
        f,_=segment_one(row,bars)
        self.assertEqual(f['second_hl_pivots'],3)
        self.assertEqual(f['second_hl_up_steps'],2)

    def test_first_peak_and_correction_order(self):
        row,bars=example()
        f,ex=segment_one(row,bars)
        events={p['kind']:p for p in ex if p['kind']!='SECOND_PIVOT_LOW'}
        self.assertLess(events['FIRST_LOW']['open_ts'],events['FIRST_PEAK']['open_ts'])
        self.assertLess(events['FIRST_PEAK']['open_ts'],events['CORRECTION_HL']['open_ts'])
        self.assertGreater(f['correction_atr'],0)

    def test_earlier_deeper_trough_is_a_flag_not_an_exclusion(self):
        row,bars=example()
        bars.loc[22,'low']=103.
        f,ex=segment_one(row,bars)
        self.assertEqual(f['phase_status'],'EARLIER_LOWER_CORRECTION_TROUGH')
        self.assertTrue(f['correction_trough_before_audit_hl'])
        self.assertAlmostEqual(f['audit_hl_above_correction_trough_atr'],1.)
        self.assertFalse(f['correction_bar_trough_pivot_confirmed_pre'])
        troughs=[v for v in ex if v['kind']=='CORRECTION_BAR_TROUGH']
        self.assertEqual(troughs[0]['open_ts'],22*TF)
        self.assertFalse(troughs[0]['confirmed_pre'])

    def test_unresolved_first_rise_is_censored_not_excluded(self):
        row,bars=example()
        bars.loc[5,'high']=120.  # Max is exactly on starting low anchor.
        for j in (10,15):
            bars.loc[j,'low']=105.
        pivots=json.loads(row['pre96_pivot_lows_json'])
        for p in pivots:
            if p['open_ts'] in (10*TF,15*TF):
                p['low']=105.
        row['pre96_pivot_lows_json']=json.dumps(pivots)
        f,ex=segment_one(row,bars)
        self.assertFalse(f['first_rise_visible_in_pre_window'])
        self.assertEqual(f['phase_status'],'FIRST_RISE_UNRESOLVED')
        self.assertTrue(np.isnan(f['first_rise_atr']))
        self.assertTrue(np.isnan(f['first_rise_bars']))
        self.assertEqual(len([v for v in ex if v['kind']=='CORRECTION_HL']),1)

    def test_combined_missing_first_rise_and_earlier_trough(self):
        row,bars=example()
        bars.loc[5,'high']=120.
        bars.loc[22,'low']=103.
        f,_=segment_one(row,bars)
        self.assertEqual(
            f['phase_status'], 'FIRST_RISE_UNRESOLVED_AND_EARLIER_LOWER_TROUGH')
        self.assertFalse(f['first_rise_visible_in_pre_window'])
        self.assertTrue(f['correction_trough_before_audit_hl'])

    def test_existing_strict_case_has_no_phase_warning(self):
        row,bars=example()
        f,_=segment_one(row,bars)
        self.assertEqual(f['phase_status'],'AUDIT_HL_IS_CORRECTION_TROUGH')
        self.assertTrue(f['first_rise_visible_in_pre_window'])
        self.assertEqual(f['audit_hl_above_correction_trough_atr'],0)

    def test_peak_high_confirmed_pre_flag(self):
        row,bars=example()
        f,_=segment_one(row,bars)
        self.assertTrue(f['first_peak_confirmed_pre'])
        self.assertTrue(f['second_peak_confirmed_pre'])

    def test_wrong_pivot_low_rejected(self):
        row,bars=example()
        row['new_hl_low']=104.7
        with self.assertRaisesRegex(ValueError,'NEW_HL_PIVOT_MISMATCH'):
            segment_one(row,bars)

    def test_select_one_attempt_per_level(self):
        attempts=[SEED,'a'*24,'b'*24]
        rows=[]
        for ix,attempt in enumerate(attempts):
            for w in [24,48,96]:
                rows.append(dict(attempt_id=attempt,symbol='XUSDT',level_id='L1' if ix<2 else 'L2',
                    window_30m=w,line_state='INICIAL_ROTA' if w==96 else 'INICIAL_INTACTA',
                    sequence='ROTA_NUEVO_HL_ESTRICTO',slope_reduced=True,
                    zone_reapproach_before_breakout=True,initial_slope_atr_per_bar=.3+ix*.1,
                    slope_ratio_to_initial=.4,low_vs_last_anchor_atr=.2,
                    pre_closed_ts=96*TF,initial_break_closed_ts=24*TF,
                    new_hl_open_ts=24*TF,new_hl_known_closed_ts=27*TF,new_hl_low=104.,
                    anchor_first_ts=5*TF,anchor_third_ts=15*TF,anchor_confirmed_ts=18*TF,
                    original_pre_pattern='ASCENDING_CLEAN'))
        c=pd.DataFrame([dict(attempt_id=key,atr_pre=1.,zone_low=115.,zone_high=115.5,
            pre96_pivot_lows_json='[]') for key in attempts])
        selected=select_candidates(pd.DataFrame(rows),c)
        self.assertEqual(len(selected),2)
        self.assertIn(SEED,set(selected.attempt_id))

    def test_pre_prefix_strict_last_bar(self):
        row,bars=example()
        sub=prefix_candles(bars,96*TF)
        self.assertEqual(int(sub.timestamp.iloc[-1]),95*TF)


if __name__=='__main__':
    unittest.main(verbosity=2)
