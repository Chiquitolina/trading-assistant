"""V3.1.3: frozen-only bounded caches and birth/stage replay navigation.

Pure synthetic data; no VPS, network, HLL, LIVE or service mutation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch
import unittest
import sys

import numpy as np
import pandas as pd

from btc_multiswing_60d_explorer_v1 import DAY, M, TF_MINUTES, aggregate_closed
from dashboard.views import reaction_multiswing_btc60d_view_v1 as view
from test_multiswing_ui_v33_event_navigation import _AutoUI, example_data


class FrozenCacheV313(unittest.TestCase):
    def setUp(self):
        for cache in view._V313_CACHES.values():
            cache.clear()

    def test_only_verified_hashes_enable_data_cache(self):
        self.assertFalse(view.v313_verified_cache_source({'source_sha256':'mock',
                                                         'catalog_sha256':'mock'}))
        self.assertFalse(view.v313_verified_cache_source({}))
        self.assertTrue(view.v313_verified_cache_source({
            'source_sha256':'1'*64,'catalog_sha256':'a'*64}))

    def test_region_cache_hits_and_invalidates_per_cutoff_tf_geometry_and_catalog(self):
        a=object()
        known=pd.DataFrame({'x':[1]})
        calls=[]
        def fake(rows,cutoff,**params):
            calls.append((cutoff,dict(params)))
            return (pd.DataFrame({'n':[len(calls)]}),pd.DataFrame(),{'n':len(calls)})
        def invoke(catalog=a,cutoff=1000,spread=.2,tfs=('15m','30m')):
            return view.v313_region_build(catalog,known,cutoff=cutoff,
                selected_tfs=tfs,side='BOTH',tolerance_pct=spread,
                lookback_days=7,separation=60,enabled=True)
        with patch.object(view,'construct_regions',side_effect=fake):
            first=invoke()
            self.assertIs(first,invoke())
            self.assertEqual(len(calls),1)
            self.assertIsNot(first,invoke(cutoff=1001))
            self.assertIsNot(first,invoke(spread=.3))
            self.assertIsNot(first,invoke(tfs=('1h',)))
            self.assertIsNot(first,invoke(catalog=object()))
            self.assertEqual(len(calls),5)
            # A data mutation or a test/mock path must bypass the process cache.
            invoke(cutoff=1000)
            self.assertEqual(len(calls),5)
            view.v313_region_build(a,known,cutoff=1000,selected_tfs=('15m','30m'),
                side='BOTH',tolerance_pct=.2,lookback_days=7,
                separation=60,enabled=False)
            self.assertEqual(len(calls),6)

    def test_group_metrics_and_navigation_event_cache(self):
        catalog=object()
        rows=pd.DataFrame({'test':[1]})
        with patch.object(view,'group_asof',return_value=pd.DataFrame({'a':[1]})) as m:
            a=view.v313_group_build(catalog,rows,cutoff=42,
                selected_tfs=('15m',),side='LOW',collapse=True,
                multi_only=False,enabled=True)
            self.assertIs(a,view.v313_group_build(catalog,rows,cutoff=42,
                selected_tfs=('15m',),side='LOW',collapse=True,
                multi_only=False,enabled=True))
            self.assertEqual(m.call_count,1)
            view.v313_group_build(catalog,rows,cutoff=43,
                selected_tfs=('15m',),side='LOW',collapse=True,
                multi_only=False,enabled=True)
            self.assertEqual(m.call_count,2)
        full,stages,_,_,_,end,rid=example_data()
        one=view.v313_events_build(full,stages,region_id=rid,source_end=end,
                                   gap_minutes=30,enabled=True)
        self.assertIs(one,view.v313_events_build(full,stages,region_id=rid,
                                   source_end=end,gap_minutes=30,enabled=True))
        self.assertIsNot(one,view.v313_events_build(full,stages,region_id=rid,
                                   source_end=end,gap_minutes=60,enabled=True))

    def test_precomputed_bars_identical_to_existing_for_all_six_tfs(self):
        begin=int(datetime(2026,11,3,0,0,tzinfo=timezone.utc).timestamp()*1000)
        n=3*24*60+39
        t=np.arange(n,dtype=np.int64)*M+begin
        # Distinct OHLC every minute, sufficient to check UTC boundaries and aggregation.
        o=100+np.sin(np.arange(n)/17)*2
        c=o+np.cos(np.arange(n)/9)*.21
        hi=np.maximum(o,c)+.13
        lo=np.minimum(o,c)-.15
        full=pd.DataFrame({'timestamp':t,'open':o,'high':hi,'low':lo,'close':c})
        windows=[(begin+17*M,begin+DAY+13*60*M+31*M),
                 (begin+3*M,begin+3*DAY+21*M),
                 (begin+2*DAY,begin+3*DAY+39*M)]
        for tf in TF_MINUTES:
            with self.subTest(tf=tf):
                agg=view.v313_preaggregated(full,tf,enabled=True)
                self.assertIs(agg,view.v313_preaggregated(full,tf,enabled=True))
                for left,right in windows:
                    expected=aggregate_closed(full,tf,left,right)
                    result=view.v313_slice_bars(agg,tf,left,right)
                    pd.testing.assert_frame_equal(result,expected)
                    self.assertTrue((result.timestamp + TF_MINUTES[tf]*M <= right).all())
                    self.assertTrue((result.timestamp + TF_MINUTES[tf]*M > left).all())

    def test_bounded_cache_does_not_grow_without_limit(self):
        data=object()
        for k in range(40):
            view.v313_memo('events',(k,), (data,),lambda k=k:k,enabled=True)
        self.assertEqual(len(view._V313_CACHES['events']),
                         view._V313_CAPACITY['events'])
        with self.assertRaises(ValueError):
            view.v313_memo('unknown',(1,),(),lambda:1,enabled=True)


class EventSelectionV313(unittest.TestCase):
    def test_birth_and_subsequent_confirmation_have_distinct_cutoffs(self):
        full,stages,regions,t,start,end,rid=example_data()
        later=t+30*M
        stages=stages.copy()
        second=stages.iloc[0].copy()
        stages.loc[0,'known_until']=later
        second['known_from']=later
        second['independent_extrema']=3
        second['band_low']=98.5
        second['known_until']=end
        stages=pd.concat([stages,pd.DataFrame([second])],ignore_index=True)
        def render(ui):
            source={'start':start,'end':end,'source_sha256':'mock',
                    'catalog_sha256':'mock','source_hll_parity':'PARITY_OK'}
            empty=pd.DataFrame(columns=['plot_ts','timeframe','group_key',
                'pivot_id','side','price','physical_identity_state'])
            counters={'representations':0,'verified_extrema':0,
                      'multi_tf_extrema':0,'ambiguous_representations':0}
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
        birth=_AutoUI(rid,origin='BIRTH',show_context=True)
        render(birth)
        born=int(stages.iloc[0]['known_from'])
        self.assertEqual(birth.session_state['ms60_v31_last_cutoff'],born)
        self.assertEqual(birth.plot_keys,['ms60_chart','ms60_v32_context_chart'])
        self.assertFalse(any(sh.yref=='paper' and sh.type=='rect'
            for sh in birth.figures['ms60_chart'].layout.shapes))
        stage=_AutoUI(rid,origin='CONFIRMATION',show_context=True)
        render(stage)
        self.assertEqual(stage.session_state['ms60_v31_last_cutoff'],later)
        self.assertNotEqual(born,later)
        # The subsequent stage is confirmed at the decision minute. It may
        # have zero width until a later minute, so do not demand a retrospective
        # visible rectangle left of its own confirmation instant.
        self.assertFalse(any(abs(float(sh.y0)-98.5)<1e-9
            for sh in birth.figures['ms60_chart'].layout.shapes if sh.yref=='y'))
        birth_ev=view.v33_structural_event_index(full,stages,region_id=rid,
            source_end=end)
        stage_row=birth_ev[(birth_ev.event_type=='CONFIRMATION') &
                           (birth_ev.decision_ts==later)].iloc[0]
        self.assertEqual(stage_row.extrema_at_event,3)

    def test_post_minute_availability_near_end_of_freeze_is_exact(self):
        _,_,_,t,start,end,_=example_data()
        self.assertEqual((end-(end-36*M))//M,36)
        stop,status=view.v31_reveal_post_window(pre_cutoff=end-36*M,
                         source_end=end,reveal=True,horizon_minutes=720)
        self.assertEqual(stop,end)
        self.assertEqual(status,'CUTOFF_CENSORED')


if __name__=='__main__':
    unittest.main()
