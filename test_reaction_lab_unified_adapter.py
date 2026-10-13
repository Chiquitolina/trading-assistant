import ast
from pathlib import Path
import sys
import unittest
import numpy as np
import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parent))
from reaction_lab_unified_adapter import (
    post_15m_from_1m, combined_archived_15m, frozen_room, inspector_rows,
)

PROJECT=Path(__file__).resolve().parent
APP=PROJECT/'dashboard/app.py'


class CausalCandleTests(unittest.TestCase):
    def _path(self, start=900_000, bars=30, missing=()):
        return pd.DataFrame([
            dict(timestamp=start+i*60_000,open=10+i,high=10.5+i,low=9.5+i,close=10.2+i)
            for i in range(bars) if i not in missing
        ])

    def test_only_complete_full_fifteen_minute_blocks(self):
        result=post_15m_from_1m(self._path(),known_ts=0)
        self.assertEqual(result.timestamp.tolist(),[900_000,1_800_000])
        self.assertEqual(result.iloc[0]['open'],10.0)
        self.assertEqual(result.iloc[0]['close'],24.2)
        self.assertEqual(result.iloc[0]['high'],24.5)

    def test_gap_drops_candle_instead_of_fabricating(self):
        result=post_15m_from_1m(self._path(missing=(3,)),known_ts=0)
        self.assertEqual(result.timestamp.tolist(),[1_800_000])

    def test_partial_current_candle_not_completed_with_future_1m(self):
        result=post_15m_from_1m(self._path(start=900_000,bars=30),known_ts=1_120_000)
        self.assertEqual(result.timestamp.tolist(),[1_800_000])

    def test_frozen_15m_rejects_asof_lookahead(self):
        archived=[dict(timestamp=t,open=1,high=2,low=.5,close=1.5)
                  for t in (0,900_000,1_800_000)]
        result=combined_archived_15m(archived,pd.DataFrame(),known_ts=1_200_000)
        self.assertEqual(result.timestamp.tolist(),[0])

    def test_combined_has_post_candles_but_no_redis_dependency(self):
        archived=[dict(timestamp=0,open=1,high=2,low=.5,close=1.5)]
        result=combined_archived_15m(archived,self._path(),known_ts=900_000)
        self.assertEqual(result.timestamp.tolist(),[0,900_000,1_800_000])

    def test_room_future_confirmation_never_drawn(self):
        row={'known_ts':1_000_000,'nearest_opposing_room_pct':3,
             'nearest_opposing_swing_price':105,
             'nearest_opposing_swing_actionable_timestamp':1_000_001}
        self.assertIsNone(frozen_room(row)['nearest_opposing_room_pct'])

    def test_rsi_not_asserted_if_incomplete(self):
        row={'aligned_rsi_extreme_count':2,'rsi14_15m':28}
        self.assertIsNone(frozen_room(row)['aligned_rsi_extreme_count'])

    def test_original_event_id_retained_and_no_mfe_fabrication(self):
        df=inspector_rows(pd.DataFrame([dict(event_key='ABC|LONG|900000',retest_timestamp=900000,
            symbol='ABC',signal='LONG',swing_price=1)]),tz='America/Argentina/Buenos_Aires')
        self.assertEqual(df['event_key'].iloc[0],'ABC|LONG|900000')
        self.assertTrue(pd.isna(df['reaction_mfe_pct'].iloc[0]))
        self.assertEqual(str(df['retest_time'].dt.tz), 'America/Argentina/Buenos_Aires')


class SharedRendererTests(unittest.TestCase):
    def test_exactly_one_shared_plot_builder_and_inspector(self):
        tree=ast.parse(APP.read_text(encoding='utf-8'))
        names=[node.name for node in tree.body if isinstance(node,ast.FunctionDef)]
        self.assertEqual(names.count('build_confirmed_swing_retest_detail_chart'),1)
        self.assertEqual(names.count('render_confirmed_swing_retest_inspector'),1)

    def test_legacy_and_continuous_reuse_same_inspector(self):
        source=APP.read_text()
        self.assertIn('render_confirmed_swing_retest_inspector(view)',source)
        self.assertIn('shared_retest_inspector=render_confirmed_swing_retest_inspector',source)
        view=(PROJECT/'reaction_lab_research_view.py').read_text()
        self.assertIn('selected_event=shared_retest_inspector(',view)
        self.assertNotIn('def render_persisted_retest_inspector(',view)

    def test_scanner_canonical_functions_have_unmodified_ast(self):
        previous=Path('/mnt/data/reaction_lab_full_context_release/dashboard/app.py')
        if not previous.is_file():self.skipTest('Previous baseline not mounted')
        old=ast.parse(previous.read_text());new=ast.parse(APP.read_text())
        def get(root,name):
            return next(n for n in root.body if isinstance(n,ast.FunctionDef) and n.name==name)
        helpers=(' _prepare_confirmed_swing_retest_candles _build_confirmed_swing_retest_forward_cache _find_confirmed_swing_retest get_volume_exhaustion_swing_confirmation_info _confirmed_swing_volume_features_at _attach_confirmed_swing_volume_context _attach_confirmed_swing_reaction_run _attach_confirmed_swing_order_recovery_analysis _confirmed_swing_first_touch_key scan_confirmed_swing_retests_all_symbols').strip().split()
        for name in helpers:
            a=ast.dump(get(old,name),include_attributes=False)
            b=ast.dump(get(new,name),include_attributes=False)
            self.assertEqual(a,b,name)

    def test_fig_can_plot_archived_plus_future_using_canonical_builder(self):
        import plotly.graph_objects as go
        # Extract functions without running Streamlit's top-level app code.
        source=APP.read_text();tree=ast.parse(source)
        names=['_prepare_confirmed_swing_retest_candles','build_confirmed_swing_retest_detail_chart']
        funcs=[next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name) for name in names]
        env=dict(pd=pd,np=np,go=go,TZ='UTC',
            CONFIRMED_SWING_RETEST_DETAIL_DEFAULT_CANDLES=80,
            CONFIRMED_SWING_RETEST_DETAIL_MIN_CANDLES=40,
            CONFIRMED_SWING_RETEST_DETAIL_MAX_CANDLES=200,
            CONFIRMED_SWING_RETEST_DETAIL_TIMEFRAME='15m')
        exec(compile(ast.Module(body=funcs,type_ignores=[]),str(APP),'exec'),env)
        candles=pd.DataFrame([dict(timestamp=i*900_000,open=10,high=12,low=9,close=11)
                              for i in range(10)])
        row=dict(pivot_timestamp=900_000,actionable_timestamp=3_600_000,
            departure_timestamp=5_400_000,retest_timestamp=11_700_000,
            swing_price=10,entry_price=11,departure_price=12,retest_price=10.2,
            status='REACTION',symbol='ABC',signal='LONG',timeframe='15m')
        fig=env['build_confirmed_swing_retest_detail_chart'](
            candles,row,chart_candle_limit=80,show_event_marker_without_closed_15m=True)
        self.assertTrue(any(t.name=='REACTION' for t in fig.data))
        self.assertEqual(sum(1 for t in fig.data if t.type=='candlestick'),1)

    def test_shared_inspector_renders_persisted_event_with_original_chart(self):
        import plotly.graph_objects as go
        source=APP.read_text();tree=ast.parse(source)
        names=['_prepare_confirmed_swing_retest_candles',
               'build_confirmed_swing_retest_detail_chart',
               'render_confirmed_swing_retest_inspector']
        funcs=[next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name) for name in names]
        class St:
            def __init__(self):self.charts=[];self.keys=[]
            def selectbox(self,_,options,**kwargs):
                self.keys.append(kwargs.get('key'));return options[0]
            def checkbox(self,*args,**kwargs):
                self.keys.append(kwargs.get('key'));return True
            def slider(self,*args,**kwargs):
                self.keys.append(kwargs.get('key'));return kwargs.get('value',80)
            def columns(self,num):return [self]*int(num)
            def metric(self,*args,**kwargs):pass
            def caption(self,*args,**kwargs):pass
            def info(self,*args,**kwargs):pass
            def warning(self,*args,**kwargs):raise AssertionError('Unexpected missing snapshot')
            def plotly_chart(self,fig,*args,**kwargs):self.charts.append(fig)
            def json(self,*args,**kwargs):pass
        st=St()
        env=dict(pd=pd,np=np,go=go,st=st,TZ='UTC',
            CONFIRMED_SWING_RETEST_DETAIL_DEFAULT_CANDLES=80,
            CONFIRMED_SWING_RETEST_DETAIL_MIN_CANDLES=40,
            CONFIRMED_SWING_RETEST_DETAIL_MAX_CANDLES=200,
            CONFIRMED_SWING_RETEST_DETAIL_FETCH_MAX=1000,
            CONFIRMED_SWING_RETEST_DETAIL_TIMEFRAME='15m')
        exec(compile(ast.Module(body=funcs,type_ignores=[]),str(APP),'exec'),env)
        row=dict(event_key='TEST|LONG|11700000',symbol='TEST',signal='LONG',timeframe='15m',
            detector='3x3',status='REACTION',swing_price=10,retest_price=10.2,
            retest_close=10.2,pivot_timestamp=900_000,
            actionable_timestamp=3_600_000,departure_timestamp=5_400_000,
            departure_price=12,retest_timestamp=11_700_000,
            entry_price=11,retest_time=pd.Timestamp('1970-01-01 03:15:00',tz='UTC'))
        view=pd.DataFrame([row]);candles=pd.DataFrame([
            dict(timestamp=i*900_000,open=10,high=12,low=9,close=11) for i in range(20)
        ])
        provider=lambda event,limit:candles
        room_provider=lambda event:dict(nearest_opposing_room_pct=3,
            nearest_opposing_swing_price=10.5,nearest_opposing_swing_tf='30m',
            reaction_price=10.2,aligned_rsi_extreme_count=0)
        selected=env['render_confirmed_swing_retest_inspector'](
            view,candles_provider=provider,room_provider=room_provider,
            ui_key_prefix='continuous_')
        self.assertEqual(selected['event_key'],'TEST|LONG|11700000')
        self.assertEqual(len(st.charts),1)
        self.assertTrue(any(t.name=='REACTION' for t in st.charts[0].data))
        self.assertTrue(all(k.startswith('continuous_') for k in st.keys if k))


if __name__=='__main__':unittest.main()
