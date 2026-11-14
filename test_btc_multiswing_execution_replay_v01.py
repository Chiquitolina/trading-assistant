"""Causal event rules, honest reference prices and immutable replay checks."""
from __future__ import annotations
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import btc_multiswing_execution_replay_v01 as e

ROOT=Path(__file__).resolve().parent


def fixture(initial=98.,length=80,changes=None,stages=None):
    changes=changes or {0:105.,1:115.}
    opens=np.arange(-10,length,dtype='int64')*e.M
    prices=[];last=initial;rows=[]
    for op in opens:
        minute=int(op//e.M)
        value=float(changes.get(minute,last)) if minute>=0 else initial
        rows.append(dict(timestamp=int(op),open=last,high=max(last,value)+.01,low=min(last,value)-.01,close=value))
        last=value
    stages=stages or [dict(region_id='r',known_from=-20*e.M,band_low=100.,band_high=110.,independent_extrema=2)]
    market=e.Market(pd.DataFrame(rows),e.StageIndex(pd.DataFrame(stages)))
    return market,dict(event_id='visit',region_id='r',episode_id='ep',pre_cutoff_ts=0)


class EventRules(unittest.TestCase):
    def events(self,**kwargs):
        market,visit=fixture(**kwargs)
        rows,health=e.replay_visit(market,visit)
        return market,visit,pd.DataFrame(rows),health

    def test_defense_requires_two_full_bars_after_contact(self):
        _,_,f,_=self.events(initial=112.,changes={0:105.,1:112.})
        self.assertEqual(f.loc[f.event_type.eq('CONTACT')].available_ts.iloc[0],e.M)
        defense=f.loc[f.event_type.eq('DEFENSE_CONFIRMED')]
        self.assertEqual(defense.available_ts.tolist(),[15*e.M])
        self.assertEqual(defense.direction.tolist(),['ABOVE'])

    def test_defense_is_symmetric_and_not_decided_by_high_low_origin(self):
        _,_,f,_=self.events(initial=98.,changes={0:105.,1:98.})
        d=f.loc[f.event_type.eq('DEFENSE_CONFIRMED')]
        self.assertEqual(d.available_ts.tolist(),[15*e.M])
        self.assertEqual(d.direction.tolist(),['BELOW'])

    def test_break_and_retest_hold_do_not_reuse_global_close_streak(self):
        _,_,f,_=self.events(changes={0:105.,1:115.,16:108.,17:115.})
        self.assertEqual(f.loc[f.event_type.eq('BREAK_UP_CONFIRMED')].available_ts.iloc[0],15*e.M)
        touch=f.loc[f.event_type.eq('RETEST_CONTACT')].iloc[0]
        hold=f.loc[f.event_type.eq('RETEST_HOLD_CONFIRMED')].iloc[0]
        self.assertEqual(touch.available_ts,17*e.M)
        self.assertEqual(hold.available_ts,30*e.M)
        self.assertGreaterEqual(hold.bar_open_ts,touch.available_ts)
        self.assertEqual(hold.parent_event_id,touch.parent_event_id)
        self.assertEqual(hold.contact_event_id,touch.event_id)
        self.assertEqual(hold.origin_position,'BELOW')

    def test_outside_continuation_does_not_invent_a_retest(self):
        _,_,f,_=self.events()
        self.assertFalse(f.event_type.eq('RETEST_CONTACT').any())
        self.assertFalse(f.event_type.eq('RETEST_HOLD_CONFIRMED').any())
        self.assertEqual(f.loc[f.event_type.eq('ACCEPT_OUTSIDE')].available_ts.tolist(),[25*e.M])

    def test_touch_immediately_after_break_requires_separation_first(self):
        _,_,f,_=self.events(changes={0:105.,1:115.,15:108.,16:115.,18:108.,19:115.})
        self.assertEqual(f.loc[f.event_type.eq('RETEST_CONTACT')].available_ts.iloc[0],19*e.M)

    def test_confirmed_defense_and_its_later_failure_both_survive(self):
        _,_,f,_=self.events(initial=112.,changes={0:105.,1:112.,16:95.})
        defense=f.loc[f.event_type.eq('DEFENSE_CONFIRMED')].iloc[0]
        failure=f.loc[f.event_type.eq('DEFENSE_FAILED')].iloc[0]
        self.assertEqual(failure.parent_event_id,defense.event_id)
        self.assertGreater(failure.available_ts,defense.available_ts)
        self.assertTrue(f.event_type.eq('BREAK_DOWN_CONFIRMED').any())

    def test_inside_exit_can_reverse_to_a_real_cross_instead_of_getting_stuck(self):
        _,_,f,_=self.events(initial=105.,changes={0:105.,1:95.,21:115.})
        exit=f.loc[f.event_type.eq('EXIT_CONFIRMED')].iloc[0]
        self.assertEqual(exit.family,'')
        reverse=f.loc[f.event_type.eq('EXIT_REVERSED')].iloc[0]
        cross=f.loc[f.event_type.eq('BREAK_UP_CONFIRMED')].iloc[0]
        self.assertEqual(reverse.parent_event_id,exit.event_id)
        self.assertEqual(cross.origin_position,'BELOW')
        self.assertEqual(cross.family,'BREAK_DIRECT')

    def test_reentry_inside_is_not_a_complete_failure(self):
        _,_,f,_=self.events(changes={0:105.,1:115.,21:105.,26:115.})
        self.assertTrue(f.event_type.eq('REENTER_BAND').any())
        self.assertFalse(f.event_type.eq('BREAK_FAILED').any())

    def test_geometry_change_resets_pending_counts_and_preserves_old_event(self):
        stages=[dict(region_id='r',known_from=-20*e.M,band_low=100.,band_high=110.,independent_extrema=2),
                dict(region_id='r',known_from=9*e.M,band_low=100.,band_high=114.,independent_extrema=3)]
        _,_,f,h=self.events(changes={0:105.,1:112.},stages=stages)
        self.assertEqual(h['geometry_changes'],1)
        self.assertTrue(f.event_type.eq('GEOMETRY_REVALIDATE').any())
        self.assertFalse(f.event_type.eq('BREAK_UP_CONFIRMED').any())
        self.assertEqual(f.loc[f.event_type.eq('CONTACT')].band_high.iloc[0],110.)

    def test_new_extremum_without_changed_bounds_does_not_reset_counts(self):
        stages=[dict(region_id='r',known_from=-20*e.M,band_low=100.,band_high=110.,independent_extrema=2),
                dict(region_id='r',known_from=9*e.M,band_low=100.,band_high=110.,independent_extrema=3)]
        _,_,f,h=self.events(stages=stages)
        self.assertEqual(h['geometry_changes'],0)
        self.assertEqual(f.loc[f.event_type.eq('BREAK_UP_CONFIRMED')].available_ts.iloc[0],15*e.M)

    def test_stage_available_at_minute_close_cannot_rewrite_prior_range(self):
        stages=[dict(region_id='r',known_from=-20*e.M,band_low=100.,band_high=110.,independent_extrema=2),
                dict(region_id='r',known_from=e.M,band_low=80.,band_high=90.,independent_extrema=3)]
        _,_,f,_=self.events(initial=98.,changes={0:92.},stages=stages)
        self.assertFalse(f.event_type.eq('CONTACT').any())
        self.assertEqual(f.loc[f.event_type.eq('GEOMETRY_REVALIDATE')].available_ts.iloc[0],e.M)

    def test_future_candles_and_stages_do_not_change_event_prefix(self):
        market,visit=fixture()
        prefix,_=e.replay_visit(market,visit,available_end=20*e.M)
        future=market.frame.copy()
        future.loc[future.timestamp.ge(20*e.M),['open','high','low','close']]=500.
        stages=[dict(region_id='r',known_from=-20*e.M,band_low=100.,band_high=110.,independent_extrema=2),
                dict(region_id='r',known_from=25*e.M,band_low=400.,band_high=500.,independent_extrema=3)]
        modified=e.Market(future,e.StageIndex(pd.DataFrame(stages)))
        after,_=e.replay_visit(modified,visit,available_end=20*e.M)
        self.assertEqual(prefix,after)

    def test_missing_geometry_and_truncation_are_censored(self):
        market,visit=fixture(stages=[dict(region_id='another',known_from=0,band_low=100.,band_high=110.,independent_extrema=2)])
        rows,h=e.replay_visit(market,visit)
        self.assertFalse(rows);self.assertFalse(h['data_complete']);self.assertTrue(h['right_censored'])
        market,visit=fixture(length=20)
        _,h=e.replay_visit(market,visit)
        self.assertTrue(h['data_complete']);self.assertTrue(h['right_censored'])

    def test_gapped_minute_source_fails_closed(self):
        market,_=fixture()
        with self.assertRaisesRegex(ValueError,'NONCONTIGUOUS'):
            e.Market(market.frame.drop(index=12),market.stages)

    def test_prefix_before_arrival_is_not_reported_as_observed_history(self):
        market,visit=fixture()
        with self.assertRaisesRegex(ValueError,'PREFIX_BEFORE_ARRIVAL'):e.replay_visit(market,visit,available_end=-e.M)

    def test_confirmed_families_have_no_actual_order_or_profit_fields(self):
        _,_,f,_=self.events()
        for field in ('order_id','pnl','win','tp','sl','filled_price'):self.assertNotIn(field,f.columns)
        self.assertTrue(f.decision_ts.eq(f.available_ts).all())


class ReferenceAndIntegrity(unittest.TestCase):
    def test_reference_beyond_source_end_is_censored_without_a_fake_price(self):
        market,visit=fixture(length=15)
        rows,_=e.replay_visit(market,visit)
        refs=e.entry_references(market,pd.DataFrame(rows).loc[lambda f:f.family.ne('')])
        self.assertTrue(refs.reference_open_ts.isna().all())
        self.assertTrue(refs.reference_open_price.isna().all())
        self.assertTrue(e.path_outcomes(market,refs,(30,)).status.eq('NO_REFERENCE_OPEN').all())
    def test_reference_is_after_availability_and_never_a_claimed_fill(self):
        market,visit=fixture()
        rows,_=e.replay_visit(market,visit)
        triggers=pd.DataFrame(rows).loc[lambda f:f.family.ne('')]
        refs=e.entry_references(market,triggers)
        self.assertTrue(refs.reference_open_ts.gt(refs.available_ts).all())
        self.assertTrue(refs.reference_open_ts.eq(refs.available_ts+e.M).all())
        self.assertTrue(refs.reference_status.eq('REFERENCE_ONLY_EXITS_COSTS_UNSET').all())
        self.assertFalse(refs.net_pnl_available.any())

    def test_partial_path_is_not_reported_as_a_complete_markout(self):
        market,visit=fixture(length=50)
        rows,_=e.replay_visit(market,visit)
        refs=e.entry_references(market,pd.DataFrame(rows).loc[lambda f:f.family.ne('')])
        outcomes=e.path_outcomes(market,refs,(30,60))
        self.assertEqual(outcomes.status.tolist(),['COMPLETE_GROSS_PATH','RIGHT_CENSORED'])
        self.assertTrue(pd.isna(outcomes.gross_markout_pct.iloc[1]))
        self.assertTrue(pd.isna(outcomes.mfe_pct.iloc[1]))

    def test_pre_context_contains_only_closed_clocks_and_no_future_labels(self):
        market,visit=fixture()
        rows,_=e.replay_visit(market,visit)
        triggers=pd.DataFrame(rows).loc[lambda f:f.family.ne('')]
        pre=e.trigger_pre_context(market,triggers)
        for tf in (1,5,15,60,240):
            self.assertTrue((pre[f'pre_{tf}m_closed_ts'].isna()|pre[f'pre_{tf}m_closed_ts'].le(pre.pre_cutoff_ts)).all())
        for label in ('family','event_type','direction','mfe_pct','gross_markout_pct'):self.assertNotIn(label,pre)

    def test_plan_does_not_create_an_output_or_read_missing_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=subprocess.run([sys.executable,str(Path(e.__file__)),'--root',tmp,'--plan'],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('MS_EXECUTION_V01_PLAN',result.stdout)
            self.assertFalse((Path(tmp)/e.DEFAULT_OUTPUT).exists())

    def test_output_is_immutable_and_reuse_verifies_hashes(self):
        market,visit=fixture()
        study=dict(market=market,visits=pd.DataFrame([visit]),source={},fingerprints={})
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'run'
            with patch.object(e,'load_inputs',return_value=study):
                first=e.run(Path(tmp),folder)
                second=e.run(Path(tmp),folder)
            self.assertEqual(first,second)
            (folder/'summary.json').write_text('{}')
            with self.assertRaisesRegex(ValueError,'HASH_MISMATCH'):e.run(Path(tmp),folder)

    def test_overlapping_visits_preserve_observations_and_physical_dedup(self):
        market,visit=fixture()
        second={**visit,'event_id':'visit2','episode_id':'ep2'}
        frames,summary=e.compute(dict(market=market,visits=pd.DataFrame([visit,second])))
        self.assertEqual(summary['counts']['trigger_observations'],2)
        self.assertEqual(summary['counts']['physical_triggers'],1)
        self.assertEqual(len(frames['trigger_candidates_v01.csv']),2)


class RealCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (ROOT/e.V13/'manifest.json').exists():raise unittest.SkipTest('Estudio real V1.3 aún no disponible')
        cls.study=e.load_inputs(ROOT)

    def test_selected_case_has_no_defense_and_breaks_at_0140(self):
        visit=self.study['visits'].loc[self.study['visits'].event_id.eq(e.SELECTED_CASE)].iloc[0].to_dict()
        rows,health=e.replay_visit(self.study['market'],visit)
        f=pd.DataFrame(rows)
        self.assertEqual(e.utc(f.loc[f.event_type.eq('CONTACT')].available_ts.iloc[0]),'2026-10-29 01:04 UTC')
        self.assertFalse(f.event_type.eq('DEFENSE_CONFIRMED').any())
        self.assertEqual(e.utc(f.loc[f.event_type.eq('BREAK_DOWN_CONFIRMED')].available_ts.iloc[0]),'2026-10-29 01:40 UTC')
        self.assertFalse(f.event_type.eq('RETEST_CONTACT').any())
        self.assertFalse(health['right_censored'])
        refs=e.entry_references(self.study['market'],f.loc[f.family.ne('')])
        self.assertEqual(e.utc(refs.reference_open_ts.iloc[0]),'2026-10-29 01:41 UTC')

    def test_real_event_prefix_remains_identical_when_cut_earlier(self):
        visit=self.study['visits'].loc[self.study['visits'].event_id.eq(e.SELECTED_CASE)].iloc[0].to_dict()
        all_rows,_=e.replay_visit(self.study['market'],visit)
        cutoff=int(visit['pre_cutoff_ts'])+60*e.M
        prefix,_=e.replay_visit(self.study['market'],visit,available_end=cutoff)
        self.assertEqual(prefix,[r for r in all_rows if r['available_ts']<=cutoff])

    def test_user_september_example_confirms_defense_only_at_1320(self):
        vid='msv2_328a9f856221c0e15000|V13|1789391160000'
        visit=self.study['visits'].loc[self.study['visits'].event_id.eq(vid)].iloc[0].to_dict()
        rows,_=e.replay_visit(self.study['market'],visit);f=pd.DataFrame(rows)
        self.assertEqual(e.utc(f.loc[f.event_type.eq('CONTACT')].available_ts.iloc[0]),'2026-09-14 13:07 UTC')
        defense=f.loc[f.event_type.eq('DEFENSE_CONFIRMED')]
        self.assertEqual(len(defense),1)
        self.assertEqual(e.utc(defense.available_ts.iloc[0]),'2026-09-14 13:20 UTC')
        self.assertEqual(defense.direction.iloc[0],'ABOVE')
        refs=e.entry_references(self.study['market'],defense)
        self.assertEqual(e.utc(refs.reference_open_ts.iloc[0]),'2026-09-14 13:21 UTC')

    def test_real_patterns_are_symmetric_when_prices_and_bounds_are_mirrored(self):
        # Use actual paths, reflected around a positive anchor; do not tune
        # another parameter set for support versus resistance.
        market=self.study['market'];anchor=200_000.
        reflected=market.frame.copy()
        reflected['open']=anchor-market.frame.open
        reflected['close']=anchor-market.frame.close
        reflected['high']=anchor-market.frame.low
        reflected['low']=anchor-market.frame.high
        for vid,expected in [(e.SELECTED_CASE,'BREAK_UP_CONFIRMED'),
                            ('msv2_328a9f856221c0e15000|V13|1789391160000','DEFENSE_CONFIRMED')]:
            visit=self.study['visits'].loc[self.study['visits'].event_id.eq(vid)].iloc[0].to_dict()
            stage=self.study['stages'].loc[self.study['stages'].region_id.eq(visit['region_id'])].copy()
            low=anchor-stage.band_high;high=anchor-stage.band_low
            stage['band_low']=low;stage['band_high']=high
            mirror=e.Market(reflected,e.StageIndex(stage))
            original,_=e.replay_visit(market,visit);changed,_=e.replay_visit(mirror,visit)
            a=[r for r in original if r['family']];b=[r for r in changed if r['family']]
            self.assertEqual([r['available_ts'] for r in a],[r['available_ts'] for r in b])
            self.assertEqual([e.opposite(r['direction']) for r in a],[r['direction'] for r in b])
            self.assertEqual(b[0]['event_type'],expected)


class InspectorVisibility(unittest.TestCase):
    def test_reference_price_and_outcome_wait_for_their_own_available_clock(self):
        from dashboard.views import reaction_multiswing_execution_v01_view as ui
        events=pd.DataFrame([dict(visit_id='v',event_id='one',available_ts=100),dict(visit_id='v',event_id='future',available_ts=200)])
        data=dict(events=events,triggers=events.copy(),
            references=pd.DataFrame([dict(visit_id='v',event_id='one',reference_open_ts=101,reference_open_price=500),
                                     dict(visit_id='v',event_id='future',reference_open_ts=201,reference_open_price=999)]),
            outcomes=pd.DataFrame([dict(visit_id='v',event_id='one',evaluation_available_ts=160),
                                   dict(visit_id='v',event_id='future',evaluation_available_ts=260)]))
        _,triggers,refs,outcomes=ui.visible_payload(data,'v',100)
        self.assertEqual(len(triggers),1);self.assertTrue(refs.empty);self.assertTrue(outcomes.empty)
        _,_,refs,outcomes=ui.visible_payload(data,'v',150)
        self.assertEqual(refs.reference_open_price.tolist(),[500]);self.assertTrue(outcomes.empty)
        _,_,_,outcomes=ui.visible_payload(data,'v',160)
        self.assertEqual(outcomes.event_id.tolist(),['one'])

    def test_navigation_resets_shared_post_key_before_rendering_widget(self):
        from dashboard.views import reaction_multiswing_execution_v01_view as ui
        state={ui.POST_KEY:True}
        self.assertTrue(ui.blind_navigation(state,('v',100)))
        self.assertFalse(state[ui.POST_KEY]);state[ui.POST_KEY]=True
        self.assertFalse(ui.blind_navigation(state,('v',100)))
        self.assertTrue(state[ui.POST_KEY])
        ui.blind_navigation(state,('v',101));self.assertFalse(state[ui.POST_KEY])

    def test_pre_chart_contains_real_candles_but_no_future_trigger(self):
        from dashboard.views import reaction_multiswing_execution_v01_view as ui
        market,visit=fixture();rows,_=e.replay_visit(market,visit)
        events=pd.DataFrame(rows)
        stages=pd.DataFrame(market.stages.groups['r'][1])
        figure=ui.execution_figure(market.frame.assign(closed_ts=market.closes),stages,events,
            region='r',cutoff=0,end=0,start=-10*e.M,tf=1,origin_side='HIGH')
        candles=[t for t in figure['data'] if t['type']=='candlestick']
        self.assertTrue(len(candles[0]['x']))
        for trace in figure['data']:
            for x in trace.get('x',[]):self.assertLessEqual(int(pd.Timestamp(x).timestamp()*1000),0)
        self.assertNotIn('Ruptura arriba',json.dumps(figure,ensure_ascii=False))
        try:import plotly.graph_objects as go
        except ImportError:return
        go.Figure(figure)

    def test_execution_route_returns_before_general_structural_controls(self):
        from dashboard.views import reaction_multiswing_execution_v01_view as ui
        from dashboard.views import reaction_multiswing_behavior_v13_view as structural
        from types import SimpleNamespace
        from unittest.mock import Mock
        fake=SimpleNamespace(session_state={},markdown=Mock(),selectbox=Mock(return_value='EXECUTION_V01'))
        with patch.dict(sys.modules,{'streamlit':fake}),patch.object(structural,'load_index',return_value={}),\
             patch.object(ui,'render_execution_v01',return_value={'status':'HOOK_OK'}) as renderer:
            result=structural.render_multiswing_behavior_v13(ROOT)
        self.assertEqual(result,{'status':'HOOK_OK'})
        renderer.assert_called_once_with(ROOT)
        self.assertFalse(fake.session_state[structural.POST_KEY])


if __name__=='__main__':unittest.main()
