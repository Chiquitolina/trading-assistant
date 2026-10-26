import csv
import tempfile
import unittest
from pathlib import Path

from hll_v22_backfill_signal_1m import (M, FIELDS, load_windows, merge_windows,
                                      download_symbol, check_file)


class BackfillV22Tests(unittest.TestCase):
    def test_merge_overlapping_windows_and_keep_distant_windows(self):
        self.assertEqual(merge_windows([(60000, 180000), (120000, 240000),
                                        (540000, 660000)]), [(60000, 240000), (540000, 660000)])

    def test_signals_window_starts_one_min_after_signal(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'signals.csv'
            with p.open('w', newline='') as fh:
                w = csv.DictWriter(fh, fieldnames=['event_id','symbol','known_closed_ts'])
                w.writeheader()
                w.writerow(dict(event_id='e1',symbol='ABCUSDT',known_closed_ts=str(30*M)))
                w.writerow(dict(event_id='e2',symbol='ABCUSDT',known_closed_ts=str(32*M)))
            windows = load_windows(p, 4)
            self.assertEqual(windows['ABCUSDT'], [(31*M, 37*M)])

    def test_download_has_sparse_between_windows_but_not_within(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            windows = [(10*M, 14*M), (30*M, 32*M)]
            def fake_fetch(sym, start, end, limit):
                self.assertEqual(sym, 'ABCUSDT')
                self.assertEqual((end-start)//M, limit)
                return [[t, '1', '2', '1', '1.5', '100', t+M-1, '200']
                        for t in range(start, end, M)]
            stats = download_symbol('ABCUSDT', windows, out, fetcher=fake_fetch, min_free_gb=0)
            self.assertEqual(stats['bars1m'], 6)
            self.assertEqual(check_file(out/'ABCUSDT_1m.csv', windows)['bars1m'], 6)
            self.assertEqual(download_symbol('ABCUSDT',windows,out,fetcher=lambda *_:self.fail('should skip'),min_free_gb=0)['bars1m'],6)

    def test_missing_kline_fails_closed_without_csv(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            def incomplete(sym, start, end, limit):
                return []
            with self.assertRaises(ValueError):
                download_symbol('ABCUSDT', [(10*M, 12*M)], out, fetcher=incomplete, min_free_gb=0)
            self.assertFalse((out/'ABCUSDT_1m.csv').exists())
            self.assertFalse(list(out.glob('*.partial')))


if __name__ == '__main__':
    unittest.main()
