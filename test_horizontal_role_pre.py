"""Regression tests for PRE-only structural level roles (research, not LIVE)."""
import unittest
from copy import deepcopy

import pandas as pd

from reaction_horizontal_level_research import (
    ROLE_REBOUND, ROLE_BREAK, ROLE_IN_ZONE, ROLE_NO_NEAR, ROLE_UNKNOWN,
    ROLE_LABELS, build_pre_roles, classify_role_pre, group_summary,
)


def event(key='e', side='LONG', complete=1):
    return {'event_key':key,'side':side,'all_covered':complete,
            'retest_ts':60_000_000,'known_low':99.5,'known_high':100.3,
            'outcome_norm':'SL_FIRST'}


def zone(key='e', label='z', low=99.0, high=99.5, close=100.0,
         atr=2.0, episodes=2, tf='15m', origin='PRIOR_LOW'):
    return {'event_key':key,'zone_id':label,'zone_low':low,'zone_high':high,
            'pre_last_close':close,'atr_pre':atr,'timeframe':tf,
            'gap_to_zone_atr':max(low-close, close-high, 0)/atr if atr else None,
            'pivot_count':2,'defense_episodes_pre':episodes,
            'formation_closed_ts':10_000_000,'retest_ts':60_000_000,
            'level_origin':origin,'known_touch':False,
            'level_state_pre':'ACCEPTED_DEFENDED','outcome_norm':'TP_FIRST'}


class RoleResearchTests(unittest.TestCase):
    def test_long_support_rebound(self):
        r=classify_role_pre(event(side='LONG'),[zone()])
        self.assertEqual(r['pre_role'],ROLE_REBOUND)
        self.assertEqual(r['role_position'],'SUPPORT_BELOW')
        self.assertAlmostEqual(r['role_distance_atr'],.25)

    def test_short_resistance_rebound(self):
        r=classify_role_pre(event(side='SHORT'),[zone(low=100.5,high=101.0)])
        self.assertEqual(r['pre_role'],ROLE_REBOUND)
        self.assertEqual(r['role_position'],'RESISTANCE_ABOVE')

    def test_long_resistance_requires_break(self):
        r=classify_role_pre(event(side='LONG'),[zone(low=100.5,high=101.0)])
        self.assertEqual(r['pre_role'],ROLE_BREAK)

    def test_short_support_requires_break(self):
        r=classify_role_pre(event(side='SHORT'),[zone()])
        self.assertEqual(r['pre_role'],ROLE_BREAK)

    def test_inside_zone_ambiguous_for_both_sides(self):
        for side in ('LONG','SHORT'):
            r=classify_role_pre(event(side=side),[zone(low=99.5,high=100.5)])
            self.assertEqual(r['pre_role'],ROLE_IN_ZONE)

    def test_equally_close_opposite_sides_ambiguous(self):
        z1=zone(label='a',low=99.6,high=99.8)
        z2=zone(label='b',low=100.2,high=100.4)
        r=classify_role_pre(event(),[z1,z2])
        self.assertEqual(r['pre_role'],ROLE_IN_ZONE)
        self.assertEqual(r['role_near_count'],2)

    def test_out_of_range_or_no_levels_is_neutral_control(self):
        r=classify_role_pre(event(),[zone(low=95,high=96)])
        self.assertEqual(r['pre_role'],ROLE_NO_NEAR)
        self.assertIsNone(r['role_zone_id'])
        self.assertEqual(classify_role_pre(event(),[])['pre_role'],ROLE_NO_NEAR)

    def test_missing_history_is_unknown_not_no_level(self):
        r=classify_role_pre(event(complete=0),[zone()])
        self.assertEqual(r['pre_role'],ROLE_UNKNOWN)
        r=classify_role_pre(event(),[zone(atr=None)])
        self.assertEqual(r['pre_role'],ROLE_UNKNOWN)

    def test_nearest_beats_more_defenses(self):
        more_defended=zone(label='further',low=99,high=99.2,episodes=50)
        closer=zone(label='nearer',low=99.6,high=99.7,episodes=1)
        r=classify_role_pre(event(),[more_defended,closer])
        self.assertEqual(r['role_zone_id'],'nearer')

    def test_qualify_only_confirmed_defended_levels(self):
        invalid=zone(episodes=0)
        r=classify_role_pre(event(),[invalid])
        self.assertEqual(r['pre_role'],ROLE_NO_NEAR)
        invalid=zone();invalid['formation_closed_ts']=invalid['retest_ts']
        self.assertEqual(classify_role_pre(event(),[invalid])['pre_role'],ROLE_NO_NEAR)

    def test_flip_history_high_can_be_support(self):
        r=classify_role_pre(event(side='LONG'),
                            [zone(origin='PRIOR_HIGH',low=99.2,high=99.4)])
        self.assertEqual(r['pre_role'],ROLE_REBOUND)
        self.assertEqual(r['role_origin'],'PRIOR_HIGH')

    def test_known_and_outcome_never_change_pre_role_or_zone(self):
        ev=event(side='SHORT');zo=zone()
        expected=classify_role_pre(ev,[zo])
        ev2=deepcopy(ev);zo2=deepcopy(zo)
        ev2.update(known_low=1,known_high=10000,outcome_norm='TP_FIRST',
                   first_touch_minute=5)
        zo2.update(known_touch=True,known_hold=True,state_known='DIRECT_TOUCH_HELD_KNOWN',
                   outcome_norm='SL_FIRST')
        got=classify_role_pre(ev2,[zo2])
        self.assertEqual(got,expected)

    def test_build_one_per_event_timeframe_and_no_outcome(self):
        events=pd.DataFrame([event('long','LONG'),event('short','SHORT')])
        levels=pd.DataFrame([zone('long',tf='30m'),zone('short',tf='15m')])
        out=build_pre_roles(events,levels)
        self.assertEqual(len(out),2)
        self.assertEqual(set(out.pre_role),{ROLE_REBOUND,ROLE_BREAK})
        out15=build_pre_roles(events,levels,'15m')
        self.assertEqual(out15.set_index('event_key').loc['long','pre_role'],ROLE_NO_NEAR)
        with self.assertRaises(ValueError):build_pre_roles(events,levels,'5m')

    def test_summary_counts_no_hit_and_nan_not_as_losses(self):
        outcomes=['TP_FIRST','SL_FIRST','NO_HIT','SIN_RESULTADO']
        df=pd.DataFrame({'role_pre_label':[ROLE_LABELS[ROLE_REBOUND]]*4,
                         'outcome_norm':outcomes})
        s=group_summary(df,col='role_pre_label',min_resolved=2).iloc[0]
        self.assertEqual(int(s['TP']),1)
        self.assertEqual(int(s['SL']),1)
        self.assertEqual(int(s['NO_HIT']),1)
        self.assertEqual(int(s['SIN_RESULTADO']),1)
        self.assertEqual(int(s['N resueltos']),2)


if __name__=='__main__':unittest.main()


class RoleInspectorIntegrationTests(unittest.TestCase):
    def test_inspector_renders_pre_role_zone_when_known_touch_chose_other_zone(self):
        """The plotted level MUST match the independent PRE-role choice."""
        import sys
        import tempfile
        import types
        from pathlib import Path
        from unittest.mock import patch
        import plotly.graph_objects as go
        class FakePane:
            def __init__(self,parent): self.parent=parent
            def __enter__(self): return self
            def __exit__(self,*args): return False
            def metric(self,*args,**kwargs): pass
            def selectbox(self,label,options,**kwargs): return list(options)[0]
            def radio(self,label,options,index=0,**kwargs): return list(options)[index]
            def select_slider(self,label,options,value,**kwargs): return value
            def caption(self,*args,**kwargs): pass
        class FakeSt(types.ModuleType):
            def __init__(self):
                super().__init__('streamlit')
                self.errors=[];self.tables=[];self.plots=0
            def cache_data(self,**kwargs): return lambda f:f
            def columns(self,n,**kwargs):
                return [FakePane(self) for _ in range(n if isinstance(n,int) else len(n))]
            def markdown(self,*args,**kwargs): pass
            def caption(self,*args,**kwargs): pass
            def warning(self,*args,**kwargs): pass
            def error(self,msg,*args,**kwargs): self.errors.append(str(msg))
            def info(self,*args,**kwargs): pass
            def selectbox(self,label,options,**kwargs): return list(options)[0]
            def select_slider(self,label,options,value,**kwargs): return value
            def expander(self,*args,**kwargs): return FakePane(self)
            def dataframe(self,data,**kwargs): self.tables.append(data)
            def plotly_chart(self,*args,**kwargs): self.plots+=1
            def download_button(self,*args,**kwargs): pass
        from dashboard.views.reaction_horizontal_level_rich_card_view import render_horizontal_level_rich_card

        fake=FakeSt()
        engine=types.ModuleType('reaction_structural_level_atlas_v2')
        engine.v1=types.SimpleNamespace(lineage=types.SimpleNamespace(ROOT_REL=Path('reports/reaction_lab')))
        engine.OUTPUT='reaction_structural_level_atlas_v2'
        atlas_view=types.ModuleType('dashboard.views.reaction_structural_level_atlas_v2_view')
        ev=pd.DataFrame([{'event_key':'e','retest_ts':60_000_000,
                          'symbol':'TESTUSDT','side':'LONG','date_utc':'2026-10-21',
                          'all_covered':1}])
        lv=pd.DataFrame([
            zone('e',label='PRE_SUPPORT',low=99.0,high=99.5,atr=2.0,tf='15m'),
            zone('e',label='KNOWN_RESISTANCE',low=101.0,high=101.5,atr=2.0,tf='30m'),
        ])
        atlas_view.load=lambda _: (lv,ev,{'version':'structural-level-atlas-v2.0','events':1})
        selected=[]
        def draw(root,item,zones,**kwargs):
            selected.extend(zones.zone_id.tolist())
            return go.Figure(go.Candlestick(x=pd.date_range('2026-10-21',periods=4,freq='15min'),
                open=[100.0]*4,high=[101.4]*4,low=[98.9]*4,close=[100.0]*4))
        atlas_view._read_structural_figure=draw
        overlay=types.ModuleType('dashboard.views.reaction_outcome_overlay')
        overlay.get_post_bars=lambda *a: ([], 'MISSING')
        overlay.overlay_future=lambda fig,*a,**kwargs:fig
        overlay.outcome_explanation=lambda *a,**kwargs:'No path'
        with tempfile.TemporaryDirectory() as root:
            folder=Path(root)/'reports/reaction_lab/reaction_structural_level_atlas_v2'
            folder.mkdir(parents=True)
            for fn in ('summary.json','event_map.csv','candidate_levels.csv'):
                (folder/fn).write_text('stub')
            outcomes=pd.DataFrame([{'event_key':'e','outcome':'TP_FIRST',
                                    'entry_ts':60_060_000,'entry_price':101.2,
                                    'first_touch_minute':15}])
            with patch.dict(sys.modules,{'streamlit':fake,
                            'reaction_structural_level_atlas_v2':engine,
                            'dashboard.views.reaction_structural_level_atlas_v2_view':atlas_view,
                            'dashboard.views.reaction_outcome_overlay':overlay}):
                with patch('dashboard.views.reaction_horizontal_level_rich_card_view.load_retest_candles',
                           side_effect=lambda root,data:data.assign(known_open=101.2,
                               known_high=101.3,known_low=101.1,known_close=101.2)):
                    render_horizontal_level_rich_card(outcomes,root,tp_pct=1.,sl_pct=1.,outcome_horizon=360)
        self.assertEqual(fake.errors,[])
        self.assertEqual(selected,['PRE_SUPPORT'])
        self.assertEqual(fake.plots,1)
        self.assertGreaterEqual(len(fake.tables),3)
