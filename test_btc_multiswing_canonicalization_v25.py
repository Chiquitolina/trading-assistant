#!/usr/bin/env python3
"""Tests for causal Multi-Swing V2.5A, including nontransitive consolidation."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import pandas as pd

from btc_multiswing_canonicalization_v25 import (
    PAIR_COLUMNS, audit_relations, main, _write_immutable,
)

M = 60_000
D = 1440 * M
T = 1_800_000_000_000


def make_data(specs):
    """spec: id, side, birth minute offset, (low,high), member IDs, optional stages.

    A final region can have members confirmed after birth; stage list contains
    snapshots (minute offset, low, high, member_count).
    """
    regions=[]
    stages=[]
    known={}
    for spec in specs:
        rid,side,birth,band,ids,*extra=spec
        confirmed=extra[0] if extra else [birth]*len(ids)
        details=extra[1] if len(extra)>1 else [(birth,band[0],band[1],len(ids))]
        for pid,when in zip(ids,confirmed):
            known[pid]=min(known.get(pid,10**20),T+when*M)
        regions.append({'region_id':rid,'side':side,'born_ts':T+birth*M,
                        'members':','.join(ids)})
        for when,low,high,number in details:
            stages.append({'region_id':rid,'side':side,
                           'known_from':T+when*M,'known_until':T+(when+1440)*M,
                           'band_low':low,'band_high':high,
                           'independent_extrema':number})
    return pd.DataFrame(regions),pd.DataFrame(stages),known


def run(specs,**kwargs):
    regions,stages,known=make_data(specs)
    return audit_relations(regions,stages,known,T+30*D,**kwargs)


class CanonicalRelationsV25(unittest.TestCase):
    def test_b_same_side_distinct_non_destructive(self):
        p,c,s=run([
            ('a','HIGH',0,(100,100.20),['a1','a2']),
            ('b','HIGH',100,(100.05,100.24),['b1','b2']),
        ])
        self.assertEqual(s['A'],0)
        self.assertEqual(s['B'],1)
        self.assertEqual(s['C'],0)
        self.assertEqual(c.canonical_region_id.tolist(),['a','b'])
        self.assertTrue(c.original_region_preserved.all())
        self.assertEqual(p.iloc[0]['common_physical_members'],0)

    def test_a_requires_two_shared_verified_physical_members(self):
        p,c,s=run([
            ('a','HIGH',0,(100,100.2),['x','y']),
            ('b','HIGH',10,(100.01,100.21),['x','y']),
        ])
        self.assertEqual(s['A'],1)
        self.assertEqual(int(c.strict_A_alias.sum()),1)
        self.assertEqual(c.loc[c.region_id.eq('b'),'canonical_region_id'].iloc[0],'a')
        self.assertEqual(p.iloc[0]['common_physical_members'],2)
        self.assertEqual(p.iloc[0]['known_from_ts'],T+10*M)

    def test_one_shared_member_not_duplicate(self):
        p,c,s=run([
            ('a','HIGH',0,(100,100.2),['x','y']),
            ('b','HIGH',10,(100,100.2),['x','z']),
        ])
        self.assertEqual(s['A'],0)
        self.assertEqual(s['B'],1)
        self.assertEqual(p.iloc[0]['common_physical_members'],1)

    def test_opposite_sides_never_merge_even_exact_shared_members(self):
        p,c,s=run([
            ('a','LOW',0,(100,100.2),['x','y']),
            ('b','HIGH',1,(100,100.2),['x','y']),
        ])
        self.assertEqual(s['B_opposite_side'],1)
        self.assertEqual(p.iloc[0]['subtype'],'B_OPPOSITE_SIDE_CONTEXT')
        self.assertEqual(c.canonical_region_id.tolist(),['a','b'])

    def test_c_requires_quiet_interval_at_second_birth(self):
        p,c,s=run([
            ('a','HIGH',0,(100,100.15),['x','y']),
            ('b','HIGH',8*1440,(100.03,100.18),['b1','b2']),
        ])
        self.assertEqual(s['C'],1)
        self.assertEqual(p.iloc[0]['subtype'],'C_POSSIBLE_TEMPORAL_SUCCESSOR')
        self.assertEqual(c.iloc[-1]['possible_predecessors_C'],'a')
        self.assertEqual(c.canonical_region_id.tolist(),['a','b'])

    def test_long_gap_does_not_automatically_assert_succession(self):
        p,c,s=run([
            ('a','HIGH',0,(100,100.15),['x','y']),
            ('b','HIGH',18*1440,(100.03,100.18),['b1','b2']),
        ])
        self.assertEqual(s['C'],0)
        self.assertEqual(s['B'],1)

    def test_recent_confirmed_member_prevents_false_successor(self):
        p,c,s=run([
            ('a','HIGH',0,(100,100.15),['x','y','z'],[0,0,7*1440],
             [(0,100,100.15,2),(7*1440,100,100.18,3)]),
            ('b','HIGH',8*1440,(100.02,100.17),['b1','b2']),
        ])
        self.assertEqual(s['C'],0)
        self.assertEqual(s['B'],1)
        self.assertEqual(p.iloc[0]['old_extrema_asof'],3)
        self.assertEqual(p.iloc[0]['quiet_minutes_at_younger_birth'],1440)

    def test_future_member_has_no_effect_on_pair_geometry_or_identity(self):
        specs=[
            ('a','HIGH',0,(100,100.15),['x','y','late'],[0,0,10*1440],
             [(0,100,100.15,2),(10*1440,100,100.8,3)]),
            ('b','HIGH',1*1440,(100.02,100.18),['b1','b2'])]
        p,_,_=run(specs)
        p2,_,_=run([
            ('a','HIGH',0,(100,100.15),['x','y']),
            ('b','HIGH',1*1440,(100.02,100.18),['b1','b2'])])
        for col in ['relation','subtype','center_distance_pct','iou',
                    'old_extrema_asof','common_physical_members']:
            self.assertEqual(p.iloc[0][col],p2.iloc[0][col],col)

    def test_future_mutation_refused_as_of_pair(self):
        regions,stages,known=make_data([
            ('a','LOW',0,(100,100.15),['a1','a2','a3'],[0,0,12*1440],
             [(0,100,100.15,2),(1*1440,100,100.3,3)]),
            ('b','LOW',2*1440,(100.02,100.12),['b1','b2'])])
        with self.assertRaisesRegex(ValueError,'STAGE_MEMBER_ASOF_CONFLICT'):
            audit_relations(regions,stages,known,T+30*D)

    def test_no_transitive_visual_family_chain(self):
        # a similar to b; b similar to c; a not similar enough to c.
        specs=[
            ('a','HIGH',0,(100.00,100.20),['a1','a2']),
            ('b','HIGH',20,(100.04,100.24),['b1','b2']),
            ('c','HIGH',40,(100.08,100.28),['c1','c2']),
        ]
        p,c,_=run(specs,strict_iou=.6)
        self.assertEqual(c.iloc[1]['visual_family_anchor_REVIEW_ONLY'],'a')
        self.assertTrue(any(x['older_region_id']=='b' and x['younger_region_id']=='c' and
                            x['visual_redundancy_candidate'] for x in p.to_dict('records')))
        # b is already a member of a's star, therefore c cannot join via b.
        self.assertEqual(c.iloc[2]['visual_family_anchor_REVIEW_ONLY'],'c')
        # Crucial: c must NOT silently inherit a's anchor from a match with b.
        self.assertNotEqual(c.iloc[2]['visual_family_anchor_REVIEW_ONLY'],'a')

    def test_concurrent_birth_never_assumes_a_strictly_prior_market_event(self):
        p,_,_=run([
            ('a','HIGH',0,(100,100.2),['a1','a2']),
            ('b','HIGH',0,(100.01,100.21),['b1','b2'])])
        self.assertEqual(p.iloc[0]['older_birth_ts'],p.iloc[0]['younger_birth_ts'])
        self.assertEqual(p.iloc[0]['known_from_ts'],T)

    def test_distant_geometry_is_not_related(self):
        p,c,s=run([
            ('a','HIGH',0,(100,100.15),['a1','a2']),
            ('b','HIGH',10,(102,102.15),['b1','b2'])])
        self.assertEqual(len(p),0)
        self.assertEqual(len(c),2)

    def test_invalid_params_rejected(self):
        r,s,k=make_data([('a','HIGH',0,(100,100.2),['x','y'])])
        with self.assertRaises(ValueError):
            audit_relations(r,s,k,T+30*D,quiet_days=14,successor_max_days=14)
        with self.assertRaises(ValueError):
            audit_relations(r,s,k,T+30*D,near_pct=0)

    def test_missing_physical_member_fails_closed(self):
        r,s,k=make_data([('a','HIGH',0,(100,100.2),['x','y'])])
        k.pop('y')
        with self.assertRaisesRegex(ValueError,'UNKNOWN_PHYSICAL'):
            audit_relations(r,s,k,T+30*D)

    def test_empty_catalog_stays_empty(self):
        r=pd.DataFrame(columns=['region_id','side','born_ts','members'])
        s=pd.DataFrame(columns=['region_id','side','known_from','band_low','band_high','independent_extrema'])
        p,c,summary=audit_relations(r,s,{},T)
        self.assertEqual(len(p),0)
        self.assertEqual(list(p.columns),PAIR_COLUMNS)
        self.assertEqual(len(c),0)
        self.assertEqual(summary['regions'],0)

    def test_plan_reads_no_frozen_files_or_writes(self):
        with tempfile.TemporaryDirectory() as t:
            output=Path(t)/'not_written'
            with redirect_stdout(io.StringIO()) as stdout:
                result=main(['--plan','--project-root',t,'--output-dir',str(output)])
            self.assertEqual(result,0)
            self.assertIn('V25_PLAN',stdout.getvalue())
            self.assertFalse(output.exists())

    def test_immutable_writer_and_hashes(self):
        p,c,s=run([
            ('a','HIGH',0,(100,100.15),['x','y']),
            ('b','HIGH',15,(100.01,100.17),['b1','b2'])])
        with tempfile.TemporaryDirectory() as root:
            output=Path(root)/'v25'
            _write_immutable(output,p,c,s,{'cutoff':T+30*D},
                {'source_sha256':'fake-synthetic'},'synthetic-parent-hash')
            manifest=json.loads((output/'manifest.json').read_text())
            self.assertIn('relations_v25.csv',manifest['sha256'])
            self.assertTrue((output/'canonicalization_v25.csv').is_file())
            with self.assertRaises(FileExistsError):
                _write_immutable(output,p,c,s,{}, {},'again')

if __name__=='__main__':
    unittest.main()
