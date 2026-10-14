import unittest
from run_runner_delayed_entry_audit import eligible,simulate,selected,cohort,calc,summaries

def bars(seq,entry=100):
 return [(i*60000, o, h, l, c) for i,(o,h,l,c) in enumerate(seq)]
class TestDelays(unittest.TestCase):
 def test_strict_tp_and_sl(self):
  p=bars([(100,101.999,99.5,101)]*200)
  self.assertEqual(simulate(p,'LONG',0,2,1)['result'],'NO_HIT')
  p=bars([(100,102.01,99.5,101)]*200)
  self.assertEqual(simulate(p,'LONG',0,2,1)['result'],'TP_FIRST')
 def test_same_bar_stop_first(self):
  p=bars([(100,103,98,100)]*200)
  self.assertEqual(simulate(p,'LONG',0,2,1)['result'],'AMBIGUOUS_SL_FIRST')
 def test_close_confirmation_prior_bar_only(self):
  p=bars([(100,100.5,99.5,99.5)]*5 +[(100,103,98,100)]*195)
  self.assertFalse(eligible(p,'LONG',5,True))
  p[4]=(240000,100,101,99,100.2)
  self.assertTrue(eligible(p,'LONG',5,True))
  self.assertTrue(eligible(p,'SHORT',5,False))
 def test_delayed_uses_new_entry_price(self):
  p=bars([(100,100.2,99.8,100)]*5 + [(110,112.5,109,111)]*180)
  z=simulate(p,'LONG',5,2,1)
  self.assertEqual(z['entry_price'],110)
  self.assertEqual(z['result'],'TP_FIRST')
 def test_no_hindsight_into_selection(self):
  p=bars([(100,101,99,100.1)]*5 + [(100,105,80,90)]*180)
  self.assertTrue(eligible(p,'LONG',5,True))
  self.assertEqual(simulate(p,'LONG',5,2,1)['result'],'AMBIGUOUS_SL_FIRST')
 def test_matched_comparison_subset(self):
  r=dict(event_key='X|LONG|0',symbol='X',signal='LONG',known_ts='60000',retest_timestamp='0',
         max_departure_pct='2',opposing_room_1h_pct='3',volume_rank_pct_4h='80',return_rank_pct_4h='81')
  p=bars([(100,100.4,99.8,100.2)]*5 + [(100,102.5,99.5,102)]*190)
  details,issues=calc([r],{r['event_key']:p})
  self.assertFalse(issues)
  a,b=summaries(details,[r])
  self.assertTrue(any(x['group']=='4_FACTORS' and x['mode']=='WAIT_5_FAVORABLE_CLOSE' and x['scenario']=='TP2_SL1' for x in a))
  self.assertTrue(any(x['mode']=='WAIT_5_FAVORABLE_CLOSE' and x['matched_n']==1 for x in b))

if __name__=='__main__':unittest.main()
