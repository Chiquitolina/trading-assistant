#!/usr/bin/env python3
"""Unit and invariant tests for V1.2.2 retrospective dependency index."""
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

import btc_multiswing_structural_behavior_v122 as m


class DependencyGroupingTests(unittest.TestCase):
    def setUp(self):
        rows = [
            (1,'M15_CLOSED','BREAK_ABOVE_2C','UP','a','r1'),
            (1,'M15_CLOSED','BREAK_ABOVE_2C','UP','b','r2'),
            (1,'M15_CLOSED','BREAK_ABOVE_2C','UP','c','r2'), # same region, not an extra band
            (2,'M15_CLOSED','ACCEPT_INSIDE_3C',None,'d','r1'),
            (2,'M15_CLOSED','ACCEPT_INSIDE_3C',None,'e','r2'),
            (2,'M15_CLOSED','ACCEPT_INSIDE_3C',None,'f','r3'),
            (2,'M15_CLOSED','ACCEPT_INSIDE_3C','UP','g','r4'), # DIFFERENT group
            (3,'H1_CLOSED','DEFEND_ORIGIN_2C','DOWN','h','r1'), # solo
        ]
        self.df = pd.DataFrame(rows, columns=m.GROUP_COLS + ['physical_transition_id','region_id'])
        base = self.df.iloc[:3].copy()
        base['concurrent_bands'] = 2
        base['audit_role'] = m.ROLE
        self.old = base[m.INDEX_COLS]

    def test_preserve_null_direction_as_separate_group(self):
        full, delta, counts = m.rebuild_clock_dependency(self.df, self.old)
        self.assertEqual(len(full), 6)
        self.assertEqual(len(delta), 3)
        self.assertTrue(delta.direction.isna().all())
        self.assertEqual(counts['new_dependency_groups'], 2)
        self.assertEqual(counts['recovered_null_direction_groups'], 1)
        self.assertEqual(counts['max_concurrent_bands'], 3)
        self.assertNotIn('g', full.physical_transition_id.values)
        self.assertNotIn('h', full.physical_transition_id.values)

    def test_all_prior_rows_unchanged(self):
        full, _, _ = m.rebuild_clock_dependency(self.df, self.old)
        for i in ('a','b','c'):
            self.assertEqual(full.set_index('physical_transition_id').loc[i,'concurrent_bands'], 2)

    def test_reject_parent_index_drift(self):
        old = self.old.copy()
        old.loc[old.physical_transition_id.eq('a'),'concurrent_bands'] = 3
        with self.assertRaisesRegex(ValueError, 'OLD_ROW_DRIFT'):
            m.rebuild_clock_dependency(self.df, old)

    def test_reject_old_row_not_in_rebuild(self):
        old = self.old.copy()
        old.loc[old.physical_transition_id.eq('a'),'physical_transition_id'] = 'absent'
        with self.assertRaisesRegex(ValueError, 'NOT_SUBSET'):
            m.rebuild_clock_dependency(self.df, old)

    def test_reject_duplicated_physical_id(self):
        df = pd.concat([self.df, self.df.iloc[[0]]], ignore_index=True)
        with self.assertRaisesRegex(ValueError, 'NOT_UNIQUE'):
            m.rebuild_clock_dependency(df, self.old)

    def test_reject_duplicated_old_id(self):
        old = pd.concat([self.old, self.old.iloc[[0]]], ignore_index=True)
        with self.assertRaisesRegex(ValueError, 'NOT_UNIQUE'):
            m.rebuild_clock_dependency(self.df, old)

    def test_reject_extraneous_non_null_omission(self):
        old = self.old.iloc[:2].copy()  # event c has direction UP; cannot silently omit
        with self.assertRaisesRegex(ValueError, 'NON_NULL_DIRECTION_OMISSION'):
            m.rebuild_clock_dependency(self.df, old)

    def test_reject_no_missing_events(self):
        base = self.df.copy()
        base['concurrent_bands'] = base.groupby(m.GROUP_COLS,dropna=False)['region_id'].transform('nunique')
        base['audit_role'] = m.ROLE
        old = base.loc[base.concurrent_bands.gt(1),m.INDEX_COLS]
        with self.assertRaisesRegex(ValueError, 'NO_OMITTED_EVENTS'):
            m.rebuild_clock_dependency(self.df, old)

    def test_reject_missing_required_fields(self):
        with self.assertRaisesRegex(ValueError, 'REQUIRED_COLUMNS'):
            m.rebuild_clock_dependency(self.df.drop(columns=['confirmation_ts']), self.old)

    def test_output_immutable(self):
        full, delta, counts = m.rebuild_clock_dependency(self.df, self.old)
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            v12, v121, out = root/'v12', root/'v121', root/'out'
            v12.mkdir(); v121.mkdir(); out.mkdir()
            (v12/'manifest.json').write_text('{}')
            (v121/'manifest.json').write_text('{}')
            with self.assertRaisesRegex(FileExistsError, 'IMMUTABLE_OUTPUT'):
                m.write_immutable(out,full,delta,counts,v12=v12,v121=v121,m12={},m121={})

    def test_write_manifest_and_parent_hashes(self):
        full, delta, counts = m.rebuild_clock_dependency(self.df, self.old)
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); v12=root/'v12'; v121=root/'v121'; out=root/'out'
            v12.mkdir(); v121.mkdir()
            (v12/'manifest.json').write_text('{"x":1}')
            (v121/'manifest.json').write_text('{"x":2}')
            m.write_immutable(out,full,delta,counts,v12=v12,v121=v121,
                m12={'source_provenance':{'start':1}},m121={})
            self.assertTrue((out/'manifest.json').exists())
            result=json.loads((out/'manifest.json').read_text())
            self.assertEqual(result['parent_v12_manifest_sha256'],m.sha256(v12/'manifest.json'))
            self.assertEqual(result['parent_v121_manifest_sha256'],m.sha256(v121/'manifest.json'))
            self.assertEqual(result['files_sha256']['clock_dependency_v122.csv'],m.sha256(out/'clock_dependency_v122.csv'))
            self.assertEqual(len(pd.read_csv(out/'clock_dependency_recovered_v122.csv')),3)
            with self.assertRaises(FileExistsError):
                m.write_immutable(out,full,delta,counts,v12=v12,v121=v121,m12={},m121={})

    def test_reject_output_nested_in_immutable_parent(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaisesRegex(ValueError, 'OUTPUT_MUST_BE_OUTSIDE'):
                m.main(['--run', '--project-root', t,
                        '--output-dir', str(Path(t) / m.DEFAULT_V121 / 'corrupted')])

    def test_plan_does_not_write_files(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(m.main(['--plan','--project-root',t]), 0)
            self.assertEqual(list(Path(t).iterdir()),[])


if __name__=='__main__':
    unittest.main()
