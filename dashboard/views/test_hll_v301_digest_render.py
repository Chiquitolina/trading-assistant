"""Smoke regression: execute the real full290 rendering entry path.

The defect was not in helper functions; it was an UnboundLocalError only when
Streamlit invoked render_full290 and tried to hash the selected PRE source.
"""
from __future__ import annotations

import ast
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from dashboard.views import hll_v301_full_inspector as view


class ReachedFilters(Exception):
    """Controlled stop after the opening dashboard entry inventory succeeds."""


class FullInspectorRenderingRegression(unittest.TestCase):
    def test_render_does_not_shadow_module_level_digest(self):
        source = Path(view.__file__).read_text(encoding='utf-8')
        tree = ast.parse(source)
        render = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'render_full290')
        local_imports = [alias.asname or alias.name for node in ast.walk(render)
                         if isinstance(node, ast.ImportFrom) for alias in node.names]
        self.assertNotIn('digest', local_imports)
        self.assertNotIn('PRE', local_imports)

    def test_render_reaches_filters_with_actual_source_digest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            folder = root / 'reports' / 'test_pre'
            folder.mkdir(parents=True)
            src = folder / 'pre_cohort.csv'
            pre = pd.DataFrame([{
                'attempt_id':'test1', 'symbol':'AAAUSDT',
                'post_outcome':'SIN_FLIP_EN_VENTANA',
                'flip_confirmed_closed_ts':float('nan'),
            }])
            pre.to_csv(src,index=False)
            actual_digest = view.digest(src)
            self.assertEqual(len(actual_digest), 64)

            fake_streamlit = types.ModuleType('streamlit')
            fake_streamlit.markdown = lambda *a, **kw: None
            fake_streamlit.caption = lambda *a, **kw: None
            fake_streamlit.info = lambda *a, **kw: None
            fake_streamlit.error = lambda *a, **kw: self.fail('Unexpected Streamlit error')
            fake_streamlit.cache_data = lambda *a, **kw: (lambda fn: fn)
            fake_streamlit.columns = lambda *a, **kw: (_ for _ in ()).throw(ReachedFilters())
            inventory = pd.DataFrame([{
                'attempt_id':'test1',
                'entry_status':'SIN_SENAL_CONFIRMADA',
                'entry_reason':'Ninguna ruta confirmada',
            }])
            ready = {'V291': False, 'V29': False, 'V292': False}
            with patch.dict(sys.modules, {'streamlit': fake_streamlit}), \
                 patch.object(view, 'verify_artifacts', return_value=ready), \
                 patch.object(view, 'audit_entry_evidence', return_value=inventory) as audit:
                # We intentionally stop at first columns() call, after the
                # real digest calculation and source inventory are completed.
                with self.assertRaises(ReachedFilters):
                    view.render_full290(root, folder, pre)
                self.assertEqual(audit.call_count, 1)
                self.assertEqual(audit.call_args.args[-1], actual_digest)


if __name__ == '__main__':
    unittest.main()
