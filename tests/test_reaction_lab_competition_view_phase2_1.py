"""Tests for display-only competition improvements. Run on installed Streamlit VPS."""
import unittest
import pandas as pd

from dashboard.views.reaction_lab_competition_view import (
    _blocked_summary, _trajectory_segments,
)


class CompetitionViewTests(unittest.TestCase):
    def bars(self):
        return pd.DataFrame({
            "timestamp_ms": [0, 60000, 120000, 180000, 240000],
            "open": [100, 100, 100, 100, 100],
            "high": [100, 101, 101, 108, 109],
            "low": [100, 99, 97, 95, 90],
            "close": [100, 101, 98, 105, 109],
        })

    def test_exit_uses_executable_price_not_exit_bar_close(self):
        out = _trajectory_segments(self.bars(), anchor=100, side="SHORT",
                                   fill_ms=60000, exit_ms=180000, exit_price=102)
        self.assertEqual(out["active"]["timestamp_ms"].tolist(), [60000, 120000])
        self.assertEqual(out["market_after"]["timestamp_ms"].tolist(), [240000])
        self.assertEqual(out["exit_point"][0], 180000)
        self.assertAlmostEqual(out["exit_point"][1], -2.0)

    def test_no_fill_is_market_only(self):
        out = _trajectory_segments(self.bars(), anchor=100, side="LONG")
        self.assertTrue(out["active"].empty)
        self.assertIsNone(out["exit_point"])
        self.assertEqual(len(out["market_after"]), 5)

    def test_long_fill_exit(self):
        out = _trajectory_segments(self.bars(), anchor=100, side="LONG",
                                   fill_ms=60000, exit_ms=120000, exit_price=102)
        self.assertEqual(out["active"]["timestamp_ms"].tolist(), [60000])
        self.assertAlmostEqual(out["exit_point"][1], 2.0)

    def test_blocked_summary_filters_actual_eligibility(self):
        shadow = pd.DataFrame([
            {"event_key": "A", "shadow_class": "ELIGIBLE_BLOCKED_SLOT", "shadow_status": "TP", "shadow_fill_ts": "10", "shadow_net_pct": "1.8"},
            {"event_key": "B", "shadow_class": "ELIGIBLE_BLOCKED_SLOT", "shadow_status": "NO_FILL", "shadow_fill_ts": "", "shadow_net_pct": ""},
            {"event_key": "C", "shadow_class": "ELIGIBLE_BLOCKED_SLOT", "shadow_status": "SL", "shadow_fill_ts": "20", "shadow_net_pct": "-2.1"},
            {"event_key": "D", "shadow_class": "OUTSIDE_FROZEN_FILTERS", "shadow_status": "TP", "shadow_fill_ts": "30", "shadow_net_pct": "10.0"},
        ])
        decisions = pd.DataFrame([
            {"event_key": x, "owner_stage": "OPEN_POSITION", "owner_at_decision": "X"}
            for x in "ABCD"
        ])
        data, rows = _blocked_summary(shadow, decisions)
        self.assertEqual(len(rows), 3)
        self.assertEqual(data["filled"], 2)
        self.assertEqual(data["tp"], 1)
        self.assertEqual(data["sl"], 1)
        self.assertEqual(data["no_fill"], 1)
        self.assertAlmostEqual(data["mean_net_pct_filled"], -.15)


if __name__ == "__main__":
    unittest.main()
