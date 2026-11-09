"""Tests de congelamiento BTC offline. No requieren red ni datos del VPS."""
import csv
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from btc_multiswing_60d_freeze_v1 import (
    Source, FIELDS, MINUTE_MS, TF30_MS, aggregate_30m, check_row, compare_hll,
    sha256, validate_sources, write_csv, build,
)


class BTCMultiswingFreezeTests(unittest.TestCase):
    def setUp(self):
        self.d = TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.root = Path(self.d.name)
        self.base = 1_800_000_000_000 - 1_800_000_000_000 % TF30_MS
        self.rows = []
        for i in range(60):
            x = 100 + i
            self.rows.append({
                'timestamp': str(self.base + MINUTE_MS * i),
                'open': str(x), 'high': str(x + 2),
                'low': str(x - 2), 'close': str(x + 1),
                'volume': '1.5', 'quoteVolume': '10',
            })
        self.sources = []
        for j in range(5):
            path = self.root / f'source_{j}.csv'
            write_csv(path, self.rows[j * 12:(j + 1) * 12])
            self.sources.append(Source(str(j), path, self.base + j * 12 * MINUTE_MS,
                                       self.base + (j + 1) * 12 * MINUTE_MS, sha256(path)))

    def test_all_segments_and_boundary(self):
        got, proof = validate_sources(self.sources, self.base, self.base + 60 * MINUTE_MS)
        self.assertEqual(len(got), 60)
        self.assertEqual(len(proof), 5)
        self.assertEqual(got[12]['timestamp'], self.rows[12]['timestamp'])
        self.assertEqual(got[48]['timestamp'], self.rows[48]['timestamp'])

    def test_30m_aggregate(self):
        d = aggregate_30m(self.rows)
        self.assertEqual(len(d), 2)
        self.assertEqual(d[0]['open'], '100')
        self.assertEqual(d[0]['close'], '130')
        self.assertEqual(d[0]['high'], '131')
        self.assertEqual(d[0]['low'], '98')
        self.assertEqual(d[0]['volume'], '45.0')

    def test_gap_fails_closed(self):
        write_csv(self.root / 'gapped.csv', self.rows[1:12])
        bad = list(self.sources)
        bad[0] = Source('bad', self.root / 'gapped.csv', bad[0].start, bad[0].end)
        with self.assertRaisesRegex(ValueError, 'gap/duplicado'):
            validate_sources(bad, self.base, self.base + 60 * MINUTE_MS)

    def test_source_boundary_gap_fails(self):
        bad = list(self.sources)
        s = bad[2]
        bad[2] = Source(s.label, s.path, s.start + MINUTE_MS, s.end)
        with self.assertRaisesRegex(ValueError, 'Hueco/solapamiento'):
            validate_sources(bad, self.base, self.base + 60 * MINUTE_MS)

    def test_modified_frozen_hash_fails(self):
        path = self.sources[0].path
        with path.open('a') as f:
            f.write('\n')
        with self.assertRaisesRegex(ValueError, 'Hash'):
            validate_sources(self.sources, self.base, self.base + 60 * MINUTE_MS)

    def test_invalid_ohlc_fails(self):
        bad = dict(self.rows[0], high='99')
        with self.assertRaisesRegex(ValueError, 'OHLC'):
            check_row(bad, self.base, 'test')

    def test_not_finite_fails(self):
        bad = dict(self.rows[0], volume='NaN')
        with self.assertRaisesRegex(ValueError, 'rango'):
            check_row(bad, self.base, 'test')

    def test_bad_30m_alignment_fails(self):
        with self.assertRaises(ValueError):
            aggregate_30m(self.rows[1:31])

    def test_hll_ohlc_parity_and_mismatch(self):
        p = self.root / 'hll.csv'
        rows = aggregate_30m(self.rows)
        write_csv(p, rows)
        self.assertEqual(compare_hll(p, rows)['status'], 'PARITY_OK')
        rows[0]['close'] = '999'
        self.assertEqual(compare_hll(p, rows)['status'], 'PARITY_NOT_ESTABLISHED')

    def test_existing_output_not_overwritten(self):
        out = self.root / 'already_frozen'
        out.mkdir()
        marker = out / 'preserve.txt'
        marker.write_text('original')
        with self.assertRaises(FileExistsError):
            build(self.sources, self.base + 30 * MINUTE_MS,
                  self.base + (60 * 24 * 60 + 30) * MINUTE_MS, out)
        self.assertEqual(marker.read_text(), 'original')

    def test_short_source_is_rejected(self):
        path = self.root / 'short.csv'
        write_csv(path, self.rows[:11])
        bad = list(self.sources)
        s = bad[0]
        bad[0] = Source(s.label, path, s.start, s.end)
        with self.assertRaisesRegex(ValueError, '11 velas, esperadas 12'):
            validate_sources(bad, self.base, self.base + 60 * MINUTE_MS)

    def test_hll_missing_not_assumed_parity(self):
        rows = aggregate_30m(self.rows)
        self.assertEqual(compare_hll(self.root / 'missing.csv', rows)['status'], 'UNAVAILABLE')


if __name__ == '__main__':
    unittest.main()
