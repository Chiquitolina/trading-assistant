import ast
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

ROOT=Path(__file__).resolve().parent


class FakeColumn:
    def metric(self,*a,**kw):pass
    def selectbox(self,label,options,*a,**kw):return list(options)[0]
    def select_slider(self,label,options=None,value=None,**kw):return value or options[0]
    def radio(self,label,options,index=0,**kw):return options[index]
    def markdown(self,*a,**kw):pass
    def info(self,*a,**kw):pass
    def caption(self,*a,**kw):pass
    def __enter__(self):return self
    def __exit__(self,*a):return False


class FakeStreamlit(types.ModuleType):
    def __init__(self):
        super().__init__('streamlit')
        self.plots=0
        self.errors=[]
        self.texts=[]
    def cache_data(self,**kwargs):return lambda f:f
    def columns(self,n,**kwargs):return [FakeColumn() for _ in range(n if isinstance(n,int) else len(n))]
    def selectbox(self,label,options,*a,**kw):return list(options)[0]
    def select_slider(self,label,options=None,value=None,**kw):return value or options[0]
    def radio(self,label,options,index=0,**kw):return options[index]
    def markdown(self,t,*a,**kw):self.texts.append(t)
    def caption(self,*a,**kw):pass
    def info(self,*a,**kw):pass
    def warning(self,*a,**kw):pass
    def dataframe(self,*a,**kw):pass
    def plotly_chart(self,*a,**kw):self.plots+=1
    def error(self,s,*a,**kw):self.errors.append(str(s))


class RichCardViewSmoke(unittest.TestCase):
    def test_new_tab_is_registered_and_compiles(self):
        tree=ast.parse((ROOT/'dashboard/views/reaction_microstructure_quality_view.py').read_text())
        text=(ROOT/'dashboard/views/reaction_microstructure_quality_view.py').read_text()
        self.assertIn('🟩 Horizontal Level Rich Card',text)
        self.assertIn('with tabs[10]:',text)
        self.assertEqual(text.count('with tabs[10]:'),1)
        self.assertIsNotNone(tree)

    def test_dashboard_streamlit_path_no_hidden_keyerror(self):
        fake=FakeStreamlit()
        engine=types.ModuleType('reaction_structural_level_atlas_v2')
        engine.v1=types.SimpleNamespace(lineage=types.SimpleNamespace(ROOT_REL=Path('reports/reaction_lab')))
        engine.OUTPUT='reaction_structural_level_atlas_v2'
        import plotly.graph_objects as go
        from dashboard.views import reaction_structural_level_atlas_v2_view as source
        from dashboard.views import reaction_outcome_overlay as overlay
        from dashboard.views import reaction_horizontal_level_rich_card_view as view
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)
            folder=p/'reports/reaction_lab/reaction_structural_level_atlas_v2'
            folder.mkdir(parents=True)
            (folder/'summary.json').write_text('{}')
            ctx=p/'reports/reaction_lab/reaction_full_context_v1.sqlite3'
            ctx.touch()
            atlas=pd.DataFrame([{
                'event_key':'a','symbol':'TESTUSDT','side':'SHORT','date_utc':'2026-10-12',
                'retest_ts':15*60_000,'all_covered':1,'primary_zone_low':99,
                'primary_zone_high':101,'primary_zone_id':'z','primary_timeframe':'15m',
                'primary_distance_atr':0.0,'primary_state_pre':'ACCEPTED_DEFENDED',
                'setup_pre':'RUPTURA_POTENCIAL','known_candle_label':'PRUEBA_SIN_QUIEBRE',
                'pre_contacts_5':4,'pressure_pre':True,
            }])
            levels=pd.DataFrame([{'event_key':'a','timeframe':'15m','zone_id':'z',
                                  'level_price':100,'zone_low':99,'zone_high':101}])
            source_rows=pd.DataFrame([{'event_key':'a','outcome':'SL_FIRST','entry_ts':16*60_000,
                                     'entry_price':102.0,'first_touch_minute':20}])
            with patch.dict(sys.modules,{'streamlit':fake,'reaction_structural_level_atlas_v2':engine}), \
                patch.object(source,'load',return_value=(levels,atlas,{})), \
                patch.object(view.logic,'enrich_events',side_effect=lambda project, events:events), \
                patch.object(source,'_read_structural_figure',return_value=go.Figure()), \
                patch.object(overlay,'get_post_bars',return_value=([],'MISSING_PATH')):
                view.render_horizontal_level_rich_card(source_rows,p,
                    tp_pct=1.0,sl_pct=0.5,outcome_horizon=180,entry_mode='OBSERVED_OPEN')
            self.assertFalse(fake.errors)
            self.assertEqual(fake.plots,1)
            self.assertTrue(any('Horizontal Level Rich Card' in t for t in fake.texts))

if __name__=='__main__':unittest.main()
