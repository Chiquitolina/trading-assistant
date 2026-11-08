import unittest
import sys
import types
from pathlib import Path
import pandas as pd
try:
    import horizontal_breakout_lab
except ModuleNotFoundError:
    fake=types.ModuleType("horizontal_breakout_lab")
    fake.TF=1_800_000;fake.ROOT=Path(".");fake.Archive=object
    fake.aggregate_15m_to_30m=lambda x:x;fake.normalize=lambda b,tf:b
    sys.modules["horizontal_breakout_lab"]=fake
from hll_structural_recovery_v22b import analyze_loss,TF


def bars(seq):
    return pd.DataFrame([dict(timestamp=i*TF,open=c,high=c+0.3,low=c-0.3,close=c,volume=1)
                         for i,c in enumerate(seq)])

class RecoveryV22B(unittest.TestCase):
    def test_defense_loss_recovery_and_hold(self):
        b=bars([101,100,99,101,102,103,102,99])
        r=analyze_loss(b,2*TF,'ABOVE',(99.5,100.5),(99,101),horizon_bars=5)
        self.assertEqual(r['first_level_recovery_ts'],4*TF)
        self.assertEqual(r['zone_recovery_2close_ts'],6*TF)
        self.assertEqual(r['zone_hold_bars_after_2close'],1)
        self.assertTrue(r['complete_horizon'])
    def test_zone_vs_line_not_same(self):
        b=bars([100,99,100.2,100.3,100.4,100.2])
        r=analyze_loss(b,2*TF,'ABOVE',(99.5,100),(99,101),horizon_bars=3)
        self.assertIsNotNone(r['level_recovery_2close_ts'])
        self.assertIsNone(r['zone_recovery_2close_ts'])
    def test_censored_is_not_failure(self):
        b=bars([100,99,101,102])
        r=analyze_loss(b,2*TF,'ABOVE',(99,100),(99,100),horizon_bars=9)
        self.assertFalse(r['complete_horizon'])
        self.assertIsNotNone(r['zone_recovery_2close_ts'])
        self.assertIn('RIGHT_CENSORED',r['ambiguity_note'])
    def test_reverse_for_resistance(self):
        b=bars([99,102,100,98,97,96])
        r=analyze_loss(b,2*TF,'BELOW',(99,101),(99,101),horizon_bars=3)
        self.assertEqual(r['zone_recovery_2close_ts'],5*TF)
    def test_missing_path_rejected(self):
        b=bars([100,99,101]);b.loc[1,'timestamp']+=1
        with self.assertRaises(ValueError):
            analyze_loss(b,TF,'ABOVE',(99,100),(99,100))

if __name__=='__main__':unittest.main()
