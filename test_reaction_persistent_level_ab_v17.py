import csv
import sqlite3
import tempfile
from pathlib import Path
import struct
import unittest
import zlib
from reaction_persistent_level_ab_v17 import (GROUPS, inspect_label_file, load_labels, simulate_path,
          validate_path, first_observed_index, one_slot, summarize, BAR, M, locate_labels)

class TestCore(unittest.TestCase):
    def bars(self, high=101, low=99, count=360, ts=60_000):
        return [(ts+i*M,100.,high,low,100.) for i in range(count)]
    def test_bar_struct_matches_frozen_auto_path(self):
        row=self.bars()[0];raw=zlib.compress(BAR.pack(*row))
        from reaction_persistent_level_ab_v17 import decode_path
        self.assertEqual(decode_path(raw)[0],row)
    def test_valid_full_360(self):
        self.assertTrue(validate_path(self.bars(),0,M,360))
        self.assertFalse(validate_path(self.bars(count=359),0,M,359))
    def test_no_imputed_gaps(self):
        b=self.bars();b[4]=(b[4][0]+M,*b[4][1:]);self.assertFalse(validate_path(b,0,M,360))
    def test_delayed_observable_entry(self):
        self.assertEqual(first_observed_index(self.bars(),60_000,180_000),2)
        self.assertIsNone(first_observed_index(self.bars(),None,180_000))
    def test_tp_long(self):
        b=self.bars(high=101,low=99.5);b[3]=(b[3][0],100,104,99.5,103)
        r=simulate_path(b,0,'LONG');self.assertEqual(r['state'],'TP_FIRST')
        self.assertGreater(r['net_pct'],1.7)
    def test_sl_long(self):
        b=self.bars(high=100.1,low=99.5);b[0]=(b[0][0],100,100.1,97,98)
        r=simulate_path(b,0,'LONG');self.assertEqual(r['state'],'SL_FIRST');self.assertLess(r['net_pct'],-1.5)
    def test_both_conservative(self):
        b=self.bars(high=104,low=97)
        r=simulate_path(b,0,'LONG');self.assertEqual(r['state'],'BOTH_SAME_1M_SL_CONSERVATIVE')
    def test_tp_short(self):
        b=self.bars(high=100.3,low=98.5);b[2]=(b[2][0],100,100.3,96,97)
        r=simulate_path(b,0,'SHORT');self.assertEqual(r['state'],'TP_FIRST')
    def test_no_hit_timeout(self):
        b=self.bars(high=100.4,low=99.8)
        r=simulate_path(b,0,'LONG');self.assertEqual(r['state'],'NO_HIT')
        self.assertEqual(r['exit_ts'],b[-1][0]+M)
    def test_label_duplicate_fails(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'event_labels.csv'
            p.write_text('event_key,group\na,PERSISTENT_LEVEL_NEAR_PRE\na,PERSISTENT_LEVEL_NEAR_PRE\n')
            with self.assertRaises(RuntimeError):load_labels(inspect_label_file(p))
    def test_informative_separate_unknown(self):
        r=[{'group':GROUPS[0],'outcome':'TP_FIRST','net_pct':1.8,'symbol':'X'},
           {'group':GROUPS[2],'outcome':'NO_HIT','net_pct':-1,'symbol':'Y'}]
        self.assertEqual(summarize(r,GROUPS[0])['N_labels'],1)
        self.assertEqual(summarize(r,GROUPS[1])['N_labels'],0)
    def test_one_slot_discards_overlap(self):
        a=[{'group':GROUPS[0],'entry_ts':60_000,'exit_ts':300_000,'net_pct':1.,'symbol':'A','event_key':'a','outcome':'TP_FIRST'},
           {'group':GROUPS[0],'entry_ts':100_000,'exit_ts':240_000,'net_pct':-1.,'symbol':'B','event_key':'b','outcome':'SL_FIRST'},
           {'group':GROUPS[0],'entry_ts':300_000,'exit_ts':420_000,'net_pct':1.,'symbol':'C','event_key':'c','outcome':'TP_FIRST'}]
        x=one_slot(a,GROUPS[0]);self.assertEqual((x['trades'],x['skipped_slot_busy']),(2,1))

if __name__=='__main__':unittest.main()

class TestSQLiteReadOnly(unittest.TestCase):
    def test_joins_same_event_key_and_complete_path_only(self):
        import sqlite3
        import json
        from reaction_persistent_level_ab_v17 import load_reactions, EVENT_DB, PATH_DB
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/EVENT_DB).parent.mkdir(parents=True,exist_ok=True)
            conn=sqlite3.connect(root/EVENT_DB)
            conn.execute('CREATE TABLE events(event_key TEXT PRIMARY KEY,symbol TEXT,side TEXT,retest_ts INTEGER,known_ts INTEGER,first_seen_ms INTEGER,observation_type TEXT)')
            conn.executemany('INSERT INTO events VALUES (?,?,?,?,?,?,?)',[
                ('A|LONG|0','A','LONG',0,60_000,60_000,'LIVE_OBSERVED'),
                ('B|SHORT|0','B','SHORT',0,60_000,60_000,'LIVE_OBSERVED'),
                ('C|LONG|0','C','LONG',0,60_000,60_000,'REPLAY_BACKFILLED')])
            conn.commit();conn.close()
            conn=sqlite3.connect(root/PATH_DB)
            conn.execute('CREATE TABLE events(event_key TEXT PRIMARY KEY,entry_ts INTEGER,state TEXT,count INTEGER,path_blob BLOB)')
            valid=[(60_000+i*M,100.,100.4,99.8,100.) for i in range(360)]
            packed=zlib.compress(b''.join(BAR.pack(*row) for row in valid))
            conn.execute('INSERT INTO events VALUES(?,?,?,?,?)',('A|LONG|0',60_000,'COMPLETE',360,packed))
            conn.execute('INSERT INTO events VALUES(?,?,?,?,?)',('B|SHORT|0',60_000,'GAPPED',12,None))
            conn.commit();conn.close()
            rows,qa=load_reactions(root,{'A|LONG|0':GROUPS[0], 'B|SHORT|0':GROUPS[1], 'C|LONG|0':GROUPS[2]})
            self.assertEqual(len(rows),3)
            self.assertEqual(rows[0]['outcome'],'NO_HIT')
            self.assertEqual(rows[1]['outcome'],'PATH_MISSING_OR_INCOMPLETE')
            self.assertEqual(rows[2]['outcome'],'NOT_LIVE_OBSERVED')
            self.assertEqual(qa['labels_not_in_scanner'],0)
