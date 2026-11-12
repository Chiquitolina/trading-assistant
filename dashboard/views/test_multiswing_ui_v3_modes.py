"""UI V3.0 isolated behavioral and regression guards (no LIVE or network)."""
from __future__ import annotations

import inspect
import unittest

import pandas as pd

from dashboard.views import reaction_multiswing_btc60d_view_v1 as view
from dashboard.views.reaction_multiswing_btc60d_view_v1 import (
    V3_MODES, build_figure, render_btc_multiswing_60d_view,
    v3_execution_visual_window, v3_rank_regions_near_price,
    v3_view_policy,
)
from btc_multiswing_60d_explorer_v1 import DAY, M, aggregate_closed


class V3Modes(unittest.TestCase):
    def test_three_modes_and_default_clean_map(self):
        self.assertEqual(V3_MODES, ('MAPA', 'INSPECTOR', 'EJECUCION'))
        policy = v3_view_policy('MAPA')
        self.assertFalse(policy['draw_all_pivots_by_default'])
        self.assertFalse(policy['allow_post_inspection'])
        self.assertFalse(policy['read_only_execution_preview'])

    def test_inspector_keeps_history_separate(self):
        policy = v3_view_policy('INSPECTOR')
        self.assertTrue(policy['show_region_audit'])
        self.assertTrue(policy['allow_post_inspection'])
        self.assertFalse(policy['draw_all_pivots_by_default'])

    def test_execution_only_read_only_preview(self):
        policy = v3_view_policy('EJECUCION')
        self.assertTrue(policy['read_only_execution_preview'])
        self.assertFalse(policy['allow_post_inspection'])
        self.assertFalse(policy['draw_all_pivots_by_default'])

    def test_unknown_mode_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'INVALID_MULTISWING_UI_MODE'):
            v3_view_policy('LIVE')

    def test_proximity_ranking_is_stable_and_not_score(self):
        original = pd.DataFrame([
            {'region_id':'c', 'center': 101, 'known_from':10},
            {'region_id':'b', 'center': 99, 'known_from':10},
            {'region_id':'a', 'center':100.2, 'known_from':3},
        ])
        result = v3_rank_regions_near_price(original, 100)
        self.assertEqual(result.region_id.tolist(), ['a','b','c'])
        self.assertEqual(original.region_id.tolist(), ['c','b','a'])
        self.assertEqual(result.columns.tolist(), original.columns.tolist())

    def test_proximity_ties_use_recent_then_id(self):
        original = pd.DataFrame([
            {'region_id':'z','center':100,'known_from':1},
            {'region_id':'a','center':100,'known_from':2},
            {'region_id':'b','center':100,'known_from':2},
        ])
        result = v3_rank_regions_near_price(original,100)
        self.assertEqual(result.region_id.tolist(),['a','b','z'])

    def test_proximity_invalid_reference_refused(self):
        frame = pd.DataFrame({'region_id':['a'],'center':[99.0],'known_from':[1]})
        for price in (0,-5,float('nan'),float('inf')):
            with self.subTest(price=price):
                with self.assertRaises(ValueError):
                    v3_rank_regions_near_price(frame,price)

    def test_proximity_does_not_accept_bad_geometry(self):
        frame = pd.DataFrame({'region_id':['a'],'center':[0.0],'known_from':[1]})
        with self.assertRaisesRegex(ValueError,'INVALID_REGION_RANKING_PRICE'):
            v3_rank_regions_near_price(frame,100)

    def test_execution_window_24h_and_source_clamp(self):
        start, cutoff = 100*M, 2*DAY+100*M
        self.assertEqual(v3_execution_visual_window(source_start=start,cutoff=cutoff),
                         (cutoff-DAY,cutoff))
        self.assertEqual(v3_execution_visual_window(source_start=start,cutoff=start+2*M),
                         (start,start+2*M))

    def test_execution_window_never_accepts_nonpositive_or_overlong(self):
        for minutes in (0,1441,-1):
            with self.subTest(minutes=minutes):
                with self.assertRaises(ValueError):
                    v3_execution_visual_window(source_start=0,cutoff=DAY,minutes=minutes)
        with self.assertRaises(ValueError):
            v3_execution_visual_window(source_start=10,cutoff=10)

    def test_execution_visual_closed_minutes_only(self):
        # The same existing verified aggregation enforces complete bars at cutoff.
        stamp = 10*DAY
        frame = pd.DataFrame({
            'timestamp':[stamp+i*M for i in range(6)],
            'open':[100.]*6,'high':[101.]*6,'low':[99.]*6,'close':[100.]*6,
        })
        left,right=v3_execution_visual_window(source_start=stamp,cutoff=stamp+5*M)
        bars=aggregate_closed(frame,'1m',left,right)
        self.assertEqual(len(bars),5)
        self.assertLess(int(bars.timestamp.max()),right)

    def test_map_does_not_pass_all_pivots_by_default(self):
        source=inspect.getsource(render_btc_multiswing_60d_view)
        self.assertIn('plot_markers = markers if show_all_pivots else markers.iloc[0:0].copy()',source)
        self.assertIn("value=False,key='ms60_v3_map_pivots'",source)
        self.assertIn("if mode == 'MAPA':",source)
        self.assertIn('return\n    if mode == \'EJECUCION\':',source)

    def test_v22_v23_only_visible_on_explicit_inspector_history(self):
        source=inspect.getsource(render_btc_multiswing_60d_view)
        self.assertIn("key='ms60_v3_inspector_history'",source)
        self.assertIn("if mode == 'INSPECTOR' and inspect_history",source)
        self.assertLess(source.index("if mode == 'EJECUCION':\n        if region_inspection"),
                        source.index('audit_fixed_region('))
        self.assertIn("key='ms60_v23_visit_chart'",source)
        self.assertIn("key='ms60_v22_fixed_region_chart'",source)

    def test_ui_does_not_change_detector_or_execute_trades(self):
        source=inspect.getsource(render_btc_multiswing_60d_view)
        self.assertIn('v313_region_build(',source)
        self.assertIn('construct_regions(',inspect.getsource(view.v313_region_build))
        self.assertIn('region_member_evidence(',source)
        self.assertNotIn('order.place',source)
        self.assertNotIn('RedisMarketDataProvider',source)
        self.assertNotIn('from dashboard.views.hll_',source)
        self.assertIn('SIN triggers validados',source)


# A tiny synthetic Streamlit surface exercises the three real render branches
# without requiring a running web server or the VPS freeze in local tests.
class _Panel:
    def __init__(self, ui):
        self.ui = ui
    def __enter__(self):
        return self.ui
    def __exit__(self, *args):
        return False
    def __getattr__(self, name):
        return getattr(self.ui, name)


class _UI:
    def __init__(self, mode):
        self.mode = mode
        self.plot_keys = []
        self.session_state = {}
    def radio(self, label, options, **kwargs):
        return self.mode
    def date_input(self, label, **kwargs):
        return kwargs['value']
    def time_input(self, label, **kwargs):
        return kwargs['value']
    def selectbox(self, label, options, **kwargs):
        # Legacy render regressions explicitly exercise the unchanged manual
        # replay. New event-navigation tests cover the automatic default.
        if kwargs.get('key')=='ms60_v33_event_origin':
            return 'MANUAL'
        return options[kwargs.get('index',0)]
    def multiselect(self, label, options, **kwargs):
        return kwargs.get('default',[])
    def checkbox(self, label, **kwargs):
        return kwargs.get('value',False)
    def columns(self, specs):
        return [_Panel(self) for _ in range(specs if isinstance(specs,int) else len(specs))]
    def expander(self, *args, **kwargs):
        return _Panel(self)
    def plotly_chart(self, fig, **kwargs):
        self.plot_keys.append(kwargs.get('key'))
    def __getattr__(self, name):
        if name in ('markdown','caption','info','warning','error','metric','dataframe','json','button'):
            return lambda *args, **kwargs: None
        raise AttributeError(name)


class V3SyntheticRenderSmoke(unittest.TestCase):
    def test_map_inspector_and_execution_dont_require_live(self):
        import sys
        from unittest.mock import patch
        from datetime import datetime, timezone
        import dashboard.views.reaction_multiswing_btc60d_view_v1 as view
        cutoff=int(datetime(2026,11,8,1,tzinfo=timezone.utc).timestamp()*1000)
        start=cutoff-2*DAY
        frame=pd.DataFrame({
            'timestamp':[start+i*M for i in range(2*24*60)],
            'open':[100.0]*(2*24*60),
            'high':[101.0]*(2*24*60),
            'low':[99.0]*(2*24*60),
            'close':[100.0]*(2*24*60),
        })
        source={'start':start,'end':cutoff,
                'source_sha256':'test','catalog_sha256':'test',
                'source_hll_parity':'PARITY_OK'}
        no_pivots=pd.DataFrame(columns=['plot_ts','timeframe','group_key','pivot_id',
                                        'side','price','physical_identity_state'])
        counters={'representations':0,'verified_extrema':0,
                  'multi_tf_extrema':0,'ambiguous_representations':0}
        for mode in V3_MODES:
            with self.subTest(mode=mode):
                ui=_UI(mode)
                with patch.dict(sys.modules, {'streamlit':ui}), \
                     patch.object(view,'load_snapshot',return_value=(frame,pd.DataFrame(),source)), \
                     patch.object(view,'asof_rows',return_value=pd.DataFrame()), \
                     patch.object(view,'group_asof',return_value=no_pivots), \
                     patch.object(view,'window_evidence_stats',return_value=counters):
                    view.render_btc_multiswing_60d_view('/tmp/none')
                self.assertEqual(ui.plot_keys,['ms60_chart'])


class V3SelectedRegionRenderSmoke(unittest.TestCase):
    def test_selected_band_renders_in_all_modes_without_running_v22(self):
        import sys
        from unittest.mock import patch
        from datetime import datetime, timezone
        import dashboard.views.reaction_multiswing_btc60d_view_v1 as view
        cutoff=int(datetime(2026,11,8,1,tzinfo=timezone.utc).timestamp()*1000)
        start=cutoff-2*DAY
        full=pd.DataFrame({
            'timestamp':[start+i*M for i in range(2*24*60)],
            'open':[100.]*(2*24*60), 'high':[101.]*(2*24*60),
            'low':[99.]*(2*24*60), 'close':[100.]*(2*24*60),
        })
        source={'start':start,'end':cutoff,'source_sha256':'sha',
                'catalog_sha256':'cat','source_hll_parity':'PARITY_OK'}
        rid='msv2_testregion'
        born=cutoff-12*60*M
        regions=pd.DataFrame([{'region_id':rid,'side':'LOW','center':100.,
            'band_low':99.9,'band_high':100.1,'first_pivot_ts':start,
            'last_pivot_ts':born-M,'born_ts':born,'last_confirmation_ts':born,
            'independent_extrema':2,'members':'a,b','temporalities':'15m',
            'status':'GEOMETRIC_CANDIDATE_NOT_ACTIVE'}])
        stages=pd.DataFrame([{'region_id':rid,'side':'LOW','known_from':born,
            'known_until':cutoff,'band_low':99.9,'band_high':100.1,
            'independent_extrema':2}])
        members=pd.DataFrame([{'physical_id':'a','side':'LOW','price':99.9,
            'plot_ts':born-3*M,'first_available_asof':born,
            'last_available_asof':born,'tf_set_asof':'15m','representations_asof':1},
            {'physical_id':'b','side':'LOW','price':100.1,
            'plot_ts':born-2*M,'first_available_asof':born,
            'last_available_asof':born,'tf_set_asof':'15m','representations_asof':1}])
        no_pivots=pd.DataFrame(columns=['plot_ts','timeframe','group_key','pivot_id',
                                        'side','price','physical_identity_state'])
        counters={'representations':0,'verified_extrema':0,
                  'multi_tf_extrema':0,'ambiguous_representations':0}
        for mode in V3_MODES:
            with self.subTest(mode=mode):
                ui=_UI(mode)
                ordinary_selectbox=ui.selectbox
                def select_region(label,options,**kwargs):
                    if kwargs.get('key')=='ms60_v21_region_selection':
                        return rid
                    return ordinary_selectbox(label,options,**kwargs)
                ui.selectbox=select_region
                with patch.dict(sys.modules,{'streamlit':ui}), \
                     patch.object(view,'load_snapshot',return_value=(full,pd.DataFrame(),source)), \
                     patch.object(view,'asof_rows',return_value=pd.DataFrame()), \
                     patch.object(view,'group_asof',return_value=no_pivots), \
                     patch.object(view,'window_evidence_stats',return_value=counters), \
                     patch.object(view,'construct_regions',return_value=(regions,stages,{
                          'candidate_regions':1,'physical_extrema_evaluated':2,'ambiguous_excluded':0})), \
                     patch.object(view,'region_member_evidence',return_value=members), \
                     patch.object(view,'audit_fixed_region',side_effect=AssertionError('V22_CALLED')):
                    view.render_btc_multiswing_60d_view('/tmp/none')
                self.assertEqual(ui.plot_keys,['ms60_chart'])


if __name__ == '__main__':
    unittest.main()
