#!/usr/bin/env python3
"""Tests causales, de censura y fronteras para visitas Multi-Swing V2.3."""
import unittest
import pandas as pd

from btc_multiswing_60d_explorer_v1 import M
from btc_multiswing_structural_visits_v23 import (
    audit_structural_visits, visit_gap_sensitivity,
)
from test_btc_multiswing_region_interactions_v22 import STAGE, BASE, bar


def fixture(length=320):
    # Extremadamente simple: todos los contactos puntuales y los demas
    # minutos completamente fuera de la banda [100,102].
    touches = {10, 11, 17, 18, 25, 50, 81, 143}
    rows = []
    for n in range(length):
        if n in touches:
            rows.append(bar(n, 99, 101, 98, 99.5))
        else:
            rows.append(bar(n, 99, 99.8, 98, 99))
    return pd.DataFrame(rows)


def run(gap=30, cutoff=320, data=None, horizons=(60,240,720,1440,2880), stage=STAGE):
    return audit_structural_visits(
        fixture() if data is None else data, stage, BASE+cutoff*M,
        gap_minutes=gap, horizons=horizons)


class StructuralVisitsV23(unittest.TestCase):
    def test_gap_sensitivity_not_a_predetermined_classification(self):
        expected = {5:5, 15:4, 30:2, 60:2}
        for gap, count in expected.items():
            with self.subTest(gap=gap):
                visits, outcomes, summary = run(gap)
                self.assertEqual(len(visits), count)
                self.assertEqual(summary['contact_episodes_v22'], 6)
                self.assertEqual(len(outcomes), count*5)

    def test_gap_threshold_equal_merges_greater_splits(self):
        visits, _, _ = run(5)
        # 10,11 and 17,18 (gap = 5) merge; 25 is 6-minute gap.
        self.assertEqual(visits.contact_episodes.tolist(), [2, 1, 1, 1, 1])
        self.assertEqual(int(visits.iloc[0].bridged_noncontact_minutes), 5)
        self.assertEqual(int(visits.iloc[0].touch_minutes), 4)

    def test_visit_end_is_causally_known_gap_plus_one_bars(self):
        v,_,_ = run(5)
        # Last touched 18th minute (closed at t=19); 6 non-contact
        # minutes 19..24 are needed: know closure at t=25, NOT t=19.
        self.assertEqual(int(v.iloc[0].last_touch_close_ts), BASE+19*M)
        self.assertEqual(int(v.iloc[0].end_known_ts), BASE+25*M)
        self.assertEqual(int(v.iloc[0].first_known_ts), BASE+11*M)
        self.assertEqual(v.iloc[0].status,'ENDED_BY_INACTIVITY')

    def test_arrival_and_exit_are_observations_not_defenses(self):
        v,_,_ = run(5)
        self.assertTrue((v.arrival_side=='BELOW').all())
        self.assertTrue((v.exit_side=='BELOW').all())
        self.assertTrue((v.in_visit_max_above_pct==0).all())
        self.assertTrue((v.in_visit_max_below_pct>0).all())

    def test_end_open_at_cutoff_even_if_last_touch_finished(self):
        v,o,s = run(5,cutoff=24)
        self.assertEqual(len(v),1)
        self.assertEqual(v.iloc[0].status,'OPEN_AT_CUTOFF')
        self.assertTrue(pd.isna(v.iloc[0].end_known_ts))
        self.assertEqual(set(o.status), {'VISIT_NOT_CLOSED'})
        self.assertTrue(o.max_above_band_pct.isna().all())
        self.assertEqual(s['visits_open'], 1)

    def test_retro_future_invariance(self):
        early = run(15, cutoff=45)
        later_frame=fixture(340)
        early_future=run(15, cutoff=45, data=later_frame)
        pd.testing.assert_frame_equal(early[0],early_future[0])
        pd.testing.assert_frame_equal(early[1],early_future[1])
        self.assertEqual(early[2],early_future[2])

    def test_next_visit_censors_post_until_revisit(self):
        v,o,_ = run(5,horizons=(60,))
        first=o.iloc[0]
        self.assertEqual(first.status,'NEXT_VISIT_CENSORED')
        self.assertEqual(first.observed_minutes,0)
        self.assertEqual(int(first.last_known_ts),BASE+25*M)
        self.assertTrue(pd.isna(first.max_above_band_pct))
        self.assertTrue(pd.isna(first.max_below_band_pct))

    def test_full_horizon_and_cutoff_censored(self):
        _,o,_ = run(30,horizons=(60,240))
        last=o.loc[o.visit_number.eq(2)]
        self.assertEqual(last.status.tolist(),['FULL_HORIZON','CUTOFF_CENSORED'])
        self.assertEqual(last.observed_minutes.tolist(),[60,145])
        self.assertAlmostEqual(float(last.iloc[0].max_below_band_pct),2.0)

    def test_cutoff_does_not_pretend_60_minutes_post(self):
        _,o,_=run(5,cutoff=200,horizons=(60,))
        last=o.iloc[-1]
        self.assertEqual(last.status,'CUTOFF_CENSORED')
        self.assertEqual(last.observed_minutes,50)

    def test_stage_is_frozen_and_outside_future_ignored(self):
        stage={**STAGE,'band_low':100.0,'band_high':102.0}
        v1,o1,s1=run(30,stage=stage)
        v2,o2,s2=run(30,stage={**stage,'region_id':'msv2_other'})
        self.assertEqual(s1['fixed_band_low'],s2['fixed_band_low'])
        self.assertEqual(s1['fixed_band_high'],s2['fixed_band_high'])
        self.assertNotEqual(v1.iloc[0].region_id,v2.iloc[0].region_id)
        self.assertEqual(s1['visits'],s2['visits'])

    def test_reject_invalid_params_and_incomplete_source(self):
        for g in (0,2,31,61,True,'30'):
            with self.subTest(gap=g), self.assertRaisesRegex(ValueError,'INVALID_VISIT_GAP'):
                run(g)
        for hs in ((30,), (60,60), [60], (), (60,10), (False,)):
            with self.subTest(horizons=hs), self.assertRaisesRegex(ValueError,'INVALID_VISIT_HORIZONS'):
                run(horizons=hs)
        bad=fixture().drop(index=11)
        with self.assertRaisesRegex(ValueError,'NONCONTIGUOUS'):
            run(data=bad)
        with self.assertRaisesRegex(ValueError,'INVALID_FIXED_REGION_STAGE'):
            run(stage={**STAGE,'band_low':104})

    def test_sensitivity_summary_matches_separate_audits(self):
        from btc_multiswing_region_interactions_v22 import audit_fixed_region
        _, contacts, _ = audit_fixed_region(fixture(), STAGE, BASE+320*M,
                                             horizon_minutes=60)
        sensitivity=visit_gap_sensitivity(contacts)
        self.assertEqual(sensitivity.gap_minutes.tolist(),[5,15,30,60])
        self.assertEqual(sensitivity.visits.tolist(),[5,4,2,2])
        self.assertEqual(visit_gap_sensitivity(contacts.iloc[:0]).visits.tolist(),
                         [0,0,0,0])

    def test_horizon_never_includes_future_high_low(self):
        base=fixture()
        early=run(30,cutoff=210,data=base,horizons=(60,))
        future=base.copy()
        future.loc[250,'high']=10000
        future.loc[250,'close']=10000
        future.loc[251,'open']=10000
        pd.testing.assert_frame_equal(early[0],run(30,cutoff=210,data=future,horizons=(60,))[0])
        pd.testing.assert_frame_equal(early[1],run(30,cutoff=210,data=future,horizons=(60,))[1])

    def test_no_live_or_hll_dependency(self):
        from pathlib import Path
        root=Path(__file__).parent
        source=(root/'btc_multiswing_structural_visits_v23.py').read_text()
        view=(root/'dashboard/views/reaction_multiswing_btc60d_view_v1.py').read_text()
        self.assertNotIn('from engine.live.',source)
        self.assertNotIn('from hll_',source)
        self.assertNotIn('from engine.live.',view)
        self.assertIn('audit_structural_visits(', view)
        self.assertIn('V2.3',view)


if __name__=='__main__':
    unittest.main()
