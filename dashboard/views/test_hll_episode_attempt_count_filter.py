"""HLL Discovery navigation: sort and filter by number of CROSS_UP per episode."""
from __future__ import annotations
import io
import unittest
import zipfile
from pathlib import Path
import pandas as pd
from dashboard.views.hll_structural_discovery_view import (
    filter_episodes_by_attempt_count, sort_discovery_rows, EPISODE_SORTS,
)

class EpisodeAttemptCountFilterTests(unittest.TestCase):
    def setUp(self):
        self.rows=pd.DataFrame([
            {'episode_id':'epA','symbol':'AAAUSDT','attempts':1,'first_pre_ts':1},
            {'episode_id':'epB','symbol':'BBBUSDT','attempts':4,'first_pre_ts':2},
            {'episode_id':'epC','symbol':'CCCUSDT','attempts':2,'first_pre_ts':3},
            {'episode_id':'epD','symbol':'DDDUSDT','attempts':6,'first_pre_ts':4},
        ])

    def test_exact_count_filter(self):
        self.assertEqual(filter_episodes_by_attempt_count(self.rows,4,4).episode_id.tolist(),['epB'])

    def test_inclusive_range_and_no_original_loss(self):
        before=self.rows.copy(deep=True)
        x=filter_episodes_by_attempt_count(self.rows,2,4)
        self.assertEqual(x.episode_id.tolist(),['epB','epC'])
        pd.testing.assert_frame_equal(before,self.rows)

    def test_sort_desc_and_asc(self):
        desc=sort_discovery_rows(self.rows,'Más CROSS_UP del episodio (retrospectivo)',episodic=True)
        asc=sort_discovery_rows(self.rows,'Menos CROSS_UP del episodio (retrospectivo)',episodic=True)
        self.assertEqual(desc.episode_id.tolist(),['epD','epB','epC','epA'])
        self.assertEqual(asc.episode_id.tolist(),['epA','epC','epB','epD'])

    def test_invalid_or_incomplete_attempt_counts_rejected(self):
        for lo,hi in ((0,2),(4,3),(-1,20)):
            with self.assertRaisesRegex(ValueError,'Rango'):
                filter_episodes_by_attempt_count(self.rows,lo,hi)
        for v in (None,0,1.5,'abc'):
            bad=self.rows.copy();bad['attempts']=bad.attempts.astype(object);bad.loc[0,'attempts']=v
            with self.assertRaisesRegex(ValueError,'intentos'):
                filter_episodes_by_attempt_count(bad,1,6)
        with self.assertRaisesRegex(ValueError,'Falta'):
            filter_episodes_by_attempt_count(self.rows.drop(columns='attempts'),1,6)

    def test_retrospective_sort_not_available_to_individual_attempts(self):
        self.assertIn('Más CROSS_UP del episodio (retrospectivo)',EPISODE_SORTS)
        with self.assertRaisesRegex(ValueError,'desconocido'):
            sort_discovery_rows(self.rows,'Más CROSS_UP del episodio (retrospectivo)',episodic=False)

    def test_real_290_counts_if_zip_available(self):
        name='reports/horizontal_level_structural_overlap_audit_v1/full290_v1/episode_candidates.csv'
        direct=Path(name)
        if direct.is_file():
            source=pd.read_csv(direct,low_memory=False)
        else:
            path=Path('/mnt/data/HLL_EPISODIOS_DASHBOARD_FUENTES.zip')
            if not path.is_file():self.skipTest('Reporte full290 no disponible')
            with zipfile.ZipFile(path) as z:
                source=pd.read_csv(io.BytesIO(z.read(name)),low_memory=False)
        x=filter_episodes_by_attempt_count(source,3,18)
        self.assertEqual(len(source),22674)
        self.assertEqual(int(source.attempts.sum()),55717)
        self.assertTrue(x.attempts.ge(3).all())
        self.assertTrue(x.attempts.le(18).all())
        self.assertEqual(len(x),8011)

    def test_ui_wires_filter_only_in_episode_mode(self):
        s=Path(__file__).with_name('hll_structural_discovery_view.py').read_text()
        self.assertIn('Mínimo de intentos CROSS_UP por episodio',s)
        self.assertIn('Máximo de intentos CROSS_UP por episodio',s)
        self.assertIn('filter_episodes_by_attempt_count(filtered,min_attempts,max_attempts)',s)
        self.assertIn("key='hll_discovery_min_episode_attempts'",s)
        self.assertIn("key='hll_discovery_max_episode_attempts'",s)
        self.assertIn('no para predecir un primer breakout',s)

if __name__=='__main__':unittest.main()
