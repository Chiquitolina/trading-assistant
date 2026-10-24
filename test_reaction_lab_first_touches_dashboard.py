"""Offline read-only Continuous V1 journal/view tests; no Redis/PM2 needed."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import pandas as pd

from dashboard.views import reaction_lab_first_touches_view as v
from reaction_lab_first_touches_v1 import db_open, save_cycle, touch_identity


def _touch(symbol, side, status, pivot, retest, swing=10.0):
    return dict(symbol=symbol, signal=side, status=status, timeframe='15m', detector='3x3',
                pivot_timestamp=pivot, actionable_timestamp=pivot+15*60_000,
                retest_timestamp=retest, swing_price=swing, entry_price=swing,
                departure_timestamp=pivot+20*60_000, departure_price=swing+0.2,
                retest_price=swing-0.1 if side=='LONG' else swing+0.1,
                retest_close=swing-0.1 if side=='LONG' else swing+0.1)


class TestReadOnlyFirstTouches(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'first.sqlite3'
        self.ts = 1_796_000_000_000 // 60_000 * 60_000
        payloads = [
            _touch('ETHUSDT', 'LONG', 'TOUCH_FAILED', self.ts-90*60_000, self.ts),
            _touch('ETHUSDT', 'LONG', 'TOUCH_INDECISIVE', self.ts-100*60_000, self.ts),
            _touch('ETHUSDT', 'SHORT', 'REACTION', self.ts-110*60_000, self.ts+60_000),
            _touch('BTCUSDT', 'LONG', 'TOUCH_FAILED', self.ts-120*60_000, self.ts+120_000),
        ]
        self.payloads = {touch_identity(r)[0]:dict(r, legacy_event_key=touch_identity(r)[1])
                         for r in payloads}
        with db_open(self.db) as con:
            save_cycle(con, self.payloads, self.ts+130_000, self.ts, 'hash')

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_three_statuses_and_counts(self):
        s = v.get_summary(self.db)
        self.assertEqual(s['total'],4)
        self.assertEqual(s['counts'],{'REACTION':1,'TOUCH_FAILED':2,'TOUCH_INDECISIVE':1})
        self.assertEqual(s['live'],4)

    def test_symbol_universe_and_origin(self):
        self.assertEqual(v.get_summary(self.db,symbols=('ETHUSDT',))['total'],3)
        self.assertEqual(v.get_summary(self.db,symbols=())['total'],0)
        self.assertEqual(v.get_symbols(self.db,symbols=['ETHUSDT']),['ETHUSDT'])
        total, rows = v.get_page(self.db,origin='LIVE_OBSERVED',symbols=['BTCUSDT'])
        self.assertEqual(total,1)
        self.assertEqual(rows[0]['symbol'],'BTCUSDT')

    def test_same_minute_two_swings_not_collapsed(self):
        total, rows = v.get_page(self.db, symbol='ETHUSDT')
        self.assertEqual(total,3)
        same = [r for r in rows if r['retest_ts']==self.ts]
        self.assertEqual(len(same),2)
        self.assertNotEqual(same[0]['touch_key'],same[1]['touch_key'])

    def test_filter_and_pagination(self):
        total, rows=v.get_page(self.db,status='TOUCH_FAILED',side='LONG',limit=1,offset=1)
        self.assertEqual(total,2)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['status'],'TOUCH_FAILED')
        self.assertEqual(v.get_page(self.db,status='REACTION')[0],1)
        self.assertEqual(v.get_page(self.db,status='TOUCH_INDECISIVE')[0],1)

    def test_detail_uses_causal_json(self):
        k = next(k for k, row in self.payloads.items() if row['status']=='TOUCH_FAILED')
        row = v.get_touch(self.db,k)
        self.assertEqual(row['touch_key'],k)
        self.assertEqual(row['status'],'TOUCH_FAILED')
        self.assertEqual(row['swing_price'],10)
        self.assertIsNone(v.get_touch(self.db,'nonexistent'))

    def test_sql_injection_safe(self):
        self.assertEqual(v.get_page(self.db,symbol="ETHUSDT' OR 1=1 --")[0],0)
        for bad in ('not-a-state','REACTION\'; DROP TABLE first_touches; --'):
            with self.assertRaises(ValueError):
                v.get_page(self.db,status=bad)
        self.assertEqual(v.get_summary(self.db)['total'],4)

    def test_no_sqlite_mutations(self):
        with sqlite3.connect(self.db) as con:
            before = con.execute('SELECT COUNT(*) FROM cycles').fetchone()[0]
            con.execute('PRAGMA query_only=ON')
        v.get_summary(self.db)
        v.get_symbols(self.db)
        v.get_page(self.db)
        v.get_touch(self.db, next(iter(self.payloads)))
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM cycles').fetchone()[0],before)

    def test_missing_database_is_explicit(self):
        with self.assertRaises(FileNotFoundError):
            v.get_summary(Path(self.tmp.name)/'missing.sqlite3')

    def test_empty_database_returns_zeros(self):
        empty = Path(self.tmp.name)/'empty.sqlite3'
        with db_open(empty):
            pass
        self.assertEqual(v.get_summary(empty)['total'],0)
        self.assertEqual(v.get_page(empty)[0],0)


class TestChart(unittest.TestCase):
    def setUp(self):
        self.start = 1_796_000_000_000 // 900_000 * 900_000
        self.candles = pd.DataFrame([{
            'timestamp':self.start + i*900_000,
            'open':10.,'high':11.,'low':9.,'close':10.1
        } for i in range(50)])
        self.touch = _touch('ETHUSDT','LONG','TOUCH_FAILED',
                            self.start+5*900_000,
                            self.start+35*900_000)

    def test_draws_causal_touch(self):
        figure = v.make_touch_figure(self.candles,self.touch)
        self.assertIsNotNone(figure)
        self.assertGreaterEqual(len(figure.data),2)
        self.assertIn('TOUCH_FAILED',figure.layout.title.text)

    def test_no_historical_chart_invented(self):
        self.touch['retest_timestamp'] = self.start - 86_400_000
        self.assertIsNone(v.make_touch_figure(self.candles,self.touch))

    def test_last_15m_unclosed_chart_marker_without_synthetic_candle(self):
        self.touch['retest_timestamp']=self.start+50*900_000+120_000
        figure=v.make_touch_figure(self.candles,self.touch)
        self.assertIsNotNone(figure)
        self.assertEqual(len(figure.data[0].x),len(self.candles))

    def test_invalid_ohlc_is_ignored(self):
        frames = self.candles.copy()
        frames.loc[:,'low']=30.0
        self.assertIsNone(v.make_touch_figure(frames,self.touch))


class TestAppWiring(unittest.TestCase):
    def test_app_routes_are_scoped_to_continuous_and_two_views(self):
        path=Path(__file__).parent / 'dashboard' / 'app.py'
        source=path.read_text(encoding='utf-8')
        ast.parse(source)
        self.assertEqual(source.count('render_first_touches_journal('),1)
        self.assertIn('and lab_mode in ("📡 REACTION journal", "🔁 REACTION / Retests")',source)
        self.assertIn('cohort_mode in ("Continuous V1", "Todos")',source)
        self.assertIn('render_cohort_research(',source)


if __name__ == '__main__':
    unittest.main()
