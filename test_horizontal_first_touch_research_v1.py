#!/usr/bin/env python3
"""Small deterministic integration tests for read-only labels / outcome join."""
from __future__ import annotations
import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

import horizontal_first_touch_research_v1 as h

class HFTResearchV1(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p=Path(self.tmp.name)
        (self.p/'reports/reaction_lab').mkdir(parents=True)
        self.touchdb=self.p/h.SOURCE_REL
        with closing(sqlite3.connect(self.touchdb)) as con:
            con.execute('''CREATE TABLE first_touches(touch_key TEXT PRIMARY KEY,
                legacy_event_key TEXT,symbol TEXT,side TEXT,status TEXT,
                observation_type TEXT,retest_ts INTEGER,known_ts INTEGER)''')
            events=[('a','X|LONG|1740000000000','XUSDT','LONG','TOUCH_FAILED','LIVE_OBSERVED',1740000000000,1740000060000),
                    ('b','X|LONG|1740000060000','XUSDT','LONG','TOUCH_INDECISIVE','LIVE_OBSERVED',1740000060000,1740000120000),
                    ('c','X|SHORT|1740000120000','XUSDT','SHORT','REACTION','LIVE_OBSERVED',1740000120000,1740000180000)]
            con.executemany('INSERT INTO first_touches VALUES (?,?,?,?,?,?,?,?)',events)
            con.commit()
        with closing(sqlite3.connect(self.p/h.ARCHIVE_REL)) as con:
            con.execute('''CREATE TABLE candles(symbol TEXT,timeframe TEXT,open_ts INTEGER,
                open REAL,high REAL,low REAL,close REAL,volume REAL,quote_volume REAL)''')
            # Exact 15m/30m contiguous support. Flat OHLC produces no pivot/level.
            for tf,step,count in [('15m',900000,160),('30m',1800000,80)]:
                end=(1740000000000//step)*step
                start=end-(count-1)*step
                con.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',
                    [('XUSDT',tf,start+i*step,100,101,99,100,1000,100000) for i in range(count)])
            con.commit()

    def test_plan_is_readonly_and_balanced(self):
        before=list(self.p.glob('reports/reaction_lab/**/*'))
        result=h.plan(self.p,1,False)
        self.assertEqual(result['first_touches'],3)
        self.assertEqual(result['statuses'],{'REACTION':1,'TOUCH_INDECISIVE':1,'TOUCH_FAILED':1})
        self.assertFalse(result['any_writes'])
        self.assertEqual(list(self.p.glob('reports/reaction_lab/**/*')),before)

    def test_unknown_is_not_far(self):
        out=self.p/'report'
        report=h.run(self.p,0,False,output_dir=out)
        self.assertEqual(report['events'],3)
        with (out/'event_labels.csv').open() as f:rows=list(csv.DictReader(f))
        self.assertEqual(set(r['status'] for r in rows),{'TOUCH_FAILED','TOUCH_INDECISIVE','REACTION'})
        # At least one later event may be covered; but never invent a near label.
        for row in rows:
            self.assertIn(row['group_pre'],('UNKNOWN_HISTORY','NO_PERSISTENT_LEVEL_NEAR_PRE'))
            if row['group_pre']=='UNKNOWN_HISTORY':self.assertEqual(row['near_pre'],'')
            if row['group_pre']=='NO_PERSISTENT_LEVEL_NEAR_PRE':self.assertEqual(row['near_pre'],'0')
        with self.assertRaises(FileExistsError):h.run(self.p,0,False,output_dir=out)

    def test_analyze_joins_on_exact_touch_key(self):
        out=self.p/'labels'
        h.run(self.p,0,False,output_dir=out)
        db=self.p/h.PATHS_REL
        with closing(sqlite3.connect(db)) as con:
            con.execute('CREATE TABLE metrics(touch_key TEXT,horizon INTEGER,perspective TEXT,mfe_pct REAL,mae_pct REAL,return_pct REAL)')
            con.execute('CREATE TABLE outcomes(touch_key TEXT,horizon INTEGER,perspective TEXT,tp REAL,sl REAL,outcome TEXT)')
            con.execute("INSERT INTO metrics VALUES ('a',15,'ORIGINAL_OPEN',1.5,0.3,0.9)")
            con.execute("INSERT INTO outcomes VALUES ('a',15,'ORIGINAL_OPEN',1,1,'TP_FIRST')")
            con.execute("INSERT INTO metrics VALUES ('different',15,'ORIGINAL_OPEN',50,50,50)")
            con.commit()
        result=h.analyze(self.p,out,analysis_dir=self.p/'analysis')
        self.assertEqual(result['metric_records_in_join'],1)
        with (self.p/'analysis/comparison_by_status_side.csv').open() as f: rows=list(csv.DictReader(f))
        found=[r for r in rows if r['status']=='TOUCH_FAILED' and r['perspective']=='ORIGINAL_OPEN' and r['horizon']=='15']
        self.assertEqual(len(found),1)
        self.assertEqual(found[0]['N_tp_first'],'1')
        self.assertEqual(found[0]['median_mfe_pct'],'1.5')
        self.assertEqual(found[0]['tp_first_pct'],'100.0')

    def test_positive_near_label_and_structural_id(self):
        out=self.p/'near_labels'
        def zones(e,tf,frame):
            if tf=='30m':return [],'COMPLETE'
            z=dict(event_key=e['event_key'],symbol=e['symbol'],side=e['side'],retest_ts=e['retest_ts'],
                timeframe='15m',zone_id='event_specific_'+e['event_key'],origin_kind='LOW',
                zone_low=99.8,zone_high=100.2,center=100.,formation_closed_ts=1700000000000,
                first_pivot_ts=1699990000000,age_bars=60,pivot_matches=3,independent_visits=3,
                confirmed_responses=3,response_min_atr=0.8,distance_pre_atr=0.1,relation_pre='ABOVE',
                level_status_pre='SUPPORT_BELOW',source_coverage='COMPLETE',
                visit_timestamps='1699990000000;1699995000000;1699999000000',
                response_timestamps='1699991000000;1699996000000;1700000000000')
            return [z],'COMPLETE'
        with patch.object(h.persistent,'candidate_zones',side_effect=zones):
            summary=h.run(self.p,0,False,output_dir=out)
        self.assertEqual(summary['groups'],{'PERSISTENT_LEVEL_NEAR_PRE':3})
        with (out/'event_labels.csv').open() as f:rows=list(csv.DictReader(f))
        self.assertEqual({r['structural_level_id'] for r in rows}.__len__(),1)
        self.assertEqual({r['chosen_role_pre'] for r in rows if r['side']=='LONG'}, {'REBOTE_COMPATIBLE_PRE'})
        self.assertTrue(all(r['coverage_all']=='1' for r in rows))

    def test_freeze_hash_protects_analysis(self):
        out=self.p/'labelz'
        h.run(self.p,0,False,output_dir=out)
        (out/'event_labels.csv').write_text('tampered')
        with self.assertRaisesRegex(RuntimeError,'Frozen labels modified'):
            h.analyze(self.p,out,analysis_dir=self.p/'bad')

    def test_redis_only_never_counts_unconfirmed(self):
        class Redis:
            def lrange(self,key,start,end):
                return [json.dumps({'timestamp':1740000000000,'close_timestamp':1740001000000,
                                    'open':1,'high':2,'low':1,'close':2,'volume':50})]
        self.assertEqual(h.redis_candles(Redis(),'XUSDT','15m',1740000090000),[])

    def test_structural_id_is_not_event_key(self):
        x=dict(symbol='XUSDT',timeframe='30m',origin_kind='LOW',formation_closed_ts=123000,center=100.)
        self.assertEqual(h.mk_id({**x,'event_key':'a'}),h.mk_id({**x,'event_key':'b'}))
        self.assertNotEqual(h.mk_id(x),h.mk_id({**x,'center':105.}))

    def test_archive_conflict_fails_closed(self):
        r=[(0,1,2,1,1,50,50)]
        with self.assertRaisesRegex(RuntimeError,'OHLCV conflict'):
            h.merge_rows([('ARCHIVE',r),('REDIS',[(0,1,3,1,1,50,50)])],'XUSDT','15m')

if __name__=='__main__':unittest.main(verbosity=2)
