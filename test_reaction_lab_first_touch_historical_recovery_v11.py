import csv
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
import zlib

import reaction_lab_first_touch_historical_recovery_v11 as m

MIN=m.MINUTE

def mkbar(ts, op=100, hi=100.2, lo=99.8, cl=100):
    return (int(ts),float(op),float(hi),float(lo),float(cl))


def fixture(root, specs=None):
    specs=specs or [
        ('a','BTCUSDT','LONG','TOUCH_INDECISIVE',1_800_000_000_000,'STALE_BEFORE_COLLECTOR',0,None,100),
        ('b','BTCUSDT','SHORT','TOUCH_FAILED',1_800_000_060_000,'GAP_EXPIRED',0,None,100),
        ('c','AAVEUSDT','LONG','REACTION',1_800_000_060_000,'STALE_BEFORE_COLLECTOR',0,None,100),
    ]
    base=root/'reports/reaction_lab';base.mkdir(parents=True)
    dbfile=base/'first_touch_paths_v2.sqlite3'
    with closing(sqlite3.connect(dbfile)) as db:
        db.execute('''CREATE TABLE paths(touch_key TEXT PRIMARY KEY,symbol TEXT,side TEXT,
                    status TEXT,retest_ts INTEGER,entry_ts INTEGER,state TEXT,count INTEGER,
                    path_blob BLOB,retest_close REAL)''')
        db.executemany('INSERT INTO paths VALUES (?,?,?,?,?,?,?,?,?,?)',
                       [(k,s,side,status,ts,ts+MIN,state,n,blob,price)
                        for k,s,side,status,ts,state,n,blob,price in specs])
        db.commit()
    audit=root/'audit_events.csv'
    with audit.open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=('touch_key','symbol','side','status','retest_ts',
                                         'entry_ts','ft_state','audit_state'))
        w.writeheader()
        for k,s,side,status,ts,state,n,blob,price in specs:
            w.writerow({'touch_key':k,'symbol':s,'side':side,'status':status,'retest_ts':ts,
                        'entry_ts':ts+MIN,'ft_state':state,'audit_state':'NO_AUTO_MATCH'})
    return audit,dbfile


class PureTests(unittest.TestCase):
    def test_synthetic_provider_valid(self):
        b=m.normalize_bar('BTCUSDT',[0,'100','101','99','100'],0,MIN,MIN)
        self.assertEqual(b,mkbar(0,100,101,99,100))

    def test_provider_rejects_unclosed_and_outside(self):
        for k in [[MIN,'100','101','99','100'],[0,'100','101','99','100']]:
            with self.assertRaises(ValueError):
                m.normalize_bar('BTCUSDT',k,0,0,-MIN)

    def test_provider_rejects_bad_ohlc(self):
        for arr in [[0,'100','100','101','100'],[0,'100','100','99','101'],
                    [0,'0','1','1','1'],[0,'NaN','1','1','1']]:
            with self.assertRaises(ValueError):
                m.normalize_bar('BTCUSDT',arr,0,0,0)

    def test_decode_rejects_bad_timestamp(self):
        bad=zlib.compress(m.BAR.pack(*mkbar(MIN)))
        with self.assertRaises(ValueError):m.verify_prefix(bad,1,0)

    def test_exact_prefix_and_conflict(self):
        entry=MIN*1000
        bar=mkbar(entry)
        e={'entry_ts':entry,'source_bars':[bar],'symbol':'BTCUSDT'}
        market={'BTCUSDT':{entry:bar}}
        bs,state,parity,missing=m.reconstruct(e,market)
        self.assertEqual((len(bs),state,parity,missing),(1,'PARTIAL',1,entry+MIN))
        market['BTCUSDT'][entry]=mkbar(entry,op=99.9)
        bs,state,parity,missing=m.reconstruct(e,market)
        self.assertEqual((len(bs),state,parity), (0,'OHLC_CONFLICT',0))

    def test_gap_is_not_interpolated(self):
        entry=MIN*1000
        e={'entry_ts':entry,'source_bars':[],'symbol':'BTCUSDT'}
        market={'BTCUSDT':{entry:mkbar(entry),entry+2*MIN:mkbar(entry+2*MIN)}}
        bars,state,_,missing=m.reconstruct(e,market)
        self.assertEqual((len(bars),state,missing),(1,'PARTIAL',entry+MIN))

    def test_full_360_exact(self):
        entry=MIN*1000
        e={'entry_ts':entry,'source_bars':[],'symbol':'BTCUSDT'}
        market={'BTCUSDT':{entry+i*MIN:mkbar(entry+i*MIN) for i in range(360)}}
        bars,state,parity,missing=m.reconstruct(e,market)
        self.assertEqual((len(bars),state,parity,missing),(360,'FULL_360',0,None))

    def test_page_merging(self):
        e1={'symbol':'BTCUSDT','entry_ts':0}
        e2={'symbol':'BTCUSDT','entry_ts':MIN}
        e3={'symbol':'AAVEUSDT','entry_ts':0}
        spans,pages=m.plan_requests([e1,e2,e3])
        self.assertEqual(len(spans),2)
        self.assertEqual(len(pages),2)
        self.assertEqual(sum((b-a)//MIN for _,a,b in pages),721)

    def test_fetch_checks_closed_and_duplicate(self):
        with self.assertRaises(ValueError):
            m.fetch_bars([('BTCUSDT',0,MIN)],fetcher=lambda *a,**kw:[[0,'100','101','99','100']],
                         pause=0,asof_ms=0)
        with self.assertRaises(ValueError):
            m.fetch_bars([('BTCUSDT',0,MIN)],fetcher=lambda *a,**kw:[[0,'100','101','99','100']]*2,
                         pause=0,asof_ms=3*MIN)

    def test_fetch_forces_bounds(self):
        def fake(symbol,start,end,timeout):
            return [[start,'100','101','99','100'],[end-MIN,'100','101','99','100']]
        d,req=m.fetch_bars([('BTCUSDT',0,3*MIN)],fetcher=fake,pause=0,asof_ms=4*MIN)
        self.assertEqual(len(d['BTCUSDT']),2)
        self.assertEqual(len(req),1)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.audit,self.db=fixture(self.root)
        self.asof=1_800_000_000_000+400*MIN

    def tearDown(self):self.tmp.cleanup()

    def test_frozen_target_status_restriction_and_current_identity(self):
        events,rejected,frozen=m.load_selected(self.audit,self.db,['BTCUSDT','AAVEUSDT'],self.asof)
        self.assertEqual((len(events),frozen),(2,2))
        self.assertEqual({e['status'] for e in events},set(m.ALLOWED_STATUSES))
        with closing(sqlite3.connect(self.db)) as con:
            con.execute("UPDATE paths SET status='REACTION' WHERE touch_key='a'")
            con.commit()
        events,rejected,frozen=m.load_selected(self.audit,self.db,['BTCUSDT'],self.asof)
        self.assertEqual((len(events),rejected), (1,{'IDENTITY_CHANGED':1}))

    def test_do_not_process_unmatured(self):
        events,rejected,frozen=m.load_selected(self.audit,self.db,['BTCUSDT'],
                                               1_800_000_000_000+200*MIN)
        self.assertEqual((len(events),rejected.get('NOT_YET_MATURE')), (0,2))

    def test_do_not_change_first_touch_sqlite(self):
        before=m.sha256_file(self.db)
        events,_,_=m.load_selected(self.audit,self.db,['BTCUSDT'],self.asof)
        self.assertEqual(m.sha256_file(self.db),before)
        self.assertEqual(len(events),2)

    def test_fake_history_produces_runner_compatible_360m_outputs(self):
        events,excluded,frozen=m.load_selected(self.audit,self.db,['BTCUSDT'],self.asof)
        spans,pages=m.plan_requests(events)
        def fetch(symbol,start,end,timeout):
            out=[]
            for ts in range(start,end,MIN):
                # First event: SL first, then later TP. Second event: short runner.
                op=100;hi=100.1;lo=99.9;cl=100
                if ts==events[0]['entry_ts']+MIN:lo=98.5
                if ts==events[0]['entry_ts']+60*MIN:hi=102.5
                if ts==events[1]['entry_ts']+60*MIN:lo=97.5
                out.append([ts,str(op),str(hi),str(lo),str(cl)])
            return out
        data,details=m.fetch_bars(pages,fetcher=fetch,pause=0,asof_ms=self.asof)
        out=self.root/'new_result'
        manifest=m.make_reports(events,data,out,details,self.audit,excluded,frozen,spans,pages)
        self.assertEqual(manifest['counts']['state_FULL_360'],2)
        with closing(sqlite3.connect(out/'recovered_paths.sqlite3')) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM recovered_paths').fetchone()[0],2)
        with (out/'events_outcomes.csv').open() as f:rows={r['touch_key']:r for r in csv.DictReader(f)}
        self.assertEqual(rows['a']['retest_time_to_run_2pct'],'61')
        self.assertEqual(rows['a']['retest_tp2_result'],'SL_FIRST')
        self.assertEqual(rows['b']['retest_time_to_run_2pct'],'61')
        self.assertEqual(rows['b']['path_quality'],'CONTIGUOUS_VALID')
        self.assertTrue((out/'manifest.json').is_file())
        with self.assertRaises(FileExistsError):
            m.make_reports(events,data,out,details,self.audit,excluded,frozen,spans,pages)

    def test_command_plan_no_write_or_network(self):
        before=m.sha256_file(self.db)
        # m.main invokes nothing network with --plan; precise expected page count.
        buffer=io.StringIO()
        with mock.patch('sys.stdout',buffer):
            ret=m.main(['--project',str(self.root),'--audit-events',str(self.audit),
                        '--symbols','BTCUSDT','--plan'])
        self.assertEqual(ret,0)
        self.assertIn('FT_HIST_RECOVERY_PLAN',buffer.getvalue())
        self.assertEqual(m.sha256_file(self.db),before)
        self.assertFalse((self.root/'reports/reaction_lab/first_touch_historical_recovery_v11').exists())

    def test_run_without_explicit_out_is_refused(self):
        with self.assertRaises(SystemExit):
            m.main(['--project',str(self.root),'--audit-events',str(self.audit),
                    '--symbols','BTCUSDT','--run'])


if __name__=='__main__':
    unittest.main()
