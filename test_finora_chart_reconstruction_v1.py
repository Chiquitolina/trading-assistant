"""Regression checks for chart marks produced without future candles."""

from pathlib import Path
import unittest

import pandas as pd

from finora_chart_reconstruction_v1 import HOUR, failed_at, read_source, reconstruct, stamp, validate_hourly


class ReconstructionClocks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.candles, _ = read_source(Path(__file__).resolve().parent, 'NVDAUSDT_perpetual')
        cls.cutoff = stamp('2026-11-24T14:00:00Z')

    def test_future_prices_and_truncation_preserve_the_marks(self):
        original = reconstruct(self.candles, self.cutoff)
        truncated = self.candles[self.candles.available_ts <= self.cutoff]
        earlier = reconstruct(truncated, self.cutoff)
        changed = self.candles.copy()
        future = changed.timestamp >= self.cutoff
        changed.loc[future, ['open', 'high', 'low', 'close']] *= 3
        altered = reconstruct(changed, self.cutoff)
        for result in (earlier, altered):
            self.assertEqual(original['lines'], result['lines'])
            self.assertEqual(original['zones'], result['zones'])
            self.assertEqual(original['last_price'], result['last_price'])
        for row in original['lines'] + original['zones']:
            self.assertLessEqual(row['known_ts'], self.cutoff)
        self.assertTrue(original['bars'].available_ts.le(self.cutoff).all())

    def test_close_invalidates_zone_after_wick_only_contact(self):
        frame = validate_hourly(pd.DataFrame({
            'timestamp': [0, HOUR, 2 * HOUR],
            'open': [110, 110, 108], 'high': [112, 111, 109],
            'low': [108, 99, 98], 'close': [110, 108, 99]}))
        self.assertIsNone(failed_at(frame.iloc[:2], 'BUY', 100, 105, HOUR))
        self.assertEqual(failed_at(frame, 'BUY', 100, 105, HOUR), 3 * HOUR)

    def test_hourly_source_rejects_gaps_duplicates_and_invalid_prices(self):
        clean = self.candles.head(15)
        for damaged in (clean.drop(index=5), pd.concat([clean.head(1), clean], ignore_index=True)):
            with self.assertRaises(ValueError):
                validate_hourly(damaged)
        damaged = clean.copy()
        damaged.loc[0, 'low'] = damaged.loc[0, 'high'] + 1
        with self.assertRaises(ValueError):
            validate_hourly(damaged)


if __name__ == '__main__':
    unittest.main()
