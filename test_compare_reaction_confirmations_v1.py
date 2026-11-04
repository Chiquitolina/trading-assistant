import unittest
import pandas as pd
from compare_reaction_confirmations_v1 import observed_segment,candidate_rule,measure,evaluate,rules

T=60000

def bars(sequence):
    return pd.DataFrame([dict(timestamp=i*T,open=o,high=h,low=l,close=c)
                         for i,(o,h,l,c) in enumerate(sequence)])

def event(side='LONG'):
    return {'symbol':'TESTUSDT','event_key':'TESTUSDT|LONG|0','pivot_ts':-100*T,
            'retest_ts':0,'known_ts':T,'swing_price':100.,'side':side,
            'status':'REACTION','prominence_pct':0.2}

class ConfirmAuditorTests(unittest.TestCase):
    def test_25_rules(self):
        self.assertEqual(len(list(rules())),28)
    def test_no_future_in_confirmation(self):
        xs=bars([(100,100.6,99.8,100.5), (100.5,101,100.1,100.9), (100.9,101,100.4,100.8)]
               +[(100.8,101,99.0,99.1)]*20)
        a=candidate_rule(xs,event(),3,'CLOSE_HOLD',.10)
        self.assertTrue(a['confirmed'])
        self.assertTrue(measure(xs,event(),3,'CLOSE_HOLD',.10,15)['outcome_observed_after_confirm'])
        self.assertFalse(measure(xs,event(),3,'CLOSE_HOLD',.10,15)['held_wick'])
    def test_first_aligned_15m_candle_is_not_fifteen_minutes(self):
        seq=[(100,100.1,99.9,100.0)]*14+[(100,101.0,99.7,100.8)]+[(100.8,101,100.4,100.7)]*20
        xs=bars(seq)
        e=event();e['retest_ts']=14*T
        a=candidate_rule(xs,e,15,'15M_BAR',0)
        self.assertEqual(a['actual_wait_min'],1)
        self.assertEqual(a['confirmed_ts'],15*T)
        self.assertTrue(a['confirmed'])
        b=candidate_rule(xs,e,15,'CLOSE_HOLD',0)
        self.assertEqual(b['actual_wait_min'],15)

    def test_wick_vs_close_policy(self):
        xs=bars([(100,101,99.5,100.5),(100.5,101,99.9,100.6),(100.6,101,100.1,100.7)])
        self.assertTrue(candidate_rule(xs,event(),3,'CLOSE_HOLD',0)['confirmed'])
        self.assertFalse(candidate_rule(xs,event(),3,'WICK_HOLD',0)['confirmed'])
    def test_close_breach_blocks_both(self):
        xs=bars([(100,101,99.5,100.5),(100.5,101,99,99.5),(99.5,101,99.5,100.7)])
        for p in ('WICK_HOLD','CLOSE_HOLD'):
            self.assertFalse(candidate_rule(xs,event(),3,p,0)['confirmed'])
    def test_displacement_from_swing(self):
        xs=bars([(100,101,99.9,100.01),(100.01,100.2,100.0,100.09)])
        self.assertTrue(candidate_rule(xs,event(),2,'CLOSE_HOLD',0)['confirmed'])
        self.assertFalse(candidate_rule(xs,event(),2,'CLOSE_HOLD',.10)['confirmed'])
    def test_no_same_minute_as_future(self):
        xs=bars([(100,101,99.5,100.5)]+[(100.5,101,100.1,100.8)]*20)
        r=measure(xs,event(),1,'CLOSE_HOLD',0,15)
        self.assertTrue(r['outcome_observed_after_confirm'])
        self.assertEqual(r['entry_next_open'],100.5)
        self.assertEqual(r['confirmed_ts'],T)
    def test_gap_censored(self):
        xs=bars([(100,101,99.5,100.5)]+[(100.5,101,100,100.8)]*20)
        xs.loc[2,'timestamp']+=10000
        a=candidate_rule(xs,event(),3,'CLOSE_HOLD',0)
        self.assertEqual(a['confirmation_coverage'],'GAP')
        self.assertIsNone(a['confirmed'])
        b=measure(xs,event(),1,'CLOSE_HOLD',0,15)
        self.assertEqual(b['outcome_coverage'],'GAP')
    def test_tail_censored(self):
        xs=bars([(100,101,99.5,100.5)]+[(100.5,101,100,100.8)]*3)
        self.assertEqual(measure(xs,event(),3,'CLOSE_HOLD',0,15)['outcome_coverage'],'TAIL')
    def test_long_short_symmetry(self):
        xs=bars([(100,100.5,99.0,99.5), (99.5,99.9,99.0,99.2), (99.2,99.9,99.0,99.1)]
                +[(99.1,99.9,98.6,98.8)]*20)
        e=event('SHORT')
        self.assertTrue(candidate_rule(xs,e,3,'CLOSE_HOLD',.1)['confirmed'])
        r=measure(xs,e,3,'CLOSE_HOLD',.1,15)
        self.assertTrue(r['hit_0_3'])
    def test_same_minute_ambiguous_not_winner(self):
        xs=bars([(100,101,99.5,100.5)] + [(100.5,101.5,99.0,100.5)]*20)
        r=measure(xs,event(),1,'CLOSE_HOLD',0,15)
        self.assertEqual(r['race_0_5'],'AMBIGUOUS_1M')
        self.assertFalse(r['held_wick'])
    def test_aggregation_rejects_duplicate_input(self):
        import tempfile
        from pathlib import Path
        from aggregate_reaction_confirmations_v1 import aggregate_files
        xs=bars([(100,101,99.5,100.5)]+[(100.5,101.1,100.1,100.6)]*100)
        df,_=evaluate(xs,[event()],'TESTUSDT')
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'one.csv';df.to_csv(f,index=False)
            with self.assertRaises(ValueError):aggregate_files([f,f])
            raw,summary=aggregate_files([f])
            self.assertEqual(len(raw),len(df))
            self.assertIn('ALL',summary.side_group.to_list())

    def test_selected_vs_all_eligible_denominator(self):
        xs=bars([(100,101,99.5,100.5)]+[(100.5,101.1,100.1,100.6)]*100)
        e=event()
        details,summary=evaluate(xs,[e],'TESTUSDT')
        s=summary[(summary.confirmation_min==3)&(summary.policy=='CLOSE_HOLD')&
                  (summary.min_displacement_pct==0)&(summary.horizon_min==30)].iloc[0]
        self.assertEqual(s.original_reaction_count,1)
        self.assertEqual(s.confirmed_n,1)
        self.assertEqual(s.eligible_complete,1)

if __name__=='__main__':unittest.main()
