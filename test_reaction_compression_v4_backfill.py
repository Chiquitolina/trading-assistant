"""Selection/interval tests: no Binance requests or disk writes."""
import tempfile
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch
import reaction_compression_v4_backfill as hist
import reaction_compression_breakout_retest_v4 as v4

class HistoryTests(unittest.TestCase):
    def test_intervals_match_exact_v4_selection_and_merge_overlap(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);folder=root/v4.v3.ROOT_REL;folder.mkdir(parents=True)
            con=sqlite3.connect(folder/v4.v3.CTX_NAME)
            con.execute('CREATE TABLE events(event_key TEXT,symbol TEXT,side TEXT,retest_ts INTEGER,observation_type TEXT)')
            m=60_000
            events=[(f'X|LONG|{t}','X','LONG',t,'LIVE_OBSERVED') for t in (200_000*m,200_100*m)]
            events.append(('Z|SHORT|999','Z','SHORT',200_105*m,'RECOVERED_WINDOW'))
            con.executemany('INSERT INTO events VALUES(?,?,?,?,?)',events)
            con.commit();con.close()
            selected,intervals,prior=hist.requests_for_selected(root,0,'all-live',48)
            self.assertEqual(len(selected),2)
            self.assertEqual(list(intervals),['X'])
            self.assertEqual(len(intervals['X']),1)
            self.assertEqual(prior,180)
            self.assertEqual(intervals['X'][0][0],events[0][3]-180*60*m)
            self.assertEqual(intervals['X'][0][1],events[1][3])
            self.assertFalse((folder/v4.v3.HISTORY_NAME).exists())
            info=hist.plan(root,0,'all-live',48,0)
            self.assertEqual(info['selected_events'],2)
            self.assertFalse(info['network_calls'])
            self.assertFalse(info['writes'])

    def test_larger_scan_window_extends_backfill(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);folder=root/v4.v3.ROOT_REL;folder.mkdir(parents=True)
            con=sqlite3.connect(folder/v4.v3.CTX_NAME)
            con.execute('CREATE TABLE events(event_key TEXT,symbol TEXT,side TEXT,retest_ts INTEGER,observation_type TEXT)')
            con.execute('INSERT INTO events VALUES(?,?,?,?,?)',('X|LONG|1','X','LONG',200_000*60_000,'LIVE_OBSERVED'))
            con.commit();con.close()
            _,_,hours=hist.requests_for_selected(root,1,'all-live',72)
            self.assertEqual(hours,204)

if __name__=='__main__':unittest.main()
