import math
import unittest

import numpy as np

from dashboard.views.legacy_fail_touch_analyzer_view import (
    _touch_geometry, attach_fail_touch_metrics, summarize_failure_entries,
)


def cache(*rows):
    # row = (ts, open, high, low, close); split at explicit gaps.
    ts = np.asarray([r[0] for r in rows], dtype=np.int64)
    ends = np.empty(len(rows), dtype=np.int64)
    for i in range(len(rows) - 1, -1, -1):
        ends[i] = i if i == len(rows) - 1 or ts[i+1] != ts[i] + 60000 else ends[i+1]
    return {"timestamps": ts, "opens": np.asarray([r[1] for r in rows], dtype=float),
            "highs": np.asarray([r[2] for r in rows], dtype=float),
            "lows": np.asarray([r[3] for r in rows], dtype=float),
            "closes": np.asarray([r[4] for r in rows], dtype=float),
            "segment_end": ends}


class TestFailTouch(unittest.TestCase):
    def row(self, side="LONG"):
        if side == "LONG":
            return {"status":"TOUCH_FAILED", "signal":"LONG", "swing_price":100,
                    "retest_timestamp":0, "retest_open":100, "retest_high":101,
                    "retest_low":97, "retest_close":98}
        return {"status":"TOUCH_FAILED", "signal":"SHORT", "swing_price":100,
                "retest_timestamp":0, "retest_open":100, "retest_high":103,
                "retest_low":99, "retest_close":102}

    def test_long_touch_wick_and_penetration(self):
        g = _touch_geometry(self.row())
        self.assertAlmostEqual(g["ftr_touch_penetration_pct"], 3)
        self.assertAlmostEqual(g["ftr_touch_wick_share"], 25)
        self.assertAlmostEqual(g["ftr_fail_close_depth_pct"], 2)

    def test_short_touch_mirror(self):
        g = _touch_geometry(self.row("SHORT"))
        self.assertAlmostEqual(g["ftr_touch_penetration_pct"], 3)
        self.assertAlmostEqual(g["ftr_touch_wick_share"], 25)
        self.assertAlmostEqual(g["ftr_fail_close_depth_pct"], 2)

    def test_non_fail_never_changed(self):
        r = {**self.row(), "status":"REACTION"}
        self.assertEqual(attach_fail_touch_metrics(r, None), r)

    def test_depth_fills_only_next_open(self):
        rows = [(0,100,101,97,98), (60000,98,98.7,96,96.5), (120000,97,101,96.8,100.5)]
        out = attach_fail_touch_metrics(self.row(), cache(*rows))
        rec = out["ftr_grid"]["2.0|DEPTH_NEXT_OPEN"]
        self.assertEqual(rec["entry_ts"], 60000)
        self.assertEqual(rec["entry_price"], 98)
        self.assertEqual(rec["outcomes"]["tp1_sl1_60m"]["status"], "SL_FIRST")

    def test_reclaim_entry_never_from_failed_bar(self):
        rows = [(0,100,101,97,98), (60000,98,99.5,95,96), (120000,96,101,96,100.4), (180000,100.5,102,100,101)]
        out = attach_fail_touch_metrics(self.row(), cache(*rows))
        rec = out["ftr_grid"]["2.0|RECLAIM_PIVOT"]
        self.assertEqual(rec["signal_ts"], 120000)
        self.assertEqual(rec["entry_ts"], 180000)
        self.assertEqual(rec["entry_price"], 100.5)

    def test_partial_recovery_causal(self):
        rows = [(0,100,101,97,98), (60000,98,98,94,95), (120000,95,98,95,97), (180000,97,99,96,97.5)]
        out = attach_fail_touch_metrics(self.row(), cache(*rows))
        rec = out["ftr_grid"]["2.0|RECOVER_50"]
        self.assertEqual(rec["signal_ts"], 120000)
        self.assertEqual(rec["entry_ts"], 180000)

    def test_short_reclaim(self):
        rows = [(0,100,103,99,102), (60000,102,105,101,104), (120000,104,104,98,99), (180000,99,101,97,98)]
        out = attach_fail_touch_metrics(self.row("SHORT"), cache(*rows))
        rec = out["ftr_grid"]["2.0|RECLAIM_PIVOT"]
        self.assertEqual(rec["entry_ts"],180000)

    def test_gap_does_not_fill_missing_next_bar(self):
        rows = [(0,100,101,97,98), (120000,96,99,96,98)]
        out = attach_fail_touch_metrics(self.row(), cache(*rows))
        rec = out["ftr_grid"]["2.0|DEPTH_NEXT_OPEN"]
        self.assertIsNone(rec["entry_ts"])
        self.assertEqual(rec["outcomes"]["tp1_sl1_60m"]["status"], "ENTRY_CENSORED")

    def test_censor_without_full_horizon(self):
        rows = [(0,100,101,97,98), (60000,98,98.5,97.9,98), (120000,98,98.5,97.9,98)]
        out = attach_fail_touch_metrics(self.row(), cache(*rows))
        self.assertFalse(out["ftr_complete_180m"])
        self.assertEqual(out["ftr_grid"]["2.0|DEPTH_NEXT_OPEN"]["outcomes"]["tp2_sl1_180m"]["status"],"CENSORED")

    def test_ambiguous_sl_conservative(self):
        rows = [(0,100,101,97,98), (60000,98,100,96,99)]
        out = attach_fail_touch_metrics(self.row(), cache(*rows))
        self.assertEqual(out["ftr_grid"]["2.0|DEPTH_NEXT_OPEN"]["outcomes"]["tp1_sl1_60m"]["status"], "SL_AMBIGUOUS")

    def test_depth_threshold_cannot_fill_before_crossing(self):
        rows = [(0,100,100,99.8,99.85), (60000,99.8,100,99.3,99.4), (120000,99.4,100,97.9,98.1), (180000,98.1,98.6,98,98.5)]
        row = self.row(); row.update({"retest_open":100,"retest_high":100,"retest_low":99.8,"retest_close":99.85})
        out = attach_fail_touch_metrics(row, cache(*rows))
        self.assertEqual(out["ftr_grid"]["2.0|DEPTH_NEXT_OPEN"]["entry_ts"], 180000)

    def test_no_future_upgrades_original_status(self):
        rows = [(0,100,101,97,98), (60000,98,100,98,99), (120000,99,103,99,102)]
        out = attach_fail_touch_metrics(self.row(), cache(*rows))
        self.assertEqual(out["status"], "TOUCH_FAILED")

    def test_summary_accounts_for_no_entry(self):
        a = attach_fail_touch_metrics(self.row(), cache((0,100,101,97,98),(60000,98,100,96,98)))
        b = attach_fail_touch_metrics(self.row(), cache((0,100,100.5,99.8,99.85),(60000,99.85,100,99.8,99.9)))
        s = summarize_failure_entries([a,b], 2.0, "DEPTH_NEXT_OPEN", 2.0, 60)
        self.assertEqual(s["N failed"], 2)
        self.assertEqual(s["No entry total"], 1)
        self.assertEqual(s["Trigger censored"], 1)

    def test_without_threshold_full_window_is_no_trigger(self):
        # Failed = closed just outside tolerance, but never reaches 0.5% depth.
        row = self.row()
        row.update({"retest_open":100,"retest_high":100,"retest_low":99.85,"retest_close":99.85})
        rows = [(i*60000,99.85,100.1,99.85,99.9) for i in range(361)]
        out = attach_fail_touch_metrics(row, cache(*rows))
        self.assertEqual(out["ftr_grid"]["0.5|DEPTH_NEXT_OPEN"]["outcomes"]["tp1_sl1_180m"]["status"],"NO_TRIGGER")

    def test_unobserved_threshold_is_censored_not_no_trigger(self):
        row = self.row()
        row.update({"retest_open":100,"retest_high":100,"retest_low":99.85,"retest_close":99.85})
        rows = [(i*60000,99.85,100.1,99.85,99.9) for i in range(5)]
        out = attach_fail_touch_metrics(row, cache(*rows))
        self.assertEqual(out["ftr_grid"]["0.5|DEPTH_NEXT_OPEN"]["outcomes"]["tp1_sl1_180m"]["status"],"TRIGGER_CENSORED")

    def test_touch_without_forward_cache_keeps_geometry(self):
        out = attach_fail_touch_metrics(self.row(), None)
        self.assertAlmostEqual(out["ftr_touch_penetration_pct"],3)
        self.assertEqual(out["ftr_grid"],{})

if __name__ == "__main__":
    unittest.main()
