"""PRE/POST, closed clocks, route preservation and real-data checks for V1.3 UI."""
from __future__ import annotations
import ast
import hashlib
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd
from dashboard.views import reaction_multiswing_behavior_v13_view as u

ROOT=Path(__file__).resolve().parent
VIEW=ROOT/'dashboard/views/reaction_multiswing_btc60d_view_v1.py'


def fixtures():
    def bars(tf):
        opens=np.arange(0,600,tf)*u.M
        n=len(opens);prices=100+np.arange(n)/10
        return pd.DataFrame({'timestamp':opens,'closed_ts':opens+tf*u.M,
            'open':prices,'close':prices+.1,'high':prices+.3,'low':prices-.2})
    stages=pd.DataFrame([
        dict(region_id='r',known_from=0,known_until=u.M,band_low=100,band_high=110,independent_extrema=2),
        dict(region_id='r',known_from=360*u.M,known_until=361*u.M,band_low=99,band_high=115,independent_extrema=3)])
    ep=dict(episode_id='ep',region_id='r',pre_cutoff_ts=180*u.M,stage_low=100,stage_high=110)
    rows=[]
    for ident,ts,tf,kind,direction in [('pre',270,15,'BREAK_ABOVE_2C','ABOVE'),
           ('post',330,15,'DEFEND_ORIGIN_2C','BELOW'),('hour',360,60,'DEFEND_ORIGIN_2C','BELOW')]:
        rows.append(dict(asof_transition_id=ident,asof_physical_transition_id='p_'+ident,
            episode_id='ep',region_id='r',confirmation_ts=ts*u.M,bar_open_ts=(ts-tf)*u.M,
            source_tf='M15_CLOSED' if tf==15 else 'H1_CLOSED',event_type=kind,direction=direction,
            confirmation_close=112 if direction=='ABOVE' else 99,band_low_asof=100,band_high_asof=110,
            stage_known_from=0,evidence_status='CLOSED_BAR_CONFIRMED'))
    return {tf:bars(tf) for tf in (15,60,240)},stages,pd.DataFrame(rows),ep


class ClosedVisibility(unittest.TestCase):
    def setUp(self):self.bars,self.stages,self.events,self.ep=fixtures()
    def figure(self,**kwargs):
        params=dict(region='r',episode=self.ep,cutoff=300*u.M,end=300*u.M,tf_minutes=15,
                    lookback_minutes=300,origin_side='LOW')
        params.update(kwargs)
        return u.build_figure(self.bars[params['tf_minutes']],self.stages,self.events,**params)

    def test_post_is_absent_from_candles_markers_and_annotations(self):
        fig=self.figure()
        for trace in fig['data']:
            for value in trace.get('x',[]):
                self.assertLessEqual(int(pd.Timestamp(value).timestamp()*1000),300*u.M)
        self.assertNotIn('Defensa desde debajo',json.dumps(fig,ensure_ascii=False))

    def test_future_price_stage_and_label_changes_do_not_change_pre_payload(self):
        before=self.figure()
        self.bars[15].loc[self.bars[15].closed_ts.gt(300*u.M),['open','close','high','low']]=9999
        self.stages.loc[self.stages.known_from.gt(300*u.M),['band_low','band_high']]=[1,10000]
        self.events.loc[self.events.confirmation_ts.gt(300*u.M),'event_type']='FUTURE_LABEL_MUST_NOT_LEAK'
        self.assertEqual(before,self.figure())

    def test_hourly_and_four_hour_candles_require_closed_time(self):
        self.assertEqual(u.slice_bars(self.bars[60],0,75*u.M).closed_ts.tolist(),[60*u.M])
        self.assertTrue(u.slice_bars(self.bars[240],0,75*u.M).empty)

    def test_future_target_is_not_highlighted_before_its_confirmation(self):
        fig=self.figure(selected_transition_id='post')
        self.assertFalse(any(a.get('text','').startswith('Defensa') for a in fig['layout']['annotations']))

    def test_selected_transition_survives_shared_physical_contexts(self):
        duplicate=self.events.iloc[[0]].copy()
        duplicate['episode_id']='earlier_episode';duplicate['asof_transition_id']='first_representative'
        self.events=pd.concat([duplicate,self.events],ignore_index=True)
        fig=self.figure(selected_transition_id='pre')
        selected=[trace for trace in fig['data'] if trace.get('name')=='Transición elegida']
        self.assertEqual(len(selected),1)
        self.assertEqual(len(selected[0]['x']),1)

    def test_post_cap_and_hidden_horizon(self):
        self.assertEqual(u.visible_end(300*u.M,500*u.M,False,720),300*u.M)
        self.assertEqual(u.visible_end(300*u.M,500*u.M,True,720),500*u.M)

    def test_missing_stage_does_not_draw_a_future_band(self):
        self.stages=self.stages.loc[self.stages.known_from.gt(300*u.M)]
        fig=self.figure()
        self.assertFalse(any(t.get('legendgroup')=='band' for t in fig['data']))
        snap=u.snapshot_table(self.bars,self.stages,'r',300*u.M)
        self.assertTrue(snap['Posición con la etapa conocida al corte'].eq('UNKNOWN').all())

    def test_visual_expiry_does_not_erase_confirmed_history(self):
        self.assertIsNotNone(u.stage_at(self.stages,'r',300*u.M))

    def test_fixed_reference_is_not_projected_before_episode_start(self):
        fig=self.figure(compare_fixed=True)
        fixed=[t for t in fig['data'] if t.get('legendgroup')=='fixed']
        self.assertEqual(len(fixed),2)
        self.assertTrue(all(int(pd.Timestamp(t['x'][0]).timestamp()*1000)>=180*u.M for t in fixed))

    def test_four_hour_context_does_not_invent_events(self):
        fig=self.figure(tf_minutes=240)
        self.assertFalse(any(t.get('name')=='Eventos confirmados' for t in fig['data']))

    def test_links_and_comparison_use_the_same_boundary(self):
        links=pd.DataFrame([dict(episode_id='ep',h1_confirmation_ts=300*u.M,m15_confirmation_ts=270*u.M),
                            dict(episode_id='ep',h1_confirmation_ts=360*u.M,m15_confirmation_ts=330*u.M),
                            dict(episode_id='ep',h1_confirmation_ts=270*u.M,m15_confirmation_ts=330*u.M)])
        self.assertEqual(len(u.visible_links(links,'ep',300*u.M)),1)
        c=self.events.rename(columns={'asof_transition_id':'transition_id'})
        self.assertEqual(len(u.visible_comparison(c,'ep',300*u.M)),1)

    def test_acceptance_direction_is_not_invented(self):
        self.assertNotIn('LONG',u.event_label('ACCEPT_INSIDE_3C',''))
        self.assertNotIn('SHORT',u.event_label('ACCEPT_INSIDE_3C',''))

    def test_plotly_accepts_real_figure_schema_if_available(self):
        if importlib.util.find_spec('plotly') is None:self.skipTest('Plotly no está instalado en este entorno')
        import plotly.graph_objects as go
        for tf in (15,60,240):
            fig=go.Figure(self.figure(tf_minutes=tf,compare_fixed=True))
            self.assertTrue(len(fig.to_dict()['data']))


class NavigationRules(unittest.TestCase):
    def test_same_selection_preserves_explicit_post_but_navigation_hides_it(self):
        state={}
        self.assertTrue(u.blind_navigation(state,('ep',100)))
        self.assertFalse(state[u.POST_KEY])
        state[u.POST_KEY]=True
        self.assertFalse(u.blind_navigation(state,('ep',100)))
        self.assertTrue(state[u.POST_KEY])
        self.assertTrue(u.blind_navigation(state,('ep',101)))
        self.assertFalse(state[u.POST_KEY])

    def test_cutoff_distinguishes_visit_open_and_confirming_close(self):
        ep={'pre_cutoff_ts':100};visit={'pre_cutoff_ts':200};event={'bar_open_ts':180,'confirmation_ts':240}
        self.assertEqual(u.cutoff_for('VISIT',ep,visit),200)
        self.assertEqual(u.cutoff_for('TRANSITION',ep,transition=event),180)
        self.assertEqual(u.cutoff_for('TRANSITION',ep,transition=event,transition_cut='AT_CONFIRMATION'),240)

    def test_old_and_new_streamlit_width_contracts(self):
        self.assertEqual(u.width_options(types.SimpleNamespace(__version__='1.40.0')),{'use_container_width':True})
        self.assertEqual(u.width_options(types.SimpleNamespace(__version__='1.67.0')),{'width':'stretch'})

    def test_missing_run_displays_status_without_running_engine(self):
        st=types.SimpleNamespace(markdown=Mock(),info=Mock(),button=Mock(return_value=False))
        with patch.object(u,'load_index',side_effect=FileNotFoundError()):
            result=u.render_multiswing_behavior_v13(Path('/missing'),st=st)
        self.assertEqual(result['status'],'RUN_NOT_AVAILABLE')
        st.info.assert_called_once()

    def test_hash_mismatch_is_not_displayed_as_valid_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);file=folder/'data.csv';file.write_text('x\n1\n')
            m={'files_sha256':{'data.csv':u.sha256(file)}}
            self.assertEqual(len(u.verified_csv(folder,'data.csv',m)),1)
            file.write_text('x\n999\n')
            with self.assertRaisesRegex(ValueError,'modificado'):
                u.verified_csv(folder,'data.csv',m)

    def test_signature_changes_when_cached_input_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=root/'x';p.write_text('a')
            before=u.signature(root,[Path('x')])
            p.write_text('longer')
            self.assertNotEqual(before,u.signature(root,[Path('x')]))


class ExistingRoutePreservation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.tree=ast.parse(VIEW.read_text(encoding='utf-8'))

    def test_legacy_cache_context_and_event_helpers_are_preserved(self):
        expected={
          'v313_memo':'5ba466f73d48c748f4a0bbcc8331b66000eae78dfa311163724d67c0d23fddb7',
          'v313_region_build':'3f4961dbccec9956a71cde3d7b22fb646fce89fd0edd8eb3841b727980f09b43',
          'v313_preaggregated':'59bd5d279173a8889f5d4939e98cd0623842f9bfe92d1674cd8f4cf6c3a29e64',
          'v313_slice_bars':'2119b2467ba7414870a1dc46e5e84a514b174b072d75314e7c15db3f33c504f3',
          'v32_build_context_chart':'1284ab17e23f392337e9e2a85b43f75d9cecb8dcc6e14d5a5346a9b7574e8d48',
          'v31_pre_region_segments':'215898f9b8a87cafedba60113dedcd77772e8cd411d053d82d4d0700098e7aa3',
          'v33_structural_event_index':'68c30ac9605a5671c3376b8cd1a87828ba67dd89fe240bc4afaa79807cd1e719'}
        functions={x.name:x for x in self.tree.body if isinstance(x,ast.FunctionDef)}
        for name,wanted in expected.items():
            actual=hashlib.sha256(ast.dump(functions[name],include_attributes=False).encode()).hexdigest()
            self.assertEqual(actual,wanted,name)

    def test_legacy_render_tail_including_post_color_hotfix_is_preserved(self):
        fn=next(x for x in self.tree.body if isinstance(x,ast.FunctionDef) and x.name=='render_btc_multiswing_60d_view')
        for i,node in enumerate(fn.body):
            if isinstance(node,ast.If) and any(isinstance(x,ast.Name) and x.id=='previous_mode' for x in ast.walk(node)):
                tail=ast.Module(body=fn.body[i:],type_ignores=[]);break
        actual=hashlib.sha256(ast.dump(tail,include_attributes=False).encode()).hexdigest()
        self.assertEqual(actual,'d52ded82585d87f922f18b2e62aec6cdaae5ac6966d0399f3a26254ef7253469')

    def test_v13_returns_before_generic_freeze_loading(self):
        fn=next(x for x in self.tree.body if isinstance(x,ast.FunctionDef) and x.name=='render_btc_multiswing_60d_view')
        fake=types.ModuleType('streamlit')
        fake.session_state={u.POST_KEY:True,'ms60_v31_reveal_post':True,'ms60_reveal_post':True}
        fake.markdown=Mock();fake.radio=Mock(return_value='EJECUCION');fake.selectbox=Mock(return_value='BEHAVIOR_V13')
        namespace={'Path':Path,'V3_MODES':('MAPA','INSPECTOR','EJECUCION'),
                   'V33_REPLAY_ORIGINS':('BEHAVIOR_V13',),'v3_view_policy':lambda x:{},
                   'load_snapshot':Mock(side_effect=AssertionError('Generic loader must not run'))}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),str(VIEW),'exec'),namespace)
        with patch.dict(sys.modules,{'streamlit':fake}),patch.object(u,'render_multiswing_behavior_v13') as render:
            namespace['render_btc_multiswing_60d_view'](ROOT)
        render.assert_called_once_with(ROOT)
        namespace['load_snapshot'].assert_not_called()
        self.assertFalse(fake.session_state[u.POST_KEY])
        self.assertFalse(fake.session_state['ms60_v31_reveal_post'])
        self.assertFalse(fake.session_state['ms60_reveal_post'])

    def test_leaving_v13_hides_its_post_before_other_modes(self):
        fn=next(x for x in self.tree.body if isinstance(x,ast.FunctionDef) and x.name=='render_btc_multiswing_60d_view')
        fake=types.ModuleType('streamlit');fake.session_state={u.POST_KEY:True,'ms60_v13_active':True}
        fake.markdown=Mock();fake.caption=Mock();fake.radio=Mock(return_value='MAPA')
        namespace={'Path':Path,'V3_MODES':('MAPA','INSPECTOR','EJECUCION'),
                   'V33_REPLAY_ORIGINS':('BEHAVIOR_V13',),'v3_view_policy':lambda x:{},
                   '_perf_time':types.SimpleNamespace(perf_counter=lambda:0),
                   'load_snapshot':Mock(side_effect=RuntimeError('REACHED_LEGACY_BRANCH'))}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),str(VIEW),'exec'),namespace)
        with patch.dict(sys.modules,{'streamlit':fake}),self.assertRaisesRegex(RuntimeError,'REACHED_LEGACY_BRANCH'):
            namespace['render_btc_multiswing_60d_view'](ROOT)
        self.assertFalse(fake.session_state[u.POST_KEY])
        self.assertFalse(fake.session_state['ms60_v13_active'])


class RealFrozenStudy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (ROOT/u.RUN/'manifest.json').exists():raise unittest.SkipTest('Run V1.3 aún no disponible en esta raíz')
        cls.study=u.load_index(ROOT);cls.bars,cls.source=u.load_prices(ROOT,cls.study)

    def test_real_low_bookmark_and_pre_clocks(self):
        r,e,vid=u.BOOKMARKS['LOW_23_OCT']
        self.assertIn(e,set(self.study['episodes'].episode_id))
        self.assertIn(vid,set(self.study['visits'].event_id))
        t=u.snapshot_table(self.bars,self.study['stages'],r,1792775940000)
        self.assertEqual(t['Posición con la etapa conocida al corte'].tolist(),['INSIDE','BELOW','BELOW'])

    def test_unchanged_run_reuses_verified_index_without_reading_csv_again(self):
        u._load_index.cache_clear()
        with patch.object(u,'verified_csv',wraps=u.verified_csv) as reader:
            first=u.load_index(ROOT);calls=reader.call_count
            second=u.load_index(ROOT)
            self.assertEqual(calls,5)
            self.assertEqual(reader.call_count,calls)
            self.assertIs(first,second)

    def test_every_real_pre_figure_excludes_future_data(self):
        for bookmark in ('LOW_23_OCT','HIGH_22_OCT'):
            r,e,vid=u.BOOKMARKS[bookmark]
            ep=self.study['episodes'].set_index('episode_id').loc[e].to_dict();ep['episode_id']=e
            cut=int(self.study['visits'].set_index('event_id').loc[vid,'pre_cutoff_ts'])
            for tf in (15,60,240):
                f=u.build_figure(self.bars[tf],self.study['stages'],self.study['events'],region=r,
                    episode=ep,cutoff=cut,end=cut,tf_minutes=tf,lookback_minutes=7*1440,origin_side=ep['band_origin_side'])
                for trace in f['data']:
                    self.assertTrue(all(int(pd.Timestamp(x).timestamp()*1000)<=cut for x in trace.get('x',[])))


if __name__=='__main__':unittest.main()
