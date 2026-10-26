import unittest
import pandas as pd
from hll_v25_flow_compare import summarize
class CompareTests(unittest.TestCase):
 def test_no_outcome_selected_duplicate(self):
  f=pd.DataFrame([dict(event_id=x,market_regime='RISK_ON',side_flow='TAILWIND') for x in ['a','b','c']])
  rows=[]
  for i,e in enumerate(['b','a','c']):
   rows.append(dict(event_id=e,symbol='S',stage='RESISTANCE_CROSS_UP',known_closed_ts=0 if e!='c' else 1800000,
                    tp_pct=3.0,sl_pct=2.0,horizon_min=360,path_state='COMPLETE_1M',
                    outcome='TP_FIRST' if e=='b' else 'SL_FIRST',return_after_fee_pct=2.9 if e=='b' else -2.1))
  out=summarize(f,pd.DataFrame(rows))
  self.assertEqual(int(out.n.sum()),2)
  self.assertEqual(int(out.tp.sum()),0) # b excluded because event a sorts first
 def test_bad_scenario_raises(self):
  with self.assertRaises(ValueError): summarize(pd.DataFrame(),pd.DataFrame(dict(tp_pct=[1.0],sl_pct=[2.0],horizon_min=[360],path_state=['COMPLETE_1M'])))
if __name__=='__main__':unittest.main()
