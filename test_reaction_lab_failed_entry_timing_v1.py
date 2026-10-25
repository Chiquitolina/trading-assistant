import json
import hashlib
from contextlib import redirect_stdout
from io import StringIO
import sqlite3
import tempfile
import unittest
from pathlib import Path

import reaction_lab_failed_entry_timing_v1 as m


class TimingTests(unittest.TestCase):
    def setUp(self):
        self.row = {'side':'LONG','meta':{'swing_price':100,'retest_high':100.1,'retest_low':99.0}}

    def bars(self, closes, start=1_000_020_000):
        assert start%m.MINUTE==0
        arr=[]
        for i,c in enumerate(closes):
            o=closes[i-1] if i else 99.2
            arr.append((start+i*m.MINUTE,float(o),max(o,c)+0.01,min(o,c)-0.01,float(c)))
        return arr

    def test_immediate_original_and_inverse(self):
        b=self.bars([99.1]*30)
        self.assertEqual(m.find_entry('IMMEDIATE_ORIGINAL',b,self.row,15),(0,'LONG'))
        self.assertEqual(m.find_entry('IMMEDIATE_INVERSE',b,self.row,15),(0,'SHORT'))

    def test_reclaim_is_next_open(self):
        b=self.bars([99.4,100.10,100.3,100.4])
        self.assertEqual(m.find_entry('ORIGINAL_RECLAIM',b,self.row,15),(2,'LONG'))
        self.assertEqual(m.find_entry('ORIGINAL_RECLAIM_2_CLOSES',b,self.row,15),(3,'LONG'))

    def test_continuation_after_closed_bars(self):
        b=self.bars([99.3,99.1,99.0,99.0])
        self.assertEqual(m.find_entry('INVERSE_2_CLOSES',b,self.row,15),(2,'SHORT'))

    def test_fail_extreme_break_and_reclaim_extreme(self):
        b=self.bars([99.2,98.8,100.4,100.5])
        self.assertEqual(m.find_entry('INVERSE_BREAK_FAIL_EXTREME',b,self.row,15),(2,'SHORT'))
        self.assertEqual(m.find_entry('ORIGINAL_BREAK_FAIL_EXTREME',b,self.row,15),(3,'LONG'))

    def test_retest_rejection_requires_earlier_extension(self):
        b=self.bars([99.8,99.6,99.8,99.7,99.4])
        # j2 touches the underside at HIGH=100.0 and rejects (CLOSE=99.8)
        b[2] = (b[2][0],b[2][1],100.0,b[2][3],b[2][4])
        # An earlier candle had already extended at least 0.20% below swing
        self.assertEqual(m.find_entry('INVERSE_RETEST_REJECTION',b,self.row,15),(3,'SHORT'))

    def test_no_trigger_is_not_profitable_trade(self):
        b=self.bars([99.5]*90)
        ev=dict(self.row,key='k',symbol='BTCUSDT',origin='LIVE_OBSERVED',ts=42,
                state='COMPLETE',bars=b)
        eligible, rows=m.simulate_cohort([ev],60,1.,1.,15,.2)
        self.assertEqual(len(eligible),1)
        rec=next(r for r in rows if r['mode']=='ORIGINAL_RECLAIM')
        self.assertEqual(rec['outcome'],'NO_TRIGGER')
        self.assertIsNone(rec['net_pct'])
        s=m.summary_for([rec],1)
        self.assertEqual(s['no_trigger'],1)
        self.assertEqual(s['net_mean_per_eligible_pct'],0.)

    def test_short_original_fail_reclaim_and_continuation(self):
        row={'side':'SHORT','meta':{'swing_price':100,'retest_high':101.,'retest_low':99.9}}
        b=self.bars([100.2,100.45,100.6,99.8,99.7])
        self.assertEqual(m.find_entry('INVERSE_2_CLOSES',b,row,15),(2,'LONG'))
        self.assertEqual(m.find_entry('ORIGINAL_RECLAIM',b,row,15),(4,'SHORT'))

    def test_same_candle_tp_sl_conservative(self):
        b=[(60_000,100.,102.,98.,100.)]
        self.assertEqual(m.execute(b,0,'LONG',1,1,1),('SL_AMBIGUOUS',1,-1))
        self.assertEqual(m.execute(b,0,'SHORT',1,1,1),('SL_AMBIGUOUS',1,-1))

    def test_no_future_bar_can_trigger(self):
        b=self.bars([99.4,99.4,99.4,99.4,101.0,101.0])
        self.assertIsNone(m.find_entry('ORIGINAL_RECLAIM',b,self.row,3))
        self.assertEqual(m.find_entry('ORIGINAL_RECLAIM',b,self.row,5),(5,'LONG'))

    def test_coverage_equal_between_methods(self):
        b=self.bars([99.4]*76)
        ev=dict(self.row,key='k',symbol='BTCUSDT',origin='LIVE_OBSERVED',ts=42,
                state='ACTIVE',bars=b)
        _,r=m.simulate_cohort([ev],60,1,1,15,.2)
        self.assertEqual(len(r),len(m.MODES))
        self.assertEqual({x['touch_key'] for x in r},{'k'})
        self.assertEqual({x['hold_minutes'] for x in r},{60})
        _,r120=m.simulate_cohort([ev],120,1,1,15,.2)
        self.assertEqual(r120,[])

    def test_sqlite_is_read_only_and_decodes(self):
        with tempfile.TemporaryDirectory() as td:
            d=Path(td)
            journal=d/'journal.sqlite3';paths=d/'paths.sqlite3'
            b=self.bars([99.4]*80)
            with sqlite3.connect(journal) as c:
                c.execute('CREATE TABLE first_touches(touch_key TEXT, symbol TEXT,side TEXT,observation_type TEXT,retest_ts INTEGER,causal_json TEXT,status TEXT)')
                c.execute('INSERT INTO first_touches VALUES (?,?,?,?,?,?,?)',('k','BTCUSDT','LONG','LIVE_OBSERVED',b[0][0]-m.MINUTE,json.dumps(self.row['meta']),'TOUCH_FAILED'))
            with sqlite3.connect(paths) as c:
                c.execute('CREATE TABLE paths(touch_key TEXT,status TEXT,state TEXT,count INTEGER,path_blob BLOB,entry_ts INTEGER)')
                c.execute('INSERT INTO paths VALUES (?,?,?,?,?,?)',('k','TOUCH_FAILED','ACTIVE',len(b),m.zlib.compress(b''.join(m.BAR.pack(*x) for x in b)),b[0][0]))
            out=m.collect(journal,paths)
            self.assertEqual(len(out),1)
            self.assertEqual(len(out[0]['bars']),80)
            with m.read_only(journal) as c:
                with self.assertRaises(sqlite3.OperationalError):
                    c.execute('CREATE TABLE bad(i INTEGER)')

    def test_cli_run_writes_only_isolated_output(self):
        with tempfile.TemporaryDirectory() as td:
            d=Path(td)
            journal=d/'journal.sqlite3';paths=d/'paths.sqlite3';out=d/'new_report'
            b=self.bars([99.3]*90)
            with sqlite3.connect(journal) as c:
                c.execute('CREATE TABLE first_touches(touch_key TEXT,symbol TEXT,side TEXT,observation_type TEXT,retest_ts INTEGER,causal_json TEXT,status TEXT)')
                c.execute('INSERT INTO first_touches VALUES (?,?,?,?,?,?,?)',('k','BTCUSDT','LONG','LIVE_OBSERVED',b[0][0]-m.MINUTE,json.dumps(self.row['meta']),'TOUCH_FAILED'))
            with sqlite3.connect(paths) as c:
                c.execute('CREATE TABLE paths(touch_key TEXT,status TEXT,state TEXT,count INTEGER,path_blob BLOB,entry_ts INTEGER)')
                c.execute('INSERT INTO paths VALUES (?,?,?,?,?,?)',('k','TOUCH_FAILED','ACTIVE',len(b),m.zlib.compress(b''.join(m.BAR.pack(*x) for x in b)),b[0][0]))
            before=[hashlib.sha256(x.read_bytes()).hexdigest() for x in (journal,paths)]
            cmd=['--project',str(d),'--journal-db',str(journal),'--paths-db',str(paths),'--out',str(out)]
            with redirect_stdout(StringIO()):
                self.assertEqual(m.main(cmd+['--check']),0)
            self.assertFalse(out.exists())
            with redirect_stdout(StringIO()):
                self.assertEqual(m.main(cmd+['--run']),0)
            self.assertTrue((out/'summary.csv').exists())
            self.assertTrue((out/'event_outcomes.csv').exists())
            self.assertTrue((out/'breakdowns.csv').exists())
            self.assertTrue((out/'report.json').exists())
            after=[hashlib.sha256(x.read_bytes()).hexdigest() for x in (journal,paths)]
            self.assertEqual(before,after)

    def test_portfolio_one_slot_and_cost(self):
        rows=[{'outcome':'TP_FIRST','entry_ts':60000,'exit_ts':120000,'touch_key':'a','net_pct':1.},
              {'outcome':'TP_FIRST','entry_ts':60000,'exit_ts':120000,'touch_key':'b','net_pct':1.},
              {'outcome':'SL_FIRST','entry_ts':180000,'exit_ts':180000,'touch_key':'c','net_pct':-1.}]
        p=m.portfolio(rows)
        self.assertEqual((p['slot_trades'],p['slot_skipped']),(2,1))
        self.assertGreater(p['final_usd'],199)
        self.assertLess(p['final_usd'],201)


if __name__=='__main__': unittest.main()
