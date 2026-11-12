"""BTC Multi-Swing UI V3.1: exact UTC PRE replay and opt-in POST guards.

Tests are entirely synthetic: no Redis, network, orders or LIVE services.
Run from /home/multiservices-1m with ``python3 -m unittest -v
 test_multiswing_ui_v31_replay``.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
import sys
import unittest
from unittest.mock import patch

import pandas as pd

# Plotly's lazy graph-object imports can create distinct Shape class identities
# when multiple independent synthetic Streamlit modules render in one unittest
# process. Initialize the actual Figure+Shape validator before those mocks.
import plotly.graph_objects as go
from plotly.graph_objs.layout._shape import Shape
_PLOTLY_SHAPE_VALIDATOR_READY = (go.Figure().layout._validators['shapes'].data_class is Shape)

from btc_multiswing_60d_explorer_v1 import DAY, M, aggregate_closed
from dashboard.views.reaction_multiswing_btc60d_view_v1 import (
    v31_replay_cutoff_utc, v31_replay_visual_window,
    v31_reveal_post_window, v31_pre_region_segments,
    v31_replay_candidates, render_btc_multiswing_60d_view,
)
from test_multiswing_ui_v3_modes import _UI


def utc(day, hour=0, minute=0):
    return int(datetime(day.year, day.month, day.day, hour, minute,
                        tzinfo=timezone.utc).timestamp()*1000)


class ReplayTimeGuards(unittest.TestCase):
    def setUp(self):
        self.start = utc(date(2026, 11, 6), 1)
        self.end = utc(date(2026, 11, 8), 1)
        self.middle = utc(date(2026, 11, 7), 12, 30)

    def test_exact_utc_minute_and_closed_candle(self):
        self.assertEqual(v31_replay_cutoff_utc(date(2026,11,7),time(12,30),
                        source_start=self.start,source_end=self.end),self.middle)

    def test_first_minute_allowed_but_not_source_start(self):
        with self.assertRaisesRegex(ValueError,'REPLAY_CUTOFF_OUTSIDE_FROZEN_WINDOW'):
            v31_replay_cutoff_utc(date(2026,11,6),time(1,0),
                        source_start=self.start,source_end=self.end)
        self.assertEqual(v31_replay_cutoff_utc(date(2026,11,6),time(1,1),
                         source_start=self.start,source_end=self.end),self.start+M)

    def test_future_clock_is_refused(self):
        with self.assertRaisesRegex(ValueError,'REPLAY_CUTOFF_OUTSIDE_FROZEN_WINDOW'):
            v31_replay_cutoff_utc(date(2026,11,8),time(1,1),
                        source_start=self.start,source_end=self.end)

    def test_ambiguous_or_subminute_time_is_refused(self):
        for clock in (time(12,30,1),time(12,30,0,1),
                      time(12,30,tzinfo=timezone.utc)):
            with self.subTest(clock=str(clock)):
                with self.assertRaisesRegex(ValueError,'INVALID_REPLAY_UTC_TIME'):
                    v31_replay_cutoff_utc(date(2026,11,7),clock,
                             source_start=self.start,source_end=self.end)

    def test_replay_context_only_closed_pre(self):
        self.assertEqual(v31_replay_visual_window(source_start=self.start,
                         cutoff=self.middle,hours=24), (self.middle-DAY,self.middle))
        self.assertEqual(v31_replay_visual_window(source_start=self.start,
                         cutoff=self.start+M,hours=48),(self.start,self.start+M))
        for hours in (0,1,49,1440):
            with self.subTest(hours=hours),self.assertRaises(ValueError):
                v31_replay_visual_window(source_start=self.start,
                                      cutoff=self.middle,hours=hours)

    def test_post_must_be_opt_in_and_clamped(self):
        self.assertEqual(v31_reveal_post_window(pre_cutoff=self.middle,
                         source_end=self.end,reveal=False,horizon_minutes=240),
                         (self.middle,'HIDDEN'))
        self.assertEqual(v31_reveal_post_window(pre_cutoff=self.middle,
                         source_end=self.end,reveal=True,horizon_minutes=240),
                         (self.middle+240*M,'FULL_HORIZON'))
        self.assertEqual(v31_reveal_post_window(pre_cutoff=self.end-10*M,
                         source_end=self.end,reveal=True,horizon_minutes=240),
                         (self.end,'CUTOFF_CENSORED'))
        self.assertEqual(v31_reveal_post_window(pre_cutoff=self.end,
                         source_end=self.end,reveal=True,horizon_minutes=60),
                         (self.end,'CUTOFF_CENSORED'))

    def test_pre_five_minute_chart_excludes_partial_bar(self):
        frame=pd.DataFrame({'timestamp':[self.start+i*M for i in range(120)],
                            'open':[100.]*120,'high':[101.]*120,
                            'low':[99.]*120,'close':[100.]*120})
        cutoff=self.start+63*M  # 1h 03m, so latest 5m bar is incomplete.
        candles=aggregate_closed(frame,'5m',self.start,cutoff)
        self.assertTrue((candles.timestamp+5*M <=cutoff).all())
        self.assertTrue((candles.timestamp+5*M >cutoff-5*M).any())


class ReplayStageGuards(unittest.TestCase):
    def setUp(self):
        self.t=utc(date(2026,11,7),12,30)
        self.start=self.t-8*60*M
        self.stages=pd.DataFrame([
            {'region_id':'low','side':'LOW','known_from':self.t-6*60*M,
             'known_until':self.t-2*60*M,'band_low':99.,'band_high':100.,
             'independent_extrema':2},
            {'region_id':'low','side':'LOW','known_from':self.t-2*60*M,
             'known_until':self.t,'band_low':98.,'band_high':100.,
             'independent_extrema':3},
            {'region_id':'low','side':'LOW','known_from':self.t,
             'known_until':self.t,'band_low':97.,'band_high':100.,
             'independent_extrema':4},
            {'region_id':'low','side':'LOW','known_from':self.t+2*60*M,
             'known_until':self.t+5*60*M,'band_low':70.,'band_high':101.,
             'independent_extrema':5},
        ])

    def test_pre_stages_keep_old_geometry_before_each_confirmation(self):
        visible=v31_pre_region_segments(self.stages,region_id='low',
                       pre_start=self.start,pre_cutoff=self.t)
        self.assertEqual(visible.band_low.tolist(),[99.,98.])
        self.assertEqual(visible.visible_from.tolist(),
                         [self.t-6*60*M,self.t-2*60*M])
        self.assertEqual(visible.visible_until.tolist(),
                         [self.t-2*60*M,self.t])
        self.assertTrue((visible.visible_until<=self.t).all())

    def test_future_stage_price_mutations_cannot_change_pre_segments(self):
        original=v31_pre_region_segments(self.stages,region_id='low',
                     pre_start=self.start,pre_cutoff=self.t)
        corrupted=self.stages.copy()
        corrupted.loc[corrupted.known_from>self.t,'band_low']=-100000
        updated=v31_pre_region_segments(corrupted,region_id='low',
                     pre_start=self.start,pre_cutoff=self.t)
        pd.testing.assert_frame_equal(original,updated)

    def test_stage_known_exactly_at_cutoff_is_selectable_not_projected_backwards(self):
        # The source V2 stage is zero-duration at the exact PRE time.
        regions=pd.DataFrame([{'region_id':'low','side':'LOW',
            'born_ts':self.t-6*60*M,'last_confirmation_ts':self.t,
            'members':'a,b,c,d','temporalities':'15m','status':'CANDIDATE'}])
        selected=v31_replay_candidates(regions,self.stages.iloc[:3],
                       self.start,self.t,min_extrema=4)
        self.assertEqual(len(selected),1)
        self.assertEqual(float(selected.iloc[0].band_low),97.)
        visible=v31_pre_region_segments(self.stages,region_id='low',
                       pre_start=self.start,pre_cutoff=self.t,min_extrema=4)
        self.assertTrue(visible.empty)  # No rectangle before birth-at-cutoff.

    def test_future_birth_not_selectable(self):
        regions=pd.DataFrame([{'region_id':'future','side':'HIGH',
            'born_ts':self.t+M,'last_confirmation_ts':self.t+M,
            'members':'a,b','temporalities':'15m','status':'CANDIDATE'}])
        future=pd.DataFrame([{'region_id':'future','side':'HIGH',
            'known_from':self.t+M,'known_until':self.t+4*M,
            'band_low':100.,'band_high':101.,'independent_extrema':2}])
        result=v31_replay_candidates(regions,future,self.start,self.t)
        self.assertTrue(result.empty)


class _ReplayUI(_UI):
    def __init__(self, *, moment: time, reveal: bool, exact_region_id: str):
        super().__init__('EJECUCION')
        self.moment=moment
        self.region_id=exact_region_id
        self.fig=None
        self.post_requested=reveal
        self.cutoff_known=None
        self.messages=[]
    def date_input(self,label,**kwargs):
        return date(2026,11,7)
    def time_input(self,label,**kwargs):
        return self.moment
    def selectbox(self,label,options,**kwargs):
        if kwargs.get('key')=='ms60_v21_region_selection':
            return self.region_id
        return super().selectbox(label,options,**kwargs)
    def checkbox(self,label,**kwargs):
        if kwargs.get('key')=='ms60_v31_reveal_post':
            return bool(self.session_state.get('ms60_v31_reveal_post',False))
        return super().checkbox(label,**kwargs)
    def plotly_chart(self,fig,**kwargs):
        self.fig=fig
        super().plotly_chart(fig,**kwargs)
    def caption(self,*args,**kwargs):
        self.messages.append(' '.join(str(a) for a in args))


class ReplayRenderIsolation(unittest.TestCase):
    def test_post_reveal_only_adds_candles_not_future_structural_evidence(self):
        import dashboard.views.reaction_multiswing_btc60d_view_v1 as view
        start=utc(date(2026,11,6),1)
        end=utc(date(2026,11,8),1)
        t=utc(date(2026,11,7),12,30)
        ts=list(range(start,end,M))
        full=pd.DataFrame({'timestamp':ts,'open':[100.]*len(ts),
             'high':[101.]*len(ts),'low':[99.]*len(ts),'close':[100.]*len(ts)})
        rid='msv2_testregion'
        born=t-6*60*M
        regions=pd.DataFrame([{'region_id':rid,'side':'LOW','center':99.8,
             'band_low':98.,'band_high':100.,'first_pivot_ts':born-3*M,
             'last_pivot_ts':t-2*M,'born_ts':born,
             'last_confirmation_ts':t,'independent_extrema':3,
             'members':'a,b,c','temporalities':'15m',
             'status':'GEOMETRIC_CANDIDATE_NOT_ACTIVE'}])
        stages=pd.DataFrame([
             {'region_id':rid,'side':'LOW','known_from':born,
              'known_until':t-60*M,'band_low':99.,'band_high':100.,
              'independent_extrema':2},
             {'region_id':rid,'side':'LOW','known_from':t-60*M,
              'known_until':t,'band_low':98.,'band_high':100.,
              'independent_extrema':3},
        ])
        no_pivots=pd.DataFrame(columns=['plot_ts','timeframe','group_key',
                  'pivot_id','side','price','physical_identity_state'])
        stats={'representations':0,'verified_extrema':0,
               'multi_tf_extrema':0,'ambiguous_representations':0}
        source={'start':start,'end':end,'source_sha256':'freeze',
                'catalog_sha256':'catalog','source_hll_parity':'PARITY_OK'}
        rendered=[]
        known_cutoffs=[]
        for reveal in (False,True):
            ui=_ReplayUI(moment=time(12,30),reveal=reveal,exact_region_id=rid)
            ui.session_state['ms60_v31_last_cutoff']=t
            ui.session_state['ms60_v31_reveal_post']=reveal
            def capture_rows(catalog,cutoff,tfs,side):
                known_cutoffs.append(cutoff)
                return pd.DataFrame()
            with patch.dict(sys.modules,{'streamlit':ui}), \
                 patch.object(view,'load_snapshot',return_value=(full,pd.DataFrame(),source)), \
                 patch.object(view,'asof_rows',side_effect=capture_rows), \
                 patch.object(view,'group_asof',return_value=no_pivots), \
                 patch.object(view,'window_evidence_stats',return_value=stats), \
                 patch.object(view,'construct_regions',return_value=(regions,stages,{
                     'candidate_regions':1,'physical_extrema_evaluated':3,
                     'ambiguous_excluded':0})), \
                 patch.object(view,'region_member_evidence',return_value=pd.DataFrame()), \
                 patch.object(view,'audit_fixed_region',side_effect=AssertionError('V22_CALLED')):
                render_btc_multiswing_60d_view('/tmp/none')
            self.assertEqual(ui.plot_keys,['ms60_chart'])
            self.assertEqual(ui.session_state['ms60_v31_last_cutoff'],t)
            rendered.append(ui.fig)
        self.assertEqual(known_cutoffs,[t,t])
        pre,post=rendered
        self.assertEqual(len(pre.data[0].x),288)
        self.assertEqual(len(post.data[0].x),300)  # 60m POST / 5m
        self.assertFalse(any('POST' in str(a.text) for a in pre.layout.annotations))
        self.assertTrue(any('POST' in str(a.text) for a in post.layout.annotations))
        # No band may be shown before its own stage known_from.
        pre_bands=[s for s in pre.layout.shapes if s.yref=='y']
        self.assertTrue(all(pd.Timestamp(s.x0).value//1000000>=born
                            for s in pre_bands))
        post_bands=[s for s in post.layout.shapes if s.yref=='y']
        self.assertEqual(len(post_bands),len(pre_bands)+1)
        self.assertEqual(float(post_bands[-1].y0),98.)  # PRE last-stage snapshot

    def test_moving_pre_time_hides_already_revealed_future(self):
        # This is tested at the control boundary, not against actual LIVE time.
        from dashboard.views import reaction_multiswing_btc60d_view_v1 as view
        # A straightforward source guard prevents a future regression that
        # lets a previous time's POST remain open after moving the replay.
        import inspect
        source=inspect.getsource(view.render_btc_multiswing_60d_view)
        self.assertIn("st.session_state['ms60_v31_reveal_post'] = False",source)
        self.assertIn("st.session_state['ms60_v31_last_cutoff']",source)
        self.assertLess(source.index("st.session_state['ms60_v31_reveal_post'] = False"),
                        source.index("reveal_post=st.checkbox("))


if __name__=='__main__':
    unittest.main()
