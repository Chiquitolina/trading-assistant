#!/usr/bin/env python3
import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing

import reaction_lab_failed_behavior_v1 as fb

TS=1800000000000  # aligned start-minute timestamp


def mkbar(t,op,high,low,close):
    return (TS+t*fb.MINUTE, float(op),float(high),float(low),float(close))


def event(side='LONG',closes=None,level=100.0,fail=None,key='TEST',obs='LIVE_OBSERVED'):
    if fail is None:fail=99.7 if side=='LONG' else 100.3
    if closes is None:closes=[99.4,100.2,100.5]
    prev=fail;bars=[]
    for i,close in enumerate(closes,1):
        bars.append(mkbar(i,prev,max(prev,close),min(prev,close),close))
        prev=close
    meta=dict(swing_price=level,retest_close=fail,retest_low=min(fail,level-0.5),
              retest_high=max(fail,level+0.5),relative_volume=1.5,max_departure_pct=1.2)
    return dict(key=key,symbol='TESTUSDT',side=side,status='TOUCH_FAILED',
                ts=TS,seen_ms=TS,observation=obs,meta=meta,bars=bars,state='ACTIVE')


class TestGeometry(unittest.TestCase):
    def test_positive_break_long(self):
        self.assertAlmostEqual(fb.break_distance(99.7,100,'LONG'),.3)
        self.assertAlmostEqual(fb.break_distance(101,100,'LONG'),-1)

    def test_positive_break_short(self):
        self.assertAlmostEqual(fb.break_distance(100.4,100,'SHORT'),.4)
        self.assertAlmostEqual(fb.break_distance(99.5,100,'SHORT'),-.5)

    def test_signed_move_opposite_directions(self):
        self.assertAlmostEqual(fb.move_from_fail(99.2,99.7,'LONG'),(99.7-99.2)/99.7*100)
        self.assertAlmostEqual(fb.move_from_fail(101,100.3,'SHORT'),(101-100.3)/100.3*100)

    def test_fail_close_known_t0(self):
        b=fb.base_row(event(),.1)
        self.assertTrue(b['consistent_failed'])
        self.assertAlmostEqual(b['fail_close_penetration_pct'],.3)
        self.assertAlmostEqual(b['fail_close_beyond_band_pct'],.2)

    def test_first_reentry_on_close(self):
        ev=event()
        h=fb.horizon_stats(ev,3,100,99.7,.1)
        self.assertEqual(h['first_reenter_min'],2)
        self.assertEqual(h['first_reclaim_swing_min'],2)
        self.assertEqual(h['first_2close_reenter_min'],3)
        self.assertEqual(h['outside_closes'],1)
        self.assertEqual(h['closed_band_crossings'],1)

    def test_immediate_extension_before_recovery(self):
        ev=event(closes=[99.1,99.0,100.2])
        label,t=fb.first_race(ev['bars'],100,'LONG',99.7,.1,.5,'CLOSE')
        self.assertEqual((label,t),('EXTEND_FIRST',1))

    def test_immediate_recovery_before_extension(self):
        ev=event(closes=[100.1,98.7,100.2])
        label,t=fb.first_race(ev['bars'],100,'LONG',99.7,.1,.5,'CLOSE')
        self.assertEqual((label,t),('REENTER_FIRST',1))

    def test_ambiguous_same_candle_wick_not_close(self):
        bars=[mkbar(1,99.7,100.5,99.0,99.5)]
        self.assertEqual(fb.first_race(bars,100,'LONG',99.7,.1,.5,'INTRABAR')[0],'AMBIGUOUS')
        self.assertEqual(fb.first_race(bars,100,'LONG',99.7,.1,.5,'CLOSE')[0],'NEITHER')

    def test_short_recovery(self):
        ev=event(side='SHORT',closes=[100.5,99.9,99.7])
        h=fb.horizon_stats(ev,3,100,100.3,.1)
        self.assertEqual(h['first_reenter_min'],2)
        self.assertAlmostEqual(h['further_extension_pct'],.2)

    def test_missing_horizon_never_negative(self):
        ev=event(closes=[99.4])
        self.assertIsNone(fb.horizon_stats(ev,5,100,99.7,.1))
        self.assertIsNotNone(fb.horizon_stats(ev,1,100,99.7,.1))

    def test_lagged_features_no_future(self):
        ev1=event(closes=[99.6,99.5,99.4,98.0,97.0,96.0])
        ev2=event(closes=[99.6,99.5,99.4,100.1,102.0,103.0])
        s1=fb.minute_snapshot(ev1,3,100,99.7,.1)
        s2=fb.minute_snapshot(ev2,3,100,99.7,.1)
        self.assertEqual(s1,s2)
        self.assertNotEqual(fb.horizon_stats(ev1,5,100,99.7,.1)['last_close_change_from_fail_pct'],
                            fb.horizon_stats(ev2,5,100,99.7,.1)['last_close_change_from_fail_pct'])

    def test_censor_survival(self):
        a=event(closes=[99.4,100.2],key='A')
        b=event(closes=[99.5],key='B')
        rows=fb.reentry_survival([a,b],.1,3)
        self.assertEqual(rows[1]['censored'],1)
        self.assertEqual(rows[2]['reentries'],1)
        self.assertEqual(rows[2]['km_reentered_pct'],100)
        self.assertEqual(rows[3]['at_risk'],0)

    def test_no_lookahead_in_future_after_landmark(self):
        a=event(closes=[99.6]*3+[100.3]*15,key='A')
        base,h,l,c,bad=fb.analysis([a],.1)
        self.assertEqual(len([x for x in c if x['landmark']==3 and x['forward']==15]),1)
        row=[x for x in c if x['landmark']==3 and x['forward']==15][0]
        self.assertFalse(row['early_reclaimed'])
        self.assertTrue(row['future_any_reenter'])

    def test_metadata_missing_excluded(self):
        a=event(key='M');a['meta']['swing_price']=None
        b,h,l,c,bad=fb.analysis([a],.1)
        self.assertEqual(len(b),1);self.assertEqual(len(h),0);self.assertEqual(bad,['M'])

    def test_input_sqlite_read_only_and_full_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);journal=root/'j.sqlite3';paths=root/'p.sqlite3';out=root/'out'
            ev=event(closes=[99.4,100.2]+[100.4]*63,key='S|LONG|15m|X|1|2|3')
            with closing(sqlite3.connect(journal)) as conn:
                conn.execute('CREATE TABLE first_touches(touch_key TEXT,symbol TEXT,side TEXT,status TEXT,retest_ts INTEGER,first_seen_ms INTEGER,observation_type TEXT,causal_json TEXT)')
                conn.execute('INSERT INTO first_touches VALUES (?,?,?,?,?,?,?,?)',
                   (ev['key'],ev['symbol'],ev['side'],ev['status'],ev['ts'],ev['seen_ms'],ev['observation'],json.dumps(ev['meta'])))
                conn.commit()
            with closing(sqlite3.connect(paths)) as conn:
                conn.execute('CREATE TABLE paths(touch_key TEXT,status TEXT,state TEXT,count INTEGER,entry_ts INTEGER,path_blob BLOB)')
                conn.execute('INSERT INTO paths VALUES (?,?,?,?,?,?)',
                  (ev['key'],ev['status'],ev['state'],len(ev['bars']),TS+fb.MINUTE,
                   fb.zlib.compress(b''.join(fb.BAR.pack(*bar) for bar in ev['bars']))))
                conn.commit()
            j1=journal.read_bytes();p1=paths.read_bytes()
            found=fb.read_events(journal,paths)
            self.assertEqual(len(found),1)
            report=fb.run(journal,paths,out)
            self.assertEqual(report['failed_total'],1)
            self.assertTrue((out/'ANALYSIS.md').exists())
            self.assertTrue((out/'minute_profile.csv').exists())
            self.assertTrue((out/'reentry_survival.csv').exists())
            self.assertTrue((out/'event_horizons.csv').exists())
            self.assertTrue((out/'conditional_groups.csv').exists())
            with open(out/'event_horizons.csv',encoding='utf-8-sig') as f:
                rows=list(csv.DictReader(f))
            self.assertTrue(any(r['horizon']=='60' for r in rows))
            self.assertEqual(journal.read_bytes(),j1)
            self.assertEqual(paths.read_bytes(),p1)

    def test_temporal_consistency_keeps_dates_together(self):
        rows=[]
        for i in range(12):
            ts=TS+i*fb.MINUTE*60*24
            rows.append(dict(touch_key=str(i),symbol='X',original_side='LONG',
                 retest_ts=ts,observation_type='LIVE_OBSERVED',landmark=3,forward=30,
                 penetration_bin='0.20-0.35',early_move_bin='+0.15:+0.50',
                 future_any_reenter=bool(i%2),future_end_signed_change_pct=float(i)))
        temporal=fb.temporal_consistency(rows,3)
        allrows=[r for r in temporal if r['cohort_kind']=='ALL']
        self.assertEqual(len(allrows),3)
        self.assertEqual(sum(r['n'] for r in allrows),12)

    def test_full_minute_profile_and_censor_counts(self):
        a=event(closes=[99.4,99.5,100.3],key='A')
        b=event(closes=[99.3],key='B')
        minutes=fb.minute_by_minute([a,b],.1)
        profile=fb.minute_profile(minutes,2)
        self.assertEqual([p['evaluable_n'] for p in profile],[2,1,1])
        self.assertEqual(profile[1]['coverage_pct'],50)

    def test_wilson_bounds(self):
        low,high=fb.wilson(5,10)
        self.assertLess(low,50);self.assertGreater(high,50)
        self.assertEqual(fb.wilson(0,0),(None,None))


if __name__=='__main__':unittest.main()
