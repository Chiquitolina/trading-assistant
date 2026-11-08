"""Network-free regression tests for BTC Visual Candles V1."""
import csv
import json
from pathlib import Path
import tempfile
import unittest
from hll_btc_visual_candles_v1 import MINUTE, FIELDS, normalize, plan, run, verify_csv, timestamp_ms


def synthetic_row(t):
    return [t, '100.0', '101.0', '99.0', '100.5', '7', t + MINUTE - 1, '700']


class BTCVisualCandlesV1(unittest.TestCase):
    def test_utc(self):
        self.assertEqual(timestamp_ms('2026-11-08T01:00:00Z'), 1794099600000)
        with self.assertRaises(ValueError): timestamp_ms('2026-11-08T01:00:30Z')
        with self.assertRaises(ValueError): timestamp_ms('2026-11-08T01:00:00')

    def test_reject_broken_candles(self):
        t = 1_800_000
        self.assertEqual(normalize(synthetic_row(t), t)[0], t)
        with self.assertRaises(ValueError): normalize(synthetic_row(t), t + MINUTE)
        broken = synthetic_row(t)
        broken[2] = '98'
        with self.assertRaises(ValueError): normalize(broken, t)

    def test_plan_no_writes(self):
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / 'does_not_exist'
            data = plan(60_000, 180_000, out)
            self.assertEqual(data['expected_bars'], 2)
            self.assertFalse(out.exists())

    def test_frozen_run_and_hash_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / 'output'
            calls = []
            def fetcher(start, end):
                calls.append((start, end))
                return [normalize(synthetic_row(t), t) for t in range(start, end, MINUTE)]
            m = run(60_000, 60_000 + 1001 * MINUTE, out, fetcher=fetcher)
            self.assertEqual(len(calls), 2)
            self.assertEqual(m['bars'], 1001)
            self.assertEqual(verify_csv(out / 'BTCUSDT_1m.csv', 60_000, 60_000 + 1001 * MINUTE)['bars'], 1001)
            self.assertEqual(json.loads((out / 'manifest.json').read_text())['files_sha256'], m['files_sha256'])
            with self.assertRaises(FileExistsError): run(60_000, 120_000, out, fetcher=fetcher)

    def test_gap_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / 'output'
            def corrupt_fetch(start, end):
                return [normalize(synthetic_row(start + MINUTE), start + MINUTE)]
            with self.assertRaises(ValueError): run(60_000, 120_000, out, fetcher=corrupt_fetch)
            self.assertFalse((out / 'manifest.json').exists())
            self.assertFalse((out / 'BTCUSDT_1m.csv').exists())

    def test_verify_gap_and_duplicates(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / 'bad.csv'
            with p.open('w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(FIELDS)
                writer.writerow((60_000, '100', '101', '99', '100', '2', '200'))
                writer.writerow((60_000, '100', '101', '99', '100', '2', '200'))
            with self.assertRaises(ValueError): verify_csv(p, 60_000, 180_000)


if __name__ == '__main__':
    unittest.main()
