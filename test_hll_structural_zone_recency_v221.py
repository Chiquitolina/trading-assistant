"""Causal tests for visual freshness (does not change historical importance)."""
from __future__ import annotations

import unittest
import pandas as pd

from hll_structural_zone_recency_v221 import (
    annotate_recency, select_recent_overlay, TF, DAY)

T = (1_793_900_000_000 // TF)*TF


def rec(i, center=76000, born=None, members=None, tier='C', ttl='ACTIVE_TTL'):
    import json
    ids=members or [f'{i}_1']
    return {'zone_id':i,'level_ids':json.dumps(ids),'members':len(ids),
            'created_at':born if born is not None else T-30*DAY,
            'center':center,'lifecycle':ttl,'relative_tier':tier,
            'evidence_index':25.}


def ev(*tuples):
    return pd.DataFrame(tuples,columns=['level_id','event_type','observed_closed_ts'])


class RecencyV221(unittest.TestCase):
    def test_dedup_same_bar_multiple_lines(self):
        zones=pd.DataFrame([rec('a',members=['x','y'])])
        ts=T-2*DAY
        events=ev(('x','TEST_FROM_ABOVE',ts),('y','TEST_FROM_ABOVE',ts),
                  ('x','DEFENSE_CONFIRMED',ts))
        out=annotate_recency(zones,events,T,7).iloc[0]
        self.assertEqual(out.interactions_in_window,1)
        self.assertEqual(out.recent_status,'INTERACCION_3D')

    def test_events_before_zone_birth_not_counted(self):
        z=pd.DataFrame([rec('a',born=T-5*DAY,members=['x'])])
        events=ev(('x','TEST_FROM_BELOW',T-10*DAY))
        row=annotate_recency(z,events,T,3).iloc[0]
        self.assertEqual(row.interactions_in_window,0)
        self.assertEqual(row.recent_status,'SOLO_HISTORICA')

    def test_new_zone_without_test_is_not_confirmed_defense(self):
        z=pd.DataFrame([rec('a',born=T-DAY,members=['x'])])
        row=annotate_recency(z,ev(),T,7).iloc[0]
        self.assertTrue(row.has_recent_activity)
        self.assertEqual(row.recent_status,'ZONA_NUEVA_SIN_INTERACCION_RECIENTE')
        self.assertIsNone(row.last_interaction_ts)

    def test_future_events_do_not_repaint_cutoff(self):
        z=pd.DataFrame([rec('a',members=['x'])])
        old=ev(('x','TEST_FROM_ABOVE',T-10*DAY))
        future=ev(('x','DEFENSE_CONFIRMED',T+DAY))
        before=annotate_recency(z,old,T,7)
        after=annotate_recency(z,pd.concat([old,future]),T,7)
        self.assertEqual(before.to_dict('records'),after.to_dict('records'))

    def test_historical_grade_never_changes(self):
        z=pd.DataFrame([rec('a',tier='D')])
        out=annotate_recency(z,ev(('a_1','LOSS_CONFIRMED',T-DAY)),T,3)
        self.assertEqual(out.iloc[0].relative_tier,'D')
        self.assertEqual(out.iloc[0].recent_status,'INTERACCION_3D')

    def test_local_overlay_accepts_recent_c_and_rejects_expired(self):
        z=pd.DataFrame([rec('a',center=76000,tier='C'),
                        rec('b',center=76300,tier='B',ttl='EXPIRED')])
        r=annotate_recency(z,ev(('a_1','TEST_FROM_ABOVE',T-DAY),
                                ('b_1','TEST_FROM_BELOW',T-DAY)),T,7)
        chosen=select_recent_overlay(r,75800,limit=4)
        self.assertEqual(chosen.zone_id.tolist(),['a'])
        self.assertEqual(chosen.iloc[0].relative_tier,'C')

    def test_local_uses_near_price_rather_than_high_historic_score(self):
        z=pd.DataFrame([rec('a',center=80000,tier='A'),
                        rec('b',center=75750,tier='D')])
        r=annotate_recency(z,ev(('a_1','TEST_FROM_ABOVE',T-DAY),
                                ('b_1','TEST_FROM_ABOVE',T-DAY)),T,7)
        out=select_recent_overlay(r,75800,limit=1)
        self.assertEqual(out.zone_id.tolist(),['b'])

    def test_reserved_centers_prevent_near_duplicate_overlay(self):
        z=pd.DataFrame([rec('a',center=76010),rec('b',center=76300)])
        r=annotate_recency(z,ev(('a_1','TEST_FROM_BELOW',T-DAY),
                                ('b_1','TEST_FROM_BELOW',T-DAY)),T,7)
        out=select_recent_overlay(r,75990,limit=2,reserved_centers=[76000],spacing_pct=.45)
        self.assertEqual(out.zone_id.tolist(),[])

    def test_invalid_or_future_birth_fails_closed(self):
        with self.assertRaises(ValueError):
            annotate_recency(pd.DataFrame([rec('x',born=T+TF)]),ev(),T,7)

    def test_hide_expired_option_keeps_only_full_ttl(self):
        z=pd.DataFrame([rec('a',center=76000),
                        rec('b',center=77500,ttl='PARTIAL_EXPIRED')])
        r=annotate_recency(z,ev(('a_1','TEST_FROM_ABOVE',T-DAY),
                                ('b_1','TEST_FROM_BELOW',T-DAY)),T,7)
        out=select_recent_overlay(r,76700,limit=4,include_partial_expired=False)
        self.assertEqual(out.zone_id.tolist(),['a'])

    def test_out_of_range_filters_fail(self):
        z=pd.DataFrame([rec('a')])
        with self.assertRaises(ValueError):
            annotate_recency(z,ev(),T,15)
        r=annotate_recency(z,ev(),T,7)
        with self.assertRaises(ValueError):
            select_recent_overlay(r,0)


if __name__=='__main__':
    unittest.main()
