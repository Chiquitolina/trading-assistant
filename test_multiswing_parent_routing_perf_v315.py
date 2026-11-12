"""Regression/smoke checks for fast parent routing into Multi-Swing.

This intentionally avoids importing the large Streamlit app (and therefore
requires no Redis/DB/service credentials).  Tests operate on the checked-in
complete source and execute the short mode dispatcher in isolation.
"""
from __future__ import annotations

import ast
import copy
from pathlib import Path
import sys
import textwrap
import time
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
APP = ROOT / 'dashboard/app.py'
CHILD = ROOT / 'dashboard/views/reaction_continuous_multiswing_view.py'
MAP = 'Mapa BTC 60D · 6 TF'
EVENT = 'Evento REACTION · Continuous'
EVENT_LAB = '📍 Event swing structure'


class ParentFastRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_text = APP.read_text(encoding='utf-8')
        cls.child_text = CHILD.read_text(encoding='utf-8')
        cls.app_ast = ast.parse(cls.app_text, filename=str(APP))
        cls.child_ast = ast.parse(cls.child_text, filename=str(CHILD))

    def test_parent_compiles_without_importing_live_services(self):
        compile(self.app_text, str(APP), 'exec')
        compile(self.child_text, str(CHILD), 'exec')

    def test_navigation_is_before_cohort_load(self):
        nav = self.app_text.index('key="rcms_study_mode_v1"')
        cohort = self.app_text.index('render_cohort_research(', nav)
        child = self.app_text.index('render_continuous_multiswing_view(', cohort)
        self.assertLess(nav, cohort)
        self.assertLess(cohort, child)

    def test_cohort_is_opt_in_only_for_event_swing(self):
        self.assertIn(
            'if lab_mode != "📍 Event swing structure" or event_swing_load_cohort:',
            self.app_text,
        )
        self.assertIn('value=False,\n                    key="reaction_event_swing_load_cohort_v1"', self.app_text)
        self.assertIn('mode_override=event_swing_mode', self.app_text)
        self.assertIn('if lab_mode == "📍 Event swing structure":', self.app_text)

    def test_map_does_not_load_legacy_branch_in_todos(self):
        self.assertIn('and event_swing_mode == "Mapa BTC 60D · 6 TF"', self.app_text)
        self.assertIn('or (lab_mode == "📍 Event swing structure"', self.app_text)

    def test_dispatch_has_one_widget_owner_per_call(self):
        # The app owns the key in parent mode; the child only owns it when
        # invoked independently (backwards compatibility).
        funcs = [n for n in self.child_ast.body if isinstance(n, ast.FunctionDef)
                 and n.name == 'render_continuous_multiswing_view']
        self.assertEqual(len(funcs), 1)
        f = funcs[0]
        self.assertIn('mode_override', [a.arg for a in f.args.kwonlyargs])
        self.assertIn('if mode_override is None:', self.child_text)
        self.assertIn("key='rcms_study_mode_v1'", self.child_text)

    def test_real_child_dispatch_to_btc_without_secondary_selectbox(self):
        """Execute the actual source's early dispatch with mocked UI/modules."""
        function = next(n for n in self.child_ast.body if isinstance(n, ast.FunctionDef)
                        and n.name == 'render_continuous_multiswing_view')
        # The map branch returns before the heavy Continuous imports.  Extract
        # only this early part for an isolated executable integration test.
        end = next(i for i, n in enumerate(function.body)
                   if isinstance(n, ast.Import)
                   and any(a.name == 'pandas' for a in n.names))
        standalone = copy.deepcopy(function)
        standalone.body = standalone.body[:end]
        module_ast = ast.fix_missing_locations(ast.Module(body=[standalone], type_ignores=[]))
        ns = {'_perf_time': time}
        exec(compile(module_ast, str(CHILD), 'exec'), ns)

        calls = []
        fake_st = types.ModuleType('streamlit')
        fake_st.selectbox = lambda *args, **kwargs: calls.append(('selectbox', kwargs)) or MAP
        fake_btc = types.ModuleType('dashboard.views.reaction_multiswing_btc60d_view_v1')
        fake_btc.render_btc_multiswing_60d_view = lambda project: calls.append(('btc', project))
        with patch.dict(sys.modules, {'streamlit': fake_st,
                     'dashboard.views.reaction_multiswing_btc60d_view_v1': fake_btc}):
            ns['render_continuous_multiswing_view']('/test', mode_override=MAP)
        self.assertEqual(calls, [('btc', '/test')])

        calls.clear()
        with patch.dict(sys.modules, {'streamlit': fake_st,
                     'dashboard.views.reaction_multiswing_btc60d_view_v1': fake_btc}):
            ns['render_continuous_multiswing_view']('/test')
        self.assertEqual([x[0] for x in calls], ['selectbox', 'btc'])

        calls.clear()
        with patch.dict(sys.modules, {'streamlit': fake_st}):
            with self.assertRaises(ValueError):
                ns['render_continuous_multiswing_view']('/test', mode_override='UNSUPPORTED')
        self.assertEqual(calls, [])

    def test_parent_selector_is_functional_and_lazy(self):
        """Execute the current parent selector body with fake Streamlit widgets."""
        start = self.app_text.index('        event_swing_mode = None\n')
        end = self.app_text.index('        # Runner search always', start)
        body = textwrap.dedent(self.app_text[start:end])
        text = ('def selector(st, lab_mode, cohort_mode):\n'
                + textwrap.indent(body, '    ')
                + '    return event_swing_mode, event_swing_load_cohort\n')
        scope = {}
        exec(compile(text, str(APP), 'exec'), scope)
        mock_calls = []
        class FakeStreamlit:
            def __init__(self, selected):
                self.selected = selected
            def selectbox(self, label, choices, **kwargs):
                mock_calls.append(('selectbox', kwargs.get('key')))
                return self.selected
            def checkbox(self, label, **kwargs):
                mock_calls.append(('checkbox', kwargs.get('key'), kwargs.get('value')))
                return False
            def caption(self, label):
                mock_calls.append(('caption', label))

        selected = scope['selector'](FakeStreamlit(MAP), EVENT_LAB, 'Continuous V1')
        self.assertEqual(selected, (MAP, False))
        self.assertEqual([c[0] for c in mock_calls], ['selectbox', 'caption'])
        mock_calls.clear()
        selected = scope['selector'](FakeStreamlit(EVENT), EVENT_LAB, 'Todos')
        self.assertEqual(selected, (EVENT, False))
        self.assertEqual([c[0] for c in mock_calls], ['selectbox', 'checkbox'])
        mock_calls.clear()
        selected = scope['selector'](FakeStreamlit(MAP), 'Otro laboratorio', 'Continuous V1')
        self.assertEqual(selected, (None, False))
        self.assertFalse(mock_calls)

    def test_unrelated_lab_views_remain_present(self):
        for label in ('🔬 Retest Quality Lab', '🏃 Runner Discovery',
                      '🧬 Candidate Research', '🔁 REACTION / Retests'):
            self.assertIn(label, self.app_text)
        self.assertIn('render_first_touches_journal(', self.app_text)


if __name__ == '__main__':
    unittest.main()
