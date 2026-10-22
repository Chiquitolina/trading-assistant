"""Pure/offline selection tests; no REST, no LIVE systems."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import reaction_compression_history_backfill as hb


class TargetedHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        db = self.root / hb.ROOT_REL / hb.CTX_NAME
        db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db) as con:
            con.execute('CREATE TABLE events (event_key TEXT, symbol TEXT, retest_ts INTEGER)')
            con.executemany('INSERT INTO events VALUES (?,?,?)', [
                ('first-z', 'ZZZUSDT', 10_000_000_000),
                ('second-z', 'ZZZUSDT', 10_000_060_000),
                ('third-a', 'AAAUSDT', 10_000_120_000),
                ('fourth-b', 'BBBUSDT', 10_000_180_000),
            ])

    def tearDown(self):
        self.tmp.cleanup()

    def test_selected_events_follow_lineage_chronology_not_symbol_sort(self):
        selected = hb.tasks(self.root, max_events=2)
        self.assertEqual([x[0] for x in selected], ['ZZZUSDT'])
        self.assertEqual(selected[0][2], 10_000_060_000)
        self.assertEqual(selected[0][1], 10_000_000_000 - 180 * 60 * hb.M)

    def test_full_selection_preserved_and_sorted(self):
        selected = hb.tasks(self.root)
        self.assertEqual([x[0] for x in selected], ['AAAUSDT', 'BBBUSDT', 'ZZZUSDT'])

    def test_incremental_pilot_will_extend_to_selected_symbols_only(self):
        one = hb.tasks(self.root, max_events=1)
        three = hb.tasks(self.root, max_events=3)
        self.assertEqual(len(one), 1)
        self.assertEqual(len(three), 2)
        self.assertEqual([t[0] for t in three], ['AAAUSDT', 'ZZZUSDT'])

    def test_research_cache_separate_from_frozen_context(self):
        cache = self.root / hb.ROOT_REL / hb.HISTORY_NAME
        context = self.root / hb.ROOT_REL / hb.CTX_NAME
        self.assertNotEqual(context, cache)
        with hb.own_db(cache) as con:
            n=con.execute('SELECT COUNT(*) FROM candles').fetchone()[0]
        self.assertEqual(n,0)
        self.assertTrue(context.is_file())


if __name__ == '__main__':
    unittest.main()
