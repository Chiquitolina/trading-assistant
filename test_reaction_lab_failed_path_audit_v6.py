#!/usr/bin/env python3
"""Offline causality, geometry and no-source-mutation checks for frozen audit V6."""
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
import reaction_lab_failed_path_audit_v6 as v6

TS=1790000000000


def event(key='F',status='TOUCH_FAILED',side='LONG',tail=None,ts=TS):
    # V5 canonical morphology: impulse at +.6, pullback, second @+1.05
    prefix=[0]*5+[.6,.8,.35,.10,1.05]
    if tail is None:tail=[1.4,1.6,2.0,1.1,1.0]+[1.1]*40
    vals=prefix+tail
    base=99.8 if side=='LONG' else 100.2
    sign=-1 if side=='LONG' else 1
    last=base;bars=[]
    for i,delta in enumerate(vals):
        close=base*(1+sign*delta/100)
        bars.append((ts+(i+1)*60000,last,max(last,close),min(last,close),close))
        last=close
    return dict(key=key,symbol='AAAUSDT',side=side,status=status,ts=ts,seen_ms=ts,
       observation='LIVE_OBSERVED',meta={'retest_close':base,'swing_price':100},bars=bars,state='COMPLETE')


class FrozenV6(unittest.TestCase):
    def prepare(self,e):
        ep=v4.recognize(e,.5)
        self.assertEqual(ep['second_status'],'SECOND_BREAKOUT')
        known=v5.early_landmark(e,ep,1)
        return ep,known

    def test_a_extends_to_genuine_new_extreme(self):
        e=event();ep,known=self.prepare(e)
        self.assertEqual(known['early_state'],'EXTENDED_0P25')
        x=v6.measure_window(e,ep,known,15)
        self.assertEqual(x['pathway'],'A_ACCELERATED')
        self.assertEqual(x['first_fresh_break_delay_min'],1)
        self.assertEqual(x['competing_result'],'FRESH_BREAK_FIRST')
        self.assertGreater(x['fresh_break_progress_threshold_pct'],x['known_extreme_progress_pct'])
        self.assertAlmostEqual(x['next_open_reference'],e['bars'][known['known_min']][1])

    def test_b_signed_half_does_not_equal_new_peak(self):
        # At t1, the previously-broken peak is LOST. Price falls 0.55pp from
        # t1 but remains BELOW the frozen peak -> first +/-0.5 is misleading.
        e=event(tail=[.10,.62,.65,.64,.63,.62]+[.62]*35)
        ep,known=self.prepare(e)
        self.assertEqual(known['early_state'],'LOST_PEAK')
        x=v6.measure_window(e,ep,known,15)
        self.assertEqual(x['pathway'],'B_LOST_PEAK')
        self.assertEqual(x['landmark_first_0p5'],'CONTINUE')
        self.assertIsNone(x['first_fresh_break_delay_min'])
        self.assertEqual(x['competing_result'],'NEITHER')

    def test_b_true_rebreak_before_baseline(self):
        e=event(tail=[.65,.80,1.4,1.6]+[1.7]*41)
        ep,known=self.prepare(e)
        x=v6.measure_window(e,ep,known,15)
        self.assertEqual(x['competing_result'],'FRESH_BREAK_FIRST')
        self.assertEqual(x['first_fresh_break_delay_min'],2)

    def test_b_baseline_before_rebreak(self):
        e=event(tail=[.65,-.2,1.6]+[1.7]*41)
        ep,known=self.prepare(e)
        x=v6.measure_window(e,ep,known,15)
        self.assertEqual(x['competing_result'],'INVALIDATION_FIRST')
        self.assertEqual(x['first_baseline_cross_delay_min'],1)
        self.assertEqual(x['first_fresh_break_delay_min'],2)

    def test_later_bars_cannot_change_t1_geometry(self):
        e=event();ep,known=self.prepare(e)
        x=v6.measure_window(e,ep,known,15)
        modified=copy.deepcopy(e)
        modified['bars']=e['bars'][:known['known_min']]+[
            (t,o,h,l,c*.996) for (t,o,h,l,c) in e['bars'][known['known_min']:]]
        altered=v6.measure_window(modified,ep,known,15)
        for k in ('pathway','frozen_peak_price','known_extreme_progress_pct',
                  'fresh_break_progress_threshold_pct','landmark_close'):
            self.assertEqual(x[k],altered[k])

    def test_censored_no_false_neither(self):
        e=event();ep,known=self.prepare(e)
        e['bars']=e['bars'][:known['known_min']+4]
        x=v6.measure_window(e,ep,known,15)
        self.assertFalse(x['full_coverage'])
        self.assertEqual(x['competing_result'],'CENSORED')
        self.assertIsNone(x['first_fresh_break_delay_min'])
        self.assertIsNone(x['next_open_mfe_wick_pct'])

    def test_mirror_long_short_same_geometry(self):
        a=event(side='LONG');b=event(side='SHORT')
        pa,oa=self.prepare(a);pb,ob=self.prepare(b)
        ra=v6.measure_window(a,pa,oa,15);rb=v6.measure_window(b,pb,ob,15)
        self.assertEqual(ra['competing_result'],rb['competing_result'])
        self.assertAlmostEqual(ra['progress_to_fresh_break_pp'],rb['progress_to_fresh_break_pp'])

    def test_rebreak_followthrough_uses_next_open(self):
        e=event();ep,known=self.prepare(e)
        x=v6.measure_window(e,ep,known,15)
        r=v6.followthrough(e,ep,known,x,5)
        k=known['known_min']+x['first_fresh_break_delay_min']
        self.assertAlmostEqual(r['first_new_bar_open'],e['bars'][k][1])
        self.assertTrue(r['full_coverage'])

    def test_no_full_follow_window_if_ends_early(self):
        e=event();ep,known=self.prepare(e)
        x=v6.measure_window(e,ep,known,15)
        e['bars']=e['bars'][:known['known_min']+x['first_fresh_break_delay_min']+2]
        result=v6.followthrough(e,ep,known,x,15)
        self.assertFalse(result['full_coverage'])
        self.assertEqual(result['first_0p5'],'CENSORED')

    def test_scope_cohort_forward_only_live(self):
        e=event(ts=v6.FREEZE_MS+60000)
        ep,known=self.prepare(e)
        self.assertEqual(v6.cohort_era(e,known),'FORWARD_LIVE')
        self.assertEqual(v6.cohort_era({**e,'observation':'RECOVERED_WINDOW'},known),'BACKFILL_OR_BOUNDARY')

    def test_summary_denominator_and_pairs(self):
        e=event();ep,known=self.prepare(e)
        x=v6.measure_window(e,ep,known,15)
        incomplete={**x,'touch_key':'S','full_coverage':False,'competing_result':'CENSORED'}
        s=v6.summarize([x,incomplete],('cohort_type','pathway','horizon'))[0]
        self.assertEqual(s['N'],2)
        self.assertEqual(s['N_full'],1)
        self.assertEqual(s['N_censored'],1)
        other={**x,'touch_key':'C','cohort_type':'MATCHED_NONFAILED'}
        pair=v6.paired_audit([{'failed_key':'F','control_key':'C'}],[x,other])[0]
        self.assertTrue(pair['both_same_pathway'])

    def test_files_are_reports_only(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);rd=root/'reports/reaction_lab';rd.mkdir(parents=True)
            es=[event(),event('C','REACTION',ts=TS+120*60000)]
            db=rd/'scanner_first_touches_v1.sqlite3';paths=rd/'first_touch_paths_v2.sqlite3'
            with sqlite3.connect(db) as con:
                con.execute('CREATE TABLE first_touches(touch_key TEXT,symbol TEXT,side TEXT,status TEXT,retest_ts INTEGER,first_seen_ms INTEGER,observation_type TEXT,causal_json TEXT)')
                for e in es:con.execute('INSERT INTO first_touches VALUES(?,?,?,?,?,?,?,?)',
                    [e['key'],e['symbol'],e['side'],e['status'],e['ts'],e['seen_ms'],e['observation'],json.dumps(e['meta'])])
            with sqlite3.connect(paths) as con:
                con.execute('CREATE TABLE paths(touch_key TEXT,status TEXT,state TEXT,count INTEGER,entry_ts INTEGER,path_blob BLOB)')
                for e in es:con.execute('INSERT INTO paths VALUES(?,?,?,?,?,?)',
                    [e['key'],e['status'],'COMPLETE',len(e['bars']),e['ts']+60000,zlib.compress(b''.join(v1.BAR.pack(*b) for b in e['bars']))])
            before=[f.read_bytes() for f in (db,paths)]
            r=v6.run(root)
            self.assertEqual([f.read_bytes() for f in (db,paths)],before)
            self.assertGreater(r['n_audited_events'],0)
            for name in ('v6_paths.csv','v6_summary.csv','v6_fresh_break_followthrough.csv','v6_pair_audit.csv','ANALYSIS.md'):
                self.assertTrue((root/v6.OUT_REL/name).is_file())

if __name__=='__main__':unittest.main()
