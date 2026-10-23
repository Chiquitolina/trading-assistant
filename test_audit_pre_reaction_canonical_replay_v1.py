import tempfile
import unittest
from pathlib import Path
from audit_pre_reaction_canonical_replay_v1 import key, pilot_key, canonical_key, classification, summarize, write_rows, read_csv, as_ts

class CanonicalParityTests(unittest.TestCase):
    def setUp(self):
        self.p={'symbol':'HANAUSDT','side':'LONG','pivot_ts':'1800000000','actionable_ts':'1800900000',
                'first_touch_ts':'1800960000','status':'REACTION','opportunity_key':'HANAUSDT|LONG|p|a'}
        self.c={'symbol':'HANAUSDT','signal':'LONG','pivot_timestamp':1800000000,
                'actionable_timestamp':1800900000,'retest_timestamp':1800960000,'status':'REACTION'}
    def test_keys(self):self.assertEqual(pilot_key(self.p),canonical_key(self.c))
    def test_ts_numpy_compatible(self):self.assertEqual(as_ts('1800960000.0'),1800960000)
    def test_exact(self):self.assertEqual(classification(self.p,self.c),'EXACT_FIRST_TOUCH_PARITY')
    def test_different_first_touch(self):
        c={**self.c,'retest_timestamp':1801020000}
        self.assertEqual(classification(self.p,c),'FIRST_TOUCH_TS_MISMATCH')
    def test_different_status(self):
        c={**self.c,'status':'TOUCH_FAILED'}
        self.assertEqual(classification(self.p,c),'FIRST_TOUCH_STATUS_MISMATCH')
    def test_missing_canonical_touch(self):
        self.assertEqual(classification(self.p,None),'PILOT_TOUCH_ABSENT_CANONICAL')
    def test_missing_both_not_called_exact(self):
        p={**self.p,'status':'ARMED_NO_RETEST','first_touch_ts':''}
        self.assertEqual(classification(p,None),'NO_TOUCH_BOTH_UNRESOLVED_CAUSAL_IDENTITY')
    def test_canonical_touch_on_pilot_none(self):
        p={**self.p,'status':'ARMED_NO_RETEST','first_touch_ts':''}
        self.assertEqual(classification(p,self.c),'CANONICAL_TOUCH_PILOT_NO_TOUCH')
    def test_unknown_not_negative(self):
        p={**self.p,'status':'UNKNOWN_GAP','first_touch_ts':''}
        self.assertEqual(classification(p,None),'UNKNOWN_GAP_NO_PARITY')
    def test_summary(self):
        r={'symbol':'HANAUSDT','pilot_status':'REACTION','parity_status':'EXACT_FIRST_TOUCH_PARITY'}
        m=summarize([r],[self.c])
        self.assertEqual(m['hard_disagreements'],0)
        self.assertEqual(m['pilot_reactions'],1)
        self.assertEqual(m['canonical_only_touches'],1)
    def test_csv_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'tmp.csv';write_rows(p,[{'a':'b','num':12}],['a','num'])
            self.assertEqual(read_csv(p),[{'a':'b','num':'12'}])

if __name__=='__main__': unittest.main()
