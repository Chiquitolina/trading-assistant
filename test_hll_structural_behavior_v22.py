import json
import unittest
import pandas as pd
from hll_hierarchical_levels_v1 import TF
from hll_structural_behavior_v22 import reconstruct_episode_tables

T=TF

def event(level,typ,ts,tid):
    return dict(level_id=level,event_type=typ,observed_closed_ts=ts,test_id=tid,
                event_id=f'{level}-{typ}-{ts}')

class BehaviorV22A(unittest.TestCase):
    def setUp(self):
        self.zones=pd.DataFrame([dict(zone_id='Z',level_ids=json.dumps(['A','B']),members=2,created_at=2*T)])
    def test_dedupe_members_and_link_outcomes(self):
        data=[event('A','TEST_FROM_ABOVE',3*T,'a'),event('B','TEST_FROM_ABOVE',3*T,'b'),
              event('A','DEFENSE_CONFIRMED',4*T,'a'),event('B','LOSS_CONFIRMED',5*T,'b')]
        ep,tr,summary=reconstruct_episode_tables(self.zones,pd.DataFrame(data),6*T)
        self.assertEqual(len(ep),1)
        self.assertEqual(ep.iloc[0].test_count_unique,1)
        self.assertEqual(ep.iloc[0].member_test_count,2)
        self.assertEqual(ep.iloc[0].state,'MIXED_RESOLUTION')
        self.assertEqual(len(tr),4)
        self.assertEqual(summary.iloc[0].episodes,1)
    def test_no_future_response(self):
        data=[event('A','TEST_FROM_BELOW',3*T,'a'),event('A','DEFENSE_CONFIRMED',7*T,'a')]
        ep,_,_=reconstruct_episode_tables(self.zones,pd.DataFrame(data),5*T)
        self.assertEqual(ep.iloc[0].state,'PENDING')
    def test_no_past_zone_history(self):
        data=[event('A','TEST_FROM_ABOVE',T,'old'),event('A','TEST_FROM_ABOVE',3*T,'new')]
        ep,_,_=reconstruct_episode_tables(self.zones,pd.DataFrame(data),5*T)
        self.assertEqual(len(ep),1)
        self.assertEqual(ep.iloc[0].first_test_closed_ts,3*T)
    def test_separate_sides_and_anchored_gap(self):
        data=[event('A','TEST_FROM_ABOVE',3*T,'a'),event('A','TEST_FROM_BELOW',4*T,'b'),
              event('B','TEST_FROM_ABOVE',6*T,'c'),event('B','TEST_FROM_ABOVE',7*T,'d')]
        ep,_,_=reconstruct_episode_tables(self.zones,pd.DataFrame(data),9*T)
        self.assertEqual(len(ep),3)
    def test_reject_early_resolution(self):
        data=[event('A','TEST_FROM_ABOVE',3*T,'a'),event('A','LOSS_CONFIRMED',3*T,'a')]
        with self.assertRaises(ValueError):reconstruct_episode_tables(self.zones,pd.DataFrame(data),5*T)
    def test_future_tests_excluded(self):
        data=[event('A','TEST_FROM_ABOVE',3*T,'a'),event('A','TEST_FROM_ABOVE',6*T,'b')]
        ep,_,_=reconstruct_episode_tables(self.zones,pd.DataFrame(data),5*T)
        self.assertEqual(len(ep),1)

if __name__=='__main__': unittest.main()
