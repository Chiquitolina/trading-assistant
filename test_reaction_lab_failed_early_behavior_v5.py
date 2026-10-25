#!/usr/bin/env python3
"""Offline unit/integration tests for landmark methodology and isolated output."""
import copy
import json
import sqlite3
import tempfile
import unittest
import zlib
from pathlib import Path

import reaction_lab_failed_behavior_v1 as v1
import reaction_lab_failed_second_break_v4 as v4
import reaction_lab_failed_early_behavior_v5 as v5

TS=1790000000000


def event(key='F', status='TOUCH_FAILED', side='SHORT', moves=None, ts=TS):
    if moves is None:
        moves=[0]*5+[.6,.8,.35,.10,1.05,1.38,1.73,.75,.80]+[.85]*48
    close0=100.2 if side=='SHORT' else 99.8
    sign=1 if side=='SHORT' else -1
    last=close0;bars=[]
    for i,delta in enumerate(moves):
        c=close0*(1+sign*delta/100)
        bars.append((ts+(i+1)*60000,last,max(c,last),min(c,last),c))
        last=c
    return dict(key=key,symbol='AAAUSDT',side=side,status=status,ts=ts,seen_ms=ts,
       observation='LIVE_OBSERVED',meta={'retest_close':close0,'swing_price':100.,
       'retest_low':99.2,'retest_high':100.8},bars=bars,state='COMPLETE')


class EarlyV5(unittest.TestCase):
    def episode(self,e=None):
        ev=e or event();ep=v4.recognize(ev,.5)
        self.assertEqual(ep['second_status'],'SECOND_BREAKOUT')
        self.assertEqual(ep['second_min'],10)
        return ev,ep

    def test_landmarks_use_only_closed_bars(self):
        ev,ep=self.episode()
        for t in v5.LANDMARKS:
            x=v5.early_landmark(ev,ep,t)
            self.assertEqual(x['known_min'],10+t)
            self.assertEqual(x['landmark_close_price'],ev['bars'][9+t][4])
        self.assertIsNone(v5.early_landmark({**ev,'bars':ev['bars'][:10]},ep,1))

    def test_post_observation_future_not_reuse_current_close(self):
        ev,ep=self.episode();one=v5.early_landmark(ev,ep,1)
        w=v5.forward_window(ev,ep,one,5)
        self.assertEqual(w['first_0p25'],'CONTINUE')
        self.assertEqual(w['first_0p25_delay'],1)
        self.assertEqual(w['early_minute'],1)
        self.assertEqual(w['known_min'],11)
        # t1 was +0.33%; t2 adds >0.25% before the following reversal.

    def test_future_changes_do_not_change_known_features(self):
        ev,ep=self.episode()
        obs=v5.early_landmark(ev,ep,2)
        mutated=copy.deepcopy(ev)
        for i in range(12,len(mutated['bars'])):
            ts,o,h,l,c=mutated['bars'][i]
            mutated['bars'][i]=(ts,o,h,l,c*1.0005)
        self.assertEqual(obs,v5.early_landmark(mutated,ep,2))
        later=v5.forward_window(mutated,ep,obs,15)
        self.assertEqual(later['known_min'],obs['known_min'])

    def test_already_lost_not_future_reentry(self):
        ev,ep=self.episode()
        a=v5.early_landmark(ev,ep,3)
        self.assertEqual(a['early_state'],'LOST_PEAK')
        self.assertFalse(a['still_beyond_peak'])
        w=v5.forward_window(ev,ep,a,5)
        self.assertFalse(w['eligible_future_recross'])
        self.assertEqual(w['future_peak_recross_status'],'ALREADY_LOST')
        self.assertIsNone(w['future_peak_recross_delay_min'])

    def test_live_peaks_still_at_risk(self):
        ev,ep=self.episode()
        a=v5.early_landmark(ev,ep,1)
        self.assertTrue(a['still_beyond_peak'])
        self.assertFalse(a['already_recrossed_peak'])
        w=v5.forward_window(ev,ep,a,5)
        self.assertEqual(w['future_peak_recross_status'],'RE_CROSSED')
        self.assertEqual(w['future_peak_recross_delay_min'],2)

    def test_censored_is_not_neither(self):
        ev,ep=self.episode()
        ev['bars']=ev['bars'][:12]
        obs=v5.early_landmark(ev,ep,2)
        w=v5.forward_window(ev,ep,obs,5)
        self.assertTrue(w['censored'])
        self.assertEqual(w['first_0p5'],'CENSORED')
        self.assertIsNone(w['future_mfe_wick_pct'])
        self.assertEqual(w['future_peak_recross_status'],'CENSORED' if obs['still_beyond_peak'] else 'ALREADY_LOST')

    def test_opposite_side_symmetry(self):
        a,b=self.episode(event(side='LONG'))
        c,d=self.episode(event(side='SHORT'))
        obs_a=v5.early_landmark(a,b,1)
        obs_c=v5.early_landmark(c,d,1)
        self.assertEqual(obs_a['early_state'],obs_c['early_state'])
        self.assertAlmostEqual(obs_a['move_since_second_pct'],obs_c['move_since_second_pct'],delta=.01)
        fa=v5.forward_window(a,b,obs_a,10)
        fc=v5.forward_window(c,d,obs_c,10)
        self.assertEqual(fa['first_0p5'],fc['first_0p5'])

    def test_competing_race_detects_neither_and_both(self):
        ev,ep=self.episode()
        obs=v5.early_landmark(ev,ep,1)
        r=v5.forward_window(ev,ep,obs,20)
        self.assertEqual(r['first_0p5'],'REVERSE')
        self.assertIsInstance(r['both_0p25'],bool)
        self.assertTrue(r['full_coverage'])

    def test_summary_censors_denominator(self):
        ev,ep=self.episode()
        obs=v5.early_landmark(ev,ep,1)
        row=v5.forward_window(ev,ep,obs,5)
        missing=dict(row,full_coverage=False,first_0p5='CENSORED')
        ans=v5.summary([row,missing],('cohort_type','early_minute','forward_horizon'))[0]
        self.assertEqual(ans['N'],2)
        self.assertEqual(ans['N_full'],1)
        self.assertEqual(ans['N_censored'],1)

    def test_pair_strict_stage_and_future(self):
        e1,e2=self.episode(event('F'))
        e3,e4=self.episode(event('C','REACTION',ts=TS+120*60000))
        ka=v5.early_landmark(e1,e2,1);kb=v5.early_landmark(e3,e4,1)
        wa=v5.forward_window(e1,e2,ka,5);wb=v5.forward_window(e3,e4,kb,5)
        r=v5.paired_landmarks([{'failed_key':'F','control_key':'C'}],[ka,kb],[wa,wb])
        x=[z for z in r if z['threshold_pct']==.5 and z['landmark']==1 and z['forward_horizon']==5][0]
        self.assertTrue(x['both_full'])
        self.assertTrue(x['both_same_impulse'])
        wb['full_coverage']=False
        y=v5.paired_landmarks([{'failed_key':'F','control_key':'C'}],[ka,kb],[wa,wb])
        self.assertFalse([z for z in y if z['threshold_pct']==.5 and z['landmark']==1 and z['forward_horizon']==5][0]['both_full'])

    def test_offline_run_creates_only_reports(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); rd=root/'reports/reaction_lab';rd.mkdir(parents=True)
            journal=rd/'scanner_first_touches_v1.sqlite3'
            paths=rd/'first_touch_paths_v2.sqlite3'
            events=[event('F'),event('C','REACTION',ts=TS+120*60000)]
            with sqlite3.connect(journal) as con:
                con.execute('CREATE TABLE first_touches(touch_key TEXT,symbol TEXT,side TEXT,status TEXT,retest_ts INTEGER,first_seen_ms INTEGER,observation_type TEXT,causal_json TEXT)')
                for e in events:con.execute('INSERT INTO first_touches VALUES(?,?,?,?,?,?,?,?)',
                    [e['key'],e['symbol'],e['side'],e['status'],e['ts'],e['seen_ms'],e['observation'],json.dumps(e['meta'])])
            with sqlite3.connect(paths) as con:
                con.execute('CREATE TABLE paths(touch_key TEXT,status TEXT,state TEXT,count INTEGER,entry_ts INTEGER,path_blob BLOB)')
                for e in events:con.execute('INSERT INTO paths VALUES(?,?,?,?,?,?)',
                    [e['key'],e['status'],'COMPLETE',len(e['bars']),e['ts']+60000,zlib.compress(b''.join(v1.BAR.pack(*b) for b in e['bars']))])
            before=(journal.read_bytes(),paths.read_bytes())
            result=v5.run(root)
            self.assertEqual(result['failed_total'],1)
            self.assertEqual(result['second_break_failed_0p5'],1)
            self.assertEqual((journal.read_bytes(),paths.read_bytes()),before)
            for file in ('report.json','ANALYSIS.md','early_landmarks.csv','forward_windows.csv',
                         'focus_hypotheses_summary.csv','future_trajectories.csv'):
                self.assertTrue((root/v5.OUT_REL/file).is_file())


if __name__=='__main__':unittest.main()
