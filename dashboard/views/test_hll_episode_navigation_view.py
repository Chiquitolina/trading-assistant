"""Tests: HLL Discovery 290 episode navigation (read-only, exact level IDs)."""
from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import pandas as pd

from hll_structural_discovery_v1 import TF, sha
from dashboard.views.hll_structural_discovery_view import (
    OVERLAP_PARENT, episode_catalog, filter_episodes, load_overlap,
)


class EpisodeNavigationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.t=1_787_184_000_000
        self.src='a'*64
        self.cohort='b'*64
        self.discovery=pd.DataFrame([
            self.disc('a1','ONEUSDT','L1',0,'OSCILACION_EN_RESISTENCIA'),
            self.disc('a2','ONEUSDT','L1',8,'ASCENSO_ESCALONADO'),
            self.disc('a3','ONEUSDT','L2',10,'RECONSTRUCCION_ENTRE_CONTACTOS'),
        ])
        self.discovery_manifest={'source_sha256':{'cohort':self.cohort},
                                 'output_sha256':{'attempts_pre.csv':self.src}}
        self.amap=pd.DataFrame([
            self.mapping('a1','L1',0,'ep1','a1',0,1,2,'OSCILACION_EN_RESISTENCIA'),
            self.mapping('a2','L1',8,'ep1','a1',0,2,2,'ASCENSO_ESCALONADO'),
            self.mapping('a3','L2',10,'ep2','a3',10,1,1,'RECONSTRUCCION_ENTRE_CONTACTOS'),
        ])
        self.eps=pd.DataFrame([
            {'episode_id':'ep1','symbol':'ONEUSDT','level_id':'L1',
             'first_attempt_id':'a1','first_pre_ts':self.t,'last_pre_ts':self.t+8*TF,
             'span_bars':8,'attempts':2,'first_family_1p0':'OSCILACION_EN_RESISTENCIA',
             'distinct_families_1p0':2,'family_changes_within_episode':1},
            {'episode_id':'ep2','symbol':'ONEUSDT','level_id':'L2',
             'first_attempt_id':'a3','first_pre_ts':self.t+10*TF,'last_pre_ts':self.t+10*TF,
             'span_bars':0,'attempts':1,'first_family_1p0':'RECONSTRUCCION_ENTRE_CONTACTOS',
             'distinct_families_1p0':1,'family_changes_within_episode':0},
        ])
        self.near=pd.DataFrame([{
            'attempt_id':'a3','symbol':'ONEUSDT','pre_closed_ts':self.t+10*TF,
            'level_id':'L2','other_attempt_id':'a1','other_level_id':'L1',
            'other_pre_closed_ts':self.t,'delta_bars':10,
            'center_distance_atr':0.03,'zone_gap_atr':0.,
            'interpretation':'POSIBLE_ZONA_EQUIVALENTE_NO_FUSIONADA',
        }])

    def disc(self,aid,symbol,level,n,family):
        return {'attempt_id':aid,'symbol':symbol,'level_id':level,
                'pre_closed_ts':self.t+n*TF,'zone_low':10.,'zone_high':10.2,
                'status':'COMPLETE_PRE','family_0p5':family,'family_1p0':family,
                'family_1p5':family,'scale_agreement':'IGUAL_3_ATR'}

    def mapping(self,aid,lid,n,eid,first,anchor,ordinal,count,family):
        return {'attempt_id':aid,'symbol':'ONEUSDT','level_id':lid,
                'pre_closed_ts':self.t+n*TF,'zone_low':10.,'zone_high':10.2,
                'status':'COMPLETE_PRE','family_1p0':family,'episode_id':eid,
                'episode_first_attempt_id':first,'episode_anchor_pre_ts':self.t+anchor*TF,
                'episode_ordinal':ordinal,'episode_attempt_count':count,
                'is_first_in_episode':int(ordinal==1),'other_levels_same_candle':0,
                'candidate_other_level_prior_count':int(aid=='a3')}

    def export(self):
        files={'attempt_episode_map.csv':self.amap,'episode_candidates.csv':self.eps,
               'nearby_other_levels_review.csv':self.near,
               'level_reuse.csv':pd.DataFrame([{'symbol':'ONEUSDT'}]),
               'symbol_summary.csv':pd.DataFrame([{'symbol':'ONEUSDT'}])}
        outputs={}
        for fname,df in files.items():
            path=self.root/fname
            df.to_csv(path,index=False)
            outputs[fname]=sha(path)
        manifest={'version':'HLL_OVERLAP_AUDIT_V1','mode':'READ_ONLY_ANALYSIS',
                  'attempts':3,'symbols':1,'heuristic_episodes_gap24_cap96':2,
                  'attempts_with_prior_similar_zone_other_level':1,
                  'no_post_used':True,'different_level_ids_merged':False,
                  'source_sha256':{'discovery':self.src,'cohort':self.cohort},
                  'outputs_sha256':outputs,
                  'rule':{'same_level_exact':True,'max_gap_bars_default':24,
                          'max_span_from_first_pre_bars':96}}
        (self.root/'manifest.json').write_text(json.dumps(manifest),encoding='utf8')

    def load(self):
        return load_overlap(self.root,self.discovery,self.discovery_manifest)

    def test_exact_episode_join_and_all_attempts_preserved(self):
        self.export()
        a,e,n,m=self.load()
        self.assertEqual((len(a),len(e),len(n)),(3,2,1))
        self.assertEqual(set(a.attempt_id),set(self.discovery.attempt_id))
        self.assertEqual(e.set_index('episode_id').loc['ep1','attempts'],2)
        self.assertEqual(a.loc[a.attempt_id.eq('a3'),'level_id'].iloc[0],'L2')
        self.assertFalse(m['different_level_ids_merged'])

    def test_family_defaults_to_first_not_retrospective_best(self):
        self.export()
        a,e,_,_=self.load()
        catalog=episode_catalog(self.discovery,a,e,'family_1p0')
        earliest=catalog.loc[catalog.episode_id.eq('ep1')].iloc[0]
        self.assertEqual(earliest.first_attempt_id,'a1')
        self.assertEqual(earliest.representative_family,'OSCILACION_EN_RESISTENCIA')
        self.assertEqual(len(filter_episodes(catalog,self.discovery,a,'family_1p0',
                             family='ASCENSO_ESCALONADO')),0)
        future=filter_episodes(catalog,self.discovery,a,'family_1p0',
                               family='ASCENSO_ESCALONADO',family_match='CUALQUIERA')
        self.assertEqual(future.episode_id.tolist(),['ep1'])

    def test_family_changes_are_independent_from_selected_scale(self):
        self.export();a,e,_,_=self.load()
        data=self.discovery.copy()
        data.loc[data.attempt_id.eq('a1'),'family_0p5']='REBOTE_TRAS_CORRECCION'
        catalog=episode_catalog(data,a,e,'family_0p5')
        self.assertEqual(catalog.loc[catalog.episode_id.eq('ep1'),'representative_family'].iloc[0],
                         'REBOTE_TRAS_CORRECCION')
        self.assertEqual(len(filter_episodes(catalog,data,a,'family_0p5',change='CAMBIA')),1)

    def test_other_level_same_zone_not_merged(self):
        self.export();a,e,n,_=self.load()
        self.assertEqual(e.level_id.nunique(),2)
        self.assertEqual(n.other_level_id.iloc[0],'L1')
        self.assertEqual(n.level_id.iloc[0],'L2')
        self.assertEqual(a.loc[a.attempt_id.eq('a3'),'episode_id'].iloc[0],'ep2')

    def test_corrupt_csv_hash_refused(self):
        self.export()
        with (self.root/'attempt_episode_map.csv').open('a') as f:f.write('tamper')
        with self.assertRaisesRegex(ValueError,'modificado'):
            self.load()

    def test_other_discovery_source_refused(self):
        self.export()
        self.discovery_manifest['output_sha256']['attempts_pre.csv']='c'*64
        with self.assertRaisesRegex(ValueError,'fuentes distintas'):
            self.load()

    def test_different_level_inside_same_episode_rejected(self):
        self.amap.loc[self.amap.attempt_id.eq('a2'),'level_id']='L2'
        self.discovery.loc[self.discovery.attempt_id.eq('a2'),'level_id']='L2'
        self.export()
        with self.assertRaisesRegex(ValueError,'fusiona niveles'):
            self.load()

    def test_future_label_does_not_change_first_representative(self):
        self.export()
        a,e,_,_=self.load()
        original=episode_catalog(self.discovery,a,e,'family_1p0')
        self.discovery.loc[self.discovery.attempt_id.eq('a2'),'family_1p0']='DETERIORO_DESDE_MINIMO'
        # A frozen overlap must refuse a changed source, even with same cutoff.
        with self.assertRaisesRegex(ValueError,'inconsistente en family_1p0'):
            self.load()
        self.assertEqual(original.set_index('episode_id').loc['ep1','representative_family'],
                         'OSCILACION_EN_RESISTENCIA')

    def test_attempt_count_or_wrong_first_refused(self):
        self.amap.loc[self.amap.attempt_id.eq('a1'),'episode_attempt_count']=3
        self.export()
        with self.assertRaisesRegex(ValueError,'contadores'):
            self.load()

    def test_future_sibling_marker_is_never_past_cut(self):
        self.export();a,_,_,_=self.load()
        second=a.loc[a.attempt_id.eq('a2')].iloc[0]
        previous=a.loc[(a.episode_id.eq(second.episode_id)) &
                       (a.pre_closed_ts.lt(second.pre_closed_ts))]
        self.assertEqual(previous.attempt_id.tolist(),['a1'])
        first=a.loc[a.attempt_id.eq('a1')].iloc[0]
        self.assertTrue(a.loc[(a.episode_id.eq(first.episode_id)) &
                              (a.pre_closed_ts.lt(first.pre_closed_ts))].empty)

    def test_incomplete_first_is_retained_not_best_later(self):
        self.discovery.loc[self.discovery.attempt_id.eq('a1'),'status']='INCOMPLETE_PRE_96'
        self.discovery.loc[self.discovery.attempt_id.eq('a1'),'family_1p0']=None
        self.amap.loc[self.amap.attempt_id.eq('a1'),'status']='INCOMPLETE_PRE_96'
        self.amap.loc[self.amap.attempt_id.eq('a1'),'family_1p0']='SIN_DATOS'
        self.eps.loc[self.eps.episode_id.eq('ep1'),'first_family_1p0']='SIN_DATOS'
        self.export()
        a,e,_,_=self.load()
        result=episode_catalog(self.discovery,a,e,'family_1p0')
        first=result.loc[result.episode_id.eq('ep1')].iloc[0]
        self.assertEqual(first.representative_status,'INCOMPLETE_PRE_96')
        self.assertTrue(pd.isna(first.representative_family))
        self.assertEqual(first.first_attempt_id,'a1')

    def test_view_keeps_existing_route_and_modes(self):
        view=Path(__file__).with_name('hll_structural_discovery_view.py').read_text()
        parent=Path(__file__).with_name('hll_v28_structure_dashboard_view.py').read_text()
        self.assertIn('Episodios (mismo nivel, 24/96)',view)
        self.assertIn('Intentos CROSS_UP',view)
        self.assertIn('Cualquier intento (retrospectivo)',view)
        self.assertIn('render_hll_structural_discovery(root)',parent)
        self.assertIn("if st.session_state.get('hll_phase_mode') == 'Revisión visual ciega (PRE)':\n            return",parent)
        self.assertIn('chart_for_attempt(row,candles,scale,pre_bars,post_bars,pivots)',view)

    def _ui_smoke(self, select_attempts=False):
        from dashboard.views import hll_structural_discovery_view as view
        import sys
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        class FakeStreamlit:
            def __init__(self):
                self.errors=[];self.charts=0;self.tables=0;self.labels=[]
            def cache_data(self, **kw):return lambda f:f
            def markdown(self, text):self.labels.append(text)
            def caption(self, text):self.labels.append(text)
            def info(self, text):self.labels.append(text)
            def warning(self, text):self.labels.append(text)
            def error(self, text):self.errors.append(text)
            def columns(self, n):return [self]*n
            def __enter__(self):return self
            def __exit__(self, *args):return False
            def metric(self, *args, **kw):pass
            def selectbox(self, label, options, **kwargs):return options[0]
            def number_input(self, label, **kw):return 1
            def checkbox(self, label, value=False, **kw):return value
            def dataframe(self, *args, **kw):self.tables+=1
            def expander(self, *args, **kw):return self
            def plotly_chart(self, *args, **kw):self.charts+=1
            def radio(self, label, options, **kwargs):
                return options[-1] if select_attempts else options[0]

        fake=FakeStreamlit()
        self.export()
        overlay=(self.amap.copy(),self.eps.copy(),self.near.copy(),
                 json.loads((self.root/'manifest.json').read_text()))
        root=self.root/'project';root.mkdir()
        d=root/'reports/horizontal_level_structural_discovery_v1/full290_v1'
        d.mkdir(parents=True)
        for name in ('manifest.json','attempts_pre.csv'):(d/name).write_text('stub')
        overlap=root/OVERLAP_PARENT
        overlap.mkdir(parents=True)
        for name in ('manifest.json','attempt_episode_map.csv','episode_candidates.csv',
                     'nearby_other_levels_review.csv','level_reuse.csv','symbol_summary.csv'):
            (overlap/name).write_text('stub')
        cd=root/view.CANDLES
        cd.mkdir(parents=True)
        (cd/'ONEUSDT_30m.csv').write_text('stub')
        dm={'symbols':1,'attempts':3}
        class Fig:
            def add_vline(self, **kwargs):pass
        ohlc=sys.modules.get('hll_structural_approach_ohlc_pre')
        if ohlc is None:
            import hll_structural_approach_ohlc_pre as ohlc
        with patch.dict(sys.modules,{'streamlit':fake}), \
             patch.object(view,'load_discovery',return_value=(self.discovery,dm)), \
             patch.object(view,'load_overlap',return_value=overlay), \
             patch.object(ohlc,'load_candles',return_value=({},None),create=True), \
             patch.object(view,'chart_for_attempt',return_value=(Fig(),96,0)):
            view.render_hll_structural_discovery(root)
        return fake

    def test_episode_ui_smoke_uses_same_chart_and_no_crash(self):
        ui=self._ui_smoke(False)
        self.assertEqual(ui.errors,[])
        self.assertEqual(ui.charts,1)
        self.assertGreaterEqual(ui.tables,2)
        self.assertTrue(any('Episodio ' in txt for txt in ui.labels))

    def test_original_attempt_mode_ui_still_operates(self):
        ui=self._ui_smoke(True)
        self.assertEqual(ui.errors,[])
        self.assertEqual(ui.charts,1)
        self.assertGreaterEqual(ui.tables,1)

    def test_real_290_overlap_export_if_available(self):
        root=Path('reports/horizontal_level_structural_overlap_audit_v1/full290_v1')
        if not root.is_file() and not (root/'manifest.json').is_file():
            self.skipTest('Requiere informe overlap full290 disponible en VPS')
        manifest=json.loads((root/'manifest.json').read_text())
        a=pd.read_csv(root/'attempt_episode_map.csv',low_memory=False)
        d=a[['attempt_id','symbol','level_id','pre_closed_ts','zone_low','zone_high','status','family_1p0']].copy()
        d['family_0p5']=d['family_1p0'];d['family_1p5']=d['family_1p0']
        d['scale_agreement']='IGUAL_3_ATR'
        dm={'source_sha256':{'cohort':manifest['source_sha256']['cohort']},
            'output_sha256':{'attempts_pre.csv':manifest['source_sha256']['discovery']}}
        amap,e,n,_=load_overlap(root,d,dm)
        self.assertEqual((len(amap),len(e),len(n)),(55717,22674,9243))
        self.assertEqual(amap.episode_id.nunique(),len(e))
        self.assertEqual(e.attempts.sum(),len(amap))


if __name__=='__main__':unittest.main()
