import unittest
import pandas as pd
from compare_first_touch_confirmations_v2 import (
    status_at_touch,candidate_pre_only,measure_pre_confirmed,evaluate_all,
    pivot_touch_key,chronological_race_close_band)
from aggregate_first_touch_confirmations_v2 import pool
import compare_reaction_confirmations_v1 as v1
from pathlib import Path
import tempfile

T=60000

def tape(data):
    return pd.DataFrame([{'timestamp':i*T,'open':o,'high':h,'low':l,'close':c}
                         for i,(o,h,l,c) in enumerate(data)])

def ev(status='TOUCH_INDECISIVE',side='LONG',pivot=-10*T,close=100.4):
    return {'symbol':'TESTUSDT','event_key':'TESTUSDT|LONG|0','status':status,
            'side':side,'pivot_ts':pivot,'retest_ts':0,'known_ts':T,
            'retest_close':close,'swing_price':100.,'prominence_pct':0.2}

class AllFirstTouchConfirmationsTests(unittest.TestCase):
    def setUp(self):
        self.a=tape([(100,100.8,99.7,100.4),
                     (100.4,100.7,100.2,100.5),
                     (100.5,101,100.3,100.8)]+
                     [(100.8,101,100.5,100.8)]*80)

    def test_original_1m_only_accepts_original_reaction(self):
        for stat,want in [('REACTION',True),('TOUCH_INDECISIVE',False),('TOUCH_FAILED',False)]:
            result=candidate_pre_only(self.a,ev(stat),1,'CLOSE_HOLD',0,5*T)
            self.assertEqual(result['confirmed'],want)
            self.assertEqual(result['initial_status'],stat)

    def test_3m_rescues_indecisive(self):
        event=ev('TOUCH_INDECISIVE')
        result=candidate_pre_only(self.a,event,3,'WICK_HOLD',.1,6*T)
        self.assertTrue(result['confirmed'])
        self.assertTrue(result['new_confirmation_from_indecisive'])
        self.assertEqual(event['status'],'TOUCH_INDECISIVE')
        self.assertEqual(result['confirmed_ts'],3*T)

    def test_first_minute_closed_wrong_side_is_identified_as_reclaim(self):
        a=tape([(100,100.3,99.5,99.8),
                (99.8,100.4,100.1,100.2),
                (100.2,100.8,100.1,100.7)]+
               [(100.7,101,100.5,100.7)]*70)
        event=ev('TOUCH_INDECISIVE',close=99.8)
        result=candidate_pre_only(a,event,3,'WICK_HOLD',.1,5*T)
        self.assertTrue(result['confirmed'])
        self.assertTrue(result['initial_close_wrong_side_but_later_accepted'])
        self.assertFalse(result['t0_closed_expected_side'])

    def test_failed_touch_can_only_reclaim_after_minute_one(self):
        a=tape([(100,100.3,99.5,99.8),
                (99.8,100.4,100.1,100.2),
                (100.2,100.8,100.1,100.7)]+
               [(100.7,101,100.5,100.7)]*70)
        e=ev('TOUCH_FAILED',close=99.8)
        self.assertFalse(candidate_pre_only(a,e,1,'CLOSE_HOLD',0,5*T)['confirmed'])
        v=candidate_pre_only(a,e,3,'CLOSE_HOLD',0,5*T)
        self.assertTrue(v['late_reclaim_after_failed_touch'])
        self.assertTrue(v['initial_close_wrong_side_but_later_accepted'])

    def test_wick_vs_close_policy_for_indecisive(self):
        a=tape([(100,100.8,99.5,100.4),
                (100.4,100.8,99.8,100.5),
                (100.5,100.9,100.2,100.7)]+
               [(100.7,101.2,100.5,100.7)]*80)
        e=ev()
        self.assertTrue(candidate_pre_only(a,e,3,'CLOSE_HOLD',0,6*T)['confirmed'])
        self.assertFalse(candidate_pre_only(a,e,3,'WICK_HOLD',0,6*T)['confirmed'])

    def test_future_beyond_pre_cutoff_never_affects_confirmation(self):
        original=self.a.copy()
        altered=self.a.copy()
        altered.loc[2,'close']=99
        altered.loc[2,'low']=98
        e=ev()
        for t in (original,altered):
            r=candidate_pre_only(t[t.timestamp<2*T].copy(),e,3,'CLOSE_HOLD',0,2*T)
            self.assertFalse(r['pre_eligible'])
            self.assertIsNone(r['confirmed'])
            self.assertEqual(r['confirmation_coverage'],'TAIL')
            m=measure_pre_confirmed(t,t[t.timestamp<2*T].copy(),e,3,'CLOSE_HOLD',0,30,2*T)
            self.assertEqual(m['outcome_coverage'],'NOT_EVALUATED_NO_PRE_CONFIRMATION_DATA')

    def test_outcomes_only_start_after_rule_confirm(self):
        e=ev()
        r=measure_pre_confirmed(self.a,self.a[self.a.timestamp<6*T],e,3,'WICK_HOLD',0,15,6*T)
        self.assertTrue(r['confirmed'])
        self.assertTrue(r['outcome_observed_after_confirm'])
        self.assertEqual(r['entry_next_open'],float(self.a.loc[3,'open']))

    def test_same_candle_race_ambiguous(self):
        a=tape([(100,101,99.7,100.4),
                (100.4,102,98,100.5)]+
               [(100.5,101,100,100.7)]*80)
        e=ev('REACTION')
        r=measure_pre_confirmed(a,a[a.timestamp<2*T],e,1,'CLOSE_HOLD',0,15,2*T)
        self.assertEqual(r['race_0_5'],'AMBIGUOUS_1M')

    def test_wick_invalidates_but_close_band_does_not(self):
        # The target is hit after a sub-swing wick, but the candle never
        # CLOSES outside the tolerance band. Policy choice matters.
        a=tape([(100,101,99.7,100.4),
                (100.4,101.2,99.5,100.5)]+
               [(100.5,101,100.1,100.7)]*80)
        e=ev('REACTION')
        r=measure_pre_confirmed(a,a[a.timestamp<2*T],e,1,'CLOSE_HOLD',0,15,2*T)
        self.assertEqual(r['race_0_5'],'AMBIGUOUS_1M')
        self.assertEqual(r['race_close_band_0_5'],'FAVORABLE_FIRST')

    def test_close_band_same_bar_conservative_ambiguous(self):
        a=tape([(100,101,99.7,100.4),
                (100.4,101.2,98.5,99.5)]+
               [(99.5,101,99,100.4)]*80)
        r=measure_pre_confirmed(a,a[a.timestamp<2*T],ev('REACTION'),
                               1,'CLOSE_HOLD',0,15,2*T)
        self.assertEqual(r['race_close_band_0_5'],'AMBIGUOUS_1M')

    def test_two_distinct_pivots_same_retest_not_deduplicated(self):
        e1=ev('REACTION');e2=ev('TOUCH_INDECISIVE',pivot=-12*T)
        rows,summary=evaluate_all(self.a,[e1,e2],'TESTUSDT',6*T)
        self.assertNotEqual(pivot_touch_key(e1),pivot_touch_key(e2))
        self.assertEqual(rows.pivot_touch_id.nunique(),2)
        x=summary[(summary.side_group=='ALL')&(summary.original_status_group=='ALL')&
                  (summary.confirmation_min==1)&(summary.horizon_min==30)].iloc[0]
        self.assertEqual(x.candidate_pivot_touches,2)
        self.assertEqual(x.selected_complete_outcomes,1)

    def test_original_reaction_parity_with_v1(self):
        e=ev('REACTION')
        for k,p,d in v1.rules():
            if k>5:continue
            old=v1.candidate_rule(self.a,e,k,p,d)
            new=candidate_pre_only(self.a[self.a.timestamp<8*T],e,k,p,d,8*T)
            if old['confirmed_ts']>8*T:continue
            self.assertEqual(new['confirmed'],old['confirmed'])
            self.assertEqual(new['final_margin_pct'],old['final_margin_pct'])

    def test_gap_never_filled(self):
        a=self.a.copy();a.loc[2,'timestamp']+=1
        e=ev()
        r=candidate_pre_only(a[a.timestamp<6*T],e,3,'CLOSE_HOLD',0,6*T)
        self.assertEqual(r['confirmation_coverage'],'GAP')
        self.assertIsNone(r['confirmed'])

    def test_short_symmetry(self):
        a=tape([(100,100.3,99.2,99.7),
                (99.7,99.9,99.4,99.5),
                (99.5,99.9,99.0,99.1)]+
               [(99.1,99.5,98.6,98.9)]*80)
        e=ev('TOUCH_INDECISIVE','SHORT',close=99.7)
        r=candidate_pre_only(a,e,3,'WICK_HOLD',.1,6*T)
        self.assertTrue(r['confirmed'])
        self.assertTrue(r['new_confirmation_from_indecisive'])

    def test_rule_grid_and_all_original_statuses_in_summary(self):
        self.assertEqual(len(list(v1.rules())),28)
        e=[ev('REACTION'),ev('TOUCH_INDECISIVE',pivot=-11*T),ev('TOUCH_FAILED',pivot=-12*T)]
        rows,summary=evaluate_all(self.a,e,'TESTUSDT',6*T)
        self.assertEqual(len(rows),3*28*3)
        self.assertEqual(set(summary.original_status_group),{'ALL','REACTION','TOUCH_INDECISIVE','TOUCH_FAILED'})

    def test_pool_detects_duplicate_input(self):
        e=[ev('REACTION'),ev('TOUCH_INDECISIVE',pivot=-11*T)]
        rows,_=evaluate_all(self.a,e,'TESTUSDT',6*T)
        with tempfile.TemporaryDirectory() as tmp:
            f=Path(tmp)/'test.csv'
            rows.to_csv(f,index=False)
            with self.assertRaises(ValueError):pool([f,f])
            x,s=pool([f]);self.assertEqual(len(x),len(rows))
            self.assertIn('ALL',set(s.side_group))

if __name__=='__main__':unittest.main()
