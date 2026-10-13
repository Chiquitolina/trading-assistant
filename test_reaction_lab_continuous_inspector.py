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

    def test_render_controls_use_frozen_values_and_15m_snapshot(self):
        class Col:
            def metric(self, *args, **kwargs):
                pass
        plotted=[]
        controls=[]
        fake = types.ModuleType('streamlit')
        fake.checkbox = lambda *args, **kwargs: (controls.append(args[0]) or True)
        fake.slider = lambda *args, **kwargs: (controls.append(args[0]) or 80)
        fake.columns = lambda n: [Col() for _ in range(n)]
        fake.caption = lambda *args, **kwargs: None
        fake.warning = lambda *args, **kwargs: None
        fake.plotly_chart = lambda fig, **kwargs: plotted.append(fig)
        row = {
            'symbol':'TESTUSDT', 'signal':'LONG', 'known_ts':self.known,
            'retest_timestamp':self.retest,'retest_price':10.1,
            'retest_close':10.1,'swing_price':10,
            'pivot_timestamp':self.t + 42*900_000,
            'actionable_timestamp':self.t + 48*900_000,
            'entry_price':10,'departure_timestamp':self.t+75*900_000,
            'departure_price':10.5,
            'nearest_opposing_room_pct':5,
            'nearest_opposing_swing_tf':'1h',
            'nearest_opposing_swing_price':10.6,
            'nearest_opposing_swing_actionable_timestamp':self.t,
            'aligned_rsi_extreme_count':2,
            'rsi14_1m':33,'rsi14_5m':38,'rsi14_15m':41,'rsi14_1h':45,
        }
        with patch.dict(sys.modules, {'streamlit': fake}):
            view.render_persisted_retest_inspector(row, {'15m':self.candles}, 'REACTION / Retests')
        self.assertIn('Show causal HTF room on chart',controls)
        self.assertIn('15m candles in inspector',controls)
        self.assertEqual(len(plotted), 1)
        fig=plotted[0]
        self.assertEqual(len(fig.data[0].x),80)
        names=[x.name for x in fig.data[1:]]
        self.assertIn('Pivot',names)
        self.assertIn('Confirmed',names)
        self.assertIn('REACTION',names)
        self.assertTrue(any(shape.type=='rect' for shape in fig.layout.shapes))

    def test_future_confirmed_htf_target_is_blocked(self):
        # A corrupted HTF timestamp must not produce a causal band.
        class Col:
            def metric(self, *args, **kwargs): pass
        plotted=[];warnings=[]
        fake = types.ModuleType('streamlit')
        fake.checkbox=lambda *args, **kwargs:True
        fake.slider=lambda *args, **kwargs:80
        fake.columns=lambda n:[Col() for _ in range(n)]
        fake.caption=lambda *args, **kwargs:None
        fake.warning=lambda msg: warnings.append(msg)
        fake.plotly_chart=lambda fig, **kwargs:plotted.append(fig)
        row={'symbol':'TESTUSDT','signal':'LONG','known_ts':self.known,
             'retest_timestamp':self.retest,'retest_price':10,
             'nearest_opposing_room_pct':4,
             'nearest_opposing_swing_price':10.4,
             'nearest_opposing_swing_actionable_timestamp':self.known + 3600000}
        with patch.dict(sys.modules, {'streamlit': fake}):
            view.render_persisted_retest_inspector(row,{'15m':self.candles},'REACTION / Retests')
        self.assertTrue(warnings)
        self.assertEqual(len([shape for shape in plotted[0].layout.shapes if shape.type=='rect']),0)


if __name__=='__main__':
    unittest.main()
