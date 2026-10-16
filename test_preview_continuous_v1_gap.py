import unittest

from preview_continuous_v1_gap import cmp_payload, summarize, gap_info


class GapPreviewTests(unittest.TestCase):
    def test_same_event_key_different_swings_flagged(self):
        k = 'BNBUSDT|LONG|1791941820000'
        native = {k:{'payload':{'pivot_timestamp':1791929700000,'max_departure_pct':0.582359}}}
        latest = {k:{'payload':{'pivot_timestamp':1791933300000,'max_departure_pct':0.579647}}}
        rows, missing, quality = summarize(native,latest,{})
        self.assertEqual(len(rows),1)
        self.assertEqual(len(missing),1)
        self.assertEqual(rows[0]['quality'],'POLICY_DISAGREEMENT')
        self.assertIn('pivot_timestamp',rows[0]['different_fields'])
        self.assertEqual(quality['POLICY_DISAGREEMENT'],1)

    def test_identical_but_not_verified(self):
        k = 'AAAUSDT|SHORT|1800000000000'
        p={'pivot_timestamp':1799999900000,'max_departure_pct':2.0}
        rows, missing, quality=summarize({k:{'payload':p}},{k:{'payload':p}},{})
        self.assertEqual(rows[0]['quality'],'AGREEMENT_UNVERIFIED')
        self.assertEqual(quality['AGREEMENT_UNVERIFIED'],1)

    def test_existing_not_counted_missing(self):
        k='AAAUSDT|SHORT|1800000000000'
        rows,missing,quality = summarize({k:{'payload':{}}},{k:{'payload':{}}},{k:{'event_key':k}})
        self.assertEqual(len(rows),1)
        self.assertFalse(missing)

    def test_policy_only_id_flagged(self):
        k='BBB|LONG|1800000000000'
        rows, missing, quality=summarize({k:{'payload':{}}},{},{})
        self.assertEqual(rows[0]['different_fields'],'ONLY_ONE_POLICY')
        self.assertEqual(quality['POLICY_DISAGREEMENT'],1)

    def test_boundary_missing_caught(self):
        one = 60000
        got = gap_info([{'timestamp':one},{'timestamp':3*one}], one,one,4*one)
        self.assertEqual(got['expected_bars'],3)
        self.assertEqual(got['missing_bars'],1)
        self.assertEqual(got['first_missing_ms'],2*one)

    def test_numeric_close_same(self):
        self.assertEqual(cmp_payload({'swing_price':100.0},{'swing_price':100.0}),[])


if __name__ == '__main__':
    unittest.main()
