"""Regression tests: old and new research tabs + metadata propagation."""
import ast
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

FILE = Path(__file__).parent / 'dashboard/views/reaction_microstructure_quality_view.py'


def load_helper():
    # Extract helper without importing heavyweight project research dependencies.
    tree = ast.parse(FILE.read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == '_render_compatible_research_tab')
    module = ast.Module(body=[node], type_ignores=[])
    namespace = {}
    exec(compile(ast.fix_missing_locations(module), str(FILE), 'exec'), namespace)
    return namespace[node.name]


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.warn = []
        fake_streamlit = types.ModuleType('streamlit')
        fake_streamlit.warning = self.warn.append
        self.patch = patch.dict(sys.modules, {'streamlit': fake_streamlit})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.call = load_helper()
        self.params = dict(label='Compression Lineage', tp_pct=2.0,
                           sl_pct=1.0, outcome_horizon=60,
                           entry_mode='STRUCTURAL_OPEN')

    def test_legacy_two_positional_args_no_type_error_but_warns(self):
        def old(frame, project_root):
            return frame, project_root
        self.assertEqual(self.call(old, 'events', 'root', **self.params), ('events', 'root'))
        self.assertEqual(len(self.warn), 1)
        self.assertIn('tp_pct', self.warn[0])
        self.assertIn('no coincidir', self.warn[0])

    def test_current_signature_propagates_all_options(self):
        def current(frame, project_root, tp_pct=1, sl_pct=.5,
                    outcome_horizon=180, entry_mode='OBSERVED_OPEN'):
            return (frame, project_root, tp_pct, sl_pct, outcome_horizon, entry_mode)
        self.assertEqual(self.call(current, 'events', 'root', **self.params),
                         ('events', 'root', 2.0, 1.0, 60, 'STRUCTURAL_OPEN'))
        self.assertEqual(self.warn, [])

    def test_partial_signature_warns_and_passes_only_supported(self):
        def partial(frame, root, *, entry_mode='OBSERVED_OPEN'):
            return entry_mode
        self.assertEqual(self.call(partial, 'events', 'root', **self.params),
                         'STRUCTURAL_OPEN')
        self.assertEqual(len(self.warn), 1)

    def test_kwargs_signature_accepts_all_without_warning(self):
        def generic(frame, root, **kwargs):
            return kwargs
        got = self.call(generic, 'events', 'root', **self.params)
        self.assertEqual(got['tp_pct'], 2.0)
        self.assertEqual(got['outcome_horizon'], 60)
        self.assertFalse(self.warn)

    def test_all_four_legacy_tabs_call_adapter(self):
        txt = FILE.read_text(encoding='utf-8')
        for fn in ('render_lineage_atlas','render_v4','render_level_atlas',
                   'render_independent_structures'):
            self.assertIn(f'            {fn}, analysis_before_result_filter', txt)
        self.assertIn('with tabs[10]:', txt)
        self.assertIn('render_horizontal_level_rich_card(analysis_before_result_filter',txt)

if __name__ == '__main__':
    unittest.main()
