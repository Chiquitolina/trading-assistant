"""Regression tests for multiple Runner Discovery V2 chart inspectors.

Run from the repository root:
    python3 -m unittest -v test_runner_discovery_unique_keys

All fixtures are synthetic. The test does not open project databases,
execute Streamlit, connect to Redis, or modify LIVE services.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parent / "dashboard/views/reaction_lab_runner_discovery_v2_view.py"


def _import_ui():
    # Stub dependencies so this regression test can run without the DB or
    # the full app's data services; test only the real UI view under review.
    services = types.ModuleType("dashboard.services.runner_discovery_dna")
    for name in ("runner_labels", "compare_traits", "assign_families", "family_report"):
        setattr(services, name, lambda *a, **k: None)
    services.FAMILY_ORDER = ()
    sources = types.ModuleType("reaction_lab_runner_discovery_v2")
    sources.FEATURE_LABELS, sources.HORIZONS, sources.TARGETS = {}, (), ()
    sources.build_dataset = lambda *a, **k: None
    sources.outcome_at_horizon = lambda *a, **k: None
    sources.project_results_dir = lambda *a, **k: None
    sources.readonly_uri = lambda *a, **k: None
    with mock.patch.dict(sys.modules, {
        "dashboard": types.ModuleType("dashboard"),
        "dashboard.services": types.ModuleType("dashboard.services"),
        "dashboard.services.runner_discovery_dna": services,
        "reaction_lab_runner_discovery_v2": sources,
    }):
        spec = importlib.util.spec_from_file_location("_runner_discovery_unique_keys_under_test", SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class _FakeStreamlit(types.ModuleType):
    def __init__(self):
        super().__init__("streamlit")
        self.keys = []
        self.messages = []

    def _key(self, key):
        if key in self.keys:
            raise AssertionError(f"Duplicate Streamlit key: {key!r}")
        self.keys.append(key)

    def checkbox(self, label, *, key, **kwargs):
        self._key(key)
        return True

    def plotly_chart(self, fig, *, key, **kwargs):
        self._key(key)

    def cache_data(self, **kwargs):
        return lambda f: f

    def caption(self, message):
        self.messages.append(str(message))

    def warning(self, message):
        raise AssertionError(f"Unexpected warning: {message}")

    def info(self, message):
        raise AssertionError(f"Unexpected info: {message}")


class RunnerDiscoveryUniqueKeysTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.view = _import_ui()

    def test_namespaces_are_disjoint_and_stable(self):
        ids = [self.view._rdv2_widget_key(scope, kind)
               for scope in ('all_reactions', 'runner_detail', 'signals_detail')
               for kind in ('triple_show15m', 'triple_zoom', 'triple_chart', 'signal_chart')]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids[0], self.view._rdv2_widget_key('all_reactions', 'triple_show15m'))

    def test_unknown_scope_is_an_error(self):
        with self.assertRaises(ValueError):
            self.view._rdv2_widget_key('unknown', 'triple_chart')

    def test_all_chart_call_sites_pass_explicit_distinct_scopes(self):
        tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
        expected = {
            '_triple_15m_chart': {'all_reactions', 'runner_detail'},
            '_signal_chart': {'all_reactions', 'runner_detail', 'signals_detail'},
        }
        seen = {name: [] for name in expected}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in expected:
                scope = next((k.value for k in node.keywords if k.arg == 'widget_scope'), None)
                self.assertIsInstance(scope, ast.Constant)
                seen[node.func.id].append(scope.value)
        for name, scopes in expected.items():
            self.assertEqual(set(seen[name]), scopes)
            self.assertEqual(len(seen[name]), len(scopes), f'{name} called twice in same scope')

    def test_same_event_in_both_triple_inspectors_does_not_reuse_keys(self):
        import pandas as pd
        fake = _FakeStreamlit()
        atlas = types.ModuleType('reaction_structural_level_atlas')
        atlas.lineage = types.SimpleNamespace(ROOT_REL='unused', CTX_NAME='none.db', HISTORY_NAME='none.db')
        t0 = 1_780_000_000_000
        minute_15 = 900_000
        bars = pd.DataFrame({
            'timestamp': [t0 + i * minute_15 for i in range(10)],
            'open': [100.0]*10, 'high': [101.0]*10,
            'low': [99.0]*10, 'close': [100.0]*10,
        })
        event = {
            'symbol': 'BTCUSDT', 'side': 'LONG', 'retest_ts': t0 + 12*minute_15,
            'pattern_pre': 'TRIPLE_BOTTOM_CONFIRMED_PRE',
            'level_price': 99.0, 'level_tolerance': 0.1,
            'top_bottom_1_ts': t0 + 2*minute_15,
            'top_bottom_2_ts': t0 + 5*minute_15,
            'top_bottom_3_ts': t0 + 8*minute_15,
            'valley_1_ts': t0 + 3*minute_15,
            'valley_2_ts': t0 + 6*minute_15,
            'neckline_price': 101.0,
        }
        with mock.patch.dict(sys.modules, {'streamlit': fake, 'reaction_structural_level_atlas': atlas}), \
             mock.patch.object(self.view, '_read_triple_pre_candles', return_value=bars):
            self.view._triple_15m_chart(Path('/tmp'), event, widget_scope='all_reactions')
            self.view._triple_15m_chart(Path('/tmp'), event, widget_scope='runner_detail')
        self.assertEqual(len(fake.keys), 6)  # 2 checkboxes + zoom + plot x two inspectors
        self.assertEqual(len(fake.keys), len(set(fake.keys)))

    def test_same_event_in_three_signal_inspectors_does_not_reuse_plot_keys(self):
        fake = _FakeStreamlit()
        t0 = 1_780_000_000_000
        bars = [(t0+i*60_000, 100, 101, 99, 100.5) for i in range(5)]
        event = {
            'event_key': 'TEST|LONG|...', 'symbol': 'BTCUSDT', 'side': 'LONG',
            'retest_close': 100.0, 'entry_structural_ts': t0,
            'entry_observed_open': 100.0, 'entry_observed_ts': t0,
        }
        with mock.patch.dict(sys.modules, {'streamlit': fake}), \
             mock.patch.object(self.view, '_detail_bars', return_value=bars):
            self.view._signal_chart(Path('/tmp'), event, mode='structural', tp=2,
                                    horizon=5, widget_scope='all_reactions')
            self.view._signal_chart(Path('/tmp'), event, mode='structural', tp=2,
                                    horizon=5, show_sl=False, widget_scope='runner_detail')
            self.view._signal_chart(Path('/tmp'), event, mode='structural', tp=2,
                                    horizon=5, widget_scope='signals_detail')
        self.assertEqual(len(fake.keys), 3)
        self.assertEqual(len(fake.keys), len(set(fake.keys)))


if __name__ == '__main__':
    unittest.main()
