#!/usr/bin/env python3
"""Isolation, causal HTF, 15m replay parity and direct timeframe research tests."""
from __future__ import annotations
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

import audit_reaction_multitf_v1 as m

TF=m.v1.TF1
BASE=int(pd.Timestamp('2026-09-15T00:00:00Z').timestamp()*1000)


def tape_at(start=BASE,n=300,price=100):
    time=start+np.arange(n,dtype=np.int64)*TF
    x=np.linspace(0,10,n)
    c=price+np.sin(x)*.03
    return pd.DataFrame({'timestamp':time,'open':c,'high':c+.01,'low':c-.01,'close':c})


def event(side='LONG',known=None,price=100):
    return {'side':side,'known_ts':known if known is not None else BASE+120*TF,
            'swing_price':price,'retest_ts':BASE+119*TF}


def point(side='LONG',actionable=None,price=100):
    return {'side':side,'actionable_ts':actionable if actionable is not None else BASE,
            'swing_price':price,'pivot_ts':BASE-5*TF}


class MultiTimeframeTests(unittest.TestCase):
    def test_aggregates_real_utc_30m_and_1h(self):
        t=tape_at(n=120)
        m30=m.aggregate_closed_1m(t,'30m')
        h1=m.aggregate_closed_1m(t,'1h')
        self.assertEqual((len(m30),len(h1)),(4,2))
        self.assertEqual(m30.iloc[0]['open'],t.iloc[0]['open'])
        self.assertEqual(m30.iloc[0]['close'],t.iloc[29]['close'])
        self.assertEqual(h1.iloc[0]['high'],t.iloc[:60].high.max())
        self.assertEqual(h1.iloc[0]['low'],t.iloc[:60].low.min())
    def test_partial_and_gap_fail_closed(self):
        with self.assertRaises(ValueError):m.aggregate_closed_1m(tape_at(n=119),'1h')
        t=tape_at(n=120).drop(index=[31]).reset_index(drop=True)
        with self.assertRaises(ValueError):m.aggregate_closed_1m(t,'30m')
    def test_non_aligned_start_rejected(self):
        with self.assertRaises(ValueError):m.aggregate_closed_1m(tape_at(start=BASE+TF,n=60),'1h')
    def test_htf_level_only_after_confirmation(self):
        t=tape_at(n=180)
        e=event(known=BASE+60*TF)
        future=point(actionable=BASE+61*TF)
        r=m.known_htf_context(t,e,[future],'30m')
        self.assertEqual(r['30m_context_status'],'NO_CONFIRMED_CANDIDATE')
        self.assertIsNone(r['30m_near_0_20_pct'])
    def test_future_mutations_cannot_change_htf_context(self):
        t=tape_at(n=180)
        e=event(known=BASE+100*TF)
        p=point(actionable=BASE+50*TF)
        before=m.known_htf_context(t,e,[p],'1h')
        mutated=t.copy()
        mutated.loc[mutated.timestamp >= e['known_ts'],'close']=900
        mutated.loc[mutated.timestamp >= e['known_ts'],'high']=900
        after=m.known_htf_context(mutated,e,[p],'1h')
        self.assertEqual(before,after)
        self.assertEqual(before['1h_context_status'],'ACTIVE')
        self.assertTrue(before['1h_near_0_10_pct'])
    def test_long_level_broken_on_close_outside_band(self):
        t=tape_at(n=180)
        t.loc[t.timestamp==BASE+55*TF,'close']=99.80
        r=m.known_htf_context(t,event(known=BASE+100*TF),[point(actionable=BASE+50*TF)],'30m')
        self.assertEqual(r['30m_context_status'],'ALL_CANDIDATES_INVALIDATED')
    def test_short_level_broken_on_close_outside_band(self):
        t=tape_at(n=180)
        t.loc[t.timestamp==BASE+55*TF,'close']=100.20
        r=m.known_htf_context(t,event('SHORT',known=BASE+100*TF),[point('SHORT',actionable=BASE+50*TF)],'1h')
        self.assertEqual(r['1h_context_status'],'ALL_CANDIDATES_INVALIDATED')
    def test_htf_context_expired_point_excluded(self):
        t=tape_at(n=180)
        e=event(known=BASE+100*TF)
        stale=point(actionable=BASE-2*24*60*TF)
        r=m.known_htf_context(t,e,[stale],'30m')
        self.assertEqual(r['30m_context_status'],'NO_CONFIRMED_CANDIDATE')
    def test_htf_history_gap_unknown(self):
        t=tape_at(n=180)
        t=t.loc[t.timestamp!=BASE+70*TF].reset_index(drop=True)
        r=m.known_htf_context(t,event(known=BASE+100*TF),[point(actionable=BASE+50*TF)],'30m')
        self.assertEqual(r['30m_context_status'],'UNKNOWN_HISTORY')
    def test_same_side_only_and_absolute_distance(self):
        t=tape_at(n=180)
        e=event(known=BASE+100*TF)
        points=[point('SHORT',actionable=BASE+50*TF,price=100),
                point('LONG',actionable=BASE+50*TF,price=100.25)]
        r=m.known_htf_context(t,e,points,'1h')
        self.assertEqual(r['1h_confirmed_candidates_n'],1)
        self.assertFalse(r['1h_near_0_20_pct'])
    def test_30m_and_1h_confirmed_timestamp_are_different(self):
        from engine.live.research.swing_detector import SwingDetector
        def info(point,timeframe_candles,swing_timeframe):
            return {'actionable_timestamp':point.confirmed_timestamp+m.TF_MIN[swing_timeframe]*TF,
                    'confirmation_close':float(timeframe_candles.loc[timeframe_candles.timestamp==point.confirmed_timestamp,'close'].iloc[0]),
                    'move_pct':0.0}
        def make(tf):
            n=m.TF_MIN[tf];base=BASE
            t=tape_at(n=13*n)
            c=m.aggregate_closed_1m(t,tf)
            c.loc[3,'high']=c.high.max()+2
            pts=m.build_confirmed_points(c,tf,{'get_volume_exhaustion_swing_confirmation_info':info},SwingDetector,BASE+15*60*TF)
            target=[p for p in pts if p['side']=='SHORT' and p['pivot_ts']==int(c.iloc[3].timestamp)]
            return target[0],n
        p30,n30=make('30m');p60,n60=make('1h')
        self.assertEqual(p30['actionable_ts']-p30['pivot_ts'],(3+1)*n30*TF)
        self.assertEqual(p60['actionable_ts']-p60['pivot_ts'],(3+1)*n60*TF)
    def test_exact_parity_rejects_fabricated_event(self):
        from engine.live.research.swing_detector import SwingDetector
        from dashboard.views import hll_reaction_pre_overlay as overlay
        root=Path(__file__).resolve().parent
        app=root/'dashboard/app.py'
        if not app.is_file():self.skipTest('app.py available in VPS repo and fixture only')
        env,_,_=overlay.canonical_functions(str(app),app.stat().st_mtime_ns)
        t=tape_at(n=1200)
        cutoff=BASE+1200*TF
        visible=cutoff-600*TF
        # direct parity helper must raise on an incorrect set even when empty.
        with self.assertRaises(RuntimeError):
            m.assert_15m_parity(t,visible,cutoff,'TESTUSDT',env,SwingDetector,
                [{'side':'LONG','pivot_ts':BASE,'retest_ts':BASE,'known_ts':BASE+TF,
                  'status':'REACTION','swing_price':100.,'actionable_ts':BASE,'departure_ts':BASE}],overlay)
    def test_integration_canonical_synthetic_1m_replay(self):
        from engine.live.research.swing_detector import SwingDetector
        from dashboard.views import hll_reaction_pre_overlay as overlay
        root=Path(__file__).resolve().parent
        app=root/'dashboard/app.py'
        if not app.is_file():self.skipTest('app.py available in VPS repo and fixture only')
        env,_,_=overlay.canonical_functions(str(app),app.stat().st_mtime_ns)
        n=4200
        dates=BASE-6*60*TF+np.arange(n,dtype=np.int64)*TF
        # Repeated visible pivots of two scales, sufficient time for 1h swings.
        ix=np.arange(n)
        price=100+np.sin(ix/39)*.6+np.sin(ix/127)*.9+.00005*ix
        t=pd.DataFrame({'timestamp':dates,'open':price,'high':price+.04,
                        'low':price-.04,'close':price})
        cutoff=BASE+60*60*TF
        visible=cutoff-48*60*TF
        res=m.run_audit(t,cutoff,visible,'TESTUSDT',env,SwingDetector,overlay,context_max_age_min=360)
        self.assertEqual(res['parity'][0],res['counts']['15m'].total())
        self.assertIn('30m',res['counts'])
        self.assertIn('1h',res['counts'])
        if not res['events'].empty:
            self.assertTrue((res['events'].original_known_ts<=cutoff).all())
        self.assertEqual(len(res['context']),res['counts']['15m'].total())

if __name__=='__main__':unittest.main()
