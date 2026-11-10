"""Regresión visual BTC 60D: el contexto no cambia las auditorías causales.

Ejecutar desde /home/multiservices-1m:
python3 -m unittest -v test_multiswing_60d_visual_context_v1
"""
import inspect
import unittest

from btc_multiswing_60d_explorer_v1 import DAY, M, aggregate_closed
from dashboard.views.reaction_multiswing_btc60d_view_v1 import (
    render_btc_multiswing_60d_view, v22_visual_context_window,
)


class VisualContext60D(unittest.TestCase):
    def setUp(self):
        self.source_start = 1_000_000_000 * 1000
        self.cutoff = self.source_start + 60 * DAY
        self.stage = self.source_start + 36 * DAY

    def select(self, context, stage=None, cutoff=None):
        return v22_visual_context_window(
            source_start=self.source_start,
            cutoff=self.cutoff if cutoff is None else cutoff,
            stage_known_from=self.stage if stage is None else stage,
            context=context,
        )

    def test_full_context_starts_at_verified_source_not_stage_or_last14d(self):
        self.assertEqual(self.select('FULL_60D'), (self.source_start, self.cutoff))

    def test_since_stage_starts_at_confirmation(self):
        self.assertEqual(self.select('FROM_STAGE'), (self.stage, self.cutoff))

    def test_last7_days_is_visual_only(self):
        self.assertEqual(self.select('LAST_7D'), (self.cutoff - 7 * DAY, self.cutoff))

    def test_last7_days_respects_freeze_start(self):
        short_cutoff = self.source_start + 3 * DAY
        self.assertEqual(
            self.select('LAST_7D', stage=self.source_start+DAY, cutoff=short_cutoff),
            (self.source_start, short_cutoff),
        )

    def test_recent_stage_does_not_pull_band_into_past(self):
        stage = self.cutoff - 2 * DAY
        chart_start, _ = self.select('LAST_7D', stage=stage)
        self.assertLess(chart_start, stage)  # Context can predate formation.
        self.assertEqual(max(chart_start, stage), stage)  # Band cannot.

    def test_stage_at_cutoff_is_allowed_without_fake_closed_candle(self):
        self.assertEqual(self.select('FROM_STAGE', stage=self.cutoff),
                         (self.cutoff, self.cutoff))

    def test_wrong_context_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'INVALID_V22_VISUAL_CONTEXT'):
            self.select('INVENTED')

    def test_invalid_future_stage_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'INVALID_V22_VISUAL_TIMELINE'):
            self.select('FULL_60D', stage=self.cutoff+1)

    def test_invalid_source_window_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'INVALID_V22_VISUAL_TIMELINE'):
            self.select('FULL_60D', stage=self.source_start, cutoff=self.source_start)

    def test_full_60d_can_render_all_2880_closed_30m_bars(self):
        import numpy as np
        import pandas as pd
        # The 60D freeze starts on the hour, so 30m alignment is exact.
        start = self.source_start - self.source_start % (30*M)
        end = start + 60*DAY
        stamps = np.arange(start, end, M, dtype='int64')
        closes = np.full(len(stamps), 80_000.0)
        one_minute = pd.DataFrame({
            'timestamp': stamps, 'open': closes, 'high': closes,
            'low': closes, 'close': closes,
        })
        left, right = v22_visual_context_window(
            source_start=start, cutoff=end,
            stage_known_from=start+30*DAY, context='FULL_60D')
        bars = aggregate_closed(one_minute, '30m', left, right)
        self.assertEqual(len(bars), 2880)
        self.assertEqual(int(bars.iloc[0].timestamp), start)
        self.assertEqual(int(bars.iloc[-1].timestamp) + 30*M, end)

    def test_main_map_and_v22_windows_are_separate(self):
        source = inspect.getsource(render_btc_multiswing_60d_view)
        self.assertIn('chart_window(exploration_cutoff,start,int(span_days))', source)
        self.assertIn("chart_v22_start,_=v22_visual_context_window(", source)
        self.assertIn('source_start=start,cutoff=cutoff', source)
        self.assertNotIn('cutoff-14*DAY', source)

    def test_audit_runs_independently_of_context_selection(self):
        source = inspect.getsource(render_btc_multiswing_60d_view)
        self.assertLess(source.index('audit_fixed_region('),
                        source.index("v22_context=st.selectbox("))
        self.assertIn('full,region_inspection,cutoff,horizon_minutes=horizon', source)
        self.assertIn("if chart_v22_start<cutoff else pd.DataFrame()", source)
        self.assertIn("left=max(chart_v22_start,int(region_inspection.known_from))", source)

    def test_v23_visit_inspector_keeps_independent_zoom(self):
        source = inspect.getsource(render_btc_multiswing_60d_view)
        self.assertIn('zoom_left=max(int(region_inspection.known_from)', source)
        self.assertIn("key='ms60_v23_visit_chart'", source)
        self.assertIn("key='ms60_v22_fixed_region_chart'", source)


if __name__ == '__main__':
    unittest.main()
