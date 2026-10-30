import io
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dashboard.views.hll_phase_batch_view import (
    TANDAS, _at_scale, valid_legs, valid_pivots, validate_closed_bars,
    validate_sources, build_phase_chart, TF_MS,
)

PRE = 1787400000000


def bars(pre=PRE, n=100, future=0):
    starts = [pre-(n-i)*TF_MS for i in range(n+future)]
    return pd.DataFrame({'timestamp': starts,
        'open': [1.0]*len(starts), 'high':[1.2]*len(starts),
        'low':[0.8]*len(starts), 'close':[1.1]*len(starts)})

class PhaseBatchViewTests(unittest.TestCase):
    def test_curated_groups_and_no_duplicate_attempt(self):
        self.assertEqual([len(v) for v in TANDAS.values()], [7,4,6])
        ids=[a for v in TANDAS.values() for _,a,_,_ in v]
        self.assertEqual(len(ids),len(set(ids)))
        self.assertEqual(len(ids),17)

    def test_window_exact_and_future_invariant(self):
        baseline=validate_closed_bars(bars(),PRE)
        extended=validate_closed_bars(bars(future=20),PRE)
        self.assertEqual(len(baseline),96)
        pd.testing.assert_frame_equal(baseline,extended)
        self.assertTrue((baseline.timestamp+TF_MS<=PRE).all())

    def test_missing_gap_refused(self):
        a=bars().drop(index=12)
        with self.assertRaises(ValueError):validate_closed_bars(a,PRE)

    def test_future_forming_candle_excluded(self):
        a=bars(future=1)
        result=validate_closed_bars(a,PRE)
        self.assertEqual(int(result.timestamp.iloc[-1])+TF_MS,PRE)

    def test_duplicate_timestamp_refused(self):
        a=bars()
        a.loc[a.index[-1],'timestamp']=a.loc[a.index[-2],'timestamp']
        with self.assertRaises(ValueError):validate_closed_bars(a,PRE)

    def test_summary_join_missing_attempt_refused(self):
        s=pd.DataFrame([{'attempt_id':'a','symbol':'ABCUSDT','pre_closed_ts':PRE,
            'scale_atr':1.0,'status':'COMPLETE_PRE'}])
        p=pd.DataFrame([{'attempt_id':'x','symbol':'ABCUSDT','pre_closed_ts':PRE,
            'zone_low':1.0,'zone_high':1.1}])
        with self.assertRaises(ValueError):validate_sources(s,p)

    def test_summary_join_mismatched_cutoff_refused(self):
        s=pd.DataFrame([{'attempt_id':'a','symbol':'ABCUSDT','pre_closed_ts':PRE,
            'scale_atr':1.0,'status':'COMPLETE_PRE'}])
        p=pd.DataFrame([{'attempt_id':'a','symbol':'ABCUSDT','pre_closed_ts':PRE-TF_MS,
            'zone_low':1.0,'zone_high':1.1}])
        with self.assertRaises(ValueError):validate_sources(s,p)

    def test_future_pivot_guard(self):
        p=pd.DataFrame([{'attempt_id':'a','scale_atr':1.0,'pivot_type':'LOW',
            'pivot_open_ts':PRE-2*TF_MS,'pivot_price_close':1.0,
            'confirmed_known_closed_ts':PRE+TF_MS,'status':'CONFIRMED_PRE'},
            {'attempt_id':'a','scale_atr':1.0,'pivot_type':'LOW',
            'pivot_open_ts':PRE-4*TF_MS,'pivot_price_close':1.0,
            'confirmed_known_closed_ts':PRE-TF_MS,'status':'CONFIRMED_PRE'}])
        self.assertEqual(len(valid_pivots(p,'a',1.0,PRE)),1)

    def test_unconfirmed_leg_guard(self):
        l=pd.DataFrame([{'attempt_id':'a','scale_atr':1.0,'leg_index':1,
            'start_pivot_open_ts':PRE-8*TF_MS,'start_pivot_known_closed_ts':PRE-6*TF_MS,
            'end_extreme_open_ts':PRE-3*TF_MS,'end_extreme_confirmed_known_closed_ts':PRE+TF_MS,
            'status':'CONFIRMED_PRE'},
            {'attempt_id':'a','scale_atr':1.0,'leg_index':2,
            'start_pivot_open_ts':PRE-5*TF_MS,'start_pivot_known_closed_ts':PRE-4*TF_MS,
            'end_extreme_open_ts':PRE-TF_MS,'end_extreme_confirmed_known_closed_ts':float('nan'),
            'status':'PROVISIONAL'}])
        q=valid_legs(l,'a',1.0,PRE)
        self.assertEqual(len(q),1)
        self.assertEqual(q.iloc[0].status,'PROVISIONAL')

    def test_chart_pre_unchanged_after_future_extension(self):
        for src in (bars(),bars(future=10)):
            fig=build_phase_chart(src,PRE,0.90,1.10,pd.DataFrame(),pd.DataFrame())
            trace=fig.data[0]
            self.assertEqual(len(trace.x),96)
            self.assertEqual(float(trace.close[-1]),1.1)

    def test_existing_unified_inspector_wires_batch_once(self):
        target = Path(__file__).with_name('hll_v28_structure_dashboard_view.py')
        source = target.read_text(encoding='utf-8')
        self.assertEqual(source.count('render_hll_phase_batch(root)'), 2)
        self.assertEqual(source.count('from dashboard.views.hll_phase_batch_view import render_hll_phase_batch'), 1)
        self.assertLess(source.index('render_hll_phase_batch(root)'),
                        source.index("if mode=='290 símbolos · análisis integral':"))
        self.assertIn('if not integral.is_file():\n        render_hll_phase_batch(root)', source)
        self.assertIn('render_hll_v282_pre_followup(root,blind=blind)', source)

    def test_all_visual_attempt_ids_in_real_export(self):
        # Optional local test only when the provided real historical ZIP is available.
        import zipfile
        import io
        root = Path(__file__).resolve().parents[2]
        path = root / 'reports/horizontal_level_second_ascent_pre/core15_phase_reconstruction_pre_v1/phase_summary_pre.csv'
        if path.is_file():
            df = pd.read_csv(path)
        else:
            zpath = Path('/mnt/data/hll_phase_pre_v1_results.zip')
            if not zpath.is_file():
                self.skipTest('CSV real de Phase PRE no disponible')
            with zipfile.ZipFile(zpath) as z:
                src = next(x for x in z.namelist() if x.endswith('/phase_summary_pre.csv'))
                df = pd.read_csv(io.BytesIO(z.read(src)))
        ids = {a for batch in TANDAS.values() for _, a, _, _ in batch}
        self.assertTrue(ids <= set(df.attempt_id.astype(str)))
        self.assertEqual(df.attempt_id.nunique(), 50)

if __name__ == '__main__':unittest.main()
