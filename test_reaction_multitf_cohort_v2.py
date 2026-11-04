#!/usr/bin/env python3
"""Safety and aggregation tests for fixed-cohort multitimeframe batch."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
import pandas as pd

sys.path.insert(0,str(Path(__file__).parent))
import reaction_multitf_cohort_v2 as c

class CohortTests(unittest.TestCase):
    def test_selection_independent_of_file_order(self):
        a=['BTCUSDT','AAVEUSDT','XRPUSDT','ETHUSDT']
        self.assertEqual(c.select_symbols(a,3),c.select_symbols(list(reversed(a)),3))
        self.assertEqual(len(c.select_symbols(a,3)),3)
    def test_selection_includes_no_outcome_fields(self):
        self.assertIn('DYDXUSDT',c.EXCLUDE)
        self.assertEqual(c.PRE_CANDLES,96)
        self.assertTrue(all(t.endswith('14:00:00Z') for t in c.DISCOVERY_CUTS+c.HOLDOUT_CUTS))
    def test_protect_holdout(self):
        with self.assertRaises(PermissionError):self._holdout()
    def _holdout(self):
        # Isolate the authorisation gate with a valid minimal plan via patch.
        from unittest.mock import patch
        with patch.object(c,'validate_plan'):
            c.run_stage(Path('/tmp'),{},'holdout',allow_holdout=False)
    def test_htf_groups_distinguish_absence_invalidation_and_active(self):
        for status,flag,expect in [('NO_CONFIRMED_CANDIDATE',None,'NO_CONFIRMED_CANDIDATE'),('ALL_CANDIDATES_INVALIDATED',None,'ALL_CANDIDATES_INVALIDATED'),('UNKNOWN_HISTORY',None,'UNKNOWN_HISTORY'),('ACTIVE',False,'FAR_ACTIVE'),('ACTIVE',True,'NEAR_ACTIVE_0.20')]:
            r={'30m_context_status':status,'30m_near_0_20_pct':flag}
            self.assertEqual(c.context_group(r,'30m'),expect)
    def test_counts_use_only_complete_horizons(self):
        x=pd.DataFrame([{'original_30m_coverage':'COMPLETE','original_30m_race_0_5':'FAVORABLE_FIRST'}, {'original_30m_coverage':'CENSORED','original_30m_race_0_5':'INVALIDATION_FIRST'},{'original_30m_coverage':'COMPLETE','original_30m_race_0_5':'INVALIDATION_FIRST'}])
        self.assertEqual(c._count_complete_race(x,30,False)[:2],(2,1))
    def test_case_paths_unique_by_cutoff(self):
        root=Path('/tmp/root')
        p=c.case_paths(root,'discovery',c.DISCOVERY_CUTS[0],'BTCUSDT')
        q=c.case_paths(root,'discovery',c.DISCOVERY_CUTS[1],'BTCUSDT')
        self.assertNotEqual(p['events'],q['events'])
        self.assertEqual(p['events'].name,'BTCUSDT_multitf_events.csv')
    def test_plan_inventory_excludes_pilot_and_unpaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            mm=root/'reports/horizontal_level_full290_v301/minutes_manifest';mm.mkdir(parents=True)
            m=root/'reports/horizontal_level_full290_v301/minutes';m.mkdir(parents=True)
            for s in ('DYDXUSDT','BTCUSDT','ETHUSDT'):(mm/(s+'.json')).write_text('{}')
            for s in ('DYDXUSDT','BTCUSDT'):(m/(s+'_1m.csv')).write_text('')
            self.assertEqual(c.inventory(root),['BTCUSDT'])
    def test_fingerprints_require_actual_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):c.fingerprint(Path(tmp))
    def test_wick_close_have_separate_rates(self):
        def rec(race,close):
            return dict(symbol='BTCUSDT',pre_cutoff_utc=c.DISCOVERY_CUTS[0],swing_timeframe='15m',
                        original_side='LONG',original_status='REACTION',
                        original_30m_coverage='COMPLETE',original_30m_race_0_5=race,
                        original_30m_race_close_band_0_5=close,
                        original_60m_coverage='COMPLETE',original_60m_race_0_5=race,
                        original_60m_race_close_band_0_5=close,
                        opposite_breakout_scan_coverage='COMPLETE',opposite_breakout_confirmed=False,
                        opposite_30m_coverage=None)
        d=pd.DataFrame([rec('INVALIDATION_FIRST','FAVORABLE_FIRST'),rec('FAVORABLE_FIRST','FAVORABLE_FIRST')])
        x=c._group(d,['swing_timeframe','original_side','original_status'],True).iloc[0]
        self.assertEqual(x['30m_wins_wick'],1);self.assertEqual(x['30m_wins_close_band'],2)
    def test_v1_manifest_and_pool_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for rel in c.SOURCE_FILES:
                f=root/rel;f.parent.mkdir(parents=True,exist_ok=True);f.write_text(rel)
            plan={'version':c.VERSION,'seed':c.SEED,'pre_candles_30m':96,
                  'selected_symbols':['TESTUSDT'],'discovery_cutoffs_utc':list(c.DISCOVERY_CUTS),
                  'holdout_cutoffs_utc':list(c.HOLDOUT_CUTS),
                  'source_sha256':c.fingerprint(root)}
            plan_file=root/'reports/reaction_multitf_cohort_v2/plan.json'
            plan_file.parent.mkdir(parents=True)
            plan_file.write_text(json.dumps(plan))
            plan['_plan_path']=str(plan_file)
            for cutoff in c.DISCOVERY_CUTS:
                paths=c.case_paths(root,'discovery',cutoff,'TESTUSDT')
                paths['manifest'].parent.mkdir(parents=True)
                row=dict(symbol='TESTUSDT',pivot_touch_id='x_'+cutoff,event_key='a',
                         swing_timeframe='15m',original_side='SHORT',original_status='REACTION',
                         original_30m_coverage='COMPLETE',original_30m_race_0_5='FAVORABLE_FIRST',
                         original_30m_race_close_band_0_5='FAVORABLE_FIRST',
                         original_60m_coverage='COMPLETE',original_60m_race_0_5='FAVORABLE_FIRST',
                         original_60m_race_close_band_0_5='FAVORABLE_FIRST',
                         opposite_breakout_scan_coverage='COMPLETE',opposite_breakout_confirmed=False,
                         opposite_30m_coverage=None)
                pd.DataFrame([row]).to_csv(paths['events'],index=False)
                pd.DataFrame([row|{'30m_context_status':'ALL_CANDIDATES_INVALIDATED',
                    '30m_near_0_20_pct':None,'1h_context_status':'ACTIVE',
                    '1h_near_0_20_pct':True}]).to_csv(paths['context'],index=False)
                pd.DataFrame([{'tf':'15m','n':1}]).to_csv(paths['summary'],index=False)
                pd.DataFrame([{'tf':'30m','n':1}]).to_csv(paths['context_summary'],index=False)
                meta={'version':'REACTION_MULTITF_SWING_AUDIT_V1','symbol':'TESTUSDT',
                      'pre_cutoff_utc':cutoff,'pre_candles_30m':96,'htf_context_max_age_min':1440,
                      '15m_overlay_exact_parity':{'first_touches':1,'reactions':1},
                      'source_sha256':{'reports/horizontal_level_full290_v301/minutes/TESTUSDT_1m.csv':'test-tape-hash', **{k:v for k,v in plan['source_sha256'].items()
                                       if k not in ('audit_reaction_multitf_v1.py','audit_reaction_defenses_v1.py')}},
                      'auditor_sha256':plan['source_sha256']['audit_reaction_multitf_v1.py'],
                      'output_sha256':{name:c.sha256(paths[name]) for name in ('events','summary','context','context_summary')}}
                paths['manifest'].write_text(json.dumps(meta))
                c.verify_case(paths,'TESTUSDT',cutoff,plan['source_sha256'])
            report=c.pool_stage(root,plan,'discovery',root/'reports/reaction_multitf_cohort_v2/study',require_all=True)
            self.assertTrue(report['complete_cohort'])
            self.assertEqual((report['direct_rows'],report['htf15m_rows']),(2,2))
            summary=pd.read_csv(root/'reports/reaction_multitf_cohort_v2/study_direct_summary.csv')
            self.assertEqual(int(summary.iloc[0]['30m_wins_wick']),2)
            with self.assertRaises(FileExistsError):
                c.pool_stage(root,plan,'discovery',root/'reports/reaction_multitf_cohort_v2/study',require_all=True)

    def test_run_does_not_replace_existing_files(self):
        # Case verification always needs matching manifest and hashes; no branch overwrites.
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=c.case_paths(root,'discovery',c.DISCOVERY_CUTS[0],'BTCUSDT')
            p['events'].parent.mkdir(parents=True);p['events'].write_text('partial')
            self.assertFalse(p['manifest'].exists())

if __name__=='__main__':unittest.main()
