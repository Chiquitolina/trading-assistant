"""Strictly offline tests for outcomes; never implies an executable trade."""
import unittest
from research.micro_flow_v3.gap_aware_outcomes import evaluate_gap_aware_outcome,validate_intervals
from research.micro_flow_v3.gap_aware_outcomes import DiscoveryError

BASE=1792500000000

def row(second,price,seg=1):
    return {'event_type':'BUCKET_1S','symbol':'ETHUSDT','segment_id':seg,
            'second_start_ms':BASE+second*1000,
            'second_end_ms_exclusive':BASE+(second+1)*1000,
            'available_wall_ns':(BASE+(second+2)*1000)*1_000_000,
            'first_seq':second+1,'last_seq':second+1,
            'open':price,'close':price,'high':price,'low':price,
            'buy_qty':1,'sell_qty':0,'trades':1}

def proof(a,b,seg=1):
    return {'event_type':'ID_CHAIN_CONSISTENT_NO_TRADE_INTERVAL_DIAGNOSTIC',
            'segment_id':seg,'start_ms':BASE+a*1000,'end_ms_exclusive':BASE+b*1000,
            'seconds':b-a,'prev_agg_id':8,'next_agg_id':9,
            'verified_only_after_wall_ns':(BASE+(b+2)*1000)*1_000_000}

class GapAwareTests(unittest.TestCase):
    def evaluate(self,rows,intervals,h=10):
        return evaluate_gap_aware_outcome(rows,intervals,segment_id=1,
                                         entry_second_start_ms=BASE,
                                         entry_price=100,side='LOW',horizon_seconds=h)

    def test_recovers_id_chain_confirmed_silent_gap_but_no_fill(self):
        rows=[row(0,100),row(1,100),row(8,100.4),row(10,100.6)]
        got=self.evaluate(rows,[proof(2,8)])
        self.assertEqual(got['state'],'OBSERVED_PROXY_WITH_AUDITED_SILENT_SECONDS')
        self.assertEqual(got['id_chain_consistent_silent_seconds'],6)
        self.assertAlmostEqual(got['return_directional_pct'],0.6)
        self.assertIn('NOT_EXECUTABLE',got['caution'])

    def test_unverified_gap_remains_censored(self):
        rows=[row(0,100),row(1,100),row(8,100.4),row(10,100.6)]
        self.assertEqual(self.evaluate(rows,[])['state'],'CENSORED_UNVERIFIED_OBSERVATION_GAP')
        self.assertEqual(self.evaluate(rows,[proof(3,8)])['state'],
                         'CENSORED_UNVERIFIED_OBSERVATION_GAP')

    def test_no_fresh_endpoint_remains_censored(self):
        rows=[row(0,100),row(1,100),row(8,100.4),row(15,100.6)]
        self.assertEqual(self.evaluate(rows,[proof(2,8),proof(9,15)])['state'],
                         'CENSORED_NO_FRESH_ENDPOINT')

    def test_no_cross_segments_even_if_interval_forged(self):
        rows=[row(0,100),row(1,100),row(8,100.4,seg=2),row(10,100.6,seg=2)]
        self.assertEqual(self.evaluate(rows,[proof(2,8)])['state'],
                         'CENSORED_NO_FRESH_ENDPOINT')

    def test_invalid_intervals_rejected(self):
        p=proof(2,8)
        p['next_agg_id']=100
        with self.assertRaises(DiscoveryError):validate_intervals([p])

if __name__=='__main__':unittest.main()
