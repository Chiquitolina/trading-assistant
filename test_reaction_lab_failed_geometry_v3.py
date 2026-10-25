#!/usr/bin/env python3
import copy
import json
import sqlite3
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

import reaction_lab_failed_geometry_v3 as v3
import reaction_lab_failed_behavior_v1 as v1

TS=1790000000000

def ev(side='SHORT',moves=None,key='A',status='TOUCH_FAILED',ts=TS,symbol='AUSDT'):
    if moves is None:
        moves=[0]*5+[.6,.8,.35,.10,1.05]+[.2]*35
    fail=100.2 if side=='SHORT' else 99.8
    signed=1 if side=='SHORT' else -1
    bars=[];last=fail
    for i,m in enumerate(moves):
        c=fail*(1+signed*m/100)
        bars.append((ts+(i+1)*60000,last,max(last,c),min(last,c),c))
        last=c
    return dict(key=key,symbol=symbol,side=side,status=status,ts=ts,
                seen_ms=ts,observation='LIVE_OBSERVED',
                meta={'retest_close':fail,'swing_price':100,'retest_low':99.3,
                      'retest_high':100.8},bars=bars,state='COMPLETE')

class GeometryV3(unittest.TestCase):
    def test_anatomy_peak_and_pullback_times(self):
        a=v3.anatomical_event(ev())
        self.assertEqual((a['impulse_min'],a['peak_min'],a['pullback_min']),(6,7,8))
        self.assertTrue(a['geometry_known_at_pb'])
        self.assertAlmostEqual(a['room_to_frozen_peak_pct'],(.8-.35)/(1+.35/100),3)
        self.assertAlmostEqual(a['room_to_m5_baseline_pct'],.35/(1+.35/100),3)
        self.assertTrue(a['risk_set_future_competition'])

    def test_symmetry_long_short(self):
        a=v3.anatomical_event(ev('LONG'))
        b=v3.anatomical_event(ev('SHORT'))
        for k in ('impulse_min','peak_min','pullback_min','impulse_direction',
                  'room_to_frozen_peak_pct','room_to_m5_baseline_pct','pb_retrace_fraction_pct'):
            self.assertAlmostEqual(a[k],b[k],delta=.01) if isinstance(a[k],float) else self.assertEqual(a[k],b[k])

    def test_geometry_known_before_future(self):
        a=ev();b=copy.deepcopy(a)
        b['bars']=b['bars'][:8]+[(t,o,h,l,c*1.00001) for t,o,h,l,c in b['bars'][8:]]
        keys=('impulse_min','peak_min','pullback_min','pb_close_price',
              'room_to_frozen_peak_pct','room_to_m5_baseline_pct',
              'pb_retrace_fraction_pct','pb_distance_to_original_swing_pct')
        x=v3.anatomical_event(a);y=v3.anatomical_event(b)
        for k in keys:self.assertEqual(x[k],y[k])

    def test_post_pb_first_future_minute_not_current(self):
        e=ev()
        a=v3.anatomical_event(e)
        r=v3.post_pullback_window(e,a,5)
        self.assertEqual(r['first_0p5_delay_min'],2) # t10, AFTER t8 confirm
        self.assertEqual(r['close_first_0p5'],'CONTINUE')
        self.assertEqual(r['frozen_peak_vs_baseline_race'],'SECOND_BREAKOUT')
        self.assertEqual(r['frozen_peak_race_delay_min'],2)
        self.assertEqual(r['observed_closes_post_pb'],5)

    def test_early_cross_baseline_at_pb_not_at_future_risk(self):
        e=ev(moves=[0]*5+[.6,.9,-.05]+[.2]*40)
        a=v3.anatomical_event(e)
        self.assertFalse(a['risk_set_future_competition'])
        w=v3.post_pullback_window(e,a,10)
        self.assertEqual(w['frozen_peak_vs_baseline_race'],'BASELINE_ALREADY_AT_PB')
        self.assertFalse(w['risk_set_future_competition'])

    def test_missing_horizon_censored_not_none(self):
        e=ev(moves=[0]*5+[.6,.8,.35]+[.1]*4)
        a=v3.anatomical_event(e)
        w=v3.post_pullback_window(e,a,10)
        self.assertTrue(w['censored'])
        self.assertEqual(w['close_first_0p5'],'CENSORED')
        self.assertIsNone(w['post_pb_mfe_pct'])
        self.assertIsNone(w['first_0p5_delay_min'])
        complete=v3.post_pullback_window(e,a,4)
        self.assertTrue(complete['full_coverage'])

    def test_candle_high_low_mfe_not_close_only(self):
        e=ev()
        a=v3.anatomical_event(e)
        # Make a post-confirmation candle with wide range, unchanged close
        t,o,h,l,c=e['bars'][8]
        e['bars'][8]=(t,o,max(h,a['pb_close_price']*1.02),min(l,a['pb_close_price']*.98),c)
        r=v3.post_pullback_window(e,a,5)
        self.assertGreaterEqual(r['post_pb_mfe_pct'],.9)
        self.assertGreaterEqual(r['post_pb_mae_pct'],.9)
        self.assertEqual(r['close_first_0p5'],'CONTINUE') # t10, not bar9 wick

    def test_race_close_first_and_both(self):
        self.assertEqual(v3.race_on_closes([.2,-.6,.8],.5),('REVERSE',2,True))
        self.assertEqual(v3.race_on_closes([.1,.2,.3],.5),('NEITHER',None,False))

    def test_pair_requires_same_complete_horizon(self):
        f=ev(key='F',moves=[0]*5+[.6,.8,.35]+[.2]*40)
        c=ev(key='C',status='REACTION',ts=TS+120*60000,
             moves=[0]*5+[.6,.8,.35]+[.2]*26)  # 34 bars, fails t35 coverage
        m=[dict(failed_key='F',control_key='C',match_score=0.1,same_symbol=True)]
        p=v3.paired_strict(m,{'F':f,'C':c})[0]
        self.assertFalse(p['pair_full_initial'])
        self.assertFalse(p['pair_full_post_pb_30m'])
        c['bars'].extend([(c['bars'][-1][0]+(i+1)*60000,c['bars'][-1][4],c['bars'][-1][4],c['bars'][-1][4],c['bars'][-1][4]) for i in range(15)])
        p=v3.paired_strict(m,{'F':f,'C':c})[0]
        self.assertTrue(p['pair_full_initial'])
        self.assertTrue(p['pair_full_post_pb_30m'])

    def test_zero_bar_no_false_episode(self):
        e=ev(moves=[0,0])
        self.assertIsNone(v3.anatomical_event(e))

    def test_end_to_end_isolated_writes(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);rd=root/'reports/reaction_lab';rd.mkdir(parents=True)
            journal=rd/'scanner_first_touches_v1.sqlite3'
            paths=rd/'first_touch_paths_v2.sqlite3'
            f=ev(key='F',moves=[0]*5+[.6,.8,.35,.1,1.05]+[.2]*45)
            c=ev(key='C',status='REACTION',ts=TS+120*60000,
                 moves=[0]*5+[.6,.8,.35,.1,1.05]+[.2]*45)
            with sqlite3.connect(journal) as db:
                db.execute('CREATE TABLE first_touches (touch_key TEXT,symbol TEXT,side TEXT,status TEXT,retest_ts INTEGER,first_seen_ms INTEGER,observation_type TEXT,causal_json TEXT)')
                for x in (f,c):
                    db.execute('INSERT INTO first_touches VALUES (?,?,?,?,?,?,?,?)',
                        (x['key'],x['symbol'],x['side'],x['status'],x['ts'],x['seen_ms'],
                         x['observation'],json.dumps(x['meta'])))
            with sqlite3.connect(paths) as db:
                db.execute('CREATE TABLE paths(touch_key TEXT,status TEXT,state TEXT,count INTEGER,entry_ts INTEGER,path_blob BLOB)')
                for x in (f,c):
                    blob=zlib.compress(b''.join(v1.BAR.pack(*b) for b in x['bars']))
                    db.execute('INSERT INTO paths VALUES (?,?,?,?,?,?)',
                         (x['key'],x['status'],'COMPLETE',len(x['bars']),x['ts']+60000,blob))
            src_before=(journal.read_bytes(),paths.read_bytes())
            report=v3.run(root)
            self.assertEqual(report['failed'],1)
            self.assertEqual(report['matched_pairs'],1)
            self.assertEqual(report['pair_full_t35_0p5'],1)
            self.assertEqual((journal.read_bytes(),paths.read_bytes()),src_before)
            out=root/v3.OUT_REL
            self.assertTrue((out/'geometry_summary.csv').is_file())
            self.assertTrue((out/'paired_strict.csv').is_file())
            self.assertIn('Pares', (out/'ANALYSIS.md').read_text())

if __name__=='__main__':unittest.main()
