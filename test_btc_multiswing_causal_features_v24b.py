#!/usr/bin/env python3
"""Contract/regression checks for OFFLINE Multi-Swing V2.4B causal landmarks."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from btc_multiswing_60d_explorer_v1 import M
from btc_multiswing_information_universe_v24 import _write_run as parent_write, audit_universe
from btc_multiswing_causal_features_v24b import (
    FEATURE_COLUMNS, LANDMARKS, OUTCOME_COLUMNS, VERSION,
    _load_v24a, _region_dependency_components, _write_run, build_v24b, main,
)
from test_btc_multiswing_information_universe_v24 import (
    T0, sample_bars, sample_catalog,
)


def fixture(*, cutoff=400, bars=None, catalog=None, horizons=(60,240)):
    frame = sample_bars(cutoff) if bars is None else bars
    cat = sample_catalog() if catalog is None else catalog
    census = audit_universe(frame, cat, T0+cutoff*M,
        timeframes=('15m',), horizons=horizons)
    regions, visits, post, overlaps, meta = census
    cfg={'timeframes':meta['timeframes'],'side':meta['side'],
         'geometry':meta['geometry'],'gap_minutes':meta['gap_minutes'],
         'horizons_minutes':meta['horizons_minutes'],'cutoff':meta['cutoff']}
    result=build_v24b(frame,cat,regions,visits,overlaps,T0+cutoff*M,
                     config=cfg,parent_post_v24=post)
    return result,census,cfg


class CausalFeaturesV24B(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result,cls.census,cls.config=fixture()
        cls.features,cls.outcomes,cls.dependencies,cls.meta=cls.result

    def test_birth_landmark_exists_and_is_not_a_visit(self):
        births=self.features[self.features.landmark=='BIRTH_CONFIRMED']
        self.assertEqual(len(births),1)
        r=births.iloc[0]
        self.assertTrue(pd.isna(r.visit_number))
        self.assertTrue(pd.isna(r.first_touch_ts))
        self.assertEqual(int(r.region_age_minutes),0)
        self.assertEqual(int(r.stage_extrema_asof),2)
        self.assertEqual(r.visit_status_known,'NOT_APPLICABLE_AT_BIRTH')

    def test_first_touch_does_not_leak_visit_exit_or_future_duration(self):
        start=self.features[self.features.landmark=='FIRST_TOUCH_CONFIRMED']
        self.assertEqual(len(start),2)
        for col in ('visit_exit_side_known','visit_span_minutes_known',
                    'visit_touch_minutes_known','visit_contact_episodes_known',
                    'visit_bridged_minutes_known','visit_two_closes_above_known'):
            self.assertTrue(start[col].isna().all(),col)
        self.assertTrue((start.visit_status_known=='NOT_YET_CLOSED').all())
        self.assertTrue((start.prior_closed_visits_asof.iloc[:1]==0).all())

    def test_visit_closure_is_a_different_later_landmark(self):
        birth=self.features[self.features.landmark=='BIRTH_CONFIRMED']
        first=self.features[self.features.landmark=='FIRST_TOUCH_CONFIRMED']
        closed=self.features[self.features.landmark=='VISIT_CLOSED']
        self.assertEqual(len(birth),1)
        self.assertEqual(len(first),len(closed))
        self.assertTrue(all(int(x.known_ts)>int(first[first.visit_number==x.visit_number].iloc[0].known_ts)
                            for x in closed.itertuples(index=False)))
        self.assertTrue(closed.visit_touch_minutes_known.notna().all())
        self.assertEqual(int(closed.iloc[0].prior_closed_visits_asof),0)
        self.assertEqual(int(closed.iloc[1].prior_closed_visits_asof),1)

    def test_stage_maturity_only_after_third_confirmation(self):
        start=self.features[self.features.landmark=='FIRST_TOUCH_CONFIRMED']
        a,b=start.sort_values('visit_number').itertuples(index=False)
        self.assertEqual(int(a.stage_extrema_asof),2)
        self.assertEqual(int(b.stage_extrema_asof),3)
        self.assertEqual(int(a.physical_member_count_asof),2)
        self.assertEqual(int(b.physical_member_count_asof),3)

    def test_full_fixed_horizon_retains_revisits(self):
        a=self.outcomes[
            (self.outcomes.visit_number==1) &
            (self.outcomes.landmark=='VISIT_CLOSED') &
            (self.outcomes.horizon_minutes==240)].iloc[0]
        self.assertEqual(a.status,'FULL_HORIZON')
        self.assertTrue(bool(a.next_structural_visit_within_horizon))
        self.assertEqual(a.v23_stopped_on_revisit_status,'NEXT_VISIT_CENSORED')
        self.assertFalse(pd.isna(a.side_adjusted_close_return_pct))
        self.assertEqual(self.meta['counts']['closed_visit_fixed_full_vs_v23_censored'],1)

    def test_cutoff_censored_does_not_invent_zero_return(self):
        cens=self.outcomes[self.outcomes.status=='CUTOFF_CENSORED']
        self.assertGreater(len(cens),0)
        for col in ('max_above_birth_band_pct','max_below_birth_band_pct',
                    'favorable_excursion_pct','adverse_excursion_pct',
                    'side_adjusted_close_return_pct'):
            self.assertTrue(cens[col].isna().all(),col)
        self.assertTrue((cens.observed_minutes<cens.horizon_minutes).all())

    def test_known_time_all_pre_features_closed(self):
        self.assertTrue((self.features.known_ts % M == 0).all())
        self.assertTrue((self.features.stage_last_confirm_ts<=self.features.known_ts).all())
        self.assertTrue((self.features.confirmed_member_last_ts<self.features.known_ts).all())
        self.assertTrue((self.features.region_birth_ts<=self.features.known_ts).all())
        self.assertTrue((self.outcomes.evaluation_from_ts<=self.outcomes.last_known_ts).all())

    def test_no_retrospective_dependency_key_in_features(self):
        self.assertNotIn('retrospective_dependency_component',self.features.columns)
        self.assertNotIn('component_members',self.features.columns)
        self.assertEqual(self.dependencies.columns.tolist(),
            ['region_id','retrospective_dependency_component','component_members',
             'component_same_side_members','component_min_birth_ts',
             'component_max_birth_ts','causal_feature_no_component_id'])

    def test_future_extra_candles_do_not_change_asof_results(self):
        _,census,cfg=fixture()
        regions,visits,post,overlaps,_=census
        extended=sample_bars(470,contacts=True)
        # Future data supplied beyond same cutoff must be ignored completely.
        a=build_v24b(sample_bars(),sample_catalog(),regions,visits,overlaps,
                     T0+400*M,config=cfg,parent_post_v24=post)
        b=build_v24b(extended,sample_catalog(),regions,visits,overlaps,
                     T0+400*M,config=cfg,parent_post_v24=post)
        pd.testing.assert_frame_equal(a[0],b[0])
        pd.testing.assert_frame_equal(a[1],b[1])

    def test_mutating_future_candles_does_not_change_first_touch_features(self):
        regs,visits,post,overlaps,_=self.census
        original=sample_bars()
        changed=original.copy()
        # Alter prices after the first landmark but keep OHLC internally valid.
        j=changed.index[changed.timestamp>=T0+260*M]
        changed.loc[j,['open','high','low','close']]=[90.,91.,89.,90.]
        a=build_v24b(original,sample_catalog(),regs,visits,overlaps,
                     T0+400*M,config=self.config,parent_post_v24=post)
        b=build_v24b(changed,sample_catalog(),regs,visits,overlaps,
                     T0+400*M,config=self.config,parent_post_v24=post)
        early_a=a[0][a[0].known_ts<T0+260*M].reset_index(drop=True)
        early_b=b[0][b[0].known_ts<T0+260*M].reset_index(drop=True)
        pd.testing.assert_frame_equal(early_a,early_b)
        self.assertFalse(a[1].equals(b[1]))

    def test_no_contact_region_still_gets_birth_features(self):
        bars=sample_bars(400,contacts=False)
        (features,outcomes,dep,meta),census,_=fixture(bars=bars)
        self.assertEqual(len(census[1]),0)
        self.assertEqual(len(features),1)
        self.assertEqual(features.iloc[0].landmark,'BIRTH_CONFIRMED')
        self.assertEqual(meta['counts']['landmark_first_touch'],0)
        self.assertEqual(len(outcomes),2)

    def test_truncated_cutoff_omits_unfinished_visit_closure(self):
        (features,outcomes,_,meta),census,_=fixture(cutoff=110)
        self.assertEqual(int((features.landmark=='BIRTH_CONFIRMED').sum()),1)
        self.assertEqual(int((features.landmark=='FIRST_TOUCH_CONFIRMED').sum()),1)
        self.assertEqual(int((features.landmark=='VISIT_CLOSED').sum()),0)
        self.assertTrue((outcomes.status=='CUTOFF_CENSORED').any())

    def test_fail_closed_missing_minute_and_corrupt_parent_birth(self):
        regs,visits,post,overlaps,_=self.census
        with self.assertRaisesRegex(ValueError,'INCOMPLETE_1M'):
            build_v24b(sample_bars().drop(index=[150]),sample_catalog(),
                regs,visits,overlaps,T0+400*M,config=self.config,parent_post_v24=post)
        bad=regs.copy()
        bad.loc[0,'band_low_birth']=10
        with self.assertRaisesRegex(ValueError,'BIRTH_STAGE_CONFLICT'):
            build_v24b(sample_bars(),sample_catalog(),
                bad,visits,overlaps,T0+400*M,config=self.config,parent_post_v24=post)

    def test_reject_misaligned_v23_post_and_duplicate_visits(self):
        regs,visits,post,overlaps,_=self.census
        bad=post.copy()
        bad.loc[0,'evaluation_from_ts']=T0+40*M
        with self.assertRaisesRegex(ValueError,'CLOSE_ALIGNMENT_CONFLICT'):
            build_v24b(sample_bars(),sample_catalog(),
                regs,visits,overlaps,T0+400*M,config=self.config,parent_post_v24=bad)
        with self.assertRaisesRegex(ValueError,'CENSUS_NOT_MATCHING_V2'):
            build_v24b(sample_bars(),sample_catalog(),
                regs,pd.concat([visits,visits]).reset_index(drop=True),overlaps,
                T0+400*M,config=self.config,parent_post_v24=post)

    def test_retrospective_dependency_clustering_is_separate(self):
        b=pd.DataFrame([
            {'region_id':'a','side':'HIGH','birth_known_ts':T0},
            {'region_id':'b','side':'HIGH','birth_known_ts':T0+10*M},
            {'region_id':'c','side':'LOW','birth_known_ts':T0+20*M},
        ])
        e=pd.DataFrame([
            {'region_id':'b','preexisting_region_id':'a',
             'within_center_tolerance':True,'band_intersects':False},
        ])
        dep=_region_dependency_components(b,e)
        self.assertEqual(dep.loc[dep.region_id=='a','component_members'].iloc[0],2)
        self.assertEqual(dep.loc[dep.region_id=='b','component_members'].iloc[0],2)
        self.assertEqual(dep.loc[dep.region_id=='c','component_members'].iloc[0],1)

    def test_parent_hash_integrity_and_atomic_nonoverwrite(self):
        regions,visits,post,overlaps,parent_meta=self.census
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            source={'start':T0,'end':T0+400*M}
            parent=parent_write(root/'parent',
                (regions,visits,post,overlaps),parent_meta,source)
            r,v,p,o,m,manifest=_load_v24a(parent)
            self.assertEqual(len(r),1)
            self.assertEqual(len(v),2)
            target=_write_run(root/'child',self.result[:3],self.meta,
                              provenance=source,parent={'manifest_sha256':'test'})
            self.assertTrue((target/'manifest.json').exists())
            with self.assertRaises(FileExistsError):
                _write_run(target,self.result[:3],self.meta,
                           provenance=source,parent={})
            (parent/'visits_v24.csv').write_text('corrupt\n',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'HASH_MISMATCH'):
                _load_v24a(parent)

    def test_plan_does_not_read_sources_or_create_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            self.assertEqual(main(['--plan','--project-root',str(root)]),0)
            self.assertEqual(list(root.iterdir()),[])

    def test_columns_and_counts_are_self_consistent(self):
        self.assertEqual(self.features.columns.tolist(),FEATURE_COLUMNS)
        self.assertEqual(self.outcomes.columns.tolist(),OUTCOME_COLUMNS)
        self.assertEqual(len(self.features),self.meta['counts']['landmarks'])
        self.assertEqual(len(self.outcomes),self.meta['counts']['outcome_rows'])
        self.assertEqual(len(self.outcomes),len(self.features)*2)
        self.assertEqual(set(self.features.landmark),set(LANDMARKS))
        self.assertEqual(self.meta['version'],VERSION)


if __name__=='__main__':
    unittest.main()
