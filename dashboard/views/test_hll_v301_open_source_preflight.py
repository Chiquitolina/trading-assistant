import unittest
from dashboard.views.hll_v301_open_source_preflight import classify

class PreflightTests(unittest.TestCase):
    def test_fluid_over_guard(self):
        label,delta=classify(100,99.86139986,'V29_RECLAIM_30M')
        self.assertEqual(label,'OVER_SOURCE_GUARD')
        self.assertAlmostEqual(delta,-.13860014)
    def test_small_difference_kept_auditable(self):
        label,delta=classify(.03653,.03654,'V29_RECLAIM_30M')
        self.assertEqual(label,'WITHIN_SOURCE_GUARD')
        self.assertAlmostEqual(delta,.02737476,places=6)
    def test_flips_cannot_be_reconciled(self):
        self.assertEqual(classify(100,99.999,'FLIP_1M')[0],'FLIP_ARCHIVE_CHANGED')
    def test_missing_source(self):
        self.assertEqual(classify(100,None,'V29_CONTINUATION_30M')[0],'MISSING_EXACT_1M')
    def test_exact(self):
        self.assertEqual(classify(100,100,'FLIP_1M')[0],'EXACT')
if __name__=='__main__': unittest.main()
