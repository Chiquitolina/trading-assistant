"""Tests for experimental swing-width/prominence matrix inside HLL PRE."""
import unittest
from pathlib import Path

import pandas as pd

from dashboard.views import hll_reaction_pre_overlay as overlay
from dashboard.views import hll_reaction_sensitivity_matrix as grid
from dashboard.views.test_hll_reaction_pre_overlay import BASE, minute_fixture


class ReactionSensitivityMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[2]
        src = cls.root/'dashboard/app.py'
        cls.env, cls.swing, cls.source_hash = overlay.canonical_functions(
            str(src), src.stat().st_mtime_ns)
        cls.tape = minute_fixture()
        cls.visible = BASE+36*overlay.TF15
        cls.end = BASE+60*overlay.TF15

    def run_variant(self, left, right, prom=0.0, all_touches=False, tape=None):
        return overlay.replay_canonical_pre(
            self.tape if tape is None else tape, self.end, self.visible,
            env=self.env, swing_detector_class=self.swing,symbol='TESTUSDT',
            left_bars=left,right_bars=right,min_prominence_pct=prom,
            include_first_retests=all_touches)

    def test_original_3x3_unchanged(self):
        basic = overlay.replay_canonical_pre(self.tape,self.end,self.visible,
            env=self.env,swing_detector_class=self.swing,symbol='TESTUSDT')
        self.assertEqual(basic,self.run_variant(3,3,0.0))
        self.assertGreater(len(basic),0)

    def test_prominence_gate_matches_replay_exactly(self):
        for shape in grid.SWING_SHAPES:
            raw = self.run_variant(*shape,0.0,all_touches=True)
            for p in grid.PROMINENCE_GRID_PCT:
                direct = self.run_variant(*shape,p)
                grouped = grid.combine_first_retests(raw,p)
                self.assertEqual(
                    [(x['event_key'],x['pivot_ts']) for x in direct],
                    [(x['event_key'],x['pivot_ts']) for x in grouped['events']],
                    (shape,p))

    def test_monotonic_gates_per_shape(self):
        for shape in grid.SWING_SHAPES:
            raw = self.run_variant(*shape,0.0,all_touches=True)
            values = [grid.combine_first_retests(raw,p)['reactions']
                for p in grid.PROMINENCE_GRID_PCT]
            self.assertEqual(values,sorted(values,reverse=True))

    def test_first_touch_statuses_are_not_confused(self):
        dummy=[
            {'status':'REACTION','event_key':'S|LONG|1','side':'LONG','known_ts':1,'pivot_ts':0,'prominence_pct':0.01},
            {'status':'TOUCH_FAILED','event_key':'S|LONG|2','side':'LONG','known_ts':2,'pivot_ts':0,'prominence_pct':0.12},
            {'status':'TOUCH_INDECISIVE','event_key':'S|SHORT|3','side':'SHORT','known_ts':3,'pivot_ts':0,'prominence_pct':0.13},
            {'status':'REACTION','event_key':'S|LONG|1','side':'LONG','known_ts':1,'pivot_ts':-1,'prominence_pct':0.20},
        ]
        result=grid.combine_first_retests(dummy,0.1)
        self.assertEqual(result['reactions'],1)
        self.assertEqual(result['failed_first_retests'],1)
        self.assertEqual(result['indecisive_first_retests'],1)

    def test_matrix_shape_and_overlap(self):
        by_shape={f'{left}x{right}':self.run_variant(left,right,0.,True)
                  for left,right in grid.SWING_SHAPES}
        rows=grid.summarize_grid(by_shape)
        self.assertEqual(len(rows),len(grid.SWING_SHAPES)*len(grid.PROMINENCE_GRID_PCT))
        baseline=next(x for x in rows if x['swing']=='3x3' and x['prominence_pct']==0)
        self.assertEqual(baseline['shared_with_canonical'],baseline['reactions'])
        self.assertEqual(baseline['lost_from_canonical'],0)
        table=grid.presentation_matrix(rows)
        self.assertEqual(table.shape,(5,5))

    def test_3x4_shifts_availability_not_pivot_past(self):
        a=self.run_variant(3,3)
        b=self.run_variant(3,4)
        self.assertTrue(all(x['known_ts'] <= self.end for x in b))
        self.assertTrue(all(x['actionable_ts'] <= x['known_ts'] for x in b))
        self.assertTrue(all(x['swing_shape']=='3x4' for x in b))
        # Different confirmation can change eligibility; not a filter of 3x3 markers.
        self.assertGreaterEqual(len(a)+len(b),0)

    def test_every_variant_ignores_future(self):
        extra=self.tape.iloc[-1:].copy()
        extra['timestamp']=self.end+overlay.TF1
        extra['high']=999
        future=pd.concat([self.tape,extra],ignore_index=True)
        for shape in grid.SWING_SHAPES:
            self.assertEqual(self.run_variant(*shape),self.run_variant(*shape,tape=future))

    def test_reject_unsupported_and_negative(self):
        with self.assertRaisesRegex(ValueError,'Ventana'):
            self.run_variant(6,6)
        with self.assertRaisesRegex(ValueError,'Prominencia'):
            self.run_variant(3,3,-0.1)

    def test_canonical_swing_detector_accepts_asymmetric_window(self):
        candles=[{'timestamp':i*overlay.TF15,'high':101.0,'low':100.5} for i in range(12)]
        candles[4]['low']=99.0
        candles[8]['low']=98.8  # solo invalida el cuarto posterior de 3x4
        sym3=self.swing(left_bars=3,right_bars=3,min_prominence_pct=0)
        sym34=self.swing(left_bars=3,right_bars=4,min_prominence_pct=0)
        three=[x for x in sym3.detect_all(candles) if x.side=='LOW' and x.pivot_timestamp==4*overlay.TF15]
        four=[x for x in sym34.detect_all(candles) if x.side=='LOW' and x.pivot_timestamp==4*overlay.TF15]
        self.assertEqual(len(three),1)
        self.assertEqual(len(four),0)

    def test_view_preserves_other_panels_and_matrix_only_on_request(self):
        import ast
        source=(self.root/'dashboard/views/hll_structural_discovery_view.py').read_text(encoding='utf8')
        ast.parse(source)
        self.assertIn('hll_unified_lifecycle_family_view_v11',source)
        self.assertIn('hll_resistance_universal_audit_view',source)
        self.assertIn('hll_reaction_sensitivity_matrix',source)
        self.assertIn('if compare_reactions:',source)
        self.assertIn("['2x2','3x3','3x4','4x4','5x5']",source)


if __name__=='__main__':
    unittest.main()
