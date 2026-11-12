"""UI V3.2: optional multi-timeframe dual-panel PRE/POST replay.

Offline synthetic tests: no Redis, external data, LIVE orders, or HLL reads.
The same decision-time cutoff is shared by BOTH charts, independently of TF.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
import inspect
import sys
import unittest
from unittest.mock import patch

import pandas as pd
import plotly.graph_objects as go
from plotly.graph_objs.layout._shape import Shape
_PLOTLY_SHAPES_READY = (go.Figure().layout._validators['shapes'].data_class is Shape)
from plotly.graph_objs.layout._annotation import Annotation
_PLOTLY_ANNOTATIONS_READY = (go.Figure().layout._validators['annotations'].data_class is Annotation)

from btc_multiswing_60d_explorer_v1 import DAY, M, TF_MINUTES, aggregate_closed
from dashboard.views.reaction_multiswing_btc60d_view_v1 import (
    V32_PANEL_TFS, V32_CONTEXT_DAYS, build_figure,
    v32_build_context_chart, v32_context_window,
    render_btc_multiswing_60d_view,
)
from test_multiswing_ui_v3_modes import _UI


def utc(y=2026,m=11,d=7,h=13,minute=30):
    return int(datetime(y,m,d,h,minute,tzinfo=timezone.utc).timestamp()*1000)


def sample_full(start:int, end:int) -> pd.DataFrame:
    ts=range(start,end,M)
    return pd.DataFrame({'timestamp':list(ts),
      'open':[100.]*len(ts),'high':[101.]*len(ts),
      'low':[99.]*len(ts),'close':[100.]*len(ts)})


def example_region(t):
    rid='msv2_context_test'
    born=t-16*60*M
    regions=pd.DataFrame([{'region_id':rid,'side':'LOW',
        'center':99.5,'band_low':99.,'band_high':100.,
        'born_ts':born,'last_confirmation_ts':t,
        'independent_extrema':3,'first_pivot_ts':born-60*M,
        'last_pivot_ts':t-120*M,'members':'a,b,c',
        'temporalities':'15m,1h',
        'status':'GEOMETRIC_CANDIDATE_NOT_ACTIVE'}])
    stages=pd.DataFrame([
        {'region_id':rid,'side':'LOW','known_from':born,
         'known_until':t-6*60*M,'band_low':99.,'band_high':100.,
         'independent_extrema':2},
        {'region_id':rid,'side':'LOW','known_from':t-6*60*M,
         'known_until':t,'band_low':98.5,'band_high':100.,
         'independent_extrema':3},
        {'region_id':rid,'side':'LOW','known_from':t+60*M,
         'known_until':t+240*M,'band_low':80.,'band_high':101.,
         'independent_extrema':4},
    ])
    return rid,regions,stages


class MultiTfReplayGuards(unittest.TestCase):
    def setUp(self):
        self.t=utc()
        self.start=self.t-4*DAY
        self.end=self.t+12*60*M
        self.full=sample_full(self.start,self.end)
        self.rid,self.regions,self.stages=example_region(self.t)
        self.pre_region=self.regions.iloc[0].copy()
        self.pre_region['band_low']=98.5

    def test_all_six_timeframes_supported(self):
        self.assertEqual(tuple(TF_MINUTES),V32_PANEL_TFS)
        self.assertEqual(V32_PANEL_TFS,('1m','5m','15m','30m','1h','4h'))
        self.assertIn(60,V32_CONTEXT_DAYS)

    def test_window_clamps_to_freeze_and_rejects_invalid(self):
        self.assertEqual(v32_context_window(source_start=self.start,
          cutoff=self.t,days=7),(self.start,self.t))
        self.assertEqual(v32_context_window(source_start=self.start,
          cutoff=self.t,days=3),(self.t-3*DAY,self.t))
        for n in (-1,0,2,61):
            with self.subTest(days=n),self.assertRaises(ValueError):
                v32_context_window(source_start=self.start,cutoff=self.t,days=n)

    def test_high_timeframe_excludes_partial_pre_candles(self):
        for tf in V32_PANEL_TFS:
            with self.subTest(tf=tf):
                fig,n,left=v32_build_context_chart(self.full,stages=self.stages,
                  selected_region=self.pre_region,region_id=self.rid,
                  timeframe=tf,source_start=self.start,pre_cutoff=self.t,
                  chart_end=self.t,days=1)
                self.assertIsNotNone(fig)
                last_close=max(pd.Timestamp(x).value//1_000_000 for x in fig.data[0].x)
                self.assertLessEqual(last_close,self.t)
                # No future stage (band_low=80) can contaminate PRE.
                band_shapes=[x for x in fig.layout.shapes if x.yref=='y']
                self.assertTrue(band_shapes)
                self.assertTrue(all(float(x.y0)>=98.5 for x in band_shapes))

    def test_post_4h_candle_straddling_cutoff_appears_only_at_close(self):
        # 12:00–16:00 is not known at 13:30. It may only be shown after
        # explicitly revealing POST extending to at least 16:00.
        fig_pre,n,left=v32_build_context_chart(self.full,stages=self.stages,
             selected_region=self.pre_region,region_id=self.rid,timeframe='4h',
             source_start=self.start,pre_cutoff=self.t,chart_end=self.t,days=3)
        fig_post,np,_=v32_build_context_chart(self.full,stages=self.stages,
             selected_region=self.pre_region,region_id=self.rid,timeframe='4h',
             source_start=self.start,pre_cutoff=self.t,
             chart_end=self.t+240*M,days=3)
        xpre=[pd.Timestamp(x).value//1_000_000 for x in fig_pre.data[0].x]
        xpost=[pd.Timestamp(x).value//1_000_000 for x in fig_post.data[0].x]
        fourpm=utc(h=16,minute=0)
        self.assertNotIn(fourpm,xpre)
        self.assertIn(fourpm,xpost)
        self.assertTrue(all(x<=self.t for x in xpre))
        self.assertTrue(all(x>self.t for x in set(xpost)-set(xpre)))
        self.assertEqual(np,n+1)
        self.assertEqual(len([x for x in fig_post.layout.shapes if x.yref=='y']),
                         len([x for x in fig_pre.layout.shapes if x.yref=='y'])+1)
        self.assertEqual(float([x for x in fig_post.layout.shapes
                         if x.yref=='y'][-1].y0),98.5)

    def test_future_stage_mutation_does_not_change_pre_or_post_geometry(self):
        old=self.stages.copy()
        later=self.stages.copy()
        later.loc[later.known_from>self.t,'band_low']=-500
        def render(stages):
            fig,_,_=v32_build_context_chart(self.full,stages=stages,
                selected_region=self.pre_region,region_id=self.rid,
                timeframe='1h',source_start=self.start,
                pre_cutoff=self.t,chart_end=self.t+240*M,days=3)
            return [(float(x.y0),float(x.y1),pd.Timestamp(x.x0))
                    for x in fig.layout.shapes if x.yref=='y']
        self.assertEqual(render(old),render(later))

    def test_unselected_band_does_not_invent_evidence(self):
        fig,n,left=v32_build_context_chart(self.full,stages=None,
          selected_region=None,region_id='',timeframe='1h',
          source_start=self.start,pre_cutoff=self.t,
          chart_end=self.t,days=3)
        self.assertIsNotNone(fig)
        self.assertFalse(any(s.yref=='y' for s in fig.layout.shapes))

    def test_large_tf_does_not_silently_limit_bars(self):
        fig,n,left=v32_build_context_chart(self.full,stages=None,
          selected_region=None,region_id='',timeframe='1m',
          source_start=self.start,pre_cutoff=self.t,
          chart_end=self.t,days=3)
        self.assertIsNone(fig)
        self.assertEqual(n,3*1440)

    def test_main_replay_candles_use_close_timestamp(self):
        t=self.t
        frame=aggregate_closed(self.full,'4h',t-DAY,t+240*M)
        empty=pd.DataFrame(columns=['timeframe','side'])
        fig=build_figure(frame,empty,display_tf='4h',
          cutoff=t,chart_end=t+240*M,candle_x_at_close=True)
        self.assertTrue(all(pd.Timestamp(x).value//1_000_000 % (240*M)==0
                            for x in fig.data[0].x))
        self.assertIn(utc(h=16,minute=0),
                      [pd.Timestamp(x).value//1_000_000 for x in fig.data[0].x])


class _DualUI(_UI):
    def __init__(self, *, region_id, reveal=False, show_context=True,main_tf='5m',context_tf='1h'):
        super().__init__('EJECUCION')
        self.region_id=region_id
        self.reveal=reveal
        self.show_context=show_context
        self.main_tf=main_tf
        self.context_tf=context_tf
        self.figures={}
    def date_input(self,label,**kwargs):
        return date(2026,11,7)
    def time_input(self,label,**kwargs):
        return time(13,30)
    def selectbox(self,label,options,**kwargs):
        key=kwargs.get('key')
        if key=='ms60_v3_execution_tf':
            return self.main_tf
        if key=='ms60_v32_context_tf':
            return self.context_tf
        if key=='ms60_v32_context_days':
            return 3
        if key=='ms60_v31_post_minutes':
            return 240
        if key=='ms60_v21_region_selection':
            return self.region_id
        return super().selectbox(label,options,**kwargs)
    def checkbox(self,label,**kwargs):
        key=kwargs.get('key')
        if key=='ms60_v32_show_context':
            return self.show_context
        if key=='ms60_v31_reveal_post':
            return self.reveal
        return super().checkbox(label,**kwargs)
    def plotly_chart(self,fig,**kwargs):
        self.figures[kwargs.get('key')]=fig
        super().plotly_chart(fig,**kwargs)


class DualPanelIntegration(unittest.TestCase):
    def test_same_pre_cutoff_controls_both_charts_and_post_is_opt_in(self):
        import dashboard.views.reaction_multiswing_btc60d_view_v1 as view
        t=utc()
        start=t-4*DAY
        end=t+12*60*M
        full=sample_full(start,end)
        rid,regions,stages=example_region(t)
        source={'start':start,'end':end,
                'source_sha256':'synthetic','catalog_sha256':'synthetic',
                'source_hll_parity':'PARITY_OK'}
        no_pivots=pd.DataFrame(columns=['plot_ts','timeframe','group_key',
                  'pivot_id','side','price','physical_identity_state'])
        stats={'representations':0,'verified_extrema':0,
               'multi_tf_extrema':0,'ambiguous_representations':0}
        observed=[]
        for reveal in (False,True):
            ui=_DualUI(region_id=rid,reveal=reveal,
                       main_tf='15m',context_tf='4h')
            ui.session_state['ms60_v31_last_cutoff']=t
            ui.session_state['ms60_v31_reveal_post']=reveal
            def asof_capture(catalog,cutoff,tfs,side):
                observed.append(cutoff)
                return pd.DataFrame()
            with patch.dict(sys.modules,{'streamlit':ui}), \
                 patch.object(view,'load_snapshot',return_value=(full,pd.DataFrame(),source)), \
                 patch.object(view,'asof_rows',side_effect=asof_capture), \
                 patch.object(view,'group_asof',return_value=no_pivots), \
                 patch.object(view,'window_evidence_stats',return_value=stats), \
                 patch.object(view,'construct_regions',return_value=(regions,stages,{
                     'candidate_regions':1,'physical_extrema_evaluated':3,
                     'ambiguous_excluded':0})), \
                 patch.object(view,'region_member_evidence',return_value=pd.DataFrame()), \
                 patch.object(view,'audit_fixed_region',side_effect=AssertionError('V22_MUST_NOT_RUN')):
                render_btc_multiswing_60d_view('/tmp/no-live')
            self.assertEqual(ui.plot_keys,['ms60_chart','ms60_v32_context_chart'])
            main=ui.figures['ms60_chart']
            secondary=ui.figures['ms60_v32_context_chart']
            xmain=[pd.Timestamp(x).value//1_000_000 for x in main.data[0].x]
            xsecondary=[pd.Timestamp(x).value//1_000_000 for x in secondary.data[0].x]
            if not reveal:
                self.assertTrue(all(x<=t for x in xmain+xsecondary))
                self.assertFalse(any('POST' in str(x.text) for x in main.layout.annotations))
            else:
                self.assertTrue(any(x>t for x in xmain))
                self.assertTrue(any(x>t for x in xsecondary))
                self.assertEqual(float([x for x in secondary.layout.shapes
                     if x.yref=='y'][-1].y0),98.5)
        self.assertEqual(observed,[t,t,t,t])

    def test_second_panel_is_optional(self):
        # Preserve the original one-chart UX when unchecked.
        self.assertFalse(_DualUI(region_id='',show_context=False).show_context)

if __name__=='__main__':
    unittest.main()
