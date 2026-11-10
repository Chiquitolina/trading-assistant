#!/usr/bin/env python3
"""V2.4A: causal birth-stage census, coverage, censoring and atomic artifact tests."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from btc_multiswing_60d_explorer_v1 import M
from btc_multiswing_information_universe_v24 import (
    _birth_overlap, _classified_followup, _write_run, audit_universe,
    birth_stage_universe, main,
)

T0 = 1_790_000_000_000 // M * M


def sample_catalog(third=True):
    records = []
    for name, pivot, available, price in (
            [('p1',2,5,100.0),('p2',70,72,100.1)] +
            ([('p3',150,151,100.05)] if third else [])):
        records.append({
            'pivot_id':name,'timeframe':'15m','side':'HIGH',
            'price':price,'pivot_ts':T0+pivot*M,
            'available_ts':T0+available*M,
            'physical_id':'phys_'+name,
            'physical_identity_state':'VERIFIED',
            'physical_minute_ts':T0+pivot*M,
        })
    return pd.DataFrame(records)


def sample_bars(end_minute=400, contacts=True):
    rows=[]
    for i in range(end_minute):
        high = 100.05 if contacts and i in (90,91,200) else 99.8
        rows.append({'timestamp':T0+i*M,'open':99.5,'high':high,
                     'low':99.2,'close':99.5})
    return pd.DataFrame(rows)


def run_test(full=None,catalog=None,cutoff_min=400,**kw):
    return audit_universe(
        sample_bars() if full is None else full,
        sample_catalog() if catalog is None else catalog,
        T0+cutoff_min*M,timeframes=('15m',),horizons=(60,240),**kw)


class BirthStagesV24(unittest.TestCase):
    def test_birth_geometry_ignores_retrospective_final_stage(self):
        regions=pd.DataFrame([{'region_id':'r','side':'HIGH','born_ts':T0+72*M,
                              'band_low':50.,'band_high':500.,'independent_extrema':99}])
        stages=pd.DataFrame([
            {'region_id':'r','side':'HIGH','known_from':T0+72*M,
             'band_low':100.,'band_high':100.1,'independent_extrema':2},
            {'region_id':'r','side':'HIGH','known_from':T0+151*M,
             'band_low':90.,'band_high':120.,'independent_extrema':3},
        ])
        got=birth_stage_universe(regions,stages,T0+400*M)
        self.assertEqual(float(got.iloc[0].band_low_birth),100.)
        self.assertEqual(float(got.iloc[0].band_high_birth),100.1)
        self.assertEqual(int(got.iloc[0].extrema_at_birth),2)

    def test_simultaneous_birth_stage_uses_available_same_minute(self):
        born=T0+72*M
        regions=pd.DataFrame([{'region_id':'r','side':'HIGH','born_ts':born}])
        stages=pd.DataFrame([
            {'region_id':'r','side':'HIGH','known_from':born,
             'band_low':100.,'band_high':100.1,'independent_extrema':2},
            {'region_id':'r','side':'HIGH','known_from':born,
             'band_low':100.,'band_high':100.2,'independent_extrema':3},
        ])
        b=birth_stage_universe(regions,stages,T0+400*M)
        self.assertEqual(int(b.iloc[0].extrema_at_birth),3)

    def test_future_stage_is_refused(self):
        regions=pd.DataFrame([{'region_id':'r','side':'HIGH','born_ts':T0+72*M}])
        stages=pd.DataFrame([{'region_id':'r','side':'HIGH',
                              'known_from':T0+401*M,'band_low':100.,
                              'band_high':100.1,'independent_extrema':2}])
        with self.assertRaises(ValueError):
            birth_stage_universe(regions,stages,T0+400*M)

    def test_overlap_counts_only_preexisting_births(self):
        births=pd.DataFrame([
            dict(region_id='early',side='HIGH',birth_known_ts=T0+50*M,
                 band_low_birth=100.,band_high_birth=101.),
            dict(region_id='middle',side='HIGH',birth_known_ts=T0+100*M,
                 band_low_birth=100.1,band_high_birth=101.1),
            dict(region_id='late',side='LOW',birth_known_ts=T0+200*M,
                 band_low_birth=100.2,band_high_birth=100.9),
        ])
        pairs, info=_birth_overlap(births,0.2)
        self.assertEqual(info['early'],(0,0,0))
        self.assertEqual(info['middle'][1],1)
        self.assertEqual(info['late'][1],2)
        self.assertEqual(len(pairs),3)
        self.assertTrue((pairs.preexisting_birth_ts <= pairs.region_birth_ts).all())


class UniversalCensusV24(unittest.TestCase):
    def test_real_v2_v23_pipeline_with_birth_band(self):
        regions,visits,post,overlaps,meta=run_test()
        self.assertEqual(len(regions),1)
        r=regions.iloc[0]
        self.assertEqual(int(r.extrema_at_birth),2)
        self.assertAlmostEqual(float(r.band_low_birth),100.)
        self.assertAlmostEqual(float(r.band_high_birth),100.1)
        self.assertEqual(int(r.birth_known_ts),T0+72*M)
        self.assertEqual(int(r.visits),2)
        self.assertEqual(int(r.visits_closed),2)
        self.assertEqual(int(r.contact_episodes),2)
        self.assertEqual(len(visits),2)
        self.assertEqual(len(post),4)
        self.assertEqual(int(r.full_outcomes_60m),2)
        self.assertEqual(int(r.full_outcomes_240m),0)
        self.assertEqual(int(r.censored_outcomes),2)
        self.assertEqual(meta['counts']['regions_census'],1)
        self.assertEqual(meta['counts']['total_post_horizon_rows'],4)
        self.assertEqual(len(overlaps),0)

    def test_no_visit_is_not_classified_bad(self):
        regions,visits,post,_,meta=run_test(full=sample_bars(400,contacts=False))
        self.assertEqual(len(regions),1)
        self.assertEqual(len(visits),0)
        self.assertEqual(len(post),0)
        self.assertEqual(regions.iloc[0].evidence_status,'NO_VISITS')
        self.assertEqual(regions.iloc[0].observation_status,'NO_CONTACT_SHORT_EXPOSURE')
        self.assertEqual(meta['counts']['regions_without_visits'],1)

    def test_no_contact_long_exposure_is_not_edge_label(self):
        self.assertEqual(_classified_followup(2880,pd.DataFrame()),
                         'NO_CONTACT_WITH_48H_EXPOSURE')
        self.assertEqual(_classified_followup(0,pd.DataFrame()),
                         'NO_CLOSED_1M_FOLLOWUP')

    def test_cutoff_censors_an_open_visit(self):
        frame=sample_bars(91)
        regions,visits,post,_,_=run_test(full=frame,cutoff_min=91)
        self.assertEqual(len(regions),1)
        self.assertEqual(len(visits),1)
        self.assertEqual(visits.iloc[0]['status'],'OPEN_AT_CUTOFF')
        self.assertEqual(regions.iloc[0]['observation_status'],'ONLY_OPEN_VISITS')
        self.assertTrue((post.status=='VISIT_NOT_CLOSED').all())
        self.assertEqual(int(regions.iloc[0]['full_outcomes_60m']),0)

    def test_future_extra_candle_and_pivot_cannot_change_report(self):
        full=sample_bars()
        cat=sample_catalog()
        baseline=run_test(full=full,catalog=cat)
        altered=pd.concat([full,pd.DataFrame([{
            'timestamp':T0+400*M,'open':10000.,'high':10000.,
            'low':10000.,'close':10000.}])],ignore_index=True)
        extra=cat.iloc[[0]].copy()
        extra['pivot_id']='future'
        extra['physical_id']='phys_future'
        extra['available_ts']=T0+405*M
        extra['pivot_ts']=T0+401*M
        extra['physical_minute_ts']=T0+401*M
        mutated=run_test(full=altered,catalog=pd.concat([cat,extra],ignore_index=True))
        for a,b in zip(baseline[:4],mutated[:4]):
            pd.testing.assert_frame_equal(a,b)
        self.assertEqual(baseline[4],mutated[4])

    def test_missing_minute_fails_closed(self):
        missing=sample_bars().query('timestamp != @T0 + 123*@M').copy()
        with self.assertRaisesRegex(ValueError,'NONCONTIGUOUS'):
            run_test(full=missing)

    def test_invalid_settings_and_future_cutoff_rejected(self):
        with self.assertRaisesRegex(ValueError,'INVALID_V24_VISIT_GAP'):
            run_test(gap_minutes=6)
        with self.assertRaisesRegex(ValueError,'INVALID_V24_TIMEFRAMES'):
            audit_universe(sample_bars(),sample_catalog(),T0+400*M,
                           timeframes=('15m','15m'))
        with self.assertRaisesRegex(ValueError,'CUTOFF_OUTSIDE_FREEZE'):
            run_test(cutoff_min=401)

    def test_no_future_members_update_birth_features(self):
        early,_,_,_,_=run_test(catalog=sample_catalog(third=False))
        extended,_,_,_,_=run_test(catalog=sample_catalog(third=True))
        for field in ('birth_known_ts','band_low_birth','band_high_birth',
                      'extrema_at_birth','band_width_pct_birth'):
            self.assertEqual(early.iloc[0][field],extended.iloc[0][field])

    def test_write_is_atomic_and_never_overwrites_existing(self):
        outputs=run_test()
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'v24'
            written=_write_run(out,outputs[:4],outputs[4],{'source_sha256':'S'})
            self.assertTrue((written/'manifest.json').exists())
            manifest=json.loads((written/'manifest.json').read_text())
            self.assertIn('regions_v24.csv',manifest['sha256'])
            self.assertEqual(len(pd.read_csv(out/'regions_v24.csv')),1)
            with self.assertRaises(FileExistsError):
                _write_run(out,outputs[:4],outputs[4],{})
            self.assertFalse(any(p.name.startswith('.v24a_stage_')
                                 for p in Path(td).iterdir()))

    def test_plan_does_not_read_freeze_or_create_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(main(['--plan','--project-root',td]),0)
            self.assertFalse((Path(td)/'reports').exists())


if __name__=='__main__':
    unittest.main()
