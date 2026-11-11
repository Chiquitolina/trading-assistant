"""V2.1 display-only inspection tests: causal staged geometry, not performance."""
import unittest
import pandas as pd

from btc_multiswing_60d_explorer_v1 import M, asof_rows
from btc_multiswing_structural_confluence_v2 import (
    construct_regions, visible_region_candidates, display_region_ids,
    region_member_evidence,
)

B=1_800_000_000_000


def one(pid, minute, price, *, side='LOW', tf='15m', known=None, phys=None):
    ts=B+int(minute)*M
    return {'pivot_id':pid+'_'+tf, 'side':side, 'timeframe':tf,
            'price':price, 'pivot_ts':ts,
            'available_ts':B+int(known if known is not None else minute+10)*M,
            'physical_minute_ts':ts, 'physical_id':phys or pid,
            'physical_identity_state':'VERIFIED'}


def fixture(cutoff_minutes=400):
    data=pd.DataFrame([one('a',0,100,known=10),one('b',120,100.10,known=135),
                       one('c',240,100.15,known=255),
                       one('h',20,110,side='HIGH',known=30),
                       one('i',180,110.08,side='HIGH',known=190)])
    known=asof_rows(data,B+cutoff_minutes*M,('15m',))
    regions,stages,_=construct_regions(known,B+cutoff_minutes*M)
    return known,regions,stages


class RegionInspectorV21(unittest.TestCase):
    def test_visible_only_with_causal_stage(self):
        known,regions,stages=fixture()
        c=visible_region_candidates(regions,stages,B+135*M,B+250*M)
        self.assertEqual(len(c),2)  # LOW stage from 135 and HIGH from 190
        low=c[c.side=='LOW'].iloc[0]
        self.assertEqual(int(low.independent_extrema),2)
        self.assertEqual(float(low.band_high),100.10)
        self.assertEqual(int(low.known_from),B+135*M)

    def test_min_extrema_uses_stage_not_final_count(self):
        known,regions,stages=fixture()
        early=visible_region_candidates(regions,stages,B+135*M,B+250*M,min_extrema=3)
        self.assertTrue(early.empty)
        later=visible_region_candidates(regions,stages,B+135*M,B+350*M,min_extrema=3)
        self.assertEqual(len(later),1)
        self.assertEqual(later.iloc[0].independent_extrema,3)
        self.assertEqual(later.iloc[0].band_high,100.15)

    def test_no_future_region_visibility(self):
        _,regions,stages=fixture()
        early=visible_region_candidates(regions,stages,B,B+130*M)
        self.assertTrue(early.empty)

    def test_side_filter(self):
        _,regions,stages=fixture()
        low=visible_region_candidates(regions,stages,B,B+400*M,side='LOW')
        high=visible_region_candidates(regions,stages,B,B+400*M,side='HIGH')
        self.assertEqual(set(low.side),{'LOW'})
        self.assertEqual(set(high.side),{'HIGH'})
        self.assertEqual(len(low),len(high))

    def test_ranking_is_descriptive_and_deterministic(self):
        _,regions,stages=fixture()
        recent=visible_region_candidates(regions,stages,B,B+400*M,order='RECENT')
        count=visible_region_candidates(regions,stages,B,B+400*M,order='EXTREMA')
        self.assertEqual(recent.iloc[0].side,'LOW')
        self.assertEqual(count.iloc[0].side,'LOW')
        pd.testing.assert_frame_equal(count,visible_region_candidates(regions,stages,B,B+400*M,order='EXTREMA'))

    def test_display_limit_doesnt_mutate_candidates(self):
        _,regions,stages=fixture()
        c=visible_region_candidates(regions,stages,B,B+400*M)
        copied=c.copy(deep=True)
        shown,stats=display_region_ids(c,5)
        self.assertEqual(stats['eligible_regions'],2)
        self.assertEqual(stats['drawn_regions'],2)
        self.assertEqual(stats['hidden_by_display_limit'],0)
        self.assertEqual(len(shown),2)
        pd.testing.assert_frame_equal(c,copied)

    def test_manual_selection_not_required_to_be_top_k(self):
        _,regions,stages=fixture()
        c=visible_region_candidates(regions,stages,B,B+400*M)
        selected=str(c.region_id.iloc[-1])
        shown,stats=display_region_ids(c,5,selected)
        self.assertEqual(shown,[selected])
        self.assertEqual(stats['drawn_regions'],1)

    def test_member_evidence_is_asof_and_independent(self):
        known,regions,stages=fixture()
        selected=regions[regions.side=='LOW'].iloc[0]
        m=region_member_evidence(known,selected,B+400*M)
        self.assertEqual(len(m),3)
        self.assertEqual(m.physical_id.tolist(),['a','b','c'])
        self.assertTrue((m.last_available_asof<=B+400*M).all())
        # Adding another timeframe for a cannot create a fourth member.
        extended=pd.concat([known,pd.DataFrame([one('a',0,100,tf='5m',known=60,phys='a')])],ignore_index=True)
        m2=region_member_evidence(extended,selected,B+400*M)
        self.assertEqual(len(m2),3)
        self.assertEqual(int(m2.iloc[0].representations_asof),2)

    def test_future_member_refused(self):
        known,regions,_=fixture()
        selected=regions[regions.side=='LOW'].iloc[0]
        with self.assertRaisesRegex(ValueError,'FUTURE_REGION_MEMBER_EVIDENCE'):
            region_member_evidence(known,selected,B+200*M)

    def test_invalid_selected_id_refused(self):
        _,regions,stages=fixture()
        c=visible_region_candidates(regions,stages,B,B+400*M)
        with self.assertRaisesRegex(ValueError,'SELECTED_REGION_NOT_VISIBLE_ASOF'):
            display_region_ids(c,10,'missing')

    def test_invalid_filters_refused(self):
        _,regions,stages=fixture()
        with self.assertRaisesRegex(ValueError,'INVALID_REGION_INSPECTION_FILTER'):
            visible_region_candidates(regions,stages,B+100*M,B+100*M)
        with self.assertRaisesRegex(ValueError,'INVALID_REGION_DRAW_LIMIT'):
            display_region_ids(pd.DataFrame(),100)

    def test_dashboard_has_explicit_limits_and_no_hll_dependency(self):
        from pathlib import Path
        view=Path(__file__).parent/'dashboard/views/reaction_multiswing_btc60d_view_v1.py'
        s=view.read_text(encoding='utf-8')
        # V3 moved the old V2.1 heading into the INSPECTOR mode, but kept
        # causal candidate selection, limits and physical-member inspection.
        self.assertIn("V3_MODES = ('MAPA', 'INSPECTOR', 'EJECUCION')", s)
        self.assertIn('Mínimo de extremos independientes', s)
        self.assertIn('Máximo de regiones dibujadas', s)
        self.assertIn("if mode == 'INSPECTOR' and inspect_history", s)
        self.assertIn('region_member_evidence(',s)
        self.assertIn('visible_region_candidates(',s)
        self.assertNotIn('from hll_',s)

if __name__=='__main__':
    unittest.main()
