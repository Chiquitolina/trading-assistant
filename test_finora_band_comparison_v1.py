"""Regression tests for publication cutoffs and structural clocks."""

import unittest

import pandas as pd

from finora_band_comparison_v1 import (
    Candidate, M, Reference, compare, detect_impulse_zones, first_contact, impulse_status,
)


def sample_bars(prices):
    width = 60 * M
    bars = pd.DataFrame(prices, columns=['open', 'high', 'low', 'close'])
    bars['timestamp'] = bars.index * width
    bars['available_ts'] = bars.timestamp + width
    return bars


class PublicationCutoff(unittest.TestCase):
    def test_block_at_break_close_gap_one_close_later(self):
        bars = sample_bars([(10, 11, 9, 9.5), (9.5, 15, 9.4, 14), (14, 16, 12, 15)])
        bos = {1: ('BOS', 'UP')}
        before_third = detect_impulse_zones(bars.iloc[:2], '1h', bos)
        self.assertEqual([(c.family, c.available_ts) for c in before_third], [('OB_BOS', 120 * M)])
        zones = detect_impulse_zones(bars, '1h', bos)
        self.assertEqual([(c.family, c.low, c.high, c.origin_ts, c.available_ts) for c in zones],
                         [('OB_BOS', 9, 11, 0, 120 * M), ('FVG_BOS', 11, 12, 0, 180 * M)])

    def test_published_borders_never_rank_a_future_exact_match(self):
        target = Reference('demo', 'LINKUSDT', '1h', 140 * M, 'ZONE', 'Hueco', 10, 12, '', '', None)
        earlier = Candidate('FVG', '1h', 'a', 'UP', 10, 13, 0, 120 * M)
        future = Candidate('FVG', '1h', 'b', 'UP', 10, 12, 0, 180 * M)
        rows = compare(target, [earlier, future])
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]['exact'])
        self.assertEqual(rows[0]['error_high'], 1)
        self.assertAlmostEqual(rows[0]['iou'], 2 / 3)

    def test_wick_contact_and_later_invalidating_close_are_distinct(self):
        bars = sample_bars([(10, 11, 9, 9.5), (9.5, 15, 9.4, 14), (14, 16, 12, 15),
                            (15, 16, 10.5, 11.2), (11.2, 12, 9.5, 10.5)])
        gap = detect_impulse_zones(bars.iloc[:3], '1h', {})[-1]
        self.assertIsNone(impulse_status(gap, bars.iloc[:4]))
        self.assertEqual(impulse_status(gap, bars), 300 * M)
        minute = pd.DataFrame({'timestamp': [17 * M, 18 * M, 19 * M], 'high': [13.1, 13, 12.7],
                               'low': [11.95, 12.5, 11.99]})
        self.assertIsNone(first_contact(minute, 11, 12, 18 * M, 19 * M))
        self.assertEqual(first_contact(minute, 11, 12, 18 * M, 20 * M), 20 * M)


if __name__ == '__main__':
    unittest.main()
