import unittest
from run_pullback_limit_audit import limit_fill,simulate_at_fill,original_sim

def path(opens,highs,lows,closes):return [(i*60000,opens[i],highs[i],lows[i],closes[i]) for i in range(len(opens))]
class TestLimitAudit(unittest.TestCase):
 def test_long_limit_fill(self):
  p=path([100,99,99],[100.1,100.2,103],[99.7,98.8,98.5],[99.8,99,102])
  self.assertEqual(limit_fill(p,'LONG',1,3),(1,99))
  v=simulate_at_fill(p+[p[-1]]*178,'LONG',1,99,2,1)
  self.assertEqual(v['result'],'TP_FIRST')
 def test_short_limit_fill(self):
  p=path([100,101,100],[100.2,101.2,101.0],[99.7,100.5,97.5],[100,101,97.6])
  self.assertEqual(limit_fill(p,'SHORT',1,3),(1,101))
  v=simulate_at_fill(p+[p[-1]]*178,'SHORT',1,101,2,1)
  self.assertEqual(v['result'],'TP_FIRST')
 def test_no_fill(self):
  p=path([100]*5,[100.5]*5,[99.5]*5,[100]*5)
  self.assertEqual(limit_fill(p,'LONG',1,5)[0],None)
 def test_expiry(self):
  p=path([100]*4,[100.5]*4,[99.8,99.8,98.9,98.9],[100]*4)
  self.assertEqual(limit_fill(p,'LONG',1,2)[0],None)
  self.assertEqual(limit_fill(p,'LONG',1,3)[0],2)
 def test_sl_before_later_tp(self):
  p=path([100,99,98,101],[100,99.1,98.2,104],[99.7,98.7,97.5,100],[99.8,98.8,98.1,103]);p+= [(i*60000,100,100,99,100) for i in range(4,190)]
  i,l=limit_fill(p,'LONG',1,5)
  self.assertEqual((i,l),(1,99))
  v=simulate_at_fill(p,'LONG',i,l,2,1)
  self.assertEqual(v['result'],'SL_FIRST')
 def test_fill_bar_tp_cannot_be_credited(self):
  p=path([100,100,100],[104,100,100],[98.9,99.5,99.5],[100,100,100]);p += [(i*60000,100,100,99.5,100) for i in range(3,200)]
  i,level=limit_fill(p,'LONG',1,5)
  v=simulate_at_fill(p,'LONG',i,level,2,1)
  self.assertTrue(v['fill_bar_tp_possible_before_fill'])
  self.assertNotEqual(v['result'],'TP_FIRST')
 def test_same_bar_stop_after_limit(self):
  p=path([100]*181,[100.5]*181,[97.0]+[100]*180,[98]+[100]*180)
  i,l=limit_fill(p,'LONG',1,3)
  self.assertEqual(simulate_at_fill(p,'LONG',i,l,2,1)['result'],'SL_FIRST')
 def test_reference_original_open(self):
  p=path([100]*180,[103]+[100]*179,[99.8]*180,[102]+[100]*179)
  self.assertEqual(original_sim(p,'LONG',2,1)[0],'TP_FIRST')
 def test_short_sign_correct(self):
  p=path([100]*181,[101.1]+[99.2]*180,[99.8]+[97.8]*180,[100]*181)
  i,level=limit_fill(p,'SHORT',1,2)
  self.assertEqual((i,level),(0,101))
  self.assertEqual(simulate_at_fill(p,'SHORT',0,level,2,1)['result'],'TP_FIRST')
if __name__=='__main__':unittest.main()
