#!/usr/bin/env python3
"""Offline V1.2 regression: identity, causality, H1 initialization, links, UI contract."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

import btc_multiswing_structural_behavior_v12 as v
from btc_multiswing_structural_behavior_v11 import M
from test_btc_multiswing_structural_behavior_v11 import synthetic_bars


def event(eid,tf='M15_CLOSED',ts=120,kind='BREAK_ABOVE_2C',direction='ABOVE',
          ep='e1',orig='BELOW',parent_id=None,low=99.5,high=100.5):
    return dict(transition_id=eid,episode_id=ep,region_id='band',
         band_origin_side='HIGH',source_tf=tf,event_type=kind,
         direction=direction,origin_position=orig,confirmation_ts=ts*M,
         confirmation_utc=v.iso(ts*M),bar_open_ts=(ts-(15 if tf=='M15_CLOSED' else 60))*M,
         confirmation_close=101.,close_position='ABOVE',band_low_pre=low,
         band_high_pre=high,parent_transition_id=parent_id,
         linked_15m_transition_id=None,linked_lag_minutes=None,
         evidence_status='CLOSED_BAR_CONFIRMED',meaning='RESEARCH_ONLY')


class V12Behavior(unittest.TestCase):
    def test_physical_identity_splits_geometry_but_preserves_context(self):
        rows=pd.DataFrame([event('a'),event('b',ep='e2',orig='ABOVE'),
                           event('c',ep='e3',low=99.6)])
        physical,obs=v.canonicalize_events(rows)
        self.assertEqual(len(physical),2)
        self.assertEqual(len(obs),3)
        self.assertEqual(obs.iloc[0].physical_transition_id,obs.iloc[1].physical_transition_id)
        self.assertNotEqual(obs.iloc[0].physical_transition_id,obs.iloc[2].physical_transition_id)
        self.assertTrue(bool(physical.loc[physical.observation_count.eq(2)].context_dependent_origin.iloc[0]))

    def test_shuffled_episode_order_cannot_change_canonical_id(self):
        rows=pd.DataFrame([event('a'),event('b',ep='e2')])
        ids=v.canonicalize_events(rows)[1].physical_transition_id.tolist()
        shuffled=v.canonicalize_events(rows.iloc[::-1])[1].physical_transition_id.tolist()
        self.assertEqual(ids,list(reversed(shuffled)))

    def test_unrelated_band_is_not_same_event(self):
        rows=pd.DataFrame([event('a'),{**event('b'),'region_id':'other'}])
        self.assertEqual(len(v.canonicalize_events(rows)[0]),2)

    def test_same_close_vs_lag_vs_far(self):
        m=event('m',ts=120); h0=event('h0',tf='H1_CLOSED',ts=120)
        h1=event('h1',tf='H1_CLOSED',ts=180)
        hfar=event('hfar',tf='H1_CLOSED',ts=360)
        rows=v.canonicalize_events(pd.DataFrame([m,h0,h1,hfar]))[1]
        links=v.link_bounded(rows)
        status=links.set_index('h1_transition_id').link_status.to_dict()
        self.assertEqual(status['h0'],'SAME_CLOSE')
        self.assertEqual(status['h1'],'LAGGED_CONFIRMATION')
        self.assertEqual(status['hfar'],'OUTSIDE_WINDOW')
        self.assertEqual(int(links.loc[links.h1_transition_id.eq('h1'),'lag_minutes'].iloc[0]),60)

    def test_h1_cannot_link_to_future_15m_or_wrong_type(self):
        rows=pd.DataFrame([event('m',ts=180),event('h',tf='H1_CLOSED',ts=120),
                           event('bad',ts=120,kind='RETEST_TOUCH')])
        links=v.link_bounded(v.canonicalize_events(rows)[1])
        self.assertEqual(links.loc[0,'link_status'],'NO_MATCH')

    def test_parent_lineage_mismatch_is_explicit(self):
        mparent=event('mb',ts=60,kind='BREAK_ABOVE_2C')
        hparent=event('hb',tf='H1_CLOSED',ts=300,kind='BREAK_ABOVE_2C')
        m=event('mr',ts=120,kind='RETEST_TOUCH',parent_id='mb')
        h=event('hr',tf='H1_CLOSED',ts=180,kind='RETEST_TOUCH',parent_id='hb')
        rows=v.canonicalize_events(pd.DataFrame([mparent,hparent,m,h]))[1]
        links=v.link_bounded(rows)
        self.assertEqual(links.set_index('h1_transition_id').loc['hr','link_status'],'PARENT_MISMATCH')

    def test_hourly_first_close_uses_previous_closed_hour_not_touch_1m(self):
        # Previous H1 close ABOVE. At first H1 post-touch close BELOW,
        # V1.1 initialized from 1m arrival BELOW and did not report CLOSE_BELOW.
        hourly=synthetic_bars(60,[101.2,98.8,98.0,98.0])
        kw=dict(episode_id='ep',region_id='rid',origin_side='HIGH',
            approach='BELOW',band_low=99.5,band_high=100.5,
            touch_open=90*M,source_end=240*M,tf_minutes=60,horizon_minutes=180)
        old=v.parent.classify_transitions(hourly,**kw)
        fixed=v.classify_transitions_v12(hourly,**kw,previous_closed_position='ABOVE')
        self.assertNotIn(('CLOSE_BELOW',120*M),{(r['event_type'],r['confirmation_ts']) for r in old})
        self.assertIn(('CLOSE_BELOW',120*M),{(r['event_type'],r['confirmation_ts']) for r in fixed})
        self.assertTrue(all(r['confirmation_ts']>=r['bar_open_ts']+60*M for r in fixed))

    def test_previous_hour_state_does_not_depend_on_future_candle(self):
        hourly=synthetic_bars(60,[101,102,103,104,105])
        a=v.parent.context_asof(hourly,95*M,99.5,100.5,period_minutes=60)
        changed=copy.deepcopy(hourly);changed['close'][2:]=np.array([1e6]*3)
        b=v.parent.context_asof(changed,95*M,99.5,100.5,period_minutes=60)
        self.assertEqual(a,b)

    def test_transition_pre_strictly_uses_bar_open_not_confirmation(self):
        # 240 valid 1m candles; 15m confirmation at 1h15; snapshot at 1h.
        minutes=240
        frame=pd.DataFrame({'timestamp':np.arange(minutes)*M,
            'open':[100.]*minutes,'high':[101.]*minutes,
            'low':[99.]*minutes,'close':[100.]*minutes})
        frozen=v.parent._closed_series(frame,M)
        m15=v.parent._closed_series(v.aggregate_closed(frame,'15m',0,minutes*M),15*M)
        h1=v.parent._closed_series(v.aggregate_closed(frame,'1h',0,minutes*M),60*M)
        h4=v.parent._closed_series(v.aggregate_closed(frame,'4h',0,minutes*M),240*M)
        rows=pd.DataFrame([event('m',ts=75)])
        ep=pd.DataFrame([{'episode_id':'e1','stage_low':99.5,'stage_high':100.5,
                          'stage_known_from':0,'pre_cutoff_ts':30*M}])
        pre=v.transition_pre(frozen,m15,h1,h4,rows,ep)
        self.assertEqual(pre.iloc[0].pre_cutoff_ts,60*M)
        self.assertEqual(pre.iloc[0].pre_last_1h_close_ts,60*M)
        self.assertTrue(pre.iloc[0].pre_reference_stage_known)
        self.assertNotIn('event_type',pre.columns)
        self.assertNotIn('confirmation_close',pre.columns)

    def test_pre_context_source_boundary_no_future(self):
        hourly=synthetic_bars(60,[100,105,110,120])
        context=v._asof(hourly,119*M,99.5,100.5,60)
        self.assertEqual(context['last_close_ts'],60*M)
        self.assertEqual(context['last_close'],100.)

    def test_source_does_not_import_live_or_streamlit(self):
        src=Path(v.__file__).read_text()
        self.assertNotIn('engine.live',src)
        self.assertNotIn('import streamlit',src)
        self.assertIn('V12_IMMUTABLE_RESULT_ALREADY_EXISTS',src)

    def test_parent_real_v11_results_if_supplied(self):
        # Developer provided V11 results are optional in a clean VPS checkout.
        folder=Path('/mnt/data/ms_v12_work/data')
        if not folder.is_dir():
            self.skipTest('Local archived V1.1 ZIP not present')
        ep,mem,trans,manifest=v.verify_v11(folder)
        self.assertEqual((len(ep),len(mem),len(trans)),(1143,2587,33828))
        physical,obs=v.canonicalize_events(trans)
        self.assertEqual(len(physical),24941)
        self.assertEqual(int(physical.context_dependent_origin.sum()),2677)


if __name__=='__main__':unittest.main()

# Keep the existing 3 Streamlit modes; exercise only the V1.2 lightweight
# routing and POST reset with a synthetic chart that returns before plotting.
class V12NavigationSmoke(unittest.TestCase):
    def _run(self, *, transition=False, choose_second=False, ui=None):
        from test_multiswing_ui_v33_event_navigation import example_data
        from test_multiswing_ui_v3_modes import _UI
        from dashboard.views import reaction_multiswing_btc60d_view_v1 as view
        full,stages,regions,t,start,end,rid=example_data()
        ep_id=rid+'|EP|'+str(t)
        v0=rid+'|V1|'+str(t);v1=rid+'|V2|'+str(t+45*M)
        e0=event('ev0',tf='M15_CLOSED',ts=15)
        events=pd.DataFrame([{
            'transition_id':'test|M15|BREAK_ABOVE|'+str(t+15*M),
            'episode_id':ep_id,'confirmation_ts':t+15*M,
            'bar_open_ts':t,'source_tf':'M15_CLOSED',
            'event_type':'CLOSE_ABOVE','h1_m15_link_status':'NOT_H1'}])
        eps=pd.DataFrame([{'episode_id':ep_id,'region_id':rid,
             'band_origin_side':'LOW','pre_cutoff_ts':t,
             'stage_low':99.,'stage_high':100.,'stage_known_from':t-6*60*M}])
        visits=pd.DataFrame([{'event_id':v0,'episode_id':ep_id,
           'pre_cutoff_ts':t,'visit_number':1,'approach_position':'ABOVE'},
           {'event_id':v1,'episode_id':ep_id,
           'pre_cutoff_ts':t+45*M,'visit_number':2,'approach_position':'ABOVE'}])
        if ui is None:
            class BehaviorUI(_UI):
                def __init__(self):
                    super().__init__('EJECUCION')
                    self.transition=transition;self.second=choose_second
                def selectbox(self,label,options,**kwargs):
                    key=kwargs.get('key')
                    if key=='ms60_v33_event_origin':return 'BEHAVIOR'
                    if key=='ms60_v12_visit':return options[-1] if self.second else options[0]
                    if key=='ms60_v12_transition':return options[-1] if self.transition else ''
                    return super().selectbox(label,options,**kwargs)
                def radio(self,label,options,**kwargs):
                    if kwargs.get('key')=='ms60_v12_cut_kind':return options[0]
                    return super().radio(label,options,**kwargs)
            ui=BehaviorUI()
        else:
            ui.transition=transition
            ui.second=choose_second
        counters={'representations':0,'verified_extrema':0,
                  'multi_tf_extrema':0,'ambiguous_representations':0}
        source={'start':start,'end':end,'source_sha256':'mock',
                'catalog_sha256':'mock','source_hll_parity':'PARITY_OK'}
        with patch.dict(sys.modules,{'streamlit':ui}), \
             patch('btc_multiswing_structural_behavior_v12.read_ui_index',
                   return_value=(eps,visits,events)), \
             patch.object(view,'load_snapshot',return_value=(full,pd.DataFrame(),source)), \
             patch.object(view,'asof_rows',return_value=pd.DataFrame()), \
             patch.object(view,'v313_stats_build',return_value=counters), \
             patch.object(view,'v313_render_bars',return_value=pd.DataFrame()):
            view.render_btc_multiswing_60d_view('/tmp/no-live')
        return ui,t

    def test_new_navigation_visit_and_event_must_reset_post(self):
        ui,t=self._run(transition=False)
        self.assertEqual(ui.session_state['ms60_v31_last_cutoff'],t)
        self.assertFalse(ui.session_state['ms60_v31_reveal_post'])
        ui.session_state['ms60_v31_reveal_post']=True
        ui,t=self._run(transition=True,ui=ui)
        self.assertEqual(ui.session_state['ms60_v31_last_cutoff'],t)
        self.assertFalse(ui.session_state['ms60_v31_reveal_post'])
        ui.session_state['ms60_v31_reveal_post']=True
        ui,t=self._run(transition=False,choose_second=True,ui=ui)
        self.assertEqual(ui.session_state['ms60_v31_last_cutoff'],t+45*M)
        self.assertFalse(ui.session_state['ms60_v31_reveal_post'])

    def test_v12_feature_is_opt_in_and_retains_other_modes(self):
        from dashboard.views import reaction_multiswing_btc60d_view_v1 as view
        source=Path(view.__file__).read_text()
        self.assertIn("replay_origin == 'BEHAVIOR'",source)
        self.assertIn('read_ui_index(Path(project))',source)
        self.assertIn('v12_visits.loc[v12_visits.episode_id.eq(ep_id)]',source)
        self.assertIn('ms60_v33_last_replay_signature',source)
        self.assertIn("if mode == 'MAPA':",source)
        self.assertIn("if mode == 'INSPECTOR':",source)
        self.assertIn("'MANUAL':'Exploración libre",source)
        self.assertNotIn('pm2 restart all',source)

class V12EndToEnd(unittest.TestCase):
    def test_synthetic_full_run_tables_and_no_future_pre(self):
        n=2880;t=390*M
        full=pd.DataFrame({'timestamp':np.arange(n,dtype=np.int64)*M,
           'open':np.full(n,100.),'high':np.full(n,101.),
           'low':np.full(n,99.),'close':np.full(n,100.)})
        ep='band|EP|'+str(t);eid='band|V1|'+str(t)
        eps=pd.DataFrame([{'episode_id':ep,'region_id':'band',
             'band_origin_side':'HIGH','pre_cutoff_ts':t,
             'stage_low':99.5,'stage_high':100.5,
             'stage_known_from':t-60*M,'approach_position':'INSIDE'}])
        member=pd.DataFrame([{'event_id':eid,'episode_id':ep,
              'region_id':'band','visit_number':1,
              'birth_touch_open_ts':t,'stage_low_pre':99.5,
              'stage_high_pre':100.5,'stage_known_from':t-60*M}])
        eligible=pd.DataFrame([{'event_id':eid,'region_id':'band',
              'visit_number':1,'side':'HIGH','pre_cutoff_ts':t,
              'stage_known_from':t-60*M,'stage_low':99.5,
              'stage_high':100.5,'stage_extrema':3,
              'pre_close_1m':100.,'approach_position':'INSIDE',
              'pre_last_15m_close_ts':t,'pre_last_15m_close':100.,
              'prior_completed_birth_visits_asof':0}])
        f15=v.parent._closed_series(v.aggregate_closed(full,'15m',0,n*M),15*M)
        fh=v.parent._closed_series(v.aggregate_closed(full,'1h',0,n*M),60*M)
        trans=[]
        for minutes,bars in [(15,f15),(60,fh)]:
            trans+=v.parent.classify_transitions(bars,episode_id=ep,
                region_id='band',origin_side='HIGH',approach='INSIDE',
                band_low=99.5,band_high=100.5,touch_open=t,
                source_end=n*M,tf_minutes=minutes)
        tables,stats=v.build_v12(full,{'start':0,'end':n*M},eps,member,
                                  pd.DataFrame(trans),eligible)
        self.assertEqual(stats['episodes'],1)
        self.assertEqual(stats['member_visits'],1)
        self.assertEqual(len(tables['visit_pre_v12.csv']),1)
        self.assertEqual(stats['new_event_observations'],len(tables['transition_pre_v12.csv']))
        self.assertEqual(stats['canonical_physical_events'],len(tables['physical_transitions_v12.csv']))
        self.assertTrue((tables['transition_pre_v12.csv'].pre_cutoff_ts <=
             tables['transition_observations_v12.csv'].bar_open_ts.max()).all())
        self.assertTrue(all('outcome' not in c for c in tables['visit_pre_v12.csv'].columns))
