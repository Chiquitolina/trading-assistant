"""Run: python3 -m unittest -v test_reaction_lab_cohort_view.py"""
from pathlib import Path
import csv
import json
import sqlite3
import tempfile
import unittest

from reaction_lab_cohort_view import load_reaction_cohort


class CohortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        r = self.root / 'reports/reaction_lab'
        r.mkdir(parents=True)
        self.db = r / 'scanner_events_v1.sqlite3'
        with sqlite3.connect(self.db) as con:
            con.executescript('''
                CREATE TABLE cycles(started_ms INTEGER, error TEXT);
                CREATE TABLE events(event_key TEXT, symbol TEXT, side TEXT,
                  retest_ts INTEGER, known_ts INTEGER,
                  first_seen_ms INTEGER, observation_type TEXT);
            ''')
            con.executemany('INSERT INTO cycles VALUES (?,?)', [
                (2000000, 'not successful'), (3000000, None), (4000000, None),
            ])
            con.executemany('INSERT INTO events VALUES (?,?,?,?,?,?,?)', [
                ('BTCUSDT|LONG|2760000', 'BTCUSDT', 'LONG', 2760000, 2820000, 3100000, 'RECOVERED_WINDOW'),
                ('ETHUSDT|SHORT|3000000', 'ETHUSDT', 'SHORT', 3000000, 3060000, 3130000, 'LIVE_OBSERVED'),
                ('SOLUSDT|LONG|3100000', 'SOLUSDT', 'LONG', 3100000, 3160000, 3500000, 'RECOVERED_WINDOW'),
            ])
        self.legacy = self.root / 'candidate_v2_monitor.csv'
        with self.legacy.open('w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=['symbol', 'signal', 'status', 'retest_timestamp'])
            writer.writeheader()
            writer.writerows([
                {'symbol': 'BTCUSDT', 'signal': 'LONG', 'status': 'REACTION', 'retest_timestamp': '2760000'},
                {'symbol': 'DOGEUSDT', 'signal': 'SHORT', 'status': 'REACTION', 'retest_timestamp': '2600000'},
                {'symbol': 'ETHUSDT', 'signal': 'SHORT', 'status': 'REACTION', 'retest_timestamp': '3000000'},
            ])
        paths = r / 'auto_paths_v1.sqlite3'
        with sqlite3.connect(paths) as con:
            con.execute('CREATE TABLE events(event_key TEXT, state TEXT, count INTEGER)')
            con.execute('INSERT INTO events VALUES (?,?,?)', ('ETHUSDT|SHORT|3000000', 'ACTIVE', 25))
        self.paths = paths

    def test_prospective_excludes_startup_and_legacy(self):
        result = load_reaction_cohort(self.root, 'Continuous V1')
        self.assertEqual(result['cutoff_ms'], 3000000)
        self.assertEqual(result['shown'], 2)
        self.assertEqual(result['continuous_live_count'], 1)
        self.assertEqual(result['continuous_recovered_count'], 1)
        self.assertTrue(all(r['known_ts'] >= 3000000 for r in result['rows']))
        self.assertEqual(result['rows'][1]['path_bars'], 25)
        self.assertEqual(result['scanner_startup_recovered_count'], 1)

    def test_legacy_only_before_start(self):
        result = load_reaction_cohort(self.root, 'Legacy')
        self.assertEqual(result['shown'], 2)
        self.assertEqual({r['symbol'] for r in result['rows']}, {'BTCUSDT', 'DOGEUSDT'})
        self.assertEqual(result['legacy_monitor_count'], 2)
        btc = next(r for r in result['rows'] if r['symbol'] == 'BTCUSDT')
        self.assertIn('startup_recovered', btc['sources'])
        self.assertEqual(btc['cohort'], 'Legacy')

    def test_todos_deduplicates_and_marks_cohort(self):
        result = load_reaction_cohort(self.root, 'Todos')
        self.assertEqual(result['shown'], 4)
        self.assertEqual(len({r['event_key'] for r in result['rows']}), 4)
        self.assertEqual([r['cohort'] for r in result['rows']].count('Continuous V1'), 2)

    def test_manifest_later_cutoff(self):
        manifest = self.root / 'reports/reaction_lab/continuous_v1_cohort.json'
        manifest.write_text(json.dumps({'start_ms': 3140000}))
        result = load_reaction_cohort(self.root, 'Continuous V1')
        self.assertEqual(result['cutoff_source'], 'FROZEN_MANIFEST')
        self.assertEqual(result['shown'], 1)
        self.assertEqual(result['rows'][0]['symbol'], 'SOLUSDT')

    def test_no_scanner_means_empty_prospective(self):
        self.db.unlink()
        result = load_reaction_cohort(self.root, 'Continuous V1')
        self.assertEqual(result['shown'], 0)
        self.assertIsNone(result['cutoff_ms'])
        self.assertTrue(result['errors'])

    def test_read_only_does_not_change_databases(self):
        before = {p: p.stat().st_mtime_ns for p in [self.db, self.paths, self.legacy]}
        for mode in ('Continuous V1', 'Legacy', 'Todos'):
            load_reaction_cohort(self.root, mode)
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in before})

    def test_invalid_mode(self):
        with self.assertRaises(ValueError):
            load_reaction_cohort(self.root, 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()
