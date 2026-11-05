"""Pruebas causales de la Fase 1. Ejecutar: python3 -m unittest -v test_multi_swing_structural_quality_v1"""
import copy
import unittest
from datetime import datetime, timezone

from multi_swing_structural_quality_v1 import ResearchInputError, compute_features, MINUTE_MS


def ms(hhmm):
    return int(datetime.strptime("2026-11-05 " + hhmm, "%Y-%m-%d %H:%M")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


def fixtures():
    bars = []
    start = ms("07:30")
    for i in range(110):
        ts = start + i * MINUTE_MS
        bars.append({"timestamp": ts, "high": 101.0, "low": 100.4})
    for bar in bars:
        if bar["timestamp"] == ms("07:57"):
            bar["low"] = 100.0
    event = {"symbol": "BTCUSDT", "side": "LONG", "status": "REACTION",
             "pivot_ts": ms("07:45"), "actionable_ts": ms("08:45"),
             "retest_ts": ms("09:00"), "known_ts": ms("09:01"),
             "swing_price": 100.0}
    pivots = [
        {"timeframe": "15m", "side": "LOW", "pivot_ts": ms("07:45"),
         "available_ts": ms("08:45"), "price": 100.0, "source": "SCANNER_ORIGINAL"},
        {"timeframe": "5m", "side": "LOW", "pivot_ts": ms("07:55"),
         "available_ts": ms("08:15"), "price": 100.0},
        {"timeframe": "1m", "side": "LOW", "pivot_ts": ms("07:57"),
         "available_ts": ms("08:03"), "price": 100.0},
    ]
    return event, pivots, bars


class MultiSwingStructuralQualityV1Tests(unittest.TestCase):
    def test_triple_same_physical_extreme(self):
        e, p, b = fixtures()
        r = compute_features(e, p, b)
        self.assertEqual(r["coincident_tf_count"], 3)
        self.assertEqual(r["unique_physical_extrema"], 1)
        self.assertEqual(r["physical_structure_type"], "SAME_PHYSICAL_EXTREME")
        self.assertEqual(r["verified_pivots"], 3)

    def test_double_without_5m(self):
        e, p, b = fixtures()
        r = compute_features(e, [p[0], p[2]], b)
        self.assertEqual(r["coincident_tf_count"], 2)
        self.assertEqual(r["unique_physical_extrema"], 1)

    def test_independent_extreme_within_anchor_candle(self):
        e, p, b = fixtures()
        # Un LOW 5m próximo a nivel pero físicamente diferente del LOW de 15m.
        for bar in b:
            if bar["timestamp"] == ms("07:52"):
                bar["low"] = 100.05
        p[1] = {"timeframe": "5m", "side": "LOW", "pivot_ts": ms("07:50"),
                "available_ts": ms("08:10"), "price": 100.05}
        r = compute_features(e, p, b)
        self.assertEqual(r["unique_physical_extrema"], 2)
        self.assertEqual(r["physical_structure_type"], "DISTINCT_PHYSICAL_EXTREMES")

    def test_future_pivots_have_no_effect(self):
        e, p, b = fixtures()
        base = compute_features(e, p, b)
        future = {"timeframe": "1m", "side": "LOW", "pivot_ts": ms("07:58"),
                  "available_ts": ms("09:25"), "price": 100.0}
        self.assertEqual(base, compute_features(e, p + [future], b))

    def test_future_price_changes_cannot_change_features(self):
        e, p, b = fixtures()
        base = compute_features(e, p, b)
        later = b + [{"timestamp": ms("09:01"), "high": 999, "low": 0.1}]
        self.assertEqual(base, compute_features(e, p, later))

    def test_no_fabrication_on_gap(self):
        e, p, b = fixtures()
        gap = [x for x in b if x["timestamp"] != ms("07:57")]
        r = compute_features(e, p, gap)
        self.assertEqual(r["physical_structure_type"], "UNKNOWN")
        self.assertIsNone(r["unique_physical_extrema"])
        self.assertGreater(r["missing_ohlc_pivots"], 0)

    def test_tied_low_is_ambiguous(self):
        e, p, b = fixtures()
        for bar in b:
            if bar["timestamp"] == ms("07:58"):
                bar["low"] = 100.0
        r = compute_features(e, p, b)
        self.assertIsNone(r["unique_physical_extrema"])
        self.assertEqual(r["physical_structure_type"], "UNKNOWN")
        self.assertGreater(r["tied_extrema_pivots"], 0)

    def test_price_mismatch_unknown(self):
        e, p, b = fixtures()
        b = copy.deepcopy(b)
        for bar in b:
            if bar["timestamp"] == ms("07:57"):
                bar["low"] = 100.01
        r = compute_features(e, p, b)
        self.assertGreater(r["price_mismatch_pivots"], 0)
        self.assertIsNone(r["unique_physical_extrema"])

    def test_duplicate_snapshots_not_counted_twice(self):
        e, p, b = fixtures()
        self.assertEqual(compute_features(e, p, b), compute_features(e, p + [dict(p[1])], b))

    def test_conflicting_snapshots_rejected(self):
        e, p, b = fixtures()
        q = dict(p[1], available_ts=ms("08:20"))
        with self.assertRaises(ResearchInputError):
            compute_features(e, p + [q], b)

    def test_original_status_immutable(self):
        e, p, b = fixtures()
        for status in ("REACTION", "TOUCH_INDECISIVE", "TOUCH_FAILED"):
            e["status"] = status
            self.assertEqual(compute_features(e, p, b)["original_status"], status)

    def test_opposite_side_pivots_excluded(self):
        e, p, b = fixtures()
        opposite = {"timeframe": "1m", "side": "HIGH", "pivot_ts": ms("07:58"),
                    "available_ts": ms("08:03"), "price": 100.0}
        self.assertEqual(compute_features(e, p, b), compute_features(e, p + [opposite], b))

    def test_not_available_15m_is_invalid(self):
        e, p, b = fixtures()
        p[0]["available_ts"] = ms("09:05")
        with self.assertRaises(ResearchInputError):
            compute_features(e, p, b)

    def test_short_high_symmetric(self):
        e, p, b = fixtures()
        e["side"] = "SHORT"
        for pp in p:
            pp["side"] = "HIGH"
        for bar in b:
            bar["high"] = 102.0
        for bar in b:
            if bar["timestamp"] == ms("07:57"):
                bar["high"] = 103.0
        e["swing_price"] = 103.0
        for pp in p:
            pp["price"] = 103.0
        result = compute_features(e, p, b)
        self.assertEqual(result["physical_structure_type"], "SAME_PHYSICAL_EXTREME")
        self.assertEqual(result["coincident_tf_count"], 3)

    def test_wrong_timing_refused(self):
        e, p, b = fixtures()
        e["known_ts"] = ms("09:00")
        with self.assertRaises(ResearchInputError):
            compute_features(e, p, b)


if __name__ == "__main__":
    unittest.main()
