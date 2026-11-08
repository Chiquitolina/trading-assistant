"""Tests for optional, read-only historical zone ranking V2.1."""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

import pandas as pd

try:
    import horizontal_breakout_lab
except ModuleNotFoundError:
    fake=types.ModuleType('horizontal_breakout_lab')
    fake.TF=1_800_000;fake.ROOT=Path('.');fake.Archive=object
    fake.aggregate_15m_to_30m=lambda x:x;fake.normalize=lambda x,tf:x
    sys.modules['horizontal_breakout_lab']=fake

from hll_structural_zone_ranking_v21 import (
    rank_zone_evidence, select_historical_map, _independent, TF, DAY)
from dashboard.views.hll_hierarchical_levels_view import make_zone_chart

C=1_790_000_000_000//TF*TF + 1000*TF
S=C-60*DAY


def zone(i,price,tests,defenses=1,losses=1,birth=None,members=1,score=100):
    ids=[f'{i}_{k}' for k in range(members)]
    return dict(zone_id=i,level_ids=__import__('json').dumps(ids),members=members,
          created_at=birth if birth is not None else S+DAY,
          center=price,tier='A',importance_score=score,
          tests_since_zone_birth=tests,defenses_since_birth=defenses,
          losses_since_birth=losses,age_days=40.,lifecycle='ACTIVE_TTL')


def test_records(*rows):
    return pd.DataFrame(rows,columns=['level_id','event_type','observed_closed_ts'])


class RankV21(unittest.TestCase):
    def test_relative_tiers_not_inherited_and_non_saturated(self):
        z=pd.DataFrame([zone(f'z{i}',76000+i*1000,1+i,score=100) for i in range(10)])
        r=rank_zone_evidence(z,test_records(),C,S)
        self.assertEqual(len(r),10)
        self.assertTrue((r.v2_score==100).all())
        self.assertLess((r.relative_tier=='A').sum(),4)
        self.assertGreater((r.relative_tier=='D').sum(),0)
        self.assertEqual(r.evidence_method.nunique(),1)
        self.assertGreater(r.evidence_index.max(),r.evidence_index.min())

    def test_no_future_test_changes_historical_phase(self):
        original=zone('one',79000,3,birth=S+DAY)
        past=test_records(('one_0','TEST_FROM_ABOVE',S+10*DAY))
        future=test_records(('one_0','TEST_FROM_BELOW',C+DAY))
        a=rank_zone_evidence(pd.DataFrame([original]),past,C,S)
        b=rank_zone_evidence(pd.DataFrame([original]),pd.concat([past,future]),C,S)
        self.assertEqual(a[['primary_phase','tests_in_window','evidence_index']].to_dict('records'),
                         b[['primary_phase','tests_in_window','evidence_index']].to_dict('records'))
        self.assertEqual(a.iloc[0].last_test_closed_ts,S+10*DAY)

    def test_duplicate_level_events_not_multiplied(self):
        z=pd.DataFrame([zone('two',80000,5,members=2)])
        ts=S+DAY*3
        e=test_records(('two_0','TEST_FROM_ABOVE',ts),
                       ('two_1','TEST_FROM_ABOVE',ts),
                       ('two_0','TEST_FROM_ABOVE',ts+TF),
                       ('two_1','TEST_FROM_BELOW',ts+6*TF))
        r=rank_zone_evidence(z,e,C,S)
        self.assertEqual(r.iloc[0].tests_in_window,2)

    def test_price_spreading_and_three_time_windows(self):
        z=pd.DataFrame([zone(f'a{i}',75000+i*50,5) for i in range(8)] +
                       [zone('b',80000,4),zone('c',85000,3)])
        r=rank_zone_evidence(z,test_records(),C,S)
        q=select_historical_map(r,10,0.7)
        self.assertEqual(len(q),3)
        self.assertEqual(len(set(q.zone_id)),3)
        self.assertIn('b',q.zone_id.tolist())
        self.assertIn('c',q.zone_id.tolist())

    def test_map_never_fills_with_close_duplicates(self):
        z=pd.DataFrame([zone(str(i),75000+i*2,10+i) for i in range(10)])
        r=rank_zone_evidence(z,test_records(),C,S)
        self.assertEqual(len(select_historical_map(r,10,0.7)),1)

    def test_reject_invalid_input(self):
        z=pd.DataFrame([zone('a',80000,2,birth=C+TF)])
        with self.assertRaises(ValueError):rank_zone_evidence(z,test_records(),C,S)
        z=pd.DataFrame([zone('a',80000,2),zone('a',81000,2)])
        with self.assertRaises(ValueError):rank_zone_evidence(z,test_records(),C,S)
        with self.assertRaises(ValueError):select_historical_map(z,limit=0)

    def test_selected_structural_chart_custom_evidence_label(self):
        bars=pd.DataFrame([dict(timestamp=C-i*TF,open=100,high=102,low=99,close=101)
                           for i in range(100,0,-1)])
        leader=type('Leader',(),dict(level_id='a',formation_closed_ts=C-100*TF,
                 last_observed_closed_ts=C,zone_low=99,zone_high=102,
                 center=100,origin='HIGH',tier='A',importance_score=100))()
        z=dict(id='z',leader=leader,anchor=100,kind='HIGH',tier='A',score=100,
               count=2,min_center=99,max_center=101,low=99,high=102,
               created_at=C-90*TF,structural=True,score_label='E80.2 · P96')
        fig,_=make_zone_chart(bars,[z],C,3,'30m',selected_zone_id='z')
        names=[str(t.name) for t in fig.data]
        self.assertTrue(any('E80.2' in name for name in names))
        self.assertFalse(any('100/100' in name for name in names))

if __name__=='__main__':unittest.main()
