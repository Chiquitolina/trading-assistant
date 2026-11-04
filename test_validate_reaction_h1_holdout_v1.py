import importlib.util
from pathlib import Path
import unittest
import pandas as pd
import numpy as np

source=Path(__file__).with_name('validate_reaction_h1_holdout_v1.py')
spec=importlib.util.spec_from_file_location('h1_validator',source)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class H1ValidatorTests(unittest.TestCase):
    def frame(self):
        raw=[]
        for sym,cut,high,win in [
            ('AAA','2026-09-25T14:00:00Z',True,True),
            ('AAA','2026-09-30T14:00:00Z',False,False),
            ('BBB','2026-09-25T14:00:00Z',True,False),
            ('BBB','2026-09-30T14:00:00Z',False,False),
        ]:
            raw.append(dict(symbol=sym,pre_cutoff_utc=cut,pivot_touch_id=sym+cut,
             original_side='SHORT',original_status='REACTION',swing_timeframe='15m',
             **{'30m_context_status':'ACTIVE','30m_near_0_20_pct':high,
                '30m_group':'NEAR_ACTIVE_0.20' if high else 'FAR_ACTIVE',
                '1h_context_status':'ACTIVE','1h_near_0_20_pct':False,
                '1h_group':'FAR_ACTIVE'},
             original_30m_coverage='COMPLETE',
             original_30m_race_0_5='FAVORABLE_FIRST' if win else 'INVALIDATION_FIRST',
             original_30m_race_close_band_0_5='FAVORABLE_FIRST' if win else 'INVALIDATION_FIRST',
             opposite_breakout_scan_coverage='COMPLETE',opposite_breakout_confirmed=not win))
        return pd.DataFrame(raw)

    def test_h1_rule_separates_without_outcomes(self):
        e,meta=m.transform(self.frame())
        self.assertEqual(e.H1_BACKED.tolist(),[True,False,True,False])
        self.assertEqual(meta['primary_eligible'],4)

    def test_discovery_like_counts(self):
        e,_=m.transform(self.frame());r,tab,by=m.assess(e)
        self.assertEqual((r['backed']['n'],r['backed']['wins']),(2,1))
        self.assertEqual((r['comparator']['n'],r['comparator']['wins']),(2,0))
        self.assertEqual(r['paired_symbols']['both_groups'],2)

    def test_unknown_history_not_negative(self):
        df=self.frame();df.loc[1,'1h_context_status']='UNKNOWN_HISTORY';df.loc[1,'1h_group']='UNKNOWN_HISTORY'
        e,meta=m.transform(df)
        self.assertEqual(meta['unknown_htf'],1)
        self.assertEqual(len(e),3)

    def test_incomplete_excluded_not_loss(self):
        df=self.frame();df.loc[0,'original_30m_coverage']='CENSORED'
        e,meta=m.transform(df)
        self.assertEqual(meta['incomplete_primary_30m'],1)
        self.assertEqual(len(e),3)

    def test_context_tamper_detected(self):
        df=self.frame();df.loc[0,'30m_group']='FAR_ACTIVE'
        with self.assertRaises(ValueError):m.transform(df)

    def test_ambiguous_not_win_but_counted(self):
        df=self.frame();df.loc[0,'original_30m_race_0_5']='AMBIGUOUS_1M'
        e,_=m.transform(df);result,_,_=m.assess(e)
        self.assertEqual(result['backed']['ambiguous'],1)
        self.assertEqual(result['backed']['wins'],0)

    def test_bootstrap_deterministic(self):
        e,_=m.transform(self.frame())
        a=m.symbol_block_bootstrap(e,reps=123)
        b=m.symbol_block_bootstrap(e,reps=123)
        self.assertEqual(a,b)

    def test_both_sides_original_filter(self):
        df=self.frame();df.loc[0,'original_side']='LONG';df.loc[1,'original_status']='TOUCH_INDECISIVE'
        e,_=m.transform(df)
        self.assertEqual(len(e),2)

if __name__=='__main__':unittest.main()
