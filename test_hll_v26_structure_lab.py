#!/usr/bin/env python3
"""Deterministic causality and lifecycle tests. Offline, no external services."""
import unittest
import numpy as np
from hll_v26_structure_lab import (
    TF, Rules, classify_followup, _confirmed_low_pivots,
    _pressure_features, _prior_touch_features, _new_zone_distance,
)

BASE = 1787184000000


def path(closes, lows=None, highs=None):
    c=np.array(closes,dtype=float)
    lo=np.array(lows if lows is not None else [z-.1 for z in closes],dtype=float)
    hi=np.array(highs if highs is not None else [z+.1 for z in closes],dtype=float)
    op=BASE+TF*np.arange(len(c))
    return op,lo,hi,c


def classify(closes,lows=None,highs=None):
    op,lo,hi,cl=path(closes,lows,highs)
    return classify_followup(int(op[0])+TF,0,op,lo,hi,cl,99,101,1,Rules())


class TestStructure(unittest.TestCase):
    def test_failed_before_acceptance(self):
        out,tx=classify([102,98,103,103])
        self.assertEqual(out['status'],'FAILED_BREAKOUT')
        self.assertEqual(out['accepted_closed_ts'],'')
        self.assertEqual(tx[0]['transition'],'FAILED_BREAKOUT')
        self.assertEqual(tx[0]['known_closed_ts'],BASE+2*TF)

    def test_direct_no_retest_only_after_24_bars(self):
        out,tx=classify([102]*26,lows=[101.5]*26,highs=[102.5]*26)
        self.assertEqual(out['status'],'DIRECT_NO_RETEST_24B')
        self.assertEqual(out['accepted_closed_ts'],BASE+2*TF)
        self.assertEqual(out['direct_classification_closed_ts'],BASE+26*TF)
        self.assertEqual([a['transition'] for a in tx],['ACCEPTED_ABOVE','DIRECT_NO_RETEST_24B'])

    def test_retest_then_flip_sustained_later(self):
        c=[102,102,101.2,102,102,102,102,102]
        lo=[101.8,101.8,100.5,101.8,101.8,101.8,101.8,101.8]
        hi=[102.2,102.2,101.7,102.2,102.2,102.2,102.2,102.2]
        out,tx=classify(c,lo,hi)
        self.assertEqual(out['status'],'FLIP_SUSTAINED_4B')
        self.assertEqual(out['retest_closed_ts'],BASE+3*TF)
        self.assertEqual(out['defense_closed_ts'],BASE+4*TF)
        self.assertEqual(out['flip_sustained_closed_ts'],BASE+8*TF)
        self.assertEqual([a['transition'] for a in tx],
                         ['ACCEPTED_ABOVE','RETEST_CONTACT_FROM_ABOVE','FLIP_CONFIRMED','FLIP_SUSTAINED_4B'])

    def test_retest_failed_cannot_be_flip(self):
        out,tx=classify([102,102,98,110], lows=[101.8,101.8,97.5,109],
                        highs=[102.1,102.1,102,111])
        self.assertEqual(out['status'],'RETEST_FAILED')
        self.assertEqual(out['defense_closed_ts'],'')
        self.assertEqual(tx[-1]['known_closed_ts'],BASE+3*TF)

    def test_accepted_then_gap_under_zone_without_contact(self):
        out,tx=classify([102,102,98], lows=[101.8,101.8,97.5],highs=[102.1,102.1,98.5])
        self.assertEqual(out['status'],'ACCEPTED_THEN_FAILED_NO_CONTACT')
        self.assertEqual(tx[-1]['transition'],'ACCEPTED_THEN_FAILED_NO_CONTACT')

    def test_unresolved_not_false(self):
        out,_=classify([102,100,102,100,104,104])
        self.assertEqual(out['status'],'UNRESOLVED_FIRST4')

    def test_insufficient_future_is_censored(self):
        out,_=classify([102])
        self.assertEqual(out['status'],'CENSORED_ACCEPTANCE')
        out,_=classify([102,102])
        self.assertEqual(out['status'],'CENSORED_POST_ACCEPTANCE')
        out,_=classify([102,102,101.2],lows=[101.8,101.8,100.5],highs=[102.2,102.2,101.7])
        self.assertEqual(out['status'],'CENSORED_RETEST')

    def test_future_path_change_does_not_rewrite_earlier_clock(self):
        early=[102,102,101.2,102,102,102,102,102]
        a,ta=classify(early,lows=[101.8,101.8,100.5]+[101.8]*5,
                      highs=[102.2]*2+[101.7]+[102.2]*5)
        b,tb=classify(early+[80,120,70,120],
            lows=[101.8,101.8,100.5]+[101.8]*5+[79,119,69,119],
            highs=[102.2]*2+[101.7]+[102.2]*5+[81,121,71,121])
        self.assertEqual(a,b)
        self.assertEqual(ta,tb)

    def test_late_test_confirmation_not_prior_rejection(self):
        touch=[dict(test_id='x',touch_closed_ts=BASE+TF,side='BELOW',depth_atr=.2,
                    rejection_atr=.1,resolver_type='DEFENSE_CONFIRMED',
                    resolver_known_ts=BASE+4*TF,confirmed_rejection_distance_atr=.5)]
        pre=_prior_touch_features(touch,BASE+3*TF,[],[],1.)
        self.assertEqual(pre['prior_independent_touches'],1)
        self.assertEqual(pre['prior_confirmed_rejections'],0)
        post=_prior_touch_features(touch,BASE+5*TF,[],[],1.)
        self.assertEqual(post['prior_confirmed_rejections'],1)

    def test_failed_breakout_only_counted_after_known_failure(self):
        candidate=dict(status='FAILED_BREAKOUT',failed_closed_ts=BASE+5*TF)
        pre=_prior_touch_features([],BASE+4*TF,[],[candidate],1.)
        post=_prior_touch_features([],BASE+6*TF,[],[candidate],1.)
        self.assertEqual(pre['prior_failed_breakouts'],0)
        self.assertEqual(post['prior_failed_breakouts'],1)

    def test_pressure_no_future_confirmed_pivot(self):
        op=BASE+np.arange(55)*TF
        low=np.array([100.]*55)
        high=np.array([101.]*55)
        close=np.array([100.5]*55)
        low[30]=99;low[35]=99.3;low[40]=99.6
        piv=_confirmed_low_pivots(op,high,low)
        a=_pressure_features(42,op,high,low,close,101,1.,piv,Rules())
        high[43:]=200;low[43:]=-100;close[43:]=100
        b=_pressure_features(42,op,high,low,close,101,1.,piv,Rules())
        self.assertEqual(a,b)
        self.assertEqual(a['higher_lows_last3_pre24'],0) # pivot 40 only confirms at 42, not prebar 41

    def test_next_zone_only_from_active_confirmed_levels(self):
        lv=dict(level_id='A',origin='HIGH',zone_high=101.,zone_low=100.,formation_closed_ts=10)
        others=[lv,dict(level_id='B',origin='HIGH',zone_low=102.,formation_closed_ts=20),
                dict(level_id='C',origin='HIGH',zone_low=103.,formation_closed_ts=20),
                dict(level_id='D',origin='HIGH',zone_low=101.5,formation_closed_ts=200)]
        ends={'B':dict(end_closed_ts=70),'C':dict(end_closed_ts=140),
              'D':dict(end_closed_ts=500)}
        self.assertEqual(_new_zone_distance(lv,others,ends,100,1.),2.)


if __name__=='__main__':unittest.main(verbosity=2)
