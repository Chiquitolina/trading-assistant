#!/usr/bin/env python3
"""Offline tests: causal phase, post-break timeline, as-of Market Flow, writes."""
import copy
import json
import sqlite3
import tempfile
import unittest
import zlib
from pathlib import Path

import reaction_lab_failed_behavior_v1 as v1
import reaction_lab_failed_second_break_v4 as v4
from dashboard.views.reaction_lab_flow_asof_view import get_market_flow_asof, enrich_first_touch_page

TS=1790000000000

def event(key='F',status='TOUCH_FAILED',side='SHORT',moves=None,ts=TS):
    if moves is None:moves=[0]*5+[.6,.8,.35,.10,1.05,1.38,1.73,.75,.80]+[.85]*48
    close0=100.2 if side=='SHORT' else 99.8
    sign=1 if side=='SHORT' else -1
    bars=[];last=close0
    for i,m in enumerate(moves):
        c=close0*(1+sign*m/100)
        bars.append((ts+(i+1)*60000,last,max(c,last),min(c,last),c))
        last=c
    return {'key':key,'symbol':'AAAUSDT','side':side,'status':status,'ts':ts,
        'seen_ms':ts,'observation':'LIVE_OBSERVED','meta':{'retest_close':close0,
        'swing_price':100.,'retest_low':99.2,'retest_high':100.8},'bars':bars,'state':'COMPLETE'}

class SecondBreakV4(unittest.TestCase):
    def test_second_is_close_confirmed_and_known(self):
        e=event();a=v4.recognize(e)
        self.assertEqual(a['second_status'],'SECOND_BREAKOUT')
        self.assertEqual(a['pullback_min'],8)
        self.assertEqual(a['second_min'],10)
        self.assertEqual(a['second_delay_from_pb'],2)
        self.assertGreater(a['second_overshoot_frozen_peak_pct'],.05)

    def test_no_data_future_changes_detection(self):
        e=event();r=v4.recognize(e)
        z=copy.deepcopy(e)
        z['bars']=z['bars'][:10]+[(t,o,h,l,c*.999999) for t,o,h,l,c in z['bars'][10:]]
        same=v4.recognize(z)
        for name in ['impulse_min','peak_min','pullback_min','second_min',
                     'second_close_price','second_overshoot_frozen_peak_pct']:
            self.assertEqual(r[name],same[name])

    def test_post_start_next_candle(self):
        e=event();a=v4.recognize(e);q=v4.post_second(e,a,5)
        self.assertEqual(q['first_0p25'],'CONTINUE')
        self.assertEqual(q['first_0p25_min'],1)
        self.assertEqual(q['recross_peak_status'],'YES')
        self.assertEqual(q['first_recross_peak_min'],3)
        self.assertTrue(q['full_coverage'])
        self.assertGreater(q['post_second_mfe_pct'],.5)

    def test_symmetric_long_and_short(self):
        a=v4.recognize(event(side='LONG'))
        b=v4.recognize(event(side='SHORT'))
        self.assertEqual(a['second_min'],b['second_min'])
        x=v4.post_second(event(side='LONG'),a,5)
        y=v4.post_second(event(side='SHORT'),b,5)
        self.assertEqual(x['first_0p25'],y['first_0p25'])
        self.assertEqual(x['first_recross_peak_min'],y['first_recross_peak_min'])

    def test_censor_does_not_falsely_mark_no_hit(self):
        e=event(moves=[0]*5+[.6,.8,.35,.1,1.05,1.38])
        a=v4.recognize(e)
        q=v4.post_second(e,a,5)
        self.assertEqual(q['first_0p25'],'CENSORED')
        self.assertEqual(q['recross_peak_status'],'CENSORED')
        self.assertIsNone(q['post_second_mfe_pct'])
        self.assertTrue(q['censored'])

    def test_missing_second_outcomes_kept(self):
        e=event(moves=[0]*5+[.6,.8,.35]+[.3]*45)
        a=v4.recognize(e)
        self.assertEqual(a['second_status'],'NEITHER')
        self.assertIsNone(v4.post_second(e,a,5))

    def test_cross_base_at_pb_not_second(self):
        e=event(moves=[0]*5+[.6,.8,-.2]+[1.5]*45)
        a=v4.recognize(e)
        self.assertEqual(a['second_status'],'BASELINE_ALREADY_AT_PB')

    def test_paired_future_requires_full_both(self):
        f=event('F')
        c=event('C','REACTION',moves=[0]*5+[.6,.8,.35,.1,1.05]+[.85]*2,ts=TS+120*60000)
        a=v4.recognize(f);b=v4.recognize(c)
        fa=v4.post_second(f,a,5);ca=v4.post_second(c,b,5)
        pairs=[{'failed_key':'F','control_key':'C'}]
        r=v4.paired_post_second(pairs,[a,b],[fa,ca],5)[0]
        self.assertFalse(r['both_full'])

    def test_flow_asof_for_every_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            target=root/'reports/reaction_lab';target.mkdir(parents=True)
            db=target/'market_flow_history_4h_v1.sqlite3'
            with sqlite3.connect(db) as con:
                con.execute('CREATE TABLE flow_snapshots(symbol TEXT, snapshot_close_ms INTEGER, available_at_ms INTEGER, return_rank_4h_pct REAL,volume_rank_4h_pct REAL,relative_volume_4h REAL,return_4h_pct REAL,flow_group TEXT,primary_sector TEXT)')
                con.executemany('INSERT INTO flow_snapshots VALUES(?,?,?,?,?,?,?,?,?)',[
                    ('AAAUSDT',TS-3600000,TS-120000,90,80,1.5,4.2,'Confirmed leadership','DeFi'),
                    ('AAAUSDT',TS-360000,TS+120000,99,98,6.0,8.0,'FUTURE','DeFi')])
            touches=[{'touch_key':'A','symbol':'AAAUSDT','retest_ts':TS,'status':'REACTION'},
                     {'touch_key':'B','symbol':'AAAUSDT','retest_ts':TS,'status':'TOUCH_FAILED'},
                     {'touch_key':'C','symbol':'AAAUSDT','retest_ts':TS,'status':'TOUCH_INDECISIVE'}]
            d,state=get_market_flow_asof(root,touches)
            self.assertEqual(set(d),{'A','B','C'})
            self.assertEqual(state,'OBSERVED_ARCHIVE')
            self.assertTrue(all(d[k]['return_rank_4h_pct']==90 for k in d))
            import pandas as pd
            df,_,n=enrich_first_touch_page(root,pd.DataFrame(touches))
            self.assertEqual(n,3)
            self.assertTrue((df.return_rank_4h_pct==90).all())

    def test_flow_no_archive_does_not_generate(self):
        with tempfile.TemporaryDirectory() as td:
            d,s=get_market_flow_asof(Path(td),[{'touch_key':'x','symbol':'AAAUSDT','retest_ts':TS}])
            self.assertEqual(d,{})
            self.assertEqual(s,'NO_ARCHIVE')

    def test_offline_run_preserves_source_dbs(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);rd=root/'reports/reaction_lab';rd.mkdir(parents=True)
            journal=rd/'scanner_first_touches_v1.sqlite3'
            paths=rd/'first_touch_paths_v2.sqlite3'
            f=event('F');c=event('C','REACTION',ts=TS+120*60000)
            with sqlite3.connect(journal) as con:
                con.execute('CREATE TABLE first_touches(touch_key TEXT,symbol TEXT,side TEXT,status TEXT,retest_ts INTEGER,first_seen_ms INTEGER,observation_type TEXT,causal_json TEXT)')
                for x in [f,c]:con.execute('INSERT INTO first_touches VALUES(?,?,?,?,?,?,?,?)',
                        [x['key'],x['symbol'],x['side'],x['status'],x['ts'],x['seen_ms'],x['observation'],json.dumps(x['meta'])])
            with sqlite3.connect(paths) as con:
                con.execute('CREATE TABLE paths(touch_key TEXT,status TEXT,state TEXT,count INTEGER,entry_ts INTEGER,path_blob BLOB)')
                for x in [f,c]:con.execute('INSERT INTO paths VALUES(?,?,?,?,?,?)',
                        [x['key'],x['status'],'COMPLETE',len(x['bars']),x['ts']+60000,zlib.compress(b''.join(v1.BAR.pack(*b) for b in x['bars']))])
            before=(journal.read_bytes(),paths.read_bytes())
            report=v4.run(root)
            self.assertEqual(report['failed'],1)
            self.assertEqual(report['failed_second_break_0p5'],1)
            self.assertEqual((journal.read_bytes(),paths.read_bytes()),before)
            self.assertTrue((root/v4.OUT_REL/'post_second_windows.csv').is_file())
            self.assertTrue((root/v4.OUT_REL/'strict_paired_post_second.csv').is_file())

if __name__=='__main__':unittest.main()
