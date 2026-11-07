"""Regression tests for complete TP1..6 x SL1..5 V3 matrix."""
from __future__ import annotations

import csv
import json
from pathlib import Path
import tempfile
import unittest

import reaction_lab_failed_reversal_retest_v3 as v3
import reaction_lab_failed_reversal_retest_matrix_v31 as m
from dashboard.views.reaction_lab_failed_reversal_retest_matrix_v31_view import scenario_grid
from test_reaction_lab_failed_reversal_retest_v3 import base, long_bars, short_bars


class MatrixTests(unittest.TestCase):
    def test_all_cells_are_30_per_mode_and_sweep(self):
        self.assertEqual(m.TARGETS,(1,2,3,4,5,6))
        self.assertEqual(m.STOPS,(1,2,3,4,5))
        self.assertEqual(len(m.TARGETS)*len(m.STOPS)*len(v3.DEFAULT_DEPTHS)*len(v3.MODES),420)

    def test_new_targets_are_not_interpolated(self):
        bars=long_bars()
        for target in m.TARGETS:
            outcome=v3.evaluate(base(),bars,'IMMEDIATE_ORIGINAL',1,target,sl=3)
            self.assertEqual(outcome['target_pct'],target)
            self.assertIn(outcome['signal_state'],['SIGNAL','NO_SIGNAL'])
        with self.assertRaisesRegex(ValueError,'1 to 6'):
            v3.parse_targets([7])
        self.assertEqual(v3.parse_targets('6,2,1,4,5,3'),m.TARGETS)

    def test_SL_2_supported_and_has_real_exit_difference(self):
        seq=long_bars()
        for target in m.TARGETS:
            x=v3.evaluate(base(),seq,'IMMEDIATE_ORIGINAL',1,target,sl=2)
            self.assertAlmostEqual(x['stop_price'],x['entry_price']*.98)
            self.assertEqual(x['target_pct'],target)

    def test_old_reports_are_not_overwritten(self):
        root=Path('/tmp/failed_v31_test')
        for stop in m.STOPS:
            self.assertNotEqual(v3.config_dir(root,sl_pct=stop),m.stop_dir(root,stop))
            self.assertTrue(str(m.stop_dir(root,stop)).endswith(f'matrix_tp1to6_sl1to5/sl_{stop}pct'))

    def test_case_matrix_pivot(self):
        import pandas as pd
        rows=[]
        for tp in m.TARGETS:
            for sl in m.STOPS:
                rows.append(dict(group_type='ALL',mode='REVERSAL_OPEN',depth_pct=1,
                                 target_pct=tp,sl_pct=sl,net_worst_mean_per_signal_pct=tp-sl))
        grid=scenario_grid(pd.DataFrame(rows),'REVERSAL_OPEN',1,'net_worst_mean_per_signal_pct')
        self.assertEqual(grid.shape,(6,5))
        self.assertEqual(grid.loc[6,1],5)
        with self.assertRaisesRegex(ValueError,'Incomplete'):
            scenario_grid(pd.DataFrame(rows[:-1]),'REVERSAL_OPEN',1,'net_worst_mean_per_signal_pct')

    def test_manifest_integrity_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            manifest={'schema':v3.SCHEMA,'targets_pct':list(m.TARGETS),'modes':list(v3.MODES),
                      'settings':{'depths_pct':list(v3.DEFAULT_DEPTHS),'stop_mode':'pct',
                                  'sl_pct':2,'max_wait_min':60,'hold_min':180}}
            (folder/'manifest.json').write_text(json.dumps(manifest))
            (folder/'event_outcomes.csv').write_text('touch_key\n')
            (folder/'events.csv').write_text('touch_key\n')
            rows=[dict(group_type='ALL',mode=mode,depth_pct=depth,target_pct=tp)
                    for mode in v3.MODES for depth in v3.DEFAULT_DEPTHS for tp in m.TARGETS]
            with (folder/'summary.csv').open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
            result,_=m.verify_sl_report(folder,2)
            self.assertEqual(result['targets_pct'],list(m.TARGETS))
            manifest['targets_pct']=[2,3,5]
            (folder/'manifest.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError,'Incorrect'):
                m.verify_sl_report(folder,2)

    def test_matrix_manifest_path(self):
        self.assertIn('matrix_tp1to6_sl1to5', str(m.matrix_dir('/test/root')))


if __name__=='__main__':unittest.main()
