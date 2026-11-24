"""Regression checks for chart marks produced without future candles."""

from pathlib import Path
import unittest

import pandas as pd

from finora_chart_reconstruction_v1 import (
    HOUR, Model, bearish_gaps, failed_at, read_source, reconstruct, stamp, supply_v11, validate_hourly,
)


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


class SupplyAdjustment(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = {key: read_source(Path(__file__).resolve().parent, key)[0] for key in
                       ['NVDAUSDT_perpetual', 'BTCUSDT_perpetual', 'SHELLUSDT_spot']}
        cls.cutoff = stamp('2026-11-24T14:00:00Z')

    def test_three_chart_fixtures_reproduce_the_reviewed_geometry(self):
        expected = {
            'NVDAUSDT_perpetual': [(170.03, 171.63), (165.08, 167.35)],
            'BTCUSDT_perpetual': [(65335.1, 65776.5), (62603.2, 63637.7)],
            'SHELLUSDT_spot': [(0.02682, 0.02729)],
        }
        for key, source in self.sources.items():
            with self.subTest(symbol=key):
                result = reconstruct(source, self.cutoff)
                self.assertEqual([(z['low'], z['high']) for z in result['zones'] if z['side'] == 'SELL'], expected[key])
                self.assertTrue(all(z['formed_ts'] <= z['known_ts'] for z in result['zones'] if z['side'] == 'SELL'))
        # These two origins are down-close candles; the old up-close-only rule skips them.
        for key, origin in [('BTCUSDT_perpetual', '2026-11-15T13:00Z'), ('SHELLUSDT_spot', '2026-11-18T17:00Z')]:
            source = self.sources[key]
            bar = source[source.timestamp.eq(stamp(origin))].iloc[0]
            self.assertLess(bar.close, bar.open)

    def test_all_symbols_and_replay_cuts_ignore_later_prices(self):
        cuts = ['2026-11-16T00:00Z', '2026-11-17T04:00Z', '2026-11-19T04:00Z',
                '2026-11-19T20:00Z', '2026-11-24T04:00Z', '2026-11-24T14:00Z']
        for key, source in self.sources.items():
            for text in cuts:
                at = stamp(text)
                with self.subTest(symbol=key, cutoff=text):
                    result = reconstruct(source, at)
                    changed = source.copy()
                    changed.loc[changed.timestamp >= at, ['open', 'high', 'low', 'close']] *= 3
                    truncated = source[source.available_ts <= at]
                    for other in (reconstruct(changed, at), reconstruct(truncated, at)):
                        for field in ['lines', 'zones', 'gaps', 'supply_candidates', 'major_pivots', 'last_price']:
                            self.assertEqual(result[field], other[field], field)
                    for row in result['lines'] + result['zones'] + result['gaps'] + result['supply_candidates']:
                        self.assertLessEqual(row['known_ts'], at)
                    self.assertTrue(result['bars'].available_ts.le(at).all())

    def test_legacy_comparison_preserves_previous_geometry(self):
        source = self.sources['BTCUSDT_perpetual']
        result = reconstruct(source, self.cutoff, Model(supply_rule='compact_v1'))
        self.assertEqual([(z['low'], z['high']) for z in result['zones'] if z['side'] == 'SELL'],
                         [(65260.5, 65500.2), (68841.4, 69097.8)])
        updated = reconstruct(source, self.cutoff)
        self.assertEqual(result['lines'], updated['lines'])
        self.assertEqual([z for z in result['zones'] if z['side'] == 'BUY'],
                         [z for z in updated['zones'] if z['side'] == 'BUY'])

    def test_gap_needs_third_close_and_complete_later_fill(self):
        frame = validate_hourly(pd.DataFrame({
            'timestamp': [i * HOUR for i in range(5)],
            'open': [110, 109, 101, 103, 106], 'high': [112, 110, 104, 107, 109],
            'low': [108, 100, 100, 102, 105], 'close': [109, 101, 103, 106, 108]}))
        self.assertEqual(bearish_gaps(frame.iloc[:2]), [])
        gap = bearish_gaps(frame.iloc[:4])[0]
        self.assertEqual((gap['low'], gap['high'], gap['known_ts']), (104, 108, 3 * HOUR))
        self.assertIsNone(gap['filled_ts'])
        self.assertEqual(bearish_gaps(frame)[0]['filled_ts'], 5 * HOUR)

    def test_late_context_cannot_resurrect_a_previously_invalidated_base(self):
        values = [(110, 111, 109, 110), (110, 115, 109, 114), (114, 120, 113, 114),
                  (114, 116, 112, 114.1), (114, 114, 100, 101), (101, 105, 100, 103),
                  (103, 117, 102, 116.5)] + [(104, 105, 103, 104)] * 11
        frame = pd.DataFrame(values, columns=['open', 'high', 'low', 'close'])
        frame['timestamp'] = [i * HOUR for i in range(len(frame))]
        frame = validate_hourly(frame)
        peak = dict(side='HIGH', price=120., anchor_ts=2 * HOUR, known_ts=12 * HOUR)
        zones, _, _ = supply_v11(frame, [peak], [], Model(max_supply_width_pct=10), 104)
        self.assertEqual(zones, [])


if __name__ == '__main__':
    unittest.main()
