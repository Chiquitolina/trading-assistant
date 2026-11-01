"""Regression test for the additive HLL maturity controls."""
import csv
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd
import hll_pre_maturity_metrics_v1 as m
import hll_structural_discovery_view as view


class MaturityViewTests(unittest.TestCase):
    def _fixture(self,td):
        root=Path(td)
        attempts=[dict(attempt_id=aid,symbol='TESTUSDT',level_id='L',pre_closed_ts=1800000000000+i*m.TF,
                       status='COMPLETE_PRE') for i,aid in enumerate(['aaa','bbb'])]
        src=pd.DataFrame(attempts)
        rows=[]
        for i,a in enumerate(attempts):
            r={k:'' for k in m.FIELDS}
            r.update(a,episode_id='episode1',episode_ordinal=i+1,is_first_in_episode=int(i==0),
                     status_96='COMPLETE_PRE',status_288='INCOMPLETE_PRE_288')
            r['near_zone_share_48_half_atr']=0.5+i*.1
            rows.append(r)
        src_file=root/'attempt_maturity_pre.csv'
        epp=root/'episode_first_maturity_pre.csv'
        with src_file.open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=m.FIELDS);w.writeheader();w.writerows(rows)
        with epp.open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=m.EP_FIELDS);w.writeheader()
            w.writerow({'first_attempt_id':'aaa',**rows[0]})
        dm={'output_sha256':{'attempts_pre.csv':'original-discovery'}}
        om={'heuristic_episodes_gap24_cap96':1,
            'outputs_sha256':{'attempt_episode_map.csv':'original-overlap'}}
        man={'version':'HLL_PRE_MATURITY_METRICS_V1','mode':'OFFLINE_READ_ONLY',
             'reads_post':False,'reads_visual_labels':False,'quality_score_generated':False,
             'attempts':2,'episodes':1,
             'source_sha256':{'discovery_attempts':'original-discovery','episode_map':'original-overlap'},
             'outputs_sha256':{'attempt_maturity_pre.csv':m.sha(src_file),
                               'episode_first_maturity_pre.csv':m.sha(epp)}}
        (root/'manifest.json').write_text(json.dumps(man))
        return root,src,dm,om

    def test_valid_metrics_join_by_attempt_id(self):
        with tempfile.TemporaryDirectory() as td:
            root,src,dm,om=self._fixture(td)
            df=view.load_maturity(root,src,dm,om)
            self.assertEqual(len(df),2)
            self.assertEqual(df.attempt_id.tolist(),['aaa','bbb'])
            self.assertAlmostEqual(float(df.iloc[0].near_zone_share_48_half_atr),.5)
            self.assertTrue(pd.isna(df.iloc[0].near_zone_share_288_half_atr))

    def test_tampered_export_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root,src,dm,om=self._fixture(td)
            (root/'attempt_maturity_pre.csv').write_text('injected')
            with self.assertRaisesRegex(ValueError,'Hash'):
                view.load_maturity(root,src,dm,om)

    def test_post_or_visual_labels_manifest_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root,src,dm,om=self._fixture(td)
            mp=root/'manifest.json';man=json.loads(mp.read_text());man['reads_post']=True
            mp.write_text(json.dumps(man))
            with self.assertRaisesRegex(ValueError,'Manifiesto'):
                view.load_maturity(root,src,dm,om)

    def test_missing_288_cannot_be_fake_complete(self):
        with tempfile.TemporaryDirectory() as td:
            root,src,dm,om=self._fixture(td)
            csvp=root/'attempt_maturity_pre.csv'
            with csvp.open() as f:
                data=list(csv.DictReader(f))
            data[0]['near_zone_share_288_half_atr']='0.9'
            with csvp.open('w',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=m.FIELDS)
                writer.writeheader();writer.writerows(data)
            mp=root/'manifest.json';man=json.loads(mp.read_text())
            man['outputs_sha256']['attempt_maturity_pre.csv']=m.sha(csvp)
            mp.write_text(json.dumps(man))
            with self.assertRaisesRegex(ValueError,'Cobertura'):
                view.load_maturity(root,src,dm,om)

    def test_episode_catalog_uses_first_maturity_only(self):
        # Sorting by the representative must not use later episode attempt features.
        discovery=pd.DataFrame([
          dict(attempt_id='a1',symbol='S',level_id='l',status='COMPLETE_PRE',
               pre_closed_ts=1,family_1p0='ASCENSO_ESCALONADO',scale_agreement='IGUAL_3_ATR',
               compression_24_recent_vs_early=.8),
          dict(attempt_id='a2',symbol='S',level_id='l',status='COMPLETE_PRE',
               pre_closed_ts=2,family_1p0='OSCILACION_EN_RESISTENCIA',scale_agreement='IGUAL_3_ATR',
               compression_24_recent_vs_early=.1)])
        amap=pd.DataFrame([dict(episode_id='E',attempt_id='a1',symbol='S',level_id='l',
            pre_closed_ts=1,episode_ordinal=1,other_levels_same_candle=0,
            candidate_other_level_prior_count=0),
            dict(episode_id='E',attempt_id='a2',symbol='S',level_id='l',
            pre_closed_ts=2,episode_ordinal=2,other_levels_same_candle=0,
            candidate_other_level_prior_count=0)])
        eps=pd.DataFrame([dict(episode_id='E',first_attempt_id='a1',symbol='S',
            level_id='l',attempts=2,first_pre_ts=1,last_pre_ts=2)])
        catalog=view.episode_catalog(discovery,amap,eps,'family_1p0')
        self.assertEqual(catalog.iloc[0].compression_24_recent_vs_early,.8)
        self.assertEqual(catalog.iloc[0].representative_family,'ASCENSO_ESCALONADO')

    def test_maturity_sort_not_quality_label(self):
        rows=pd.DataFrame([{'attempt_id':'b','symbol':'S','compression_24_recent_vs_early':.8},
                          {'attempt_id':'a','symbol':'S','compression_24_recent_vs_early':.5}])
        label='Más compresión 24 vs primeras 24 (96 PRE)'
        ordered=view.sort_discovery_rows(rows,label,episodic=False)
        self.assertEqual(ordered.attempt_id.tolist(),['a','b'])
        self.assertNotIn('quality',ordered)


if __name__=='__main__':unittest.main()
