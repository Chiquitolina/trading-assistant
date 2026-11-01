"""Read-only HLL PRE touch counts and chronological sort tests."""
from __future__ import annotations

import unittest

import pandas as pd

from hll_structural_discovery_v1 import TF
from dashboard.views.hll_structural_discovery_view import (
    compute_pre_touch_metrics, sort_discovery_rows, episode_catalog,
    TOUCH_SORTS, EPISODE_SORTS,
)


class MaturitySortTests(unittest.TestCase):
    def setUp(self):
        self.t=1_787_184_000_000
        self.attempts=pd.DataFrame([
            {'attempt_id':'a1','symbol':'AUSDT','level_id':'L1','pre_closed_ts':self.t+5*TF},
            {'attempt_id':'a2','symbol':'AUSDT','level_id':'L1','pre_closed_ts':self.t+20*TF},
            {'attempt_id':'b1','symbol':'AUSDT','level_id':'L2','pre_closed_ts':self.t+20*TF},
        ])
        self.touches=pd.DataFrame([
            self.touch('t1','L1',0,'BELOW'),
            self.touch('t2','L1',2,'ABOVE'),
            self.touch('t3','L1',4,'BELOW'),
            self.touch('t4','L1',7,'BELOW'),
            self.touch('t5','L1',19,'BELOW'),
            self.touch('t6','L1',21,'BELOW'),  # Future, never visible for a2
            self.touch('t7','L2',1,'BELOW'),
            self.touch('t8','L2',3,'ABOVE'),
        ])

    def touch(self,ident,level,n,side):
        return {'symbol':'AUSDT','level_id':level,'test_id':ident,
                'touch_closed_ts':self.t+n*TF,'side':side}

    def test_time_window_and_side_and_no_other_level(self):
        metric=compute_pre_touch_metrics(self.attempts,self.touches).set_index('attempt_id')
        self.assertEqual(int(metric.loc['a1','touches_all_pre288']),3)
        self.assertEqual(int(metric.loc['a1','touches_below_pre288']),2)
        self.assertEqual(int(metric.loc['a2','touches_all_pre288']),5)
        self.assertEqual(int(metric.loc['a2','touches_below_pre288']),4)
        self.assertEqual(int(metric.loc['b1','touches_below_pre288']),1)
        self.assertEqual(int(metric.loc['a1','touch_span_pre288_bars']),4)
        self.assertEqual(int(metric.loc['a2','touch_span_pre288_bars']),19)
        self.assertEqual(float(metric.loc['a1','touch_gap_median_pre288_bars']),4.0)

    def test_appending_future_touches_never_changes_prior_metric(self):
        baseline=compute_pre_touch_metrics(self.attempts,self.touches)
        extra=self.touch('future','L1',99,'BELOW')
        after=compute_pre_touch_metrics(self.attempts,pd.concat([
            self.touches,pd.DataFrame([extra])],ignore_index=True))
        pd.testing.assert_frame_equal(baseline,after)

    def test_wide_window_not_all_time(self):
        attempts=pd.DataFrame([{'attempt_id':'a','symbol':'AUSDT','level_id':'L1',
                                'pre_closed_ts':self.t+500*TF}])
        x=pd.DataFrame([self.touch('old','L1',0,'BELOW'),
                        self.touch('recent','L1',490,'BELOW'),
                        self.touch('now','L1',500,'BELOW')])
        out=compute_pre_touch_metrics(attempts,x).iloc[0]
        self.assertEqual(out.touches_below_pre288,2)
        self.assertEqual(out.touches_below_pre96,2)
        self.assertEqual(out.touch_span_pre288_bars,10)
        self.assertEqual(out.touch_span_all_pre_bars,500)

    def test_missing_spans_are_unknown_not_zero(self):
        attempts=self.attempts.iloc[[0]].copy()
        x=self.touches[self.touches.test_id.eq('t1')].copy()
        out=compute_pre_touch_metrics(attempts,x).iloc[0]
        self.assertEqual(out.touches_all_pre288,1)
        self.assertTrue(pd.isna(out.touch_span_pre288_bars))
        self.assertTrue(pd.isna(out.touch_gap_median_pre288_bars))

    def test_duplicate_touch_fails_closed(self):
        dup=pd.concat([self.touches,self.touches.iloc[[0]]],ignore_index=True)
        with self.assertRaisesRegex(ValueError,'duplicados'):
            compute_pre_touch_metrics(self.attempts,dup)

    def test_sort_stability_and_missing_at_end(self):
        x=pd.DataFrame([
            {'attempt_id':'c','symbol':'ZUSDT','pre_closed_ts':3,'touches_below_pre288':0},
            {'attempt_id':'a','symbol':'AUSDT','pre_closed_ts':1,'touches_below_pre288':5},
            {'attempt_id':'b','symbol':'BUSDT','pre_closed_ts':2,'touches_below_pre288':5},
            {'attempt_id':'d','symbol':'CUSDT','pre_closed_ts':4,'touches_below_pre288':None},
        ])
        result=sort_discovery_rows(x,'Más contactos PRE desde abajo (288 velas)',episodic=False)
        self.assertEqual(result.attempt_id.tolist(),['a','b','c','d'])
        self.assertEqual(sort_discovery_rows(x,'Fecha PRE (actual)',episodic=False).attempt_id.tolist(),['a','b','d','c'])

    def test_retrospective_episode_sort_not_available_to_attempts(self):
        x=pd.DataFrame([{'episode_id':'ep1','symbol':'AUSDT','first_pre_ts':1,'attempts':3,'span_bars':10},
                        {'episode_id':'ep2','symbol':'AUSDT','first_pre_ts':2,'attempts':1,'span_bars':0},
                        {'episode_id':'ep3','symbol':'BUSDT','first_pre_ts':3,'attempts':5,'span_bars':5}])
        self.assertEqual(sort_discovery_rows(x,'Más CROSS_UP del episodio (retrospectivo)',episodic=True).episode_id.tolist(),['ep3','ep1','ep2'])
        self.assertEqual(sort_discovery_rows(x,'Más duración entre primer y último CROSS_UP (retrospectivo)',episodic=True).episode_id.tolist(),['ep1','ep3','ep2'])
        with self.assertRaisesRegex(ValueError,'desconocido'):
            sort_discovery_rows(x,'Más CROSS_UP del episodio (retrospectivo)',episodic=False)

    def test_episode_catalog_uses_first_touch_metric_only(self):
        data=self.attempts.iloc[:2].copy()
        data['family_1p0']=['OSCILACION_EN_RESISTENCIA','ASCENSO_ESCALONADO']
        data['scale_agreement']='IGUAL_3_ATR'
        data['status']='COMPLETE_PRE'
        data=data.merge(compute_pre_touch_metrics(data,self.touches),on='attempt_id')
        amap=pd.DataFrame([{'episode_id':'ep','attempt_id':'a1','symbol':'AUSDT',
                            'level_id':'L1','pre_closed_ts':self.t+5*TF,
                            'episode_ordinal':1,'other_levels_same_candle':0,
                            'candidate_other_level_prior_count':0},
                           {'episode_id':'ep','attempt_id':'a2','symbol':'AUSDT',
                            'level_id':'L1','pre_closed_ts':self.t+20*TF,
                            'episode_ordinal':2,'other_levels_same_candle':0,
                            'candidate_other_level_prior_count':0}])
        eps=pd.DataFrame([{'episode_id':'ep','symbol':'AUSDT','level_id':'L1',
                           'first_attempt_id':'a1','first_pre_ts':self.t+5*TF,
                           'last_pre_ts':self.t+20*TF,'span_bars':15,'attempts':2,
                           'first_family_1p0':'OSCILACION_EN_RESISTENCIA',
                           'distinct_families_1p0':2,'family_changes_within_episode':1}])
        catalog=episode_catalog(data,amap,eps,'family_1p0')
        self.assertEqual(int(catalog.touches_below_pre288.iloc[0]),2)
        self.assertNotEqual(int(catalog.touches_below_pre288.iloc[0]),4)

    def test_new_selector_labels_and_original_modes_remain(self):
        self.assertIn('Más contactos PRE desde abajo (288 velas)',TOUCH_SORTS)
        self.assertIn('Más duración entre primer y último CROSS_UP (retrospectivo)',EPISODE_SORTS)

if __name__=='__main__':
    unittest.main()
