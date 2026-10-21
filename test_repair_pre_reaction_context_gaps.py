import tempfile
from pathlib import Path
import sqlite3
import struct
import unittest
import zlib

from pre_reaction_context_recorder import open_store
from repair_pre_reaction_context_gaps import (execute, gaps, check_bar, SOURCE,
                                              autopaths_evidence, matching_ohlc, verify_aggregated_15m)

HOLE=1792595880000  # 2026-10-21 15:18 UTC
M=60000
GOOD=(2.0,2.5,1.5,2.2,10.0,22.0)


class GapRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dbfile=Path(self.tmp.name)/'ctx.sqlite3'
        self.auto=Path(self.tmp.name)/'auto.sqlite3'
        con=open_store(self.dbfile)
        key='TESTUSDT|LONG|1792595820000'
        end=HOLE+3*M
        start=end-120*M
        con.execute('''INSERT INTO events (event_key,symbol,side,retest_ts,known_ts,
            first_seen_ms,observation_type,scanner_source_hash,captured_at_ms,
            first_capture_lag_ms,capture_timeliness) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
            (key,'TESTUSDT','LONG',HOLE-M,HOLE,HOLE+2*M,
             'LIVE_OBSERVED','sha1',HOLE+3*M,60000,'ON_TIME'))
        for ts in range(start,end,M):
            if ts != HOLE:
                con.execute('''INSERT INTO candles(symbol,timeframe,open_ts,open,high,
                   low,close,volume,quote_volume,first_recorded_ms,source)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                   ('TESTUSDT','1m',ts,*GOOD,HOLE+3*M,'REDIS_CLOSED'))
        con.execute('''INSERT INTO windows (event_key,anchor_type,timeframe,anchor_ms,
           end_exclusive_ms,expected_count,saved_count,status,first_missing_ts,
           source_invalid_rows,updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
           (key,'PRE_OBSERVED','1m',HOLE+3*M-1,end,120,119,'PARTIAL',HOLE,0,HOLE+3*M))
        con.commit();con.close()
        # Auto Paths contains one verified overlapping path at missing candle
        auto=sqlite3.connect(self.auto)
        auto.execute('CREATE TABLE events(symbol TEXT, path_blob BLOB)')
        vals=struct.pack('<qdddd',HOLE,*GOOD[:4])
        auto.execute('INSERT INTO events VALUES (?,?)',('TESTUSDT',zlib.compress(vals)))
        auto.commit();auto.close()

    def test_plan_is_read_only(self):
        out=execute(self.dbfile,self.auto,'1m',100,0,False)
        self.assertEqual(out['missing_unique_bars'],1)
        db=sqlite3.connect(self.dbfile)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM candles WHERE source=?',(SOURCE,)).fetchone()[0],0)
        db.close()

    def test_fill_and_idempotent(self):
        result=execute(self.dbfile,self.auto,'1m',100,0,True,fetch=lambda *args: GOOD)
        self.assertEqual(result['inserted'],1)
        self.assertEqual(result['remaining_missing_unique_bars'],0)
        self.assertEqual(result['checked_against_auto_paths'],1)
        db=sqlite3.connect(self.dbfile)
        self.assertEqual(db.execute("SELECT status,saved_count FROM windows").fetchone(),('COMPLETE',120))
        self.assertEqual(db.execute("SELECT source FROM candles WHERE open_ts=?",(HOLE,)).fetchone()[0],SOURCE)
        db.close()
        result=execute(self.dbfile,self.auto,'1m',100,0,True,fetch=lambda *args: GOOD)
        self.assertEqual(result['requested'],0)

    def test_autopaths_mismatch_rejects(self):
        other=sqlite3.connect(self.auto)
        other.execute('DELETE FROM events')
        vals=struct.pack('<qdddd',HOLE,3.0,3.1,2.9,3.0)
        other.execute('INSERT INTO events VALUES (?,?)',('TESTUSDT',zlib.compress(vals)))
        other.commit();other.close()
        out=execute(self.dbfile,self.auto,'1m',100,0,True,fetch=lambda *args:GOOD)
        self.assertEqual(out['inserted'],0)
        self.assertIn('OHLC_DIFFER',out['rejected'][0]['reason'])
        self.assertEqual(out['remaining_missing_unique_bars'],1)

    def test_many_gaps_refuses_download(self):
        with self.assertRaisesRegex(ValueError,'exceed --max-bars'):
            execute(self.dbfile,self.auto,'1m',-1,0,True,fetch=lambda *args:GOOD)
        db=sqlite3.connect(self.dbfile)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM candles WHERE source=?',(SOURCE,)).fetchone()[0],0)
        db.close()

    def test_kline_validate(self):
        row=[HOLE,'2','2.5','1.5','2.2','10',HOLE+M-1,'22']
        self.assertEqual(check_bar(row,HOLE,'1m',HOLE+M),GOOD)
        with self.assertRaises(ValueError):
            check_bar(row,HOLE,'1m',HOLE+M-1)
        with self.assertRaises(ValueError):
            check_bar(row,HOLE+M,'1m',HOLE+2*M)

    def test_aggregated_15m_agrees_when_complete(self):
        db=sqlite3.connect(self.dbfile)
        t=(HOLE//900000)*900000
        for ts in range(t,t+900000,M):
            if ts!=HOLE:
                db.execute('''INSERT OR IGNORE INTO candles
                  (symbol,timeframe,open_ts,open,high,low,close,volume,quote_volume,
                   first_recorded_ms,source) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                   ('TESTUSDT','1m',ts,*GOOD,HOLE+3*M,'REDIS_CLOSED'))
        db.execute('''INSERT INTO candles
          (symbol,timeframe,open_ts,open,high,low,close,volume,
           first_recorded_ms,source) VALUES (?,?,?,?,?,?,?,?,?,?)''',
           ('TESTUSDT','15m',t,2.0,2.5,1.5,2.2,150.0,HOLE,'REDIS_CLOSED'))
        db.commit()
        self.assertEqual(verify_aggregated_15m(db,'TESTUSDT',HOLE,GOOD),'MATCH')
        with self.assertRaisesRegex(ValueError,'AGGREGATED_15M_MISMATCH'):
            verify_aggregated_15m(db,'TESTUSDT',HOLE,(4.0,4.0,1.5,3.0,10,22))
        db.close()

    def test_contradictory_autopaths_never_inserted(self):
        db=sqlite3.connect(self.auto)
        vals=struct.pack('<qdddd',HOLE,3.0,3.1,2.9,3.0)
        db.execute('INSERT INTO events VALUES (?,?)',('TESTUSDT',zlib.compress(vals)))
        db.commit();db.close()
        out=execute(self.dbfile,self.auto,'1m',100,0,True,fetch=lambda *args:GOOD)
        self.assertIn('contradicts itself',out['rejected'][0]['reason'])
        self.assertEqual(out['inserted'],0)


if __name__=='__main__':unittest.main()
