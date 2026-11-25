"""Birth, terminal-event, time-cut and historical-parity checks for band journals."""
from pathlib import Path
from types import SimpleNamespace
import unittest

import pandas as pd
from pandas.testing import assert_frame_equal

from finora_chart_reconstruction_v1 import HOUR, reconstruct, stamp, validate_hourly
from finora_band_lifecycle_v12 import Journal, at_cutoff, band_id, detector_snapshot, read_history, read_journal, replay

PROJECT = Path(__file__).resolve().parent


def bar(hour, low, high, close):
    return SimpleNamespace(timestamp=hour * HOUR, available_ts=(hour + 1) * HOUR,
                           open=close, low=low, high=high, close=close)


def zone(side='SELL', family='BAND'):
    return dict(side=side, family=family, low=100., high=105., entry_edge=105. if side == 'BUY' else 100.,
                origin_ts=0, formed_ts=HOUR, known_ts=2 * HOUR, rule='Synthetic base')


class JournalTransitions(unittest.TestCase):
    def test_first_detection_survives_deselection_and_later_context(self):
        j = Journal(0)
        z = zone()
        j.tick(bar(1, 90, 99, 95), [z])
        j.tick(bar(2, 99, 104, 103), [])
        j.tick(bar(3, 101, 106, 104), [dict(z, known_ts=4 * HOUR)])
        record = j.bands[band_id(z)]
        self.assertEqual(record['first_detected_ts'], 2 * HOUR)
        self.assertEqual(record['first_context_ts'], 2 * HOUR)
        self.assertIn(band_id(z), j.active)
        self.assertEqual([e['kind'] for e in j.events],
                         ['DETECTED', 'CONTACT', 'NOT_SELECTED', 'CONTEXT_UPDATED', 'SELECTED'])

    def test_birth_candle_and_earlier_wicks_are_not_later_contacts(self):
        j = Journal(0)
        z = zone()
        j.tick(bar(1, 99, 110, 95), [z])
        self.assertEqual([e['kind'] for e in j.events], ['DETECTED'])
        j.tick(bar(2, 99, 101, 99), [])
        self.assertEqual(j.events[1]['available_ts'], 3 * HOUR)
        self.assertEqual(j.events[1]['kind'], 'CONTACT')

    def test_sell_wick_and_edge_close_survive_far_close_ends_once(self):
        j = Journal(0)
        z = zone()
        j.tick(bar(1, 90, 99, 95), [z])
        j.tick(bar(2, 99, 110, 105), [])
        self.assertIn(band_id(z), j.active)
        j.tick(bar(3, 101, 111, 106), [])
        j.tick(bar(4, 102, 115, 110), [z])
        self.assertNotIn(band_id(z), j.active)
        self.assertEqual([e['kind'] for e in j.events].count('INVALIDATED'), 1)
        self.assertEqual([e['kind'] for e in j.events].count('DETECTED'), 1)

    def test_buy_is_symmetric_and_invalidating_gap_needs_no_contact(self):
        j = Journal(0)
        z = zone('BUY')
        j.tick(bar(1, 106, 110, 109), [z])
        j.tick(bar(2, 90, 110, 100), [])
        self.assertIn(band_id(z), j.active)
        j.tick(bar(3, 95, 99, 98), [])
        self.assertEqual([e['available_ts'] for e in j.events if e['kind'] == 'INVALIDATED'], [4 * HOUR])
        gap = Journal(0)
        gap.tick(bar(1, 90, 99, 95), [zone()])
        gap.tick(bar(2, 106, 110, 109), [])
        self.assertEqual([e['kind'] for e in gap.events], ['DETECTED', 'INVALIDATED', 'NOT_SELECTED'])

    def test_consecutive_touch_bars_are_one_episode(self):
        j = Journal(0)
        z = zone()
        j.tick(bar(1, 90, 99, 95), [z])
        for hour, values in enumerate([(99, 101, 99), (100, 106, 103), (90, 99, 95), (99, 101, 99)], 2):
            j.tick(bar(hour, *values), [])
        self.assertEqual([e['available_ts'] for e in j.events if e['kind'] == 'CONTACT'], [3 * HOUR, 6 * HOUR])

    def test_gray_gap_ends_on_full_wick_fill(self):
        j = Journal(0)
        z = zone(family='GAP')
        j.tick(bar(1, 90, 99, 95), [z])
        j.tick(bar(2, 99, 104, 101), [])
        self.assertIn(band_id(z), j.active)
        j.tick(bar(3, 99, 105, 101), [])
        self.assertNotIn(band_id(z), j.active)
        self.assertEqual([e['available_ts'] for e in j.events if e['kind'] == 'FILLED'], [4 * HOUR])
        self.assertFalse(any(e['kind'] == 'INVALIDATED' for e in j.events))

    def test_projection_hides_future_contacts_and_terminal_events(self):
        j = Journal(0)
        z = zone()
        j.tick(bar(1, 90, 99, 95), [z])
        j.tick(bar(2, 99, 104, 103), [])
        j.tick(bar(3, 101, 111, 106), [])
        bands, events = j.frames()
        source = validate_hourly(pd.DataFrame([vars(bar(i, 90, 111, 95)) for i in range(4)]))
        early = at_cutoff(source, bands, events, 2 * HOUR)
        self.assertIsNone(early['bands'][0]['first_contact_ts'])
        self.assertIsNone(early['bands'][0]['invalidated_ts'])
        self.assertEqual(early['events'].kind.tolist(), ['DETECTED'])
        ended = at_cutoff(source, bands, events, 4 * HOUR)
        self.assertEqual(ended['bands'][0]['invalidated_ts'], 4 * HOUR)
        self.assertEqual(ended['bands'][0]['status'], 'INVALIDATED')

    def test_clock_gaps_are_rejected(self):
        j = Journal(0)
        j.tick(bar(1, 90, 99, 95), [zone()])
        with self.assertRaises(ValueError):
            j.tick(bar(3, 90, 99, 95), [])


class HistoricalReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = ['BTCUSDT_perpetual', 'NVDAUSDT_perpetual', 'SHELLUSDT_spot']
        cls.sources = {key: read_history(PROJECT, key)[0] for key in cls.keys}

    def test_trimmed_detector_matches_full_prefix_on_all_markets(self):
        cuts = ['2026-09-25T14:00Z', '2026-10-15T04:00Z', '2026-11-19T20:00Z', '2026-11-24T14:00Z']
        for key, source in self.sources.items():
            for date in cuts:
                at = stamp(date)
                with self.subTest(symbol=key, cutoff=date):
                    full = reconstruct(source, at)
                    fast = detector_snapshot(source, at)
                    for name in ['lines', 'zones', 'gaps', 'supply_candidates']:
                        self.assertEqual(full[name], fast[name], name)

    def test_appended_and_changed_future_preserve_old_births_and_events(self):
        source = self.sources['BTCUSDT_perpetual'].head(140).copy()
        cutoff = int(source.available_ts.iloc[99])
        short_bands, short_events = replay(source.head(100))
        full_bands, full_events = replay(source)
        changed = source.copy()
        changed.loc[changed.timestamp >= cutoff, ['open', 'high', 'low', 'close']] *= 3
        altered_bands, altered_events = replay(changed)
        for bands, events in [(full_bands, full_events), (altered_bands, altered_events)]:
            assert_frame_equal(short_bands.reset_index(drop=True),
                bands[bands.first_detected_ts <= cutoff].reset_index(drop=True))
            assert_frame_equal(short_events.reset_index(drop=True),
                events[events.available_ts <= cutoff].reset_index(drop=True))

    def test_shell_first_detection_is_fixed_across_context_updates(self):
        source, bands, events, _ = read_journal(PROJECT, 'SHELLUSDT_spot')
        target = bands[(bands.family == 'BAND') & bands.low.eq(.02682) & bands.high.eq(.02729)].iloc[0]
        self.assertEqual(target.first_detected_ts, stamp('2026-11-19T04:00Z'))
        for date in ['2026-11-19T04:00Z', '2026-11-19T20:00Z', '2026-11-24T14:00Z']:
            current = at_cutoff(source, bands, events, stamp(date))
            row = next(z for z in current['bands'] if z['band_id'] == target.band_id)
            self.assertEqual(row['first_detected_ts'], target.first_detected_ts)
        updates = events[(events.band_id == target.band_id) & (events.kind == 'CONTEXT_UPDATED')]
        self.assertIn(stamp('2026-11-19T20:00Z'), updates.available_ts.tolist())

    def test_visible_sixty_days_do_not_recompute_lifetimes(self):
        for key in self.keys:
            source, bands, events, _ = read_journal(PROJECT, key)
            at = stamp('2026-11-24T14:00Z')
            short = at_cutoff(source, bands, events, at, 12)
            long = at_cutoff(source, bands, events, at, 60)
            self.assertEqual(short['bands'], long['bands'])
            self.assertEqual(len(long['bars']), 60 * 24)
            self.assertTrue(long['events'].available_ts.le(at).all())

    def test_historical_terminal_events_obey_price_and_availability_rules(self):
        for key in self.keys:
            _, bands, events, _ = read_journal(PROJECT, key)
            indexed = bands.set_index('band_id')
            for event in events[events.kind.isin(['CONTACT', 'INVALIDATED', 'FILLED'])].itertuples(index=False):
                z = indexed.loc[event.band_id]
                self.assertGreaterEqual(event.bar_open_ts, z.first_detected_ts)
                if event.kind == 'CONTACT':
                    self.assertGreaterEqual(event.high, z.low)
                    self.assertLessEqual(event.low, z.high)
                elif event.kind == 'FILLED':
                    self.assertEqual(z.family, 'GAP')
                    self.assertGreaterEqual(event.high, z.high)
                else:
                    self.assertEqual(z.family, 'BAND')
                    self.assertTrue(event.close < z.low if z.side == 'BUY' else event.close > z.high)


if __name__ == '__main__':
    unittest.main()
