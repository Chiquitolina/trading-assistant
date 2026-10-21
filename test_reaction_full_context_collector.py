from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import reaction_full_context_collector as c
import runner_precursor_pilot as p

BASE=(1_790_000_000_000//(15*p.MIN))*(15*p.MIN)


def synthetic_fetch(sym,tf,start,end):
    step=p.TFS[tf]
    bars=[]
    for ts in range(start,end+1,step):
        factor=15 if tf=='15m' else 1
        bars.append((ts,100.,100.,100.,100.,10.*factor,1000.*factor,
                     factor,5.*factor,500.*factor))
    return bars,'mock-'+sym+'-'+tf


def setup(root,invalid=False):
    parent=root/c.REL
    parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(parent/'scanner_events_v1.sqlite3') as con:
        con.execute('''CREATE TABLE events(event_key TEXT PRIMARY KEY,symbol TEXT,side TEXT,
            retest_ts INTEGER,known_ts INTEGER,first_seen_ms INTEGER,observation_type TEXT,
            source_hash TEXT,causal_json TEXT)''')
        def add(sym,side,when,seen_delay,obs='LIVE_OBSERVED'):
            con.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)',
                  (f'{sym}|{side}|{when}',sym,side,when,when+p.MIN,
                   when+p.MIN+seen_delay,obs,'frozen-causal',json.dumps({'a':1})))
        add('XUSDT','LONG',BASE+180*p.MIN,40_000)
        add('YUSDT','SHORT',BASE+190*p.MIN,90_000,'RECOVERED_WINDOW')
        if invalid:add('ZUSDT','LONG',BASE+200*p.MIN,-80_000)


class FullContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        setup(self.root)
        self.db=self.root/c.REL/c.DB

    def tearDown(self):self.tmp.cleanup()

    def test_plan_is_read_only_and_label_blind(self):
        report=c.load_plan(self.root,self.db,pre1=10,pre15=4)
        self.assertFalse(self.db.exists())
        self.assertEqual(report['events'],2)
        self.assertEqual(report['eligible_context'],2)
        self.assertGreater(report['unique_required_bars'],0)
        self.assertEqual(set(report['observation_types']),{'LIVE_OBSERVED','RECOVERED_WINDOW'})
        self.assertFalse(report['writes_or_network_calls'])
        self.assertEqual(report['anchors'],['PRE_RETEST','PRE_KNOWN','PRE_OBSERVED'])

    def test_closed_only_anchors_and_merged_requests(self):
        e=c.normalize(c.source_events(self.root))
        spans=c.spans_for(e,10,4)
        x=e[0]
        end=(x['retest_ts']//p.MIN)*p.MIN
        self.assertTrue(any(a<=end-10*p.MIN and b>=end-p.MIN
                            for a,b in spans[(x['symbol'],'1m')]))
        self.assertLess(c.required_count(spans),2*3*2*2*(10+4))
        self.assertNotIn(('YUSDT','4h'),spans)

    def test_run_resume_freeze_hash_audit_export(self):
        cap=c.download(self.root,self.db,10,4,max_requests=2,pause=0,min_free_gb=0,
                       fetch_fn=synthetic_fetch)
        self.assertFalse(cap['complete'])
        with closing(p.ro(self.db)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0],2)
        # Modifying scanner must not change frozen historical cohort.
        with sqlite3.connect(self.root/c.REL/'scanner_events_v1.sqlite3') as db:
            db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)',
                (f'ZUSDT|LONG|{BASE+210*p.MIN}','ZUSDT','LONG',BASE+210*p.MIN,
                 BASE+211*p.MIN,BASE+211*p.MIN+5_000,'LIVE_OBSERVED','new','{}'))
        frozen_sha=c.load_plan(self.root,self.db,10,4)['cohort_sha256']
        self.assertEqual(c.load_plan(self.root,self.db,10,4)['events'],2)
        done=c.download(self.root,self.db,10,4,max_requests=300,pause=0,min_free_gb=0,
                        fetch_fn=synthetic_fetch)
        self.assertTrue(done['complete'])
        again=c.download(self.root,self.db,10,4,max_requests=1,pause=0,min_free_gb=0,
                         fetch_fn=lambda *args: (_ for _ in ()).throw(AssertionError('unexpected refetch')))
        self.assertTrue(again['complete'])
        audit=c.audit(self.root,self.db)
        self.assertTrue(audit['success'],audit)
        self.assertEqual(audit['windows_per_event'],12)
        self.assertEqual(audit['aggregate']['unverified_mismatches_total'],0)
        self.assertEqual(c.load_plan(self.root,self.db,10,4)['cohort_sha256'],frozen_sha)
        summary=c.export(self.root,self.db)
        self.assertEqual(summary['events'],2)
        self.assertTrue((self.root/c.REL/c.OUT/'events_catalog.csv').is_file())
        with closing(sqlite3.connect(self.db)) as db:
            # Tampering with the frozen catalog is never silently accepted.
            db.execute("UPDATE events SET symbol='MUTATED' WHERE symbol='XUSDT'")
            db.commit()
        with self.assertRaisesRegex(RuntimeError,'integrity'):
            c.audit(self.root,self.db)

    def test_cache_import_is_provenanced_and_conflicts_fail(self):
        with closing(p.connect(self.root/c.REL/p.DB_NAME)) as v1:
            bar=synthetic_fetch('XUSDT','1m',BASE+170*p.MIN,BASE+170*p.MIN)[0][0]
            p.ingest(v1,'XUSDT','1m',bar[0],bar[0],[bar],'pilot')
        with closing(c.own_connect(self.db)) as db:
            events=c.freeze(db,self.root,10,4)
            result=c.reuse_caches(self.root,db,c.spans_for(events,10,4))
            self.assertEqual(result['inserted'],1)
            self.assertEqual(db.execute('SELECT source FROM candles').fetchone()[0],
                             'CACHE:'+p.DB_NAME)
            self.assertEqual(c.reuse_caches(self.root,db,c.spans_for(events,10,4)),{})
            changed=list(bar);changed[1]=100.01
            new,bad=c.add_bars(db,'XUSDT','1m',[changed],'BAD_SOURCE')
            self.assertEqual((new,bad),(0,1))
            self.assertEqual(db.execute('SELECT open FROM candles').fetchone()[0],100.)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM conflicts').fetchone()[0],1)

    def test_unknown_15m_aggregation_blocks_export(self):
        c.download(self.root,self.db,16,4,max_requests=300,pause=0,min_free_gb=0,
                   fetch_fn=synthetic_fetch)
        with closing(sqlite3.connect(self.db)) as db:
            ts=db.execute("SELECT open_ts FROM candles WHERE symbol='XUSDT' AND timeframe='15m' ORDER BY open_ts LIMIT 1").fetchone()[0]
            db.execute("UPDATE candles SET open=open+0.01 WHERE symbol='XUSDT' AND timeframe='15m' AND open_ts=?",(ts,))
            db.commit()
        result=c.audit(self.root,self.db)
        # If 1m coverage overlaps a changed 15m bar, it must be caught.
        with closing(p.ro(self.db)) as db:
            mismatched=c.aggregate_audit(db,c.frozen_events(db))
            if mismatched['unverified_mismatches_total']:
                self.assertFalse(result['success'])
                with self.assertRaises(RuntimeError):c.export(self.root,self.db)

    def test_invalid_metadata_preserved_and_not_downloaded(self):
        # New root rather than mutating an already frozen cohort.
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);setup(root,invalid=True)
            result=c.load_plan(root,root/c.REL/c.DB,10,4)
            self.assertEqual(result['events'],3)
            self.assertEqual(result['eligible_context'],2)
            self.assertEqual(result['excluded_by_metadata']['INVALID_CAUSAL_TIMING'],1)

    def test_provider_refetch_pins_exact_data_and_rejects_later_mutation(self):
        c.download(self.root,self.db,20,4,max_requests=300,pause=0,min_free_gb=0,
                   fetch_fn=synthetic_fetch)
        ts=BASE+165*p.MIN   # All 15 underlying 1m candles exist in our 20m history.
        with sqlite3.connect(self.db) as db:
            db.execute("""UPDATE candles SET open=100.01,high=100.01
                  WHERE symbol='XUSDT' AND timeframe='15m' AND open_ts=?""",(ts,))
        self.assertEqual(c.audit(self.root,self.db)['aggregate']['unverified_mismatches_total'],1)

        # First verification fails: independent provider 15m is 100, not stored 100.01.
        no=c.verify_provider(self.root,self.db,max_verify=10,pause=0,fetch_fn=synthetic_fetch)
        self.assertEqual(no.get('verified',0),0)
        self.assertEqual(no.get('NOT_VERIFIED',0),1)
        self.assertEqual(c.audit(self.root,self.db)['aggregate']['unverified_mismatches_total'],1)

        def exchange_returns_both_series(sym,tf,a,b):
            bars,sha=synthetic_fetch(sym,tf,a,b)
            if sym=='XUSDT' and tf=='15m' and a==ts:
                row=list(bars[0]);row[1]=100.01;row[2]=100.01
                return [tuple(row)],sha+'-different15m'
            return bars,sha

        ok=c.verify_provider(self.root,self.db,max_verify=10,pause=0,
                             fetch_fn=exchange_returns_both_series)
        self.assertEqual(ok.get('verified',0),1)
        report=c.audit(self.root,self.db)
        self.assertEqual(report['aggregate']['unverified_mismatches_total'],0)
        self.assertEqual(len(report['aggregate']['verified_provider_exceptions']),1)
        self.assertTrue(report['success'])

        # Pinned proof invalidated by ANY later change.
        with sqlite3.connect(self.db) as db:
            db.execute("""UPDATE candles SET volume=volume+1
                  WHERE symbol='XUSDT' AND timeframe='15m' AND open_ts=?""",(ts,))
        report=c.audit(self.root,self.db)
        self.assertEqual(report['aggregate']['unverified_mismatches_total'],1)
        self.assertFalse(report['success'])


if __name__=='__main__':unittest.main()
