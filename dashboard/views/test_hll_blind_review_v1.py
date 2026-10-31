"""Regression tests for persistent, PRE-only and provenance-aware blind reviews."""
from __future__ import annotations

import json
import sys
import unittest
from unittest.mock import patch
from types import ModuleType
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from dashboard.views.hll_blind_review_v1 import (
    REVIEW_REL, _cohort_rows, cohort_sha256, make_review, save_event,
    load_events, latest_reviews, review_frame,
)
from dashboard.views.hll_phase_batch_view import (
    TANDAS, build_phase_chart, validate_closed_bars, TF_MS,
)


PRE=1787400000000
ROOT=Path(__file__).resolve().parents[2]


def synthetic_cohort():
    summary=[];levels=[]
    for i in range(50):
        aid=f"{i+1:024x}"
        symbol=f"TEST{i}USDT"
        cutoff=PRE+i*TF_MS
        levels.append({"attempt_id":aid,"symbol":symbol,"pre_closed_ts":cutoff,
                       "zone_low":1.0+i/100,"zone_high":1.1+i/100})
        for atr in (0.5,1.0,1.5):
            summary.append({"attempt_id":aid,"symbol":symbol,"pre_closed_ts":cutoff,
                            "scale_atr":atr,"status":"COMPLETE_PRE"})
    return pd.DataFrame(summary),pd.DataFrame(levels)


def review(rows, aid=None, **overrides):
    params={"attempt_id":aid or rows[0]["attempt_id"],
        "quality":"DUDOSA","morphology":"INDETERMINADA",
        "origin":"MIXTO","confidence":"BAJA","previous_exposure":"NO_SE",
        "pre_bars_shown":288,"note":"Controles visuales PRE sin POST"}
    params.update(overrides)
    return make_review(rows,**params)


class BlindReviewTests(unittest.TestCase):
    def setUp(self):
        s,l=synthetic_cohort()
        self.summary=s
        self.levels=l
        self.rows=_cohort_rows(s,l)

    def test_complete_cohort_fingerprint_and_stable_obfuscated_order(self):
        self.assertEqual(len(self.rows),50)
        self.assertEqual(len({r['attempt_id'] for r in self.rows}),50)
        self.assertNotEqual([r['attempt_id'] for r in self.rows],sorted(r['attempt_id'] for r in self.rows))
        self.assertEqual(cohort_sha256(self.rows),cohort_sha256(list(reversed(self.rows))))
        again=_cohort_rows(self.summary.sample(frac=1,random_state=5),
                           self.levels.sample(frac=1,random_state=7))
        self.assertEqual(again,self.rows)

    def test_missing_scale_or_changed_cutoff_is_rejected(self):
        broken=self.summary.drop(index=0)
        with self.assertRaises(ValueError):_cohort_rows(broken,self.levels)
        changed=self.levels.copy()
        changed.loc[0,'pre_closed_ts']+=TF_MS
        with self.assertRaisesRegex(ValueError,'Corte'):_cohort_rows(self.summary,changed)

    def test_invalid_attempt_zone_duplicate_and_future_status_are_rejected(self):
        l=self.levels.copy()
        l.loc[0,'zone_low']=l.loc[0,'zone_high']
        with self.assertRaises(ValueError):_cohort_rows(self.summary,l)
        l=self.levels.copy()
        l.loc[0,'attempt_id']=l.loc[1,'attempt_id']
        with self.assertRaises(ValueError):_cohort_rows(self.summary,l)
        s=self.summary.copy()
        s.loc[0,'status']='FUTURE'
        with self.assertRaises(ValueError):_cohort_rows(s,self.levels)

    def test_future_or_algorithm_exposure_rejected_before_save(self):
        for leak in ('post_visible','model_visible','historical_labels_visible'):
            with self.subTest(leak=leak):
                with self.assertRaisesRegex(ValueError,'revisiones ciegas'):
                    review(self.rows,**{leak:True})

    def test_no_mandatory_labels_can_be_skipped(self):
        for field in ('quality','morphology','origin','confidence','previous_exposure'):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):review(self.rows,**{field:''})
        with self.assertRaises(ValueError):review(self.rows,note='x'*1201)
        with self.assertRaises(ValueError):review(self.rows,pre_bars_shown=500)

    def test_save_reload_and_revision_history_append_only(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            original=root/'reports'/'horizontal_level_second_ascent_pre'/'old_audit.csv'
            original.parent.mkdir(parents=True)
            original.write_bytes(b'ORIGINAL AUDIT UNTOUCHED')
            r1=review(self.rows)
            p1=save_event(root,r1,self.rows)
            self.assertEqual(len(load_events(root,self.rows)),1)
            self.assertTrue(str(p1).startswith(str(root/REVIEW_REL)))
            r2=review(self.rows,quality='DETERIORADA',note='corrección importante')
            p2=save_event(root,r2,self.rows)
            self.assertTrue(p1.is_file() and p2.is_file())
            self.assertNotEqual(p1,p2)
            self.assertEqual(len(load_events(root,self.rows)),2)
            latest=latest_reviews(load_events(root,self.rows))
            self.assertEqual(latest[r2['attempt_id']]['quality'],'DETERIORADA')
            self.assertEqual(len(review_frame(load_events(root,self.rows))),1)
            self.assertEqual(len(review_frame(load_events(root,self.rows),latest_only=False)),2)
            self.assertEqual(original.read_bytes(),b'ORIGINAL AUDIT UNTOUCHED')
            with self.assertRaises(FileExistsError):save_event(root,r2,self.rows)

    def test_tampered_record_and_foreign_cohort_fail_closed(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            r=review(self.rows)
            p=save_event(root,r,self.rows)
            changed=json.loads(p.read_text())
            changed['post_visible_at_save']=True
            p.write_text(json.dumps(changed))
            with self.assertRaises(ValueError):load_events(root,self.rows)
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            save_event(root,review(self.rows),self.rows)
            modified=list(dict(x) for x in self.rows)
            modified[0]['zone_low']+=0.0001
            with self.assertRaises(ValueError):load_events(root,modified)

    def test_csv_preserves_manual_provenance_not_pnl_or_model_features(self):
        r=review(self.rows,origin='DESDE_ARRIBA',previous_exposure='SI')
        df=review_frame([r])
        self.assertEqual(len(df),1)
        self.assertEqual(df.iloc[0].origin,'DESDE_ARRIBA')
        self.assertEqual(df.iloc[0].previous_exposure,'SI')
        self.assertFalse(any('family' in col or 'profit' in col or 'pnl' in col for col in df.columns))
        self.assertFalse(df.iloc[0].post_visible_at_save)

    def test_blind_chart_contains_only_pre_and_no_model_overlay(self):
        n=400
        pre=pd.DataFrame({'timestamp':[PRE-(n-i)*TF_MS for i in range(n)],
            'open':[1.0]*n,'high':[1.2]*n,'low':[.8]*n,'close':[1.1]*n})
        future=pd.DataFrame({'timestamp':[PRE+i*TF_MS for i in range(100)],
            'open':[500.0]*100,'high':[600.0]*100,'low':[400.0]*100,'close':[550.0]*100})
        a=build_phase_chart(pre,PRE,.9,1.1,pd.DataFrame(),pd.DataFrame(),
                            show_post=False,pre_bars=288,show_pivots=False,
                            show_legs=False,show_candidates=False)
        b=build_phase_chart(pd.concat([pre,future],ignore_index=True),PRE,.9,1.1,
                            pd.DataFrame(),pd.DataFrame(),show_post=False,pre_bars=288,
                            show_pivots=False,show_legs=False,show_candidates=False)
        self.assertEqual(len(a.data),1)
        self.assertEqual(len(a.data[0].x),288)
        self.assertEqual(list(a.data[0].close),list(b.data[0].close))
        self.assertEqual(a.layout.meta['post_shown'],0)
        self.assertEqual(b.layout.meta['post_shown'],0)
        self.assertEqual(int(pre.timestamp.iloc[-1])+TF_MS,PRE)

    def test_view_exits_before_any_model_rendering_in_blind_mode(self):
        source=(ROOT/'dashboard/views/hll_phase_batch_view.py').read_text(encoding='utf8')
        self.assertIn('render_blind_review(root, data, read_cached)\n            return',source)
        pos=source.index('render_blind_review(root, data, read_cached)\n            return')
        self.assertLess(pos,source.index('family_export = None',pos))
        self.assertIn('if aid in latest:',source)
        self.assertIn('if len(latest)==50:',source)
        self.assertIn('revealed.add(aid)',source)
        self.assertIn('submitted=st.form_submit_button',source)

    def test_render_blind_review_ui_smoke_and_save(self):
        from dashboard.views.hll_phase_batch_view import render_blind_review, CANDLES_REL
        rows=self.rows
        known={a for group in TANDAS.values() for _sym,a,_label,_origin in group}
        case=next(r for r in rows if r['attempt_id'] not in known)
        pre=case['pre_closed_ts']
        n=350
        candles=pd.DataFrame({'timestamp':[pre-(n-i)*TF_MS for i in range(n)],
          'open':[1.0]*n,'high':[1.2]*n,'low':[.8]*n,'close':[1.1]*n})
        class Context:
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def metric(self,*args,**kwargs):pass
        fake=ModuleType('streamlit')
        fake.session_state={}
        fake.markdown=lambda *args,**kwargs:None
        fake.caption=lambda *args,**kwargs:None
        fake.columns=lambda n:[Context() for _ in range(n)]
        fake.radio=lambda label,options,**kwargs:options[0]
        fake.selectbox=lambda label,options,**kwargs:(next((v for v in options if v!=''),options[0]))
        fake.button=lambda *args,**kwargs:False
        fake.form=lambda *args,**kwargs:Context()
        fake.text_area=lambda *args,**kwargs:'Auditoría visual en OHLC'
        fake.checkbox=lambda *args,**kwargs:False
        fake.form_submit_button=lambda *args,**kwargs:True
        fake.error=lambda msg: self.fail(f'Error UI no esperado: {msg}')
        fake.warning=lambda *args,**kwargs:None
        fake.info=lambda *args,**kwargs:None
        fake.success=lambda *args,**kwargs:None
        fake.download_button=lambda *args,**kwargs:None
        seen=[]
        fake.plotly_chart=lambda chart,**kwargs:seen.append(chart)
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            candle_path=root/CANDLES_REL/f"{case['symbol']}_30m.csv"
            candle_path.parent.mkdir(parents=True)
            candles.to_csv(candle_path,index=False)
            cache_reads=[]
            def read_cached(path_s,mtime):
                cache_reads.append(path_s)
                if path_s!=str(candle_path):
                    self.fail('Se leyó una fuente de etiquetas o datos POST en modo ciego')
                return pd.read_csv(path_s)
            with patch.dict(sys.modules,{'streamlit':fake}):
                render_blind_review(root,{'summary':self.summary,'levels':self.levels},read_cached)
            self.assertEqual(len(seen),1)
            self.assertEqual(seen[0].layout.meta['post_shown'],0)
            self.assertEqual(len(seen[0].data),1)
            self.assertEqual(cache_reads,[str(candle_path)])
            result=load_events(root,rows)
            self.assertEqual(len(result),1)
            self.assertEqual(result[0]['quality'],'CONSTRUCTIVA')

    def test_parent_v28_stops_rendering_remaining_inspector_in_blind_mode(self):
        source=(ROOT/'dashboard/views/hll_v28_structure_dashboard_view.py').read_text(encoding='utf8')
        self.assertEqual(source.count("if st.session_state.get('hll_phase_mode') == 'Revisión visual ciega (PRE)':"),2)
        self.assertEqual(source.count('render_hll_phase_batch(root)'),2)
        self.assertLess(source.index("if st.session_state.get('hll_phase_mode')"),
                        source.index("if mode=='290 símbolos · análisis integral':"))

    def test_prior_batched_labels_do_not_feed_review_generation(self):
        # Tanda references exist for later comparison, but are not inputs to make_review.
        known={aid for group in TANDAS.values() for _,aid,_,_ in group}
        self.assertEqual(len(known),17)
        self.assertNotIn('family_candidate',review(self.rows))
        self.assertNotIn('review_visual',review(self.rows))


if __name__=='__main__':
    unittest.main()
