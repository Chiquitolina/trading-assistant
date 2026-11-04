import unittest
import pandas as pd
from audit_reaction_defenses_v1 import check_path, chronological_races, utc_ms


def tape(rows):
    base=0
    return pd.DataFrame([{'timestamp':base+i*60_000,'open':o,'high':h,'low':l,'close':c}
                         for i,(o,h,l,c) in enumerate(rows)])


class DefenseAuditTests(unittest.TestCase):
    def test_long_success_before_invalidation(self):
        x=tape([(100,101,99,100)]+[(100,101,100,101)]*16)
        event={'retest_ts':0,'swing_price':99.5,'retest_close':100,'side':'LONG'}
        r=check_path(x,event,15)
        self.assertEqual(r['coverage'],'COMPLETE')
        self.assertEqual(r['race_0_5'],'FAVORABLE_FIRST')
        self.assertFalse(r['wick_beyond_swing'])

    def test_long_initial_reaction_can_fail_afterwards(self):
        x=tape([(100,101,99,100)]+[(100,100,98,99)]*16)
        e={'retest_ts':0,'swing_price':99.5,'retest_close':100,'side':'LONG'}
        r=check_path(x,e,15)
        self.assertTrue(r['wick_beyond_swing'])
        self.assertEqual(r['race_0_5'],'INVALIDATION_FIRST')

    def test_short_swing_broken(self):
        x=tape([(100,101,99,100)]+[(100,102,100,101)]*16)
        e={'retest_ts':0,'swing_price':100.5,'retest_close':100,'side':'SHORT'}
        r=check_path(x,e,15)
        self.assertTrue(r['close_beyond_swing'])
        self.assertEqual(r['race_0_5'],'INVALIDATION_FIRST')

    def test_one_candle_order_unknown(self):
        x=tape([(100,101,99,100)]+[(100,101,98,100)]*16)
        e={'retest_ts':0,'swing_price':99.5,'retest_close':100,'side':'LONG'}
        self.assertEqual(check_path(x,e,15)['race_0_5'],'AMBIGUOUS_1M')

    def test_censored_gap_not_failure(self):
        x=tape([(100,101,99,100)]+[(100,101,100,101)]*16)
        x.loc[5,'timestamp']=x.loc[5,'timestamp']+10_000 # Timestamp gap
        e={'retest_ts':0,'swing_price':99.5,'retest_close':100,'side':'LONG'}
        r=check_path(x,e,15)
        self.assertEqual(r['coverage'],'CENSORED_GAP')
        self.assertNotIn('wick_beyond_swing',r)

    def test_censored_tail(self):
        x=tape([(100,101,99,100)]+[(100,101,100,101)]*5)
        e={'retest_ts':0,'swing_price':99.5,'retest_close':100,'side':'LONG'}
        self.assertEqual(check_path(x,e,15)['coverage'],'CENSORED_TAIL')

    def test_no_next_candle_gap(self):
        x=tape([(100,101,99,100)]+[(100,101,100,101)]*16)
        x.loc[1:,'timestamp']+=60_000
        e={'retest_ts':0,'swing_price':99.5,'retest_close':100,'side':'LONG'}
        self.assertEqual(check_path(x,e,15)['coverage'],'CENSORED_GAP')

    def test_utc_requires_offset(self):
        with self.assertRaises(ValueError):utc_ms('2026-09-17 14:00')

if __name__=='__main__':unittest.main()
