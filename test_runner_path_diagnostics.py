import unittest
from analyze_runner_path_diagnostics import path_features


def mk_path(seq, entry=100.0):
    arr=[]; t=60_000
    for i,(hi,lo,close) in enumerate(seq):
        arr.append((t+i*60_000,entry if i==0 else seq[i-1][2],hi,lo,close))
    return arr

class TestPaths(unittest.TestCase):
    def test_long_tp_first(self):
        p=mk_path([(100.6,99.8,100.2),(102.5,100.1,101.8),(103,98,99)])*90
        r=path_features(p,'LONG')
        self.assertEqual(r['result'],'TP_FIRST')
        self.assertEqual(r['first_tp_2_min'],2)
        self.assertEqual(r['first_sl_1_min'],3)
    def test_stop_then_runner_long(self):
        p=mk_path([(100.5,98.5,99.0),(101,99,100),(103,100,102)])*60
        r=path_features(p,'LONG')
        self.assertEqual(r['result'],'SL_FIRST')
        self.assertTrue(r['sl1_then_tp2'])
        self.assertEqual(r['minutes_from_sl1_to_tp2'],2)
    def test_short_stop_then_runner(self):
        p=mk_path([(101.1,99.7,101),(101,96.5,97)])*90
        r=path_features(p,'SHORT')
        self.assertEqual(r['result'],'SL_FIRST')
        self.assertEqual(r['first_tp_2_min'],2)
        self.assertTrue(r['sl1_then_tp2'])
    def test_same_candle_ambiguous(self):
        p=mk_path([(102.5,98.5,100)])*180
        r=path_features(p,'LONG')
        self.assertEqual(r['result'],'AMBIGUOUS_SL_FIRST')
        self.assertTrue(r['tp2_and_sl1_same_min'])
        self.assertFalse(r['sl1_then_tp2'])
    def test_no_hit(self):
        p=mk_path([(100.5,99.5,100)])*180
        r=path_features(p,'LONG')
        self.assertEqual(r['result'],'NO_HIT')
        self.assertFalse(r['runner_ge2_from_entry'])
    def test_threshold_matches_legacy_binary_float(self):
        p=mk_path([(102.0,99.5,100)])*180
        r=path_features(p,'LONG')
        # 102/100-1 is slightly above 0.02 in binary float; legacy uses >2.
        self.assertEqual(r['result'],'TP_FIRST')
        self.assertTrue(r['runner_ge2_from_entry'])
        self.assertTrue(r['runner_gt2_from_entry'])

if __name__=='__main__':unittest.main()
