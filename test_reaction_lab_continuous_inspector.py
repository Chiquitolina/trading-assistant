"""Read-only Continuous inspector regression tests, no Streamlit server required."""
import math
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

# The test can run from repo root OR from the release folder.
HERE = Path(__file__).resolve().parent
if (HERE / 'reaction_lab_research_view.py').is_file():
    sys.path.insert(0, str(HERE))

import reaction_lab_research_view as view


class FrozenInspectorTests(unittest.TestCase):
    def setUp(self):
        self.t = 1_791_900_000_000
        self.candles = [
            {'timestamp': self.t + i * 900_000, 'open': 10.0,
             'high': 11.0, 'low': 9.0, 'close': 10.1}
            for i in range(110)
        ]
        self.known = self.t + 101 * 900_000 + 60000
        self.retest = self.known - 60000

    def test_exact_asof_full_15m_closed(self):
        frame = view.frozen_closed_15m(self.candles, self.known, 80)
        self.assertEqual(len(frame), 80)
        self.assertEqual(int(frame.iloc[-1]['timestamp']), self.t + 100 * 900_000)
        self.assertTrue((frame['timestamp'] + 900000 <= self.known).all())

    def test_never_show_future_even_if_archive_contains_it(self):
        frame = view.frozen_closed_15m(self.candles, self.known, 200)
        self.assertEqual(len(frame), 101)
        self.assertEqual(int(frame.iloc[-1]['timestamp']), self.t + 100 * 900_000)

    def test_missing_history_fails_closed(self):
        self.assertTrue(view.frozen_closed_15m([], self.known, 80).empty)
        self.assertTrue(view.frozen_closed_15m([{'timestamp': 2}], self.known, 80).empty)

    def test_room_never_infers_missing_values(self):
        row = {'nearest_opposing_room_pct': None,
               'nearest_opposing_swing_price': float('nan'),
               'aligned_rsi_extreme_count': None, 'retest_price': 12}
        room = view.persisted_room_fields(row)
        self.assertIsNone(room['room_pct'])
        self.assertIsNone(room['swing_price'])
        self.assertIsNone(room['aligned_rsi_count'])
        self.assertEqual(room['reaction_price'], 12.)

    def test_scanner_markers_not_regenerated(self):
        row = {'pivot_timestamp': 100, 'swing_price': 5,
               'actionable_timestamp': 200, 'entry_price': 5,
               'departure_timestamp': 300, 'departure_price': 7,
               'retest_timestamp': 400, 'retest_price': 5.1}
        self.assertEqual([v[0] for v in view.persisted_retest_markers(row)],
                         ['Pivot','Confirmed','Departure','REACTION'])
        self.assertEqual(view.persisted_retest_markers({}), [])

    def test_continuous_delegates_to_single_shared_legacy_inspector(self):
        import inspect
        text=inspect.getsource(view.render_cohort_research)
        self.assertIn('shared_retest_inspector(', text)
        self.assertNotIn('render_persisted_retest_inspector(', text)
        self.assertIn('ui_key_prefix=\'continuous_\'', text)

    def test_visual_adapter_preserves_causal_room_and_complete_15m_blocks(self):
        from reaction_lab_unified_adapter import frozen_room, post_15m_from_1m
        import pandas as pd
        row={'known_ts':self.known,'nearest_opposing_room_pct':4,
             'nearest_opposing_swing_price':10.4,
             'nearest_opposing_swing_actionable_timestamp':self.known + 3600000}
        self.assertIsNone(frozen_room(row)['nearest_opposing_room_pct'])
        path=pd.DataFrame([{'timestamp':self.t+i*60_000,'open':10,'high':11,'low':9,'close':10.5}
            for i in range(14)])
        self.assertTrue(post_15m_from_1m(path, self.t).empty)


if __name__=='__main__':
    unittest.main()
