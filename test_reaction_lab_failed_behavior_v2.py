import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import reaction_lab_failed_behavior_v2 as v2

TS=1790000000000

def make(side='SHORT',moves=None,key='X',status='TOUCH_FAILED',sym='AUSDT',date_offset=0,level=100):
    if moves is None:moves=[0,0,0,0,0,.6,.8,.4,.35,1.05]
    start=100.2 if side=='SHORT' else 99.8
    sign=1 if side=='SHORT' else -1
    bars=[];last=start
    for i,move in enumerate(moves,1):
        close=start*(1+sign*move/100)
        bars.append((TS+date_offset+i*60000,last,max(last,close),min(last,close),close));last=close
    meta={'retest_close':start,'swing_price':level}
    return dict(key=key,symbol=sym,side=side,status=status,ts=TS+date_offset,
        seen_ms=TS,observation='LIVE_OBSERVED',meta=meta,bars=bars,state='COMPLETE')

class LabV2(unittest.TestCase):
    def test_first_impulse_pullback_second_breakout(self):
        e=make()
        p=v2.pattern(e,landmark=5,horizon=5,threshold=.5)
        self.assertEqual(p['resolution'],'SECOND_BREAKOUT')
        self.assertEqual((p['impulse_min'],p['pullback_min'],p['second_resolution_min']),(6,8,10))
    def test_opposite_first_direction(self):
        e=make(moves=[0]*5+[-.65,-.9,-.3,-.45,-1.2])
        p=v2.pattern(e,5,5,.5)
        self.assertEqual(p['impulse_direction'],'REVERSAL')
        self.assertEqual(p['resolution'],'SECOND_BREAKOUT')
    def test_baseline_cross_at_pullback_candle(self):
        e=make(moves=[0]*5+[.6,.9,-.01,.3,.8])
        p=v2.pattern(e,5,5,.5)
        self.assertEqual(p['resolution'],'BASELINE_CROSSED')
        self.assertEqual(p['second_resolution_min'],8)
    def test_no_lookahead_features(self):
        a=make(moves=[0,0,.1,.2,.2,.6,.9,.4,.3,1.1])
        b=make(moves=[0,0,.1,.2,.2,-.8,-1.2,-.7,-.5,-.8])
        self.assertEqual(v2.observation(a),v2.observation(b))
        self.assertNotEqual(v2.pattern(a,5,5,.5)['resolution'],v2.pattern(b,5,5,.5)['resolution'])
    def test_censor_not_no_impulse(self):
        e=make(moves=[0]*6)
        p=v2.pattern(e,5,30,.5)
        self.assertFalse(p['full_coverage'])
        self.assertEqual(p['resolution'],'CENSORED_NO_IMPULSE')
    def test_complete_no_impulse(self):
        e=make(moves=[0]*35)
        p=v2.pattern(e,5,30,.5)
        self.assertEqual(p['resolution'],'NO_IMPULSE')
    def test_direction_symmetry(self):
        moves=[0]*5+[.6,.8,.4,.35,1.05]
        a=v2.pattern(make('SHORT',moves),5,5,.5)
        b=v2.pattern(make('LONG',moves),5,5,.5)
        self.assertEqual(a['resolution'],b['resolution'])
    def test_future_order_second_is_not_a_prior_peak(self):
        p=v2.pattern(make(moves=[0]*5+[.6,.9,.45,.91,1.1]),5,5,.5)
        self.assertEqual(p['resolution'],'SECOND_BREAKOUT')
        self.assertEqual(p['second_resolution_min'],10)
    def test_match_date_and_side_only(self):
        f=make(key='FAILED')
        c=make(key='CONTROL',status='REACTION',date_offset=2*3600000)
        n=make(key='WRONG',status='REACTION',side='LONG',date_offset=2*3600000)
        obs=[v2.observation(x) for x in [f,c,n]]
        matches=v2.nearest_controls([obs[0]],obs[1:])
        self.assertEqual(len(matches),1)
        self.assertEqual(matches[0]['control_key'],'CONTROL')
    def test_no_self_or_near_overlap(self):
        f=make(key='A');c=make(key='B',status='REACTION',date_offset=1*60000)
        self.assertFalse(v2.nearest_controls([v2.observation(f)],[v2.observation(c)]))
    def test_flow_not_backfill_from_future_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/v2.ARCHIVE_REL;p.parent.mkdir(parents=True)
            with sqlite3.connect(p) as con:
                con.executescript(v2.FLOW_SQL)
                con.execute('INSERT INTO flow_snapshots (symbol,snapshot_close_ms,observed_ms,available_at_ms,return_rank_4h_pct) VALUES (?,?,?,?,?)',
                   ('AUSDT',TS-3600000,TS+2*60000,TS+2*60000,95))
            row=v2.read_flow_asof(d,[make()])[0]
            self.assertNotIn('X',row)
    def test_flow_prior_observed_join_is_causal(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/v2.ARCHIVE_REL;p.parent.mkdir(parents=True)
            with sqlite3.connect(p) as con:
                con.executescript(v2.FLOW_SQL)
                con.execute('INSERT INTO flow_snapshots (symbol,snapshot_close_ms,observed_ms,available_at_ms,return_rank_4h_pct) VALUES (?,?,?,?,?)',
                   ('AUSDT',TS-3600000,TS-1000,TS-1000,96))
            row=v2.read_flow_asof(d,[make()])[0]
            self.assertEqual(row['X']['return_rank_4h_pct'],96)
    def test_asof_expired(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/v2.ARCHIVE_REL;p.parent.mkdir(parents=True)
            with sqlite3.connect(p) as con:
                con.executescript(v2.FLOW_SQL)
                con.execute('INSERT INTO flow_snapshots (symbol,snapshot_close_ms,observed_ms,available_at_ms,return_rank_4h_pct) VALUES (?,?,?,?,?)',
                   ('AUSDT',TS-10*3600000,TS-1000,TS-1000,98))
            row=v2.read_flow_asof(d,[make()])[0]
            self.assertNotIn('X',row)
    def test_readonly_source_event_integrity(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            j=p/'j.sqlite';s=p/'p.sqlite'
            e=make(moves=[0]*10)
            with sqlite3.connect(j) as db:
                db.execute('CREATE TABLE first_touches (touch_key TEXT,symbol TEXT,side TEXT,status TEXT,retest_ts INTEGER,first_seen_ms INTEGER,observation_type TEXT,causal_json TEXT)')
                db.execute('INSERT INTO first_touches VALUES(?,?,?,?,?,?,?,?)',(e['key'],e['symbol'],e['side'],e['status'],e['ts'],e['seen_ms'],e['observation'],json.dumps(e['meta'])))
            with sqlite3.connect(s) as db:
                db.execute('CREATE TABLE paths (touch_key TEXT,status TEXT,state TEXT,count INTEGER,entry_ts INTEGER,path_blob BLOB)')
                import struct,zlib
                blob=zlib.compress(b''.join(v2.base.BAR.pack(*b) for b in e['bars']))
                db.execute('INSERT INTO paths VALUES(?,?,?,?,?,?)',(e['key'],e['status'],'COMPLETE',len(e['bars']),e['ts']+60000,blob))
            actual=v2.read_all_first_touches(j,s)
            self.assertEqual(len(actual[0]['bars']),10)
            self.assertEqual(actual[0]['status'],'TOUCH_FAILED')

if __name__=='__main__':unittest.main()
