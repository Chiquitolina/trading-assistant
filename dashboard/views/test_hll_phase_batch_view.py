import io
import math
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dashboard.views.hll_phase_batch_view import (
    TANDAS, _at_scale, valid_legs, valid_pivots, validate_closed_bars,
    validate_sources, validate_family_export, family_brief, build_phase_chart, TF_MS,
    display_chart_window, _origin_vs_resistance, PRE_VISUAL_OPTIONS, POST_VISUAL_OPTIONS,
    DEFAULT_PRE_VISUAL_BARS, DEFAULT_POST_VISUAL_BARS, family_evidence_text,
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

    def test_family_evidence_empty_csv_fields_never_break_rendering(self):
        """Pandas converts empty exported strings to float NaN: reproduces UI issue."""
        csv_data = ("attempt_id,supporting_observations,contradicting_observations,limitations,family_basis\n"
                    "a,swing ascendente,,,recuperación previa\n"
                    "b,,, ,\n")
        loaded = pd.read_csv(io.StringIO(csv_data))
        good = loaded.iloc[0]
        self.assertEqual(family_evidence_text(good.supporting_observations), "swing ascendente")
        self.assertEqual(family_evidence_text(good.contradicting_observations),
                         "sin observaciones específicas")
        self.assertEqual(family_evidence_text(good.limitations, fallback="sin limitaciones adicionales"),
                         "sin limitaciones adicionales")
        for row in loaded.itertuples():
            for field in ("supporting_observations", "contradicting_observations", "limitations", "family_basis"):
                label = "**A favor:** " + family_evidence_text(getattr(row, field))
                self.assertIsInstance(label, str)
        for empty in (None, float("nan"), pd.NA, "", "   ", "nan", "None"):
            self.assertEqual(family_evidence_text(empty), "sin observaciones específicas")
        self.assertEqual(family_evidence_text(1.25), "1.25")

    def test_visual_defaults_match_requested_context_without_changing_core(self):
        self.assertEqual(DEFAULT_PRE_VISUAL_BARS, 288)
        self.assertEqual(DEFAULT_POST_VISUAL_BARS, 96)
        self.assertIn(DEFAULT_PRE_VISUAL_BARS, PRE_VISUAL_OPTIONS)
        self.assertIn(DEFAULT_POST_VISUAL_BARS, POST_VISUAL_OPTIONS)
        self.assertEqual(len(validate_closed_bars(bars(n=450), PRE)), 96)

    def test_visual_selector_options(self):
        self.assertEqual(PRE_VISUAL_OPTIONS, (48, 96, 144, 192, 288, 384))
        self.assertEqual(POST_VISUAL_OPTIONS, (12, 24, 48, 72, 96))
        self.assertEqual(96 * TF_MS, 48 * 60 * 60 * 1000)

    def test_visual_pre_48_and_384_use_same_cutoff(self):
        source = bars(n=450, future=110)
        short, ms = display_chart_window(source, PRE, pre_bars=48)
        long, ml = display_chart_window(source, PRE, pre_bars=384)
        self.assertEqual(len(short),48)
        self.assertEqual(len(long),384)
        self.assertEqual(ms['pre_shown'],48)
        self.assertEqual(ml['pre_shown'],384)
        self.assertEqual(int(short.timestamp.iloc[-1])+TF_MS,PRE)
        self.assertEqual(int(long.timestamp.iloc[-1])+TF_MS,PRE)

    def test_visual_pre_larger_than_history_is_explicit_partial(self):
        source = bars(n=120)
        shown, info = display_chart_window(source, PRE, pre_bars=384)
        self.assertEqual(len(shown),120)
        self.assertEqual(info['pre_requested'],384)
        self.assertEqual(info['pre_shown'],120)

    def test_post_48_hours_exact_and_core_unchanged(self):
        source = bars(n=400, future=120)
        baseline, b = display_chart_window(source, PRE, pre_bars=96)
        with_post, p = display_chart_window(source, PRE, pre_bars=96, show_post=True, post_bars=96)
        self.assertEqual(b['post_shown'],0)
        self.assertEqual(p['post_shown'],96)
        self.assertEqual(p['post_requested'],96)
        pd.testing.assert_frame_equal(baseline.reset_index(drop=True),with_post.iloc[:96].reset_index(drop=True))
        self.assertEqual(int(with_post.timestamp.iloc[96]),PRE)
        self.assertEqual(int(with_post.timestamp.iloc[-1]),PRE+95*TF_MS)

    def test_hidden_post_is_future_invariant_all_display_windows(self):
        base=bars(n=450)
        extended=bars(n=450, future=110)
        for lookback in PRE_VISUAL_OPTIONS:
            d1,_=display_chart_window(base,PRE,pre_bars=lookback)
            d2,_=display_chart_window(extended,PRE,pre_bars=lookback)
            pd.testing.assert_frame_equal(d1,d2)

    def test_post_gap_refused_and_missing_post_reported(self):
        source=bars(n=110,future=10)
        partial,info=display_chart_window(source,PRE,show_post=True,post_bars=96)
        self.assertEqual(info['post_shown'],10)
        self.assertEqual(info['post_requested'],96)
        bad=source.drop(index=111)
        with self.assertRaisesRegex(ValueError,'Gap en velas POST'):
            display_chart_window(bad,PRE,show_post=True,post_bars=96)

    def test_extra_pre_gap_refused(self):
        a=bars(n=450).drop(index=100)
        with self.assertRaisesRegex(ValueError,'Gap'):
            display_chart_window(a,PRE,pre_bars=384)

    def test_invalid_visual_windows_refused(self):
        with self.assertRaisesRegex(ValueError,'PRE'):
            display_chart_window(bars(n=200),PRE,pre_bars=1000)
        with self.assertRaisesRegex(ValueError,'POST'):
            display_chart_window(bars(n=200),PRE,post_bars=1000)

    def test_origin_zone_classification(self):
        self.assertEqual(_origin_vs_resistance(.8, .9, 1.1),'debajo de la resistencia')
        self.assertEqual(_origin_vs_resistance(1.0, .9, 1.1),'dentro de la zona de resistencia')
        self.assertEqual(_origin_vs_resistance(1.2, .9, 1.1),'encima de la resistencia')

    def test_visual_chart_keeps_frozen_pre_overlay_with_post(self):
        source=bars(n=450, future=100)
        baseline=build_phase_chart(source,PRE,.9,1.1,pd.DataFrame(),pd.DataFrame(),pre_bars=384)
        revealed=build_phase_chart(source,PRE,.9,1.1,pd.DataFrame(),pd.DataFrame(),
                                   pre_bars=384,show_post=True,post_bars=96)
        self.assertEqual(baseline.layout.meta['pre_shown'],384)
        self.assertEqual(revealed.layout.meta['post_shown'],96)
        self.assertEqual(len(baseline.data[0].x),384)
        self.assertEqual(len(revealed.data[0].x),480)
        self.assertEqual(len(baseline.layout.shapes),len(revealed.layout.shapes))
        self.assertGreater(len(baseline.layout.shapes),3)

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

    def test_family_export_valid_join(self):
        s=pd.DataFrame([{'attempt_id':'a','symbol':'ABCUSDT','pre_closed_ts':PRE,
            'scale_atr':1.0,'status':'COMPLETE_PRE'}])
        f=pd.DataFrame([{'attempt_id':'a','symbol':'ABCUSDT','pre_closed_ts':PRE,
            'scale_atr':1.0,'status':'DESCRIPTIVE_PRE_ONLY',
            'family_candidate':'ASCENSO_ESCALONADO','family_basis':'test',
            'supporting_observations':'x','contradicting_observations':'',
            'limitations':''}])
        validate_family_export(f,s)
        self.assertEqual(family_brief(f.iloc[0]),'Ascenso escalonado')

    def test_family_export_rejects_wrong_pre(self):
        s=pd.DataFrame([{'attempt_id':'a','symbol':'ABCUSDT','pre_closed_ts':PRE,
            'scale_atr':1.0,'status':'COMPLETE_PRE'}])
        f=pd.DataFrame([{'attempt_id':'a','symbol':'ABCUSDT','pre_closed_ts':PRE+TF_MS,
            'scale_atr':1.0,'status':'DESCRIPTIVE_PRE_ONLY',
            'family_candidate':'ASCENSO_ESCALONADO','family_basis':'test',
            'supporting_observations':'x','contradicting_observations':'',
            'limitations':''}])
        with self.assertRaisesRegex(ValueError,'cortes PRE'):
            validate_family_export(f,s)

    def test_family_export_rejects_duplicate_and_wrong_status(self):
        s=pd.DataFrame([{'attempt_id':'a','symbol':'ABCUSDT','pre_closed_ts':PRE,
            'scale_atr':1.0,'status':'COMPLETE_PRE'}])
        f=pd.DataFrame([{'attempt_id':'a','symbol':'ABCUSDT','pre_closed_ts':PRE,
            'scale_atr':1.0,'status':'DESCRIPTIVE_PRE_ONLY',
            'family_candidate':'ASCENSO_ESCALONADO','family_basis':'test',
            'supporting_observations':'x','contradicting_observations':'',
            'limitations':''}])
        with self.assertRaisesRegex(ValueError,'duplicados'):
            validate_family_export(pd.concat([f,f]),s)
        f.loc[0,'status']='TRADE_SIGNAL'
        with self.assertRaisesRegex(ValueError,'PRE'):
            validate_family_export(f,s)

    def test_family_manifest_guard(self):
        s=pd.DataFrame([{'attempt_id':'a','symbol':'ABCUSDT','pre_closed_ts':PRE,
            'scale_atr':1.0,'status':'COMPLETE_PRE'}])
        f=pd.DataFrame([{'attempt_id':'a','symbol':'ABCUSDT','pre_closed_ts':PRE,
            'scale_atr':1.0,'status':'DESCRIPTIVE_PRE_ONLY',
            'family_candidate':'ASCENSO_ESCALONADO','family_basis':'test',
            'supporting_observations':'x','contradicting_observations':'',
            'limitations':''}])
        with self.assertRaisesRegex(ValueError,'restricciones'):
            validate_family_export(f,s,manifest={'guardrails':{'reads_post':True}})

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

class AuditMatrixValidationTests(unittest.TestCase):
    def test_audit_missing_rows_rejected_before_file_access(self):
        from dashboard.views.hll_phase_batch_view import validate_audit_export
        frame=pd.DataFrame([{'attempt_id':'one'}])
        with self.assertRaisesRegex(ValueError,'incompleta'):
            validate_audit_export(frame,pd.DataFrame(),root=Path('.'),manifest={})

    def test_audit_manifest_guard_before_file_access(self):
        from dashboard.views.hll_phase_batch_view import validate_audit_export
        needed=['attempt_id','symbol','level_id','pre_closed_ts','family_1p0','family_0p5',
                'family_1p5','review_visual','review_provenance','intercontact_direction_48',
                'scales_agree','audit_flags','audit_priority']
        frame=pd.DataFrame([{x:'test' for x in needed} for _ in range(50)])
        frame['attempt_id']=[str(i) for i in range(50)]
        with self.assertRaisesRegex(ValueError,'aislamiento PRE'):
            validate_audit_export(frame,pd.DataFrame(),root=Path('.'),
                manifest={'inputs_are_pre_only':True,'reads_post':True,
                          'reads_visual_for_family':False,'modifies_live':False})

    def test_real_audit_export_validates_if_local_available(self):
        from dashboard.views.hll_phase_batch_view import validate_audit_export
        root=Path(__file__).resolve().parents[2]
        f=root/'reports/horizontal_level_second_ascent_pre/core15_pre_family_interpreter_v1/families_pre.csv'
        m=root/'reports/horizontal_level_second_ascent_pre/core15_pre_family_audit_v1/audit_matrix_pre.csv'
        j=m.with_name('manifest.json')
        if not (f.is_file() and m.is_file() and j.is_file()):self.skipTest('Requiere reportes auditoría en VPS')
        validate_audit_export(pd.read_csv(m),pd.read_csv(f),root=root,
                              manifest=__import__('json').loads(j.read_text()))
