"""Offline causal unit tests for HLL swing consolidation (no VPS needed)."""
import copy
import unittest

import pandas as pd

import hll_swing_topology_pre as mod

TF = mod.TF


def p(kind, i, price, delay=3):
    return dict(kind=('HIGH_2X2' if kind == 'H' else 'LOW_CONFIRMED_PRE'),
                open_ts=i * TF, known_closed_ts=(i + delay) * TF,
                price=price, pre_closed_ts=30 * TF)


class StructuralSwingTests(unittest.TestCase):
    def setUp(self):
        self.pre = 30 * TF
        self.atr = 1.0
        self.data = [p('L', 2, 100), p('H', 5, 108),
                     p('L', 8, 104), p('H', 11, 110),
                     p('L', 14, 107), p('H', 18, 111)]

    def test_confirmed_pivots_and_delay(self):
        rows, ambiguities = mod.load_pivots(self.data, self.pre, self.atr)
        self.assertEqual(len(rows), 6)
        self.assertEqual(ambiguities, [])
        self.assertEqual(rows[0].known_ts, 5*TF)

    def test_no_unconfirmed_post_pivot_accepted(self):
        later = p('H', 28, 112)
        with self.assertRaisesRegex(ValueError, 'INVALID_CONFIRMED_PIVOT'):
            mod.load_pivots(self.data + [later], self.pre, self.atr)

    def test_same_bar_high_low_ambiguous(self):
        same = [p('H', 21, 112), p('L', 21, 102)]
        rows, ambiguous = mod.load_pivots(self.data+same, self.pre, self.atr)
        self.assertEqual(ambiguous, [21*TF])
        self.assertEqual(len(rows), 6)

    def test_duplicate_raises(self):
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_PIVOT'):
            mod.load_pivots(self.data+[self.data[0]], self.pre, self.atr)

    def test_major_swing_known_after_opposite_pivot(self):
        rows, _ = mod.load_pivots(self.data, self.pre, self.atr)
        conf, pending = mod.structural_swings(rows, 1.0, 1.0)
        self.assertEqual(conf[0]['kind'], 'L')
        self.assertEqual(conf[0]['swing_known_ts'], 8*TF)
        self.assertEqual(conf[1]['kind'], 'H')
        self.assertEqual(conf[1]['swing_known_ts'], 11*TF)
        self.assertEqual(pending['kind'], 'H')
        self.assertEqual(pending['status'], 'PIVOT_ONLY_UNCONFIRMED_SWING')

    def test_small_reversals_not_major_swings(self):
        rows, _ = mod.load_pivots(self.data, self.pre, self.atr)
        conf, pending = mod.structural_swings(rows, 1.0, 20.0)
        self.assertEqual(conf, [])
        self.assertEqual(pending['kind'], 'L')

    def test_same_kind_extreme_is_replaced(self):
        rows, _ = mod.load_pivots([p('L', 2, 100),p('L', 3, 99),p('H', 7,105)],self.pre,1.)
        conf, _=mod.structural_swings(rows,1.,1.)
        self.assertEqual(conf[0]['price'],99)
        self.assertEqual(conf[0]['open_ts'],3*TF)

    def test_sequence_order_and_provisional_h2(self):
        rows, _=mod.load_pivots(self.data, self.pre, 1.)
        conf, pending=mod.structural_swings(rows,1.,1.)
        seq=mod.enumerate_two_leg_sequences(conf,pending,1.,112.,113.,self.pre)
        self.assertGreaterEqual(len(seq),1)
        last=seq[-1]
        self.assertEqual(last['h2_status'],'PIVOT_ONLY_UNCONFIRMED_SWING')
        self.assertFalse(last['full_structural_confirmation_pre'])
        self.assertGreater(last['second_rise_atr'],0)
        self.assertLessEqual(last['h2_pivot_known_ts'], self.pre)

    def test_provisional_h2_kept_but_flagged(self):
        rows,_=mod.load_pivots(self.data[:4],self.pre,1.)
        conf,pending=mod.structural_swings(rows,1.,1.)
        seq=mod.enumerate_two_leg_sequences(conf,pending,1.,112.,113.,self.pre)
        self.assertEqual(len(seq),1)
        self.assertEqual(seq[0]['h2_status'],'PIVOT_ONLY_UNCONFIRMED_SWING')
        self.assertIsNone(seq[0]['h2_swing_known_ts'])

    def test_no_false_pattern_without_lhlh(self):
        rows,_=mod.load_pivots(self.data[:3],self.pre,1.)
        conf,pending=mod.structural_swings(rows,1.,1.)
        self.assertEqual(mod.enumerate_two_leg_sequences(conf,pending,1.,112.,113.,self.pre),[])

    def test_input_order_invariance(self):
        rows, _=mod.load_pivots(self.data,self.pre,1.)
        other, _=mod.load_pivots(list(reversed(self.data)),self.pre,1.)
        self.assertEqual(mod.structural_swings(rows,1.,1.),mod.structural_swings(other,1.,1.))

    def test_post_extension_invariance(self):
        rows,_=mod.load_pivots(self.data,self.pre,1.)
        conf,pending=mod.structural_swings(rows,1.,1.)
        before=mod.enumerate_two_leg_sequences(conf,pending,1.,112.,113.,self.pre)
        # POST rows are not allowed in the PRE-only input and cannot alter output.
        extra=p('L',30,80)
        with self.assertRaisesRegex(ValueError,'INVALID_CONFIRMED_PIVOT'):
            mod.load_pivots(self.data+[extra],self.pre,1.)
        rows2,_=mod.load_pivots(self.data,self.pre,1.)
        conf2,pending2=mod.structural_swings(rows2,1.,1.)
        after=mod.enumerate_two_leg_sequences(conf2,pending2,1.,112.,113.,self.pre)
        self.assertEqual(before,after)

    def test_analyze_does_not_use_labels_for_selection(self):
        rows=[]
        for d in self.data:
            x=dict(d);x['attempt_id']='abc'; x['symbol']='TEST'; rows.append(x)
        base=dict(attempt_id='abc',symbol='TEST',pre_closed_ts=self.pre,pre_utc='',
                  atr_pre=1.,zone_low=112.,zone_high=113.,visual_label='GOOD')
        a=pd.DataFrame([base]);piv=pd.DataFrame(rows)
        before=mod.analyze(a,piv,thresholds=(1.,))
        a.loc[0,'visual_label']='BAD'
        after=mod.analyze(a,piv,thresholds=(1.,))
        self.assertEqual(before[0].total_LHLH_chains.tolist(),after[0].total_LHLH_chains.tolist())
        self.assertEqual(before[2].second_rise_atr.tolist(),after[2].second_rise_atr.tolist())

    def test_analyze_errors_missing_pivots(self):
        a=pd.DataFrame([dict(attempt_id='missing',symbol='TEST',pre_closed_ts=self.pre,
                             atr_pre=1.,zone_low=112.,zone_high=113.)])
        piv=pd.DataFrame([dict(attempt_id='other',**self.data[0])])
        with self.assertRaisesRegex(ValueError,'PIVOT_WITHOUT_SEGMENT'):
            mod.analyze(a,piv)

    def test_invalid_price(self):
        bad=copy.deepcopy(self.data)
        bad[0]['price']=float('nan')
        with self.assertRaisesRegex(ValueError,'INVALID_CONFIRMED_PIVOT'):
            mod.load_pivots(bad,self.pre,1.)


if __name__=='__main__':
    unittest.main()
