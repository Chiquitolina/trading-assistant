"""PRE review selection: new bands, reproducibility, and no POST leakage."""
from __future__ import annotations

import unittest

import pandas as pd

from dashboard.views import reaction_multiswing_execution_v01_view as ui


def fixtures(count=30):
    rows=[];visits=[]
    for n in range(count):
        event=f'visit_{n:02d}'
        rows.append(dict(event_id=event,region_id=f'band_{n:02d}',pre_cutoff_ts=(n+1)*86_400_000,
            flip_direction='RESISTANCE_TO_SUPPORT' if n%2 else 'SUPPORT_TO_RESISTANCE',
            reaction_pre='H1',band_age_hours=96+n,prior_h1_defenses=2,
            max_separation_pct=.5,pre_position='ABOVE' if n%2 else 'BELOW'))
        visits.append(dict(event_id=event,band_origin_side='HIGH' if n%2 else 'LOW',
                           approach_position='ABOVE' if n%2 else 'BELOW'))
    return pd.DataFrame(rows,columns=ui.REVIEW_COLUMNS),pd.DataFrame(visits,
        columns=['event_id','band_origin_side','approach_position'])


class ReviewBatch(unittest.TestCase):
    def test_only_eligible_visits_and_distinct_bands(self):
        eligible,_=fixtures()
        batch=ui.build_review_batch(eligible)
        self.assertEqual(len(batch),12)
        self.assertEqual(batch.region_id.nunique(),12)
        self.assertTrue(set(batch.event_id).issubset(eligible.event_id))

    def test_excludes_entire_reviewed_bands_not_just_bookmark_visits(self):
        eligible,_=fixtures()
        eligible.loc[0,'event_id']='different_visit_of_reviewed_band'
        batch=ui.build_review_batch(eligible,excluded_regions=['band_00','band_29'])
        self.assertTrue(set(batch.region_id).isdisjoint({'band_00','band_29'}))
        self.assertNotIn('different_visit_of_reviewed_band',batch.event_id.tolist())

    def test_first_eligible_visit_per_band(self):
        eligible,_=fixtures()
        later=eligible.iloc[[0]].copy()
        later['event_id']='later_visit';later['pre_cutoff_ts']=999*86_400_000
        batch=ui.build_review_batch(pd.concat([later,eligible],ignore_index=True),size=30)
        self.assertIn('visit_00',batch.event_id.tolist())
        self.assertNotIn('later_visit',batch.event_id.tolist())

    def test_row_order_does_not_change_sample(self):
        eligible,_=fixtures()
        before=ui.build_review_batch(eligible)
        after=ui.build_review_batch(eligible.sample(frac=1,random_state=3))
        pd.testing.assert_frame_equal(before,after)

    def test_date_rank_sample_includes_both_ends_and_is_chronological(self):
        eligible,_=fixtures()
        batch=ui.build_review_batch(eligible)
        self.assertEqual(batch.pre_cutoff_ts.iloc[0],eligible.pre_cutoff_ts.min())
        self.assertEqual(batch.pre_cutoff_ts.iloc[-1],eligible.pre_cutoff_ts.max())
        self.assertTrue(batch.pre_cutoff_ts.is_monotonic_increasing)
        self.assertEqual(batch.review_order.tolist(),list(range(1,13)))

    def test_future_labels_outcomes_and_trigger_counts_cannot_affect_selection(self):
        eligible,_=fixtures()
        before=ui.build_review_batch(eligible)
        eligible['gross_markout_pct']=range(len(eligible))
        eligible['future_event_type']='FUTURE_MUST_NOT_LEAK'
        eligible['trigger_count']=999
        after=ui.build_review_batch(eligible)
        pd.testing.assert_frame_equal(before,after)
        self.assertNotIn('FUTURE_MUST_NOT_LEAK',after.to_csv(index=False))

    def test_smaller_pool_is_not_filled_with_ineligible_visits(self):
        eligible,_=fixtures(4)
        batch=ui.build_review_batch(eligible)
        self.assertEqual(batch.event_id.tolist(),eligible.event_id.tolist())

    def test_empty_pool_does_not_restore_excluded_bands(self):
        eligible,_=fixtures(4)
        batch=ui.build_review_batch(eligible,excluded_regions=eligible.region_id)
        self.assertTrue(batch.empty)
        self.assertIn('review_order',batch.columns)

    def test_single_remaining_band(self):
        eligible,_=fixtures(1)
        self.assertEqual(ui.build_review_batch(eligible).event_id.tolist(),['visit_00'])

    def test_invalid_size_is_rejected(self):
        eligible,_=fixtures()
        for size in (0,-1,2.5,True):
            with self.subTest(size=size),self.assertRaises(ValueError):
                ui.build_review_batch(eligible,size=size)

    def test_missing_pre_identifiers_is_rejected(self):
        eligible,_=fixtures()
        with self.assertRaisesRegex(ValueError,'identificadores PRE'):
            ui.build_review_batch(eligible.drop(columns='pre_cutoff_ts'))


class ReviewSheet(unittest.TestCase):
    def test_sheet_uses_canonical_origin_and_approach_without_assigning_trade(self):
        eligible,visits=fixtures()
        batch=ui.build_review_batch(eligible)
        sheet=ui.review_sheet(batch,visits)
        self.assertEqual(sheet['Origen de banda'].iloc[0],'LOW')
        self.assertEqual(sheet['Llegada desde PRE'].iloc[0],'Abajo')
        self.assertTrue(sheet['Hipótesis LONG-SHORT (manual)'].eq('').all())

    def test_post_columns_are_never_copied_to_sheet(self):
        eligible,visits=fixtures()
        before=ui.review_sheet(ui.build_review_batch(eligible),visits)
        eligible['winner']=True;eligible['family']='FUTURE_MUST_NOT_LEAK'
        visits['future_price']=99999;visits['future_event']='FUTURE_MUST_NOT_LEAK'
        after=ui.review_sheet(ui.build_review_batch(eligible),visits)
        pd.testing.assert_frame_equal(before,after)

    def test_all_manual_fields_start_blank(self):
        eligible,visits=fixtures()
        sheet=ui.review_sheet(ui.build_review_batch(eligible),visits)
        manual=[c for c in sheet if c.endswith('(manual)')]
        self.assertEqual(len(manual),5)
        self.assertTrue(sheet[manual].eq('').all().all())

    def test_empty_sheet_keeps_schema(self):
        eligible,visits=fixtures()
        batch=ui.build_review_batch(eligible,excluded_regions=eligible.region_id)
        self.assertTrue(ui.review_sheet(batch,visits).empty)


if __name__=='__main__':
    unittest.main()
