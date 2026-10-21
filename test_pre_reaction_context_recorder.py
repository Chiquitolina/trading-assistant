#!/usr/bin/env python3
"""Offline tests for standalone prospective Pre-Reaction Context Recorder."""
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
import zlib

import pre_reaction_context_recorder as m

ONE=60_000
FIFTEEN=15*ONE
BASE=1_792_569_600_000  # Synthetic UTC minute-aligned reference.
BASE -= BASE % FIFTEEN


class FakeRedis:
    def __init__(self): self.histories={};self.calls=[]
    def lrange(self,k,start,end):
        self.calls.append((k,start,end))
        return self.histories.get(k,[])[start:] if end==-1 else self.histories.get(k,[])[start:end+1]


def candle(ts,ms,price=100.0):
    return {'timestamp':ts,'close_timestamp':ts+ms-1,'open':price,'high':price+1,
            'low':price-1,'close':price+0.3,'volume':10.0,'quoteVolume':1003.0}


def fill_redis(redis_,symbols=('AGTUSDT',),end=BASE+ONE*8):
    for sym in symbols:
        for tf,(ms,_) in m.TFS.items():
            candles=[candle(ts,ms) for ts in range(BASE-160*FIFTEEN, end+ms,ms)]
            redis_.histories[m.history_key(sym,tf)]=[json.dumps(v).encode() for v in candles]


def build_scan(path, events):
    con=sqlite3.connect(str(path))
    con.execute('''CREATE TABLE events (event_key TEXT PRIMARY KEY,symbol TEXT,side TEXT,
        retest_ts INT,known_ts INT,first_seen_ms INT,observation_type TEXT,source_hash TEXT)''')
    for e in events:
        con.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)',e)
    con.commit();con.close()


def event(sym='AGTUSDT', retest=BASE,seen=None,side='LONG',observation='LIVE_OBSERVED'):
    known=retest+ONE
    if seen is None:seen=known+30_000
    return (f'{sym}|{side}|{retest}',sym,side,retest,known,seen,observation,'123hash')


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.store_path=self.root/'context.db'
        self.scan_path=self.root/'scan.db'
        self.redis=FakeRedis()
        fill_redis(self.redis)
        self.e=event()
        build_scan(self.scan_path,[self.e])
        self.scan=m.ro_db(self.scan_path)
        self.addCleanup(lambda: self.scan.close())
        self.db=m.open_store(self.store_path)
        self.addCleanup(lambda: self.db.close())
        m.initialize_start(self.db,BASE-ONE,0)
        self.now=self.e[5]+20_000

    def run_once(self,now=None):
        return m.run_cycle(self.scan,self.db,self.redis,self.now if now is None else now)

    def test_full_two_timeframes_three_anchors_and_strict_cutoffs(self):
        result=self.run_once()
        self.assertEqual(result['new_events'],1)
        self.assertEqual(result['retried_events'],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM windows').fetchone()[0],6)
        states=self.db.execute('SELECT timeframe,anchor_type,saved_count,status FROM windows').fetchall()
        for tf,anchor,n,status in states:
            self.assertEqual(status,'COMPLETE',(tf,anchor,n))
            self.assertEqual(n,m.TFS[tf][1])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM candles WHERE open_ts>=? AND timeframe='1m'",(BASE+ONE,)).fetchone()[0],0)
        self.assertEqual(self.db.execute("SELECT saved_count FROM windows WHERE timeframe='1m' AND anchor_type='PRE_RETEST'").fetchone()[0],120)
        self.assertEqual(m.status(self.store_path)['events_by_timeliness']['ON_TIME'],1)
        report=m.audit(self.store_path,self.scan_path)
        self.assertTrue(report['success'],report)
        self.assertEqual(report['complete'],6)
        self.assertEqual(report['scanner_missing_since_start'],0)

    def test_replay_on_second_run_no_redownload_no_duplicates(self):
        first=self.run_once()
        total=self.db.execute('SELECT COUNT(*) FROM candles').fetchone()[0]
        second=self.run_once(now=self.now+ONE)
        self.assertEqual(second['new_events'],0)
        self.assertEqual(second['retried_events'],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM candles').fetchone()[0],total)
        self.assertGreater(first['bars_saved'],0)

    def test_missing_minute_retry_repairs_within_deadline(self):
        key=m.history_key('AGTUSDT','1m')
        self.redis.histories[key]=[r for r in self.redis.histories[key]
                                   if json.loads(r)['timestamp']!=BASE-20*ONE]
        self.run_once()
        rows=self.db.execute("SELECT status,saved_count FROM windows WHERE anchor_type='PRE_RETEST' AND timeframe='1m'").fetchone()
        self.assertEqual(rows,('PARTIAL',119))
        self.assertEqual(self.db.execute('SELECT finalized FROM events').fetchone()[0],0)
        self.redis.histories[key].append(json.dumps(candle(BASE-20*ONE,ONE)).encode())
        after=self.run_once(now=self.now+ONE)
        self.assertEqual(after['retried_events'],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM windows WHERE status="COMPLETE"').fetchone()[0],6)
        self.assertTrue(m.audit(self.store_path,self.scan_path)['success'])

    def test_contradictory_duplicate_quarantined(self):
        k=m.history_key('AGTUSDT','1m')
        altered=candle(BASE-10*ONE,ONE,price=150)
        self.redis.histories[k].append(json.dumps(altered).encode())
        output=self.run_once()
        self.assertGreater(output['conflicts_encountered'],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM conflicts').fetchone()[0],1)
        self.assertEqual(self.db.execute("SELECT status FROM windows WHERE anchor_type='PRE_RETEST' AND timeframe='1m'").fetchone()[0],'CONFLICT')
        self.assertFalse(m.audit(self.store_path,self.scan_path)['success'])

    def test_late_classification_and_recovered_source(self):
        con=sqlite3.connect(str(self.scan_path))
        con.execute('UPDATE events SET observation_type=?',('RECOVERED_WINDOW',))
        con.commit();con.close()
        self.scan.close()
        self.scan=m.ro_db(self.scan_path)
        r=m.run_cycle(self.scan,self.db,self.redis,self.e[5]+5*ONE)
        self.assertEqual(r['new_events'],1)
        status=m.status(self.store_path)
        self.assertEqual(status['events_by_observation']['RECOVERED_WINDOW'],1)
        self.assertEqual(status['events_by_timeliness']['DELAYED'],1)

    def test_unfinished_candle_and_15m_boundary(self):
        k=m.history_key('AGTUSDT','15m')
        self.redis.histories[k].append(json.dumps(candle(BASE+FIFTEEN,FIFTEEN)).encode())
        self.run_once()
        mx=self.db.execute("SELECT MAX(open_ts) FROM candles WHERE timeframe='15m'").fetchone()[0]
        self.assertEqual(mx,BASE-FIFTEEN)
        self.assertTrue(m.audit(self.store_path,self.scan_path)['success'])

    def test_cursor_batch_no_missed_events_same_first_seen(self):
        e2=event('AGTUSDT',BASE-ONE,seen=self.e[5])
        con=sqlite3.connect(str(self.scan_path))
        con.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)',e2)
        con.commit();con.close()
        self.scan.close();self.scan=m.ro_db(self.scan_path)
        one=m.run_cycle(self.scan,self.db,self.redis,self.now,batch_size=1)
        two=m.run_cycle(self.scan,self.db,self.redis,self.now,batch_size=1)
        self.assertEqual(one['new_events'],1)
        self.assertEqual(two['new_events'],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],2)
        self.assertEqual(m.audit(self.store_path,self.scan_path)['scanner_missing_since_start'],0)

    def test_store_conflict_later_is_flagged_not_overwritten(self):
        self.run_once()
        sym='AGTUSDT'; ts=BASE-5*ONE
        initial=self.db.execute('SELECT open FROM candles WHERE symbol=? AND timeframe=? AND open_ts=?',(sym,'1m',ts)).fetchone()[0]
        key=m.history_key(sym,'1m')
        self.redis.histories[key] = [r for r in self.redis.histories[key] if json.loads(r)['timestamp']!=ts]
        self.redis.histories[key].append(json.dumps(candle(ts,ONE,price=200)).encode())
        # A second future event reobserves this same minute. Existing OHLC must not change.
        e2=event(sym,BASE+ONE,seen=self.e[5]+ONE)
        con=sqlite3.connect(str(self.scan_path));con.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)',e2);con.commit();con.close()
        self.scan.close();self.scan=m.ro_db(self.scan_path)
        self.run_once(now=self.now+2*ONE)
        self.assertEqual(self.db.execute('SELECT open FROM candles WHERE symbol=? AND timeframe=? AND open_ts=?',(sym,'1m',ts)).fetchone()[0],initial)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM conflicts').fetchone()[0],1)
        report=m.audit(self.store_path,self.scan_path)
        self.assertFalse(report['success'])
        self.assertEqual(report['stale_window_rows'],0)

    def test_crosscheck_auto_paths(self):
        self.run_once()
        path=self.root/'paths.db'
        con=sqlite3.connect(str(path))
        con.execute('CREATE TABLE events(symbol TEXT,path_blob BLOB,first_seen_utc_ms INT)')
        bars=[candle(BASE-5*ONE+i*ONE,ONE) for i in range(4)]
        blob=zlib.compress(b''.join(struct.pack('<qdddd',x['timestamp'],x['open'],x['high'],x['low'],x['close']) for x in bars))
        con.execute('INSERT INTO events VALUES (?,?,?)',('AGTUSDT',blob,self.e[5]))
        con.commit();con.close()
        pair=m.audit(self.store_path,self.scan_path,path,10)['auto_paths_parity']
        self.assertEqual(pair['compared'],4)
        self.assertEqual(pair['matched'],4)
        self.assertEqual(pair['differ'],0)

    def test_invalid_prices_and_bad_timeframe(self):
        raw=candle(BASE-ONE,ONE)
        raw['high']=90
        parsed,conflicted,invalid=m.parse_redis_bars([json.dumps(raw)],'AGTUSDT','1m',self.now)
        self.assertEqual(len(parsed),0)
        self.assertEqual(invalid,1)
        raw=candle(BASE-ONE,ONE)
        raw['timeframe']='15m'
        self.assertEqual(m.parse_redis_bars([json.dumps(raw)],'AGTUSDT','1m',self.now)[2],1)

    def test_default_prospective_bootstrap_skips_old_events(self):
        db2=m.open_store(self.root/'new.db')
        start=m.initialize_start(db2,self.e[5]+10*ONE,0)
        self.assertEqual(start,self.e[5]+10*ONE)
        self.assertEqual(len(m.fetch_new_events(self.scan,db2,100)),0)
        db2.close()


if __name__=='__main__':unittest.main()
