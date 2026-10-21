"""Offline tests of prospective gap worker. No Binance/network access."""
from __future__ import annotations
import sqlite3
import tempfile
import time
from pathlib import Path
import unittest
from urllib.error import HTTPError, URLError

from pre_reaction_context_recorder import open_store
import pre_reaction_gap_worker as w

MINUTE=60_000
GOOD=(2.0,2.5,1.5,2.2,10.0,22.0)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        self.ctx=self.root/'context.sqlite3'
        self.state=self.root/'state.sqlite3'
        self.auto=self.root/'missing-autopaths.sqlite3'
        self.now=int(time.time()*1000)//MINUTE*MINUTE
        self.missing=self.now-25*MINUTE
        self.calls=[]
        c=open_store(self.ctx)
        self.make_gap(c,'TESTUSDT','1m',self.missing)
        c.commit();c.close()

    def make_gap(self,db,symbol,tf,ts):
        ms,count=w.TFS[tf]
        end=ts+3*ms
        start=end-count*ms
        key=f'{symbol}|LONG|{ts-ms}'
        db.execute('''INSERT INTO events(event_key,symbol,side,retest_ts,known_ts,
             first_seen_ms,observation_type,scanner_source_hash,captured_at_ms,
             first_capture_lag_ms,capture_timeliness) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
             (key,symbol,'LONG',ts-ms,ts,ts+2*ms,'LIVE_OBSERVED',
              'sha',ts+3*ms,ms,'ON_TIME'))
        for t in range(start,end,ms):
            if t!=ts:
                db.execute('''INSERT INTO candles(symbol,timeframe,open_ts,open,high,
                    low,close,volume,quote_volume,first_recorded_ms,source)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                    (symbol,tf,t,*GOOD,ts+3*ms,'REDIS_CLOSED'))
        db.execute('''INSERT INTO windows(event_key,anchor_type,timeframe,anchor_ms,
           end_exclusive_ms,expected_count,saved_count,status,first_missing_ts,
           source_invalid_rows,updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
           (key,'PRE_OBSERVED',tf,end-1,end,count,count-1,'PARTIAL',ts,0,ts+3*ms))

    def fake(self,symbol,tf,ts,now):
        self.calls.append((symbol,tf,ts))
        return GOOD

    def run_worker(self,**kw):
        opts={'now_ms':self.now,'min_free_mb':0,'pace_seconds':0,'fetch':self.fake}
        opts.update(kw)
        return w.run_once(self.ctx,self.auto,self.state,**opts)

    def test_plan_is_strictly_read_only(self):
        r=self.run_worker(read_only=True)
        self.assertEqual(r['missing_unique_bars'],1)
        self.assertEqual(r['candidate_state'],{'READY':1})
        self.assertEqual(self.calls,[])
        self.assertFalse(self.state.exists())

    def test_repair_and_auditable_source(self):
        r=self.run_worker()
        self.assertEqual(r['inserted'],1)
        self.assertEqual(r['refreshed_windows'],1)
        self.assertEqual(r['no_auto_paths_overlap'],1)
        db=sqlite3.connect(self.ctx)
        self.assertEqual(db.execute('SELECT saved_count,status FROM windows').fetchone(),(120,'COMPLETE'))
        self.assertEqual(db.execute('SELECT source FROM candles WHERE open_ts=?',(self.missing,)).fetchone()[0],w.SOURCE)
        db.close()
        state=sqlite3.connect(self.state)
        self.assertEqual(state.execute('SELECT state,attempts FROM tasks').fetchone(),('DONE',1))
        state.close()
        again=self.run_worker()
        self.assertEqual(again['requested'],0)
        self.assertEqual(len(self.calls),1)

    def test_grace_after_candle_close_and_first_capture(self):
        # Timestamp passed closure threshold, but event was recorded only now.
        db=sqlite3.connect(self.ctx)
        db.execute('UPDATE events SET captured_at_ms=?',(self.now-2*MINUTE,))
        db.commit();db.close()
        result=self.run_worker(grace_minutes=5)
        self.assertEqual(result['requested'],0)
        self.assertEqual(result['candidate_state']['GRACE'],1)
        self.assertEqual(self.calls,[])

    def test_auto_backfill_both_timeframes(self):
        db=sqlite3.connect(self.ctx)
        missing15=(self.missing//900_000-2)*900_000
        self.make_gap(db,'TEST15USDT','15m',missing15)
        db.commit();db.close()
        r=self.run_worker()
        self.assertEqual(r['requested'],2)
        self.assertEqual(r['inserted'],2)
        self.assertCountEqual([x[1] for x in self.calls],['1m','15m'])
        self.assertEqual(r['missing_by_timeframe'],{'1m':1,'15m':1})

    def test_recovered_15m_requires_match_when_1m_complete(self):
        db=sqlite3.connect(self.ctx)
        t=(self.missing//900_000-2)*900_000
        self.make_gap(db,'TEST15USDT','15m',t)
        for minute in range(15):
            ts=t+minute*MINUTE
            db.execute('''INSERT INTO candles(symbol,timeframe,open_ts,open,high,
                low,close,volume,quote_volume,first_recorded_ms,source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                ('TEST15USDT','1m',ts,*GOOD,self.now,'REDIS_CLOSED'))
        db.commit();db.close()
        def fetch(symbol,tf,ts,now):
            if tf=='15m':return GOOD  # volume 10 contradicts 15 bars x 10 = 150
            return GOOD
        r=w.run_once(self.ctx,self.auto,self.state,now_ms=self.now,
                     min_free_mb=0,pace_seconds=0,fetch=fetch)
        self.assertTrue(any(x['tf']=='15m' and x['state']=='BLOCKED' for x in r['rejected']))
        db=sqlite3.connect(self.ctx)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM candles WHERE symbol='TEST15USDT' AND timeframe='15m' AND open_ts=?",(t,)).fetchone()[0],0)
        db.close()

    def test_retry_persisted_no_hammering(self):
        def offline(*args):
            self.calls.append(args[:3])
            raise URLError('network down')
        r=w.run_once(self.ctx,self.auto,self.state,now_ms=self.now,
                     min_free_mb=0,pace_seconds=0,fetch=offline)
        self.assertEqual(r['requested'],1)
        self.assertEqual(r['rejected'][0]['state'],'RETRY')
        second=self.run_worker()
        self.assertEqual(second['requested'],0)
        self.assertEqual(second['candidate_state']['BACKOFF'],1)
        self.assertEqual(len(self.calls),1)
        db=sqlite3.connect(self.state)
        self.assertGreater(db.execute('SELECT next_retry_ms FROM tasks').fetchone()[0],int(time.time()*1000))
        db.close()

    def test_ohlc_parity_mismatch_blocks(self):
        # Inject contradictory independent Auto Paths OHLC without touching LIVE.
        import zlib,struct
        a=sqlite3.connect(self.auto)
        a.execute('CREATE TABLE events(symbol TEXT,path_blob BLOB)')
        a.execute('INSERT INTO events VALUES (?,?)',('TESTUSDT',
          zlib.compress(struct.pack('<qdddd',self.missing,5,5.5,4.5,5.2))))
        a.commit();a.close()
        out=self.run_worker()
        self.assertEqual(out['inserted'],0)
        self.assertEqual(out['blocked'],1)
        self.assertEqual(out['rejected'][0]['state'],'BLOCKED')
        another=self.run_worker()
        self.assertEqual(another['candidate_state']['BLOCKED'],1)
        self.assertEqual(len(self.calls),1)
        db=sqlite3.connect(self.ctx)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM candles WHERE open_ts=?',(self.missing,)).fetchone()[0],0)
        db.close()

    def test_global_api_throttle_pauses_everything(self):
        def restricted(*args):
            self.calls.append(args[:3])
            raise HTTPError('https://fapi.binance.com',429,'Rate limit',{},None)
        out=w.run_once(self.ctx,self.auto,self.state,now_ms=self.now,
                       min_free_mb=0,pace_seconds=0,fetch=restricted)
        self.assertEqual(out['action'],'API_PAUSED_AFTER_ERROR')
        paused=self.run_worker()
        self.assertEqual(paused['action'],'API_PAUSED')
        self.assertEqual(len(self.calls),1)

    def test_unique_gap_per_symbol_not_one_global_timestamp(self):
        db=sqlite3.connect(self.ctx)
        self.make_gap(db,'OTHERUSDT','1m',self.missing)
        db.commit();db.close()
        r=self.run_worker()
        self.assertEqual(r['requested'],2)
        self.assertEqual(r['inserted'],2)
        self.assertEqual({x[0] for x in self.calls},{'TESTUSDT','OTHERUSDT'})

    def test_batch_is_bounded_and_resumes(self):
        db=sqlite3.connect(self.ctx)
        self.make_gap(db,'OTHERUSDT','1m',self.missing)
        db.commit();db.close()
        r=self.run_worker(max_bars=1)
        self.assertEqual(r['inserted'],1)
        r=self.run_worker(max_bars=1)
        self.assertEqual(r['inserted'],1)
        self.assertEqual(self.run_worker(read_only=True)['missing_unique_bars'],0)

    def test_storage_limit_prevents_network(self):
        with self.assertRaisesRegex(RuntimeError,'free'):
            self.run_worker(min_free_mb=999_999_999)
        self.assertEqual(self.calls,[])
        self.assertFalse(self.state.exists())


if __name__=='__main__':unittest.main()
