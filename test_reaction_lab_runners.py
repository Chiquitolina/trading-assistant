"""Tests for permanent Continuous V1 >1% runner archive."""
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from reaction_lab_auto_paths import encode_path
from reaction_lab_runners import analyze_path,run_once,status,THRESHOLD
from reaction_lab_unified_adapter import read_persisted_runners

T=1791900000000
K='AAAUSDT|LONG|'+str(T)
K2='BBBUSDT|SHORT|'+str(T)


def candles(retest, values):
    return [(retest+(i+1)*60000,*v) for i,v in enumerate(values)]


class RunnerMathTests(unittest.TestCase):
    def test_long_cross_and_peak_timestamp(self):
        m=analyze_path(candles(T,[(10,10.05,9.8,10),(10,10.25,9.6,10)]),
                       retest_ts=T,retest_close=10,side='LONG')
        self.assertAlmostEqual(m['peak_run_pct'],2.5)
        self.assertEqual(m['threshold_cross_ts'],T+120000)
        self.assertEqual(m['peak_ts'],T+120000)
        self.assertAlmostEqual(m['max_adverse_pct'],4)
    def test_short_cross_and_peak(self):
        m=analyze_path(candles(T,[(10,10.1,9.85,10),(10,10.5,9.7,10)]),
                       retest_ts=T,retest_close=10,side='SHORT')
        self.assertAlmostEqual(m['peak_run_pct'],3)
        self.assertAlmostEqual(m['max_adverse_pct'],5)
    def test_sub_threshold_is_not_runner(self):
        self.assertIsNone(analyze_path(candles(T,[(10,10.03,9.9,10)]),
                                       retest_ts=T,retest_close=10,side='LONG'))
    def test_path_gap_stops_run(self):
        bars=[(T+60000,10,10.02,9.95,10), (T+180000,10,100,9,10)]
        self.assertIsNone(analyze_path(bars,retest_ts=T,retest_close=10,side='LONG'))


class RunnerArchiveTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.root=Path(self.t.name)
        self.dir=self.root/'reports/reaction_lab'
        self.dir.mkdir(parents=True)
        with sqlite3.connect(self.dir/'scanner_events_v1.sqlite3') as con:
            con.executescript('''CREATE TABLE cycles (started_ms INTEGER,error TEXT);
                 CREATE TABLE events(event_key TEXT PRIMARY KEY,symbol TEXT,side TEXT,retest_ts INTEGER,
                 known_ts INTEGER,observation_type TEXT,source_hash TEXT,causal_json TEXT);''')
            con.execute('INSERT INTO cycles VALUES (?,NULL)',(T-1_000_000,))
            for k,s,side,baseline in ((K,'AAAUSDT','LONG',10),(K2,'BBBUSDT','SHORT',20)):
                con.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)',(
                    k,s,side,T,T+60000,'LIVE_OBSERVED','hash',json.dumps({'retest_close':baseline,'pivot_price':baseline-1})))
        with sqlite3.connect(self.dir/'auto_paths_v1.sqlite3') as con:
            con.execute('''CREATE TABLE events(event_key TEXT PRIMARY KEY,path_blob BLOB,
                          count INTEGER,state TEXT,updated_utc_ms INTEGER)''')
            for k in (K,K2):
                con.execute('INSERT INTO events VALUES (?,NULL,0,?,?)',(k,'WAITING',100))
        with sqlite3.connect(self.dir/'causal_context_v1.sqlite3') as con:
            con.executescript('''CREATE TABLE contexts(event_key TEXT PRIMARY KEY,status TEXT,
                 context_json TEXT,notes_json TEXT,candles_json TEXT);
                 CREATE TABLE followups(event_key TEXT PRIMARY KEY,path_state TEXT,followup_json TEXT);
                 CREATE TABLE v1_regime_events(event_key TEXT PRIMARY KEY,status TEXT,metrics_json TEXT);''')
            con.execute('INSERT INTO contexts VALUES (?,?,?,?,?)',(
                K,'READY',json.dumps({'rsi14_15m':30}),json.dumps([]),json.dumps({'15m':[]})))
            con.execute('INSERT INTO v1_regime_events VALUES (?,?,?)',(
                K,'READY',json.dumps({'sector_return_pct_4h':0.5})))
            con.execute('INSERT INTO followups VALUES (?,?,?)',(
                K,'ACTIVE',json.dumps({'mfe_180m_pct':2.0})))
        self.dest=self.dir/'runners_v1.sqlite3'
    def update_path(self,key,bars,timestamp,state='ACTIVE'):
        with sqlite3.connect(self.dir/'auto_paths_v1.sqlite3') as con:
            con.execute('''UPDATE events SET path_blob=?,count=?,state=?,updated_utc_ms=?
                       WHERE event_key=?''',(encode_path(bars),len(bars),state,timestamp,key))
    def test_archive_threshold_not_batch_and_monotone_and_independent(self):
        self.update_path(K,candles(T,[(10,10.09,9.9,10)]),1000)
        first=run_once(self.root,self.dest,1200)
        self.assertEqual(first['new_runners'],0)
        self.update_path(K,candles(T,[(10,10.09,9.9,10),(10,10.2,9.8,10)]),2000)
        second=run_once(self.root,self.dest,2400)
        self.assertEqual(second['new_runners'],1)
        self.assertEqual(second['runner_total'],1)
        self.assertEqual(status(self.dest)['runners'],1)
        with sqlite3.connect(self.dest) as con:
            row=con.execute('SELECT first_qualified_at_ms,peak_run_pct,context_json,v1_regime_json,path_blob FROM runners').fetchone()
            self.assertEqual(row[0],2400)
            self.assertAlmostEqual(row[1],2)
            self.assertEqual(json.loads(row[2])['rsi14_15m'],30)
            self.assertEqual(json.loads(row[3])['sector_return_pct_4h'],0.5)
            self.assertEqual(len(__import__('reaction_lab_auto_paths').decode_path(row[4])),2)
        self.update_path(K,candles(T,[(10,10.09,9.9,10),(10,10.2,9.8,10),(10,10.8,9.5,10)]),3000,'COMPLETE')
        third=run_once(self.root,self.dest,3200)
        self.assertEqual(third['runners_updated'],1)
        with sqlite3.connect(self.dest) as con:
            found=con.execute('SELECT first_qualified_at_ms,peak_run_pct,threshold_cross_ts,path_count FROM runners').fetchone()
        self.assertEqual(found[0],2400)
        self.assertAlmostEqual(found[1],8)
        self.assertEqual(found[2],T+120000)
        self.assertEqual(found[3],3)
        again=run_once(self.root,self.dest,5000)
        self.assertEqual(again['new_runners'],0)
        self.assertEqual(again['runners_updated'],0)
        self.assertEqual(len(read_persisted_runners(self.root)),1)
        # Source DB untouched by worker.
        with sqlite3.connect(self.dir/'auto_paths_v1.sqlite3') as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM events').fetchone()[0],2)
    def test_short_and_no_v1_filter(self):
        self.update_path(K2,candles(T,[(20,20.1,19.6,20)]),1000)
        out=run_once(self.root,self.dest,1500)
        self.assertEqual(out['new_runners'],1)
        with sqlite3.connect(self.dest) as con:
            self.assertEqual(con.execute('SELECT side FROM runners').fetchone()[0],'SHORT')
            self.assertIsNone(con.execute('SELECT context_json FROM runners').fetchone()[0])
    def test_wrong_cohort_does_not_archive(self):
        (self.dir/'continuous_v1_cohort.json').write_text(json.dumps({'start_ms':T+120000}))
        self.update_path(K,candles(T,[(10,10.5,9.5,10)]),1000)
        out=run_once(self.root,self.dest,2000)
        self.assertEqual(out['runner_total'],0)
    def test_read_only_check_does_not_create_archive(self):
        self.assertFalse(self.dest.exists())
        self.assertEqual(status(self.dest),{'database_exists':False})
        self.assertFalse(self.dest.exists())


if __name__ == '__main__': unittest.main()
