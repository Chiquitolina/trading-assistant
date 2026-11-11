"""Causal geometric regions from distinct physical extrema; no HLL/LIVE changes."""
import unittest

import pandas as pd

from btc_multiswing_60d_explorer_v1 import M, asof_rows
from btc_multiswing_structural_confluence_v2 import (
    construct_regions,visible_region_stages,
)
from dashboard.views.reaction_multiswing_btc60d_view_v1 import build_figure

B=1_800_000_000_000


def one(pid, minute, price, *, side='LOW', tf='15m', known=None, phys=None, state='VERIFIED'):
    ts=B+int(minute)*M
    av=B+int(known if known is not None else minute+10)*M
    return {'pivot_id':pid+'_'+tf, 'side':side,'timeframe':tf,'price':price,
            'pivot_ts':ts,'available_ts':av,'physical_minute_ts':ts if state=='VERIFIED' else None,
            'physical_id':phys if phys is not None else (pid if state=='VERIFIED' else ''),
            'physical_identity_state':state}


class CausalRegionsV2(unittest.TestCase):
    def test_two_independent_extrema_form_region_at_second_confirmation(self):
        data=pd.DataFrame([one('a',0,100,known=10),one('b',120,100.1,known=135)])
        old=asof_rows(data,B+134*M,('15m',))
        r,s,_=construct_regions(old,B+134*M)
        self.assertTrue(r.empty)
        present=asof_rows(data,B+135*M,('15m',))
        r,s,stats=construct_regions(present,B+135*M)
        self.assertEqual(len(r),1)
        self.assertEqual(r.iloc[0].independent_extrema,2)
        self.assertEqual(int(r.iloc[0].born_ts),B+135*M)
        self.assertEqual(int(s.iloc[0].known_from),B+135*M)
        self.assertEqual(stats['physical_extrema_evaluated'],2)

    def test_one_physical_three_timeframes_is_not_a_region(self):
        data=pd.DataFrame([one('a',0,100,tf=tf,known=k,phys='same')
                           for tf,k in [('1m',6),('5m',30),('15m',60)]])
        known=asof_rows(data,B+200*M,('1m','5m','15m'))
        r,_,stats=construct_regions(known,B+200*M)
        self.assertEqual(stats['physical_extrema_evaluated'],1)
        self.assertTrue(r.empty)

    def test_same_price_high_low_are_not_merged(self):
        data=pd.DataFrame([one('a',0,100),one('b',120,100,side='HIGH')])
        r,_,_=construct_regions(asof_rows(data,B+200*M,('15m',)),B+200*M)
        self.assertTrue(r.empty)

    def test_tied_extrema_excluded_not_independent(self):
        data=pd.DataFrame([one('a',0,100),one('b',120,100.05,state='TIED_EXTREMA')])
        r,_,stats=construct_regions(asof_rows(data,B+200*M,('15m',)),B+200*M)
        self.assertTrue(r.empty)
        self.assertEqual(stats['ambiguous_excluded'],1)

    def test_too_close_in_time_is_not_confirming_extremum(self):
        data=pd.DataFrame([one('a',0,100),one('b',1,100.05)])
        r,_,stats=construct_regions(asof_rows(data,B+200*M,('15m',)),B+200*M)
        self.assertTrue(r.empty)
        self.assertEqual(stats['near_time_extrema_skipped'],1)

    def test_price_spread_prevents_transitive_chain(self):
        data=pd.DataFrame([one('a',0,100),one('b',120,100.19),
                           one('c',240,100.37)])
        r,_,_=construct_regions(asof_rows(data,B+400*M,('15m',)),B+400*M)
        self.assertEqual(len(r),1)
        self.assertEqual(r.iloc[0].independent_extrema,2)
        self.assertLess(r.iloc[0].band_high,100.3)

    def test_no_stages_change_earlier_band_using_future(self):
        data=pd.DataFrame([one('a',0,100,known=10),one('b',120,100.01,known=135),
                           one('c',240,100.15,known=255)])
        early=asof_rows(data,B+200*M,('15m',))
        r0,s0,_=construct_regions(early,B+200*M)
        self.assertEqual(s0.iloc[0].band_high,100.01)
        late=asof_rows(data,B+300*M,('15m',))
        r1,s1,_=construct_regions(late,B+300*M)
        self.assertEqual(int(s1.iloc[0].known_from),B+135*M)
        self.assertEqual(int(s1.iloc[0].known_until),B+255*M)
        self.assertEqual(s1.iloc[0].band_high,100.01)
        self.assertEqual(s1.iloc[1].band_high,100.15)
        self.assertEqual(r0.iloc[0].region_id,r1.iloc[0].region_id)

    def test_future_mutation_does_not_change_pre(self):
        base=pd.DataFrame([one('a',0,100),one('b',120,100.1)])
        old=asof_rows(base,B+200*M,('15m',))
        r0,s0,_=construct_regions(old,B+200*M)
        more=pd.concat([base,pd.DataFrame([one('c',300,100.05)])],ignore_index=True)
        r1,s1,_=construct_regions(asof_rows(more,B+200*M,('15m',)),B+200*M)
        pd.testing.assert_frame_equal(r0,r1)
        pd.testing.assert_frame_equal(s0,s1)

    def test_visual_stages_do_not_start_before_birth_or_end_after_cutoff(self):
        data=pd.DataFrame([one('a',0,100),one('b',120,100.1)])
        r,s,_=construct_regions(asof_rows(data,B+200*M,('15m',)),B+200*M)
        shown=visible_region_stages(s,B+0*M,B+200*M)
        self.assertEqual(int(shown.iloc[0].visible_from),int(r.iloc[0].born_ts))
        self.assertEqual(int(shown.iloc[0].visible_until),B+200*M)
        earlier=visible_region_stages(s,B,B+100*M)
        self.assertTrue(earlier.empty)

    def test_empty_and_invalid_params(self):
        r,s,stats=construct_regions(pd.DataFrame(),B)
        self.assertTrue(r.empty)
        self.assertTrue(s.empty)
        self.assertEqual(stats['candidate_regions'],0)
        with self.assertRaisesRegex(ValueError,'INVALID_REGION_PARAMETERS'):
            construct_regions(pd.DataFrame(),B,tolerance_pct=0)

    def test_reject_unfiltered_future_input(self):
        data=pd.DataFrame([one('a',0,100,known=400)])
        with self.assertRaisesRegex(ValueError,'FUTURE_SWING_INPUT_NOT_ASOF'):
            construct_regions(data,B+200*M)

    def test_lookback_window(self):
        data=pd.DataFrame([one('a',0,100),one('b',3*1440,100.02)])
        known=asof_rows(data,B+4*1440*M,('15m',))
        r,_,_=construct_regions(known,B+4*1440*M,lookback_days=1)
        self.assertTrue(r.empty)

    def test_view_has_no_hll_import_or_live_mutation(self):
        from pathlib import Path
        path=Path(__file__).parent/'dashboard/views/reaction_multiswing_btc60d_view_v1.py'
        source=path.read_text(encoding='utf-8')
        # V3 makes geometric bands available in all three research modes;
        # the legacy toggle label no longer exists.
        self.assertIn('show_regions=True', source)
        self.assertIn('Parámetros geométricos V2 · avanzados', source)
        self.assertIn('V2 · Regiones por extremos físicos independientes · PRE', source)
        self.assertIn('construct_regions(',source)
        self.assertNotIn('from hll_',source)
        self.assertNotIn('requests.get(',source)


if __name__=='__main__':
    unittest.main()


class DashboardV2Smoke(unittest.TestCase):
    def test_v2_toggle_adds_pre_only_band(self):
        import sys,types
        from pathlib import Path
        from unittest.mock import patch
        from btc_multiswing_60d_explorer_v1 import ms
        from dashboard.views.reaction_multiswing_btc60d_view_v1 import render_btc_multiswing_60d_view
        end=ms('2026-11-08T01:00:00Z')
        start=ms('2026-09-09T01:00:00Z')
        bar_start=end-2*1440*M
        bars=pd.DataFrame({'timestamp':[bar_start+i*M for i in range(2*1440)],
                           'open':[100.0]*2880,'high':[101.0]*2880,
                           'low':[99.0]*2880,'close':[100.0]*2880})
        data=pd.DataFrame([
            one('X',0,100.0,known=10),one('Y',120,100.10,known=135),
        ])
        # Move events into latest visible 7-day window.
        delta=bar_start-B+200*M
        for field in ('pivot_ts','available_ts','physical_minute_ts'):
            data[field] += delta
        class FakeUI(types.ModuleType):
            def __init__(self):
                super().__init__('streamlit')
                self.figures=[];self.errors=[];self.tables=[]
            def __enter__(self): return self
            def __exit__(self,*a): return False
            def markdown(self,*a,**k): pass
            def radio(self,label,options,**k): return 'MAPA'
            def caption(self,*a,**k): pass
            def columns(self,n): return [self]*(n if isinstance(n,int) else len(n))
            def date_input(self,*a,**k): return k['value']
            def selectbox(self,label,options,**k): return options[k.get('index',0)]
            def multiselect(self,*a,**k): return ['15m']
            def checkbox(self,label,**k): return True if label.startswith('V2 · Regiones') else k.get('value',False)
            def metric(self,*a,**k): pass
            def info(self,*a,**k): pass
            def warning(self,*a,**k): self.errors.append(a)
            def error(self,*a,**k): self.errors.append(a)
            def dataframe(self,*a,**k): self.tables.append(a)
            def expander(self,*a,**k): return self
            def json(self,*a,**k): pass
            def plotly_chart(self,figure,**k): self.figures.append(figure)
        fake=FakeUI()
        with patch.dict(sys.modules,{'streamlit':fake}),patch(
            'dashboard.views.reaction_multiswing_btc60d_view_v1.load_snapshot',
            return_value=(bars,data,{'start':start,'end':end,
                'source_sha256':'x','catalog_sha256':'y','source_hll_parity':'PARITY_OK'})
        ):
            render_btc_multiswing_60d_view(Path('/tmp/no_network'))
        self.assertFalse(fake.errors)
        self.assertEqual(len(fake.figures),1)
        self.assertGreaterEqual(len(fake.figures[0].layout.shapes),1)
        self.assertEqual(fake.figures[0].data[0].type,'candlestick')
        # MAPA intentionally has no verbose pivot or region tables.
        self.assertEqual(len(fake.tables),0)
