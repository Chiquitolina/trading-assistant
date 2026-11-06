import csv
from contextlib import redirect_stdout
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import reaction_lab_first_touch_recovery_batches_v12 as m

MINUTE=60000


def ev(key,symbol,idx=0):
    return {'touch_key':key,'symbol':symbol,'entry_ts':(1000+idx*4)*MINUTE}


class PlanningTests(unittest.TestCase):
    def test_two_symbols_together(self):
        events=[ev('A','BTCUSDT'),ev('B','ETHUSDT')]
        batches,partial,oversized=m.build_batches(events,set(),max_pages=10)
        self.assertEqual((len(batches),len(partial),len(oversized)),(1,0,0))
        self.assertEqual(batches[0]['events'],2)
        self.assertEqual(set(batches[0]['symbols']),{'BTCUSDT','ETHUSDT'})

    def test_whole_symbol_never_split(self):
        events=[ev('a','BTCUSDT'),ev('b','BTCUSDT',1),ev('c','ETHUSDT')]
        batches,_,_=m.build_batches(events,set(),max_events=2)
        self.assertEqual([b['events'] for b in batches],[2,1])
        self.assertEqual(batches[0]['symbols'],['BTCUSDT'])

    def test_completed_and_partial_are_skipped(self):
        events=[ev('a','BTCUSDT'),ev('b','BTCUSDT',1),ev('c','ETHUSDT')]
        batches,partial,oversized=m.build_batches(events,{'a','c'})
        self.assertEqual(batches,[])
        self.assertEqual(partial,[{'symbol':'BTCUSDT','completed':1,'pending':1}])
        self.assertEqual(oversized,[])

    def test_oversized_symbol_skipped(self):
        events=[ev('a','BTCUSDT'),ev('b','BTCUSDT',1)]
        batches,partial,oversized=m.build_batches(events,set(),max_events=1)
        self.assertFalse(batches)
        self.assertEqual(oversized[0]['symbol'],'BTCUSDT')

    def test_total_budget(self):
        batches=[{'events':3,'pages':20,'unique_symbol_minutes':4000},
                 {'events':3,'pages':20,'unique_symbol_minutes':4000}]
        self.assertEqual(len(m.choose_batches(batches,10,35,10000)),1)
        self.assertEqual(len(m.choose_batches(batches,1,300,100000)),1)

    def test_plan_does_not_write_or_download(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            db=root/'reports/reaction_lab/first_touch_paths_v2.sqlite3'
            db.parent.mkdir(parents=True)
            with sqlite3.connect(db) as conn:
                conn.execute('''CREATE TABLE paths(touch_key TEXT PRIMARY KEY,symbol TEXT,side TEXT,
                   status TEXT,retest_ts INTEGER,entry_ts INTEGER,state TEXT,count INTEGER,
                   path_blob BLOB,retest_close REAL)''')
                conn.execute('INSERT INTO paths VALUES(?,?,?,?,?,?,?,?,?,?)',
                             ('A','BTCUSDT','LONG','TOUCH_FAILED',0,MINUTE,
                              'STALE_BEFORE_COLLECTOR',0,None,100.0))
            audit=root/'audit.csv'
            with audit.open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=['touch_key','symbol','side','status','retest_ts','entry_ts',
                                               'ft_state','audit_state'])
                w.writeheader()
                w.writerow({'touch_key':'A','symbol':'BTCUSDT','side':'LONG','status':'TOUCH_FAILED',
                            'retest_ts':0,'entry_ts':MINUTE,
                            'ft_state':'STALE_BEFORE_COLLECTOR','audit_state':'NO_AUTO_MATCH'})
            buf=io.StringIO()
            with redirect_stdout(buf),mock.patch.object(m,'single_batch_main',side_effect=AssertionError('network')):
                rc=m.main(['--project',str(root),'--audit-events',str(audit),'--plan'])
            self.assertEqual(rc,0)
            self.assertIn('"selected_events": 1',buf.getvalue())
            self.assertFalse((root/'reports/reaction_lab/first_touch_historical_recovery_v11').exists())


if __name__=='__main__':unittest.main()
