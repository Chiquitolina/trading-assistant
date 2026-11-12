"""V3.1.2: retrospective event navigation; no LIVE or strategy orders."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch
import sys
import unittest

import pandas as pd

from btc_multiswing_60d_explorer_v1 import DAY, M
from dashboard.views import reaction_multiswing_btc60d_view_v1 as view
from test_multiswing_ui_v3_modes import _UI


def utc(day=7,hour=13,minute=30):
    return int(datetime(2026,11,day,hour,minute,tzinfo=timezone.utc).timestamp()*1000)


def example_data():
    t=utc()
    start=t-DAY
    end=t+8*60*M
    stamps=list(range(start,end,M))
    full=pd.DataFrame({'timestamp':stamps,
        'open':[101.5]*len(stamps),
        'high':[102.0]*len(stamps),
        'low':[101.0]*len(stamps),
        'close':[101.5]*len(stamps)})
    rid='msv2_auto_test'
    born=t-6*60*M
    stages=pd.DataFrame([{
        'region_id':rid,'known_from':born,'known_until':end,
        'band_low':99.,'band_high':100.,'independent_extrema':2,
        'side':'LOW',
    }])
    regions=pd.DataFrame([{'region_id':rid,'side':'LOW','center':99.5,
        'band_low':99.,'band_high':100.,'first_pivot_ts':born-5*M,
        'last_pivot_ts':born-2*M,'born_ts':born,
        'last_confirmation_ts':born,'independent_extrema':2,
        'members':'a,b','temporalities':'15m,30m',
        'status':'GEOMETRIC_CANDIDATE_NOT_ACTIVE'}])
    # First touch is known only after 13:30–13:31 has CLOSED.
    full.loc[full.timestamp.eq(t),'low']=99.5
    full.loc[full.timestamp.eq(t+2*M),'low']=99.7
    full.loc[full.timestamp.eq(t+45*M),'low']=99.8
    return full,stages,regions,t,start,end,rid


class EventIndexV33(unittest.TestCase):
    def setUp(self):
        self.full,self.stages,self.regions,self.t,self.start,self.end,self.rid=example_data()

    def event_rows(self, gap=30, full=None, stages=None):
        return view.v33_structural_event_index(
            self.full if full is None else full,
            self.stages if stages is None else stages,
            region_id=self.rid,source_end=self.end,gap_minutes=gap)

    def test_first_touch_decision_time_is_bar_open_not_post_close(self):
        events=self.event_rows()
        contact=events.loc[events.event_type.eq('ARRIVAL')].sort_values('decision_ts')
        self.assertEqual(contact.decision_ts.tolist(),[self.t,self.t+2*M,self.t+45*M])
        self.assertEqual(view.v33_auto_decision_cutoff(
            contact.iloc[0],source_start=self.start,source_end=self.end,
            event_type='ARRIVAL'),self.t)
        # The 1m bar starting exactly at the decision time is not PRE.
        pre=self.full.loc[self.full.timestamp<self.t]
        self.assertNotIn(self.t,pre.timestamp.tolist())
        self.assertEqual(pre.low.min(),101.0)

    def test_visit_groups_follow_selected_thirty_minute_gap(self):
        visits=self.event_rows().loc[lambda x:x.event_type.eq('VISIT')].sort_values('decision_ts')
        self.assertEqual(visits.decision_ts.tolist(),[self.t,self.t+45*M])
        self.assertEqual(visits.visit_number.tolist(),[1,2])
        self.assertEqual(self.event_rows(gap=60).event_type.eq('VISIT').sum(),1)
        self.assertEqual(self.event_rows(gap=5).event_type.eq('VISIT').sum(),2)

    def test_confirmation_starts_at_known_from_not_physical_pivot(self):
        rows=self.event_rows().loc[lambda x:x.event_type.eq('CONFIRMATION')]
        self.assertEqual(rows.decision_ts.tolist(),[self.t-6*60*M])
        self.assertEqual(rows.extrema_at_event.tolist(),[2])

    def test_stage_geometry_is_selected_asof_contact_open(self):
        stages=self.stages.copy()
        extra=stages.iloc[0].copy()
        extra['known_from']=self.t+30*M
        extra['band_low']=98.
        extra['band_high']=101.
        extra['independent_extrema']=3
        stages.loc[0,'known_until']=self.t+30*M
        stages=pd.concat([stages,pd.DataFrame([extra])],ignore_index=True)
        events=self.event_rows(stages=stages)
        first=events[(events.event_type=='ARRIVAL') &
                     (events.decision_ts==self.t)].iloc[0]
        self.assertEqual(first.band_low,99.)
        self.assertEqual(first.stage_known_from,self.t-6*60*M)
        self.assertTrue(all(r.stage_known_from <= r.decision_ts
            for r in events.itertuples(index=False)))
        self.assertTrue(all(r.stage_known_from <= self.t
            for r in events.loc[events.decision_ts<=self.t].itertuples(index=False)))

    def test_future_candles_do_not_change_earlier_event_index(self):
        earlier_end=self.t+20*M
        old=view.v33_structural_event_index(self.full,self.stages,
            region_id=self.rid,source_end=earlier_end)
        newer=self.full.copy()
        newer.loc[newer.timestamp>=earlier_end,'low']=99.1
        fresh=view.v33_structural_event_index(newer,self.stages,
            region_id=self.rid,source_end=self.end)
        cut=fresh.loc[fresh.decision_ts<earlier_end]
        pd.testing.assert_frame_equal(
            old[old.event_type!='VISIT'].reset_index(drop=True),
            cut[cut.event_type!='VISIT'].reset_index(drop=True))

    def test_ignores_touch_before_birth_and_after_visual_expiry(self):
        full=self.full.copy()
        born=int(self.stages.iloc[0].known_from)
        full.loc[full.timestamp.eq(born-M),'low']=99.3
        stages=self.stages.copy()
        stages.loc[0,'known_until']=self.t+20*M
        full.loc[full.timestamp.eq(self.t+30*M),'low']=99.4
        rows=self.event_rows(full=full,stages=stages)
        touches=rows[rows.event_type=='ARRIVAL'].decision_ts.tolist()
        self.assertNotIn(born-M,touches)
        self.assertNotIn(self.t+30*M,touches)

    def test_same_minute_confirmations_do_not_invent_previous_stage(self):
        stages=pd.DataFrame([
            {**self.stages.iloc[0].to_dict(),'known_until':self.t-3*60*M},
            {**self.stages.iloc[0].to_dict(),'known_from':self.t-3*60*M,
             'known_until':self.t-3*60*M,'independent_extrema':3},
            {**self.stages.iloc[0].to_dict(),'known_from':self.t-3*60*M,
             'independent_extrema':4},
        ])
        rows=self.event_rows(stages=stages)
        first=rows[(rows.event_type=='ARRIVAL')&(rows.decision_ts==self.t)].iloc[0]
        self.assertEqual(first.extrema_at_event,4)
        self.assertEqual(rows[rows.event_type=='CONFIRMATION'].shape[0],2)

    def test_invalid_source_and_event_are_rejected(self):
        with self.assertRaises(ValueError):
            self.event_rows(full=self.full.iloc[1:].drop(index=2))
        with self.assertRaisesRegex(ValueError,'INVALID_AUTO_REPLAY_EVENT_TYPE'):
            view.v33_auto_decision_cutoff(
                self.event_rows().iloc[0],source_start=self.start,
                source_end=self.end,event_type='INVALID')


class _AutoUI(_UI):
    def __init__(self,rid,origin='ARRIVAL',reveal=False,show_context=True):
        super().__init__('EJECUCION')
        self.rid=rid
        self.origin=origin
        self.reveal=reveal
        self.show_context=show_context
        self.pick_latest=False
        self.cutoff_timestamps=[]
        self.figures={}
    def selectbox(self,label,options,**kwargs):
        key=kwargs.get('key')
        if key=='ms60_v33_event_origin':
            return self.origin
        if key=='ms60_v33_region_anchor':
            return self.rid
        if key=='ms60_v33_event_choice':
            return options[0] if self.pick_latest else options[-1]
        if key=='ms60_v32_context_tf':
            return '1h'
        if key=='ms60_v32_context_days':
            return 1
        if key=='ms60_v31_post_minutes':
            return 60
        return super().selectbox(label,options,**kwargs)
    def checkbox(self,label,**kwargs):
        key=kwargs.get('key')
        if key=='ms60_v32_show_context':
            return self.show_context
        if key=='ms60_v31_reveal_post':
            return self.session_state.get(key,self.reveal)
        return super().checkbox(label,**kwargs)
    def plotly_chart(self,fig,**kwargs):
        self.figures[kwargs.get('key')]=fig
        super().plotly_chart(fig,**kwargs)


class AutoNavigationRenderV33(unittest.TestCase):
    def render(self,ui,full,stages,regions,start,end):
        counters={'representations':0,'verified_extrema':0,
                  'multi_tf_extrema':0,'ambiguous_representations':0}
        empty=pd.DataFrame(columns=['plot_ts','timeframe','group_key',
            'pivot_id','side','price','physical_identity_state'])
        source={'start':start,'end':end,'source_sha256':'mock',
                'catalog_sha256':'mock','source_hll_parity':'PARITY_OK'}
        ui.plot_keys=[]
        with patch.dict(sys.modules,{'streamlit':ui}), \
             patch.object(view,'load_snapshot',return_value=(full,pd.DataFrame(),source)), \
             patch.object(view,'asof_rows',return_value=pd.DataFrame()), \
             patch.object(view,'group_asof',return_value=empty), \
             patch.object(view,'window_evidence_stats',return_value=counters), \
             patch.object(view,'construct_regions',return_value=(regions,stages,{
                'candidate_regions':1,'physical_extrema_evaluated':2,
                'ambiguous_excluded':0})), \
             patch.object(view,'region_member_evidence',return_value=pd.DataFrame()):
            view.render_btc_multiswing_60d_view('/tmp/no-live')

    def test_selected_arrival_syncs_both_panels_and_keeps_contact_bar_post(self):
        full,stages,regions,t,start,end,rid=example_data()
        counters={'representations':0,'verified_extrema':0,
                  'multi_tf_extrema':0,'ambiguous_representations':0}
        empty=pd.DataFrame(columns=['plot_ts','timeframe','group_key',
            'pivot_id','side','price','physical_identity_state'])
        source={'start':start,'end':end,'source_sha256':'mock',
                'catalog_sha256':'mock','source_hll_parity':'PARITY_OK'}
        ui=_AutoUI(rid)
        with patch.dict(sys.modules,{'streamlit':ui}), \
             patch.object(view,'load_snapshot',return_value=(full,pd.DataFrame(),source)), \
             patch.object(view,'asof_rows',return_value=pd.DataFrame()), \
             patch.object(view,'group_asof',return_value=empty), \
             patch.object(view,'window_evidence_stats',return_value=counters), \
             patch.object(view,'construct_regions',return_value=(regions,stages,{
                'candidate_regions':1,'physical_extrema_evaluated':2,
                'ambiguous_excluded':0})), \
             patch.object(view,'region_member_evidence',return_value=pd.DataFrame()):
            view.render_btc_multiswing_60d_view('/tmp/no-live')
        self.assertEqual(ui.plot_keys,['ms60_chart','ms60_v32_context_chart'])
        self.assertIn('ms60_v33_last_replay_signature',ui.session_state)
        pre_cutoff=ui.session_state['ms60_v31_last_cutoff']
        self.assertEqual(pre_cutoff,t)
        for name in ui.plot_keys:
            fig=ui.figures[name]
            x=[int(pd.Timestamp(v).value//1_000_000) for v in fig.data[0].x]
            self.assertTrue(all(v<=pre_cutoff for v in x))
            self.assertFalse(any(s.yref=='paper' and s.type=='rect'
                                 for s in fig.layout.shapes))

    def test_event_change_resets_post_and_reselects_pre_timestamp(self):
        full,stages,regions,t,start,end,rid=example_data()
        ui=_AutoUI(rid,reveal=True,show_context=False)
        self.render(ui,full,stages,regions,start,end)
        first=ui.session_state['ms60_v31_last_cutoff']
        self.assertEqual(first,t)
        ui.session_state['ms60_v31_reveal_post']=True
        ui.pick_latest=True
        self.render(ui,full,stages,regions,start,end)
        later=ui.session_state['ms60_v31_last_cutoff']
        self.assertEqual(later,t+45*M)
        self.assertFalse(ui.session_state['ms60_v31_reveal_post'])
        self.assertFalse(any(s.yref=='paper' and s.type=='rect'
            for s in ui.figures['ms60_chart'].layout.shapes))

    def test_reveal_post_after_auto_selection_preserves_pre_geometry(self):
        full,stages,regions,t,start,end,rid=example_data()
        ui=_AutoUI(rid,reveal=False,show_context=True)
        self.render(ui,full,stages,regions,start,end)
        first=ui.figures['ms60_chart']
        self.assertEqual(ui.session_state['ms60_v31_last_cutoff'],t)
        first_bands=[(float(s.y0),float(s.y1)) for s in first.layout.shapes
                     if s.yref=='y']
        self.assertTrue(first_bands)
        ui.session_state['ms60_v31_reveal_post']=True
        self.render(ui,full,stages,regions,start,end)
        second=ui.figures['ms60_chart']
        context=ui.figures['ms60_v32_context_chart']
        self.assertEqual(len(second.data[0].x),len(first.data[0].x)+12)
        self.assertTrue(any(s.yref=='paper' and s.type=='rect'
                            for s in second.layout.shapes))
        self.assertTrue(any(s.yref=='paper' and s.type=='rect'
                            for s in context.layout.shapes))
        second_bands=[(float(s.y0),float(s.y1)) for s in second.layout.shapes
                      if s.yref=='y']
        self.assertEqual(second_bands[:len(first_bands)],first_bands)
        self.assertEqual(second_bands[-1],(99.,100.))

    def test_manual_source_controls_are_kept(self):
        import inspect
        source=inspect.getsource(view.render_btc_multiswing_60d_view)
        self.assertIn("if replay_origin == 'MANUAL':",source)
        self.assertIn("key='ms60_v31_replay_clock'",source)
        self.assertIn("key='ms60_v31_reveal_post'",source)
        self.assertNotIn('place_order(',source)


if __name__=='__main__':
    unittest.main()
