import ast
import sys
import types
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd

ROOT=Path(__file__).resolve().parent

class Pane:
    def __init__(self,parent):self.parent=parent
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def metric(self,*args,**kwargs):pass
    def selectbox(self,label,options,**kwargs):return list(options)[0]
    def radio(self,label,options,index=0,**kwargs):return list(options)[index]
    def select_slider(self,label,options,value,**kwargs):return value
    def caption(self,*args,**kwargs):pass

class FakeSt(types.ModuleType):
    def __init__(self):
        super().__init__('streamlit'); self.errors=[];self.tables=[];self.plots=0;self.captions=[]
    def cache_data(self,**kwargs):return lambda f:f
    def columns(self,n,**kwargs):return [Pane(self) for _ in range(n if isinstance(n,int) else len(n))]
    def markdown(self,*args,**kwargs):pass
    def caption(self,msg,*args,**kwargs):self.captions.append(msg)
    def warning(self,*args,**kwargs):pass
    def error(self,msg,*args,**kwargs):self.errors.append(str(msg))
    def info(self,*args,**kwargs):pass
    def selectbox(self,label,options,**kwargs):return list(options)[0]
    def select_slider(self,label,options,value,**kwargs):return value
    def expander(self,*args,**kwargs):return Pane(self)
    def dataframe(self,df,**kwargs):self.tables.append(df)
    def plotly_chart(self,*args,**kwargs):self.plots+=1
    def download_button(self,*args,**kwargs):pass

class UiTests(unittest.TestCase):
    def test_only_existing_tab_label_replaced(self):
        source=(ROOT/'dashboard/views/reaction_microstructure_quality_view.py').read_text()
        self.assertEqual(source.count('🎯 Horizontal Level Research'),1)
        self.assertNotIn('🟩 Horizontal Level Rich Card',source)
        self.assertEqual(source.count('with tabs[10]:'),1)
        self.assertIn('render_horizontal_level_rich_card(',source)
        self.assertTrue(ast.parse(source))

    def test_render_has_no_dependency_on_hl_lh_or_retest_outcome_to_choose_levels(self):
        from dashboard.views.reaction_horizontal_level_rich_card_view import render_horizontal_level_rich_card
        fake=FakeSt()
        fake_engine=types.ModuleType('reaction_structural_level_atlas_v2')
        fake_engine.v1=types.SimpleNamespace(lineage=types.SimpleNamespace(ROOT_REL=Path('reports/reaction_lab')))
        fake_engine.OUTPUT='reaction_structural_level_atlas_v2'
        import plotly.graph_objects as go
        fake_atlas=types.ModuleType('dashboard.views.reaction_structural_level_atlas_v2_view')
        events=pd.DataFrame([{'event_key':'e1','retest_ts':60_000_000,'symbol':'ENSOUSDT',
            'side':'SHORT','date_utc':'2026-10-21','all_covered':1}])
        levels=pd.DataFrame([{'event_key':'e1','zone_id':'abc','pivot_count':2,
            'defense_episodes_pre':2,'formation_closed_ts':10_000_000,
            'retest_ts':60_000_000,'timeframe':'15m','gap_to_zone_atr':0.1,
            'known_touch':True,'zone_low':0.5,'zone_high':0.52,'level_price':0.51,
            'pre_last_close':0.53,'atr_pre':0.01}])
        fake_atlas.load=lambda _: (levels,events,{'version':'v2','events':1})
        fake_atlas._read_structural_figure=lambda *a,**kw:go.Figure(go.Candlestick(
            x=pd.date_range('2026-10-21',periods=10,freq='15min'),
            open=[1.0]*10,high=[1.1]*10,low=[0.95]*10,close=[1.05]*10,name='15m'))
        overlay=types.ModuleType('dashboard.views.reaction_outcome_overlay')
        overlay.get_post_bars=lambda *a:([],'MISSING')
        overlay.overlay_future=lambda fig,*a,**kw:fig
        overlay.outcome_explanation=lambda *a,**kw:'Sin trayecto disponible'
        # Source helper for HTF bars is optional; exercise the fallback if absent
        with tempfile.TemporaryDirectory() as t:
            f=Path(t)/'reports/reaction_lab/reaction_structural_level_atlas_v2'
            f.mkdir(parents=True)
            for name in ('summary.json','candidate_levels.csv','event_map.csv'):(f/name).write_text('stub')
            source=pd.DataFrame([{'event_key':'e1','outcome':'SL_FIRST',
                'entry_ts':60_060_000,'entry_price':0.53,'first_touch_minute':5}])
            with patch.dict(sys.modules,{'streamlit':fake,
                             'reaction_structural_level_atlas_v2':fake_engine,
                             'dashboard.views.reaction_structural_level_atlas_v2_view':fake_atlas,
                             'dashboard.views.reaction_outcome_overlay':overlay}):
                with patch('dashboard.views.reaction_horizontal_level_rich_card_view.load_retest_candles',
                           side_effect=lambda root,df:df.assign(known_open=0.51,known_high=0.53,
                                                                known_low=0.50,known_close=0.52)):
                    render_horizontal_level_rich_card(source,t)
        self.assertEqual(fake.errors,[])
        self.assertEqual(fake.plots,1)
        self.assertGreaterEqual(len(fake.tables),2)

if __name__=='__main__':unittest.main()
