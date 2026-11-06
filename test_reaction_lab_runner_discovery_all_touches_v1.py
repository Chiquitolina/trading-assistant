import csv
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zlib

from reaction_lab_runner_discovery_all_touches_v1 import (
    BAR, MINUTE, decode_checked, match_auto, outcomes_for_path,
    build_dataset, iter_events,
)


def candles(*, start=60_000, n=360, o=100, high=100.2, low=99.8):
    return [(start+i*MINUTE,o,high,low,o) for i in range(n)]


def blob(bars):
    return zlib.compress(b''.join(BAR.pack(*b) for b in bars),6)


class CalculationTests(unittest.TestCase):
    def test_hit_even_after_sl_is_not_tp_first(self):
        bars=[(60_000,100,100,98,99), (120_000,99,103,98.5,102)]
        hits,dec,_,_=outcomes_for_path(bars,'LONG',100)
        self.assertEqual(hits[2],2)
        self.assertEqual(dec[2],('SL_FIRST',1))
        self.assertIsNone(hits[5])

    def test_opposite_direction_hits(self):
        bars=[(60_000,100,100.5,97,97)]
        hits,dec,_,_=outcomes_for_path(bars,'SHORT',100)
        self.assertEqual(hits[2],1)
        self.assertEqual(dec[2],('TP_FIRST',1))

    def test_same_bar_ambiguous(self):
        bars=[(60_000,100,103,98,100)]
        hits,dec,_,_=outcomes_for_path(bars,'LONG',100)
        self.assertEqual(hits[2],1)
        self.assertEqual(dec[2],('AMBIGUOUS',1))

    def test_fifteen_min_is_valid_even_without_360(self):
        bars=candles(n=15)
        self.assertEqual(len(decode_checked(blob(bars),15,60_000)),15)

    def test_gapped_bars_refused(self):
        bars=candles(n=5)
        bars[2]=(bars[2][0]+MINUTE,*bars[2][1:])
        with self.assertRaises(ValueError):
            decode_checked(blob(bars),5,60_000)

    def test_mismatched_ohlc_refused(self):
        bars=candles(n=3)
        bars[0]=(60_000,100,99,98,100)
        with self.assertRaises(ValueError):
            decode_checked(blob(bars),3,60_000)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project=Path(self.tmp.name)
        self.root=self.project/'reports/reaction_lab'
        self.root.mkdir(parents=True)
        self.ft=self.root/'first_touch_paths_v2.sqlite3'
        self.ap=self.root/'auto_paths_v1.sqlite3'
        self.fc=sqlite3.connect(self.ft)
        self.ac=sqlite3.connect(self.ap)
        self.addCleanup(self.fc.close)
        self.addCleanup(self.ac.close)
        self.fc.execute('''CREATE TABLE paths(touch_key TEXT PRIMARY KEY, symbol TEXT,
          side TEXT, status TEXT, retest_ts INTEGER,entry_ts INTEGER,first_seen_ms INTEGER,
          state TEXT,count INTEGER,path_blob BLOB,retest_close REAL)''')
        self.ac.execute('''CREATE TABLE events(event_key TEXT PRIMARY KEY, symbol TEXT,
          side TEXT,retest_ts INTEGER,entry_ts INTEGER,count INTEGER,path_blob BLOB)''')
        self.fc.commit(); self.ac.commit()

    def ft_event(self,key,symbol,side,status,ts=0,bars=None,ref=100):
        bars=bars or []
        self.fc.execute('INSERT INTO paths VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (key,symbol,side,status,ts,ts+MINUTE,ts+MINUTE,'COMPLETE' if len(bars)==360 else 'STALE_BEFORE_COLLECTOR',
                         len(bars),blob(bars) if bars else None,ref))
        self.fc.commit()

    def auto_event(self,key,symbol,side,ts,bars):
        self.ac.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?)',
                        (key,symbol,side,ts,ts+MINUTE,len(bars),blob(bars)))
        self.ac.commit()

    def test_three_statuses_with_auto_fallback_and_missing(self):
        self.ft_event('A','BTCUSDT','LONG','REACTION',bars=candles())
        self.ft_event('B','ETHUSDT','SHORT','TOUCH_INDECISIVE',bars=[])
        self.auto_event('AUTO_B','ETHUSDT','SHORT',0,candles(n=360,high=100.2,low=97.0))
        self.ft_event('C','SOLUSDT','LONG','TOUCH_FAILED',bars=[])
        rows=list(iter_events(self.project))
        self.assertEqual(len(rows),3)
        mapped={r['touch_key']:r for r in rows}
        self.assertEqual(mapped['A']['path_source'],'FIRST_TOUCH_V2')
        self.assertEqual(mapped['B']['path_source'],'AUTO_IDENTITY_VERIFIED')
        self.assertEqual(mapped['B']['open_time_to_run_2pct'],1)
        self.assertEqual(mapped['C']['path_quality'],'NO_CONTIGUOUS_PATH')
        report=build_dataset(self.project)
        self.assertEqual(report['counts']['events'],3)
        self.assertEqual(report['counts']['evaluable_180'],2)
        self.assertEqual(report['counts']['evaluable_180_TOUCH_INDECISIVE'],1)
        p=self.project/'reports/reaction_lab/runner_discovery_all_touches_v1/all_first_touches.csv'
        with p.open() as f:
            self.assertEqual(len(list(csv.DictReader(f))),3)
        self.assertFalse((self.root/'first_touch_paths_v2.sqlite3-wal').exists())

    def test_auto_prefix_parity_conflict(self):
        first=candles(n=2)
        last=candles(n=360)
        last[0]=(60_000,101,102,100,101)
        self.ft_event('A','BTCUSDT','LONG','TOUCH_FAILED',bars=first)
        self.auto_event('AUTO','BTCUSDT','LONG',0,last)
        r=next(iter_events(self.project))
        self.assertEqual(r['path_quality'],'SOURCE_REVIEW_REQUIRED')
        self.assertEqual(r['path_count'],2)
        self.assertEqual(r['path_source'],'FT_ONLY_AUTO_OHLC_CONFLICT')

    def test_multiple_auto_identities_excluded(self):
        self.ft_event('A','BTCUSDT','LONG','TOUCH_FAILED')
        self.auto_event('AUTO1','BTCUSDT','LONG',0,candles())
        self.auto_event('AUTO2','BTCUSDT','LONG',0,candles())
        r=next(iter_events(self.project))
        self.assertEqual(r['path_count'],0)
        self.assertEqual(r['path_source'],'AMBIGUOUS_AUTO')

    def test_no_auto_db_still_evaluates(self):
        self.ac.close()
        self.ap.unlink()
        self.ft_event('A','BTCUSDT','LONG','REACTION',bars=candles())
        rows=list(iter_events(self.project))
        self.assertEqual(rows[0]['path_count'],360)

if __name__=='__main__':unittest.main()
