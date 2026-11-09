import unittest
import pandas as pd
from hll_expired_continuity_audit_v1 import group_runs, continuous_slice, side, STEP

class ContinuityAuditTests(unittest.TestCase):
    def test_side_strict(self):
        self.assertEqual(side(100, 100, 105),'INSIDE')
        self.assertEqual(side(105, 100, 105),'INSIDE')
        self.assertEqual(side(99, 100, 105),'BELOW')
        self.assertEqual(side(106, 100, 105),'ABOVE')
    def test_discontinuous_touch_runs(self):
        bars=pd.DataFrame({'timestamp':[0,STEP,2*STEP,3*STEP,4*STEP]})
        self.assertEqual(group_runs(bars,[True,True,False,True,True],0),[[1],[3,4]])
    def test_gap_breaks_run(self):
        bars=pd.DataFrame({'timestamp':[0,STEP,3*STEP,4*STEP]})
        self.assertEqual(group_runs(bars,[False,True,True,True],0),[[1],[2,3]])
    def test_no_expiry_bar(self):
        bars=pd.DataFrame({'timestamp':[0,STEP]})
        self.assertEqual(group_runs(bars,[True,False],0),[])
    def test_future_contiguous(self):
        bars=pd.DataFrame({'timestamp':[0,STEP,2*STEP,4*STEP,5*STEP]})
        self.assertEqual(continuous_slice(bars,0,4),[1,2])
        self.assertEqual(continuous_slice(bars,3,4),[4])

if __name__=='__main__':unittest.main()
