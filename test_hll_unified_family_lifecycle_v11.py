#!/usr/bin/env python3
"""Unit tests sin acceso LIVE ni modificación de fuentes."""
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

import hll_unified_family_lifecycle_v11 as h


def fixtures():
    a=[{'attempt_id':'a','split':'DISCOVERY_SYMBOL','symbol':'AAAUSDT','level_id':'lev1',
        'episode_id':'episode1','episode_ordinal':'1','pre_closed_ts':'1787292000000',
        'zone_low':'9','zone_high':'10','is_episode_first':'1',
        'fingerprint_status':'COMPLETE_PRE_96_288','pre_pattern_96':'ASCENDING_CLEAN',
        'coverage_FLIP_1M':'ENTRY_VERIFIED','coverage_V29_CONTINUATION_30M':'NO_SIGNAL',
        'coverage_V29_RECLAIM_30M':'NO_SIGNAL',
        'geo_low_up_share_96':'0.8','rebuild_last_low_step_atr_96':'0.4',
        'level_near_share_24_96':'0.7','movement_signed_efficiency_24_96':'0.4'},
       {'attempt_id':'b','split':'DISCOVERY_SYMBOL','symbol':'BBBUSDT','level_id':'lev2',
        'episode_id':'episode2','episode_ordinal':'1','pre_closed_ts':'1787292000000',
        'zone_low':'9','zone_high':'10','is_episode_first':'1',
        'fingerprint_status':'COMPLETE_PRE_96_288','pre_pattern_96':'ASCENDING_CLEAN',
        'coverage_FLIP_1M':'ENTRY_VERIFIED','coverage_V29_CONTINUATION_30M':'NOT_COVERED',
        'coverage_V29_RECLAIM_30M':'NOT_COVERED',
        'geo_low_up_share_96':'0.7','rebuild_last_low_step_atr_96':'0.3',
        'level_near_share_24_96':'0.65','movement_signed_efficiency_24_96':'0.3'}]
    d=[];m=[]
    for i,x in enumerate(a):
        d.append({'attempt_id':x['attempt_id'],'symbol':x['symbol'],'level_id':x['level_id'],
          'pre_closed_ts':x['pre_closed_ts'],'zone_low':x['zone_low'],'zone_high':x['zone_high'],
          'status':'COMPLETE_PRE','family_0p5':'ASCENSO_ESCALONADO',
          'family_1p0':'REBOTE_TRAS_CORRECCION','family_1p5':'REBOTE_TRAS_CORRECCION',
          'scale_agreement':'DISCREPA_ATR'})
        m.append({**d[-1],'episode_id':x['episode_id'],'episode_ordinal':'1','is_first_in_episode':'1'})
    entries=[{**a[0],'entry_id':'ea','route':'FLIP_1M','entry_1m_verified':'YES_SOURCE_ENTRY_VERIFIED',
              'entry_ts':'1787292060000','entry_ts_int':'1787292060000'},
             {**a[1],'entry_id':'eb','route':'FLIP_1M','entry_1m_verified':'YES_SOURCE_ENTRY_VERIFIED',
              'entry_ts':'1787292060000','entry_ts_int':'1787292060000'}]
    outs=[{'entry_id':'ea','attempt_id':'a','symbol':'AAAUSDT','episode_id':'episode1',
        'split':'DISCOVERY_SYMBOL','route':'FLIP_1M','entry_ts':'1787292060000',
        'pre_pattern_96':'ASCENDING_CLEAN','scenario':'TP3_SL1P5_48H','state':'TP_FIRST','net_pct':'2.9'},
        {'entry_id':'eb','attempt_id':'b','symbol':'BBBUSDT','episode_id':'episode2',
        'split':'DISCOVERY_SYMBOL','route':'FLIP_1M','entry_ts':'1787292060000',
        'pre_pattern_96':'ASCENDING_CLEAN','scenario':'TP3_SL1P5_48H','state':'SL_FIRST','net_pct':'-1.6'}]
    sm={'pilot_attempts':2,'verified_entry_events':2,'pilot_episodes':2,'outcome_scenarios':2}
    return a,entries,outs,d,m,sm


class UnifiedV11Tests(unittest.TestCase):
    def test_exact_join_and_preserve_outcomes(self):
        at,en,ou=h.enrich(*fixtures(),expected_universe=2)
        self.assertEqual(len(at),2);self.assertEqual(len(en),2);self.assertEqual(len(ou),2)
        self.assertEqual(ou[0]['state'],'TP_FIRST')
        self.assertEqual(en[0]['family_1p0'],'REBOTE_TRAS_CORRECCION')
        self.assertEqual(at[1]['coverage_V29_CONTINUATION_30M'],'NOT_COVERED')

    def test_wrong_cut_rejected(self):
        x=fixtures(); x[3][0]['pre_closed_ts']='1787293800000'
        with self.assertRaisesRegex(ValueError,'Different PRE cut'):h.enrich(*x,expected_universe=2)

    def test_wrong_level_rejected(self):
        x=fixtures();x[3][0]['level_id']='other'
        with self.assertRaisesRegex(ValueError,'Wrong level'):h.enrich(*x,expected_universe=2)

    def test_wrong_zone_rejected(self):
        x=fixtures();x[3][0]['zone_high']='11'
        with self.assertRaisesRegex(ValueError,'zone_high'):h.enrich(*x,expected_universe=2)

    def test_wrong_episode_rejected(self):
        x=fixtures();x[4][0]['episode_id']='other'
        with self.assertRaisesRegex(ValueError,'episode_id'):h.enrich(*x,expected_universe=2)

    def test_wrong_family_1atr_vs_frozen_map(self):
        x=fixtures();x[3][0]['family_1p0']='ASCENSO_ESCALONADO'
        with self.assertRaisesRegex(ValueError,'map differs'):h.enrich(*x,expected_universe=2)

    def test_scale_agreement_must_match(self):
        x=fixtures();x[3][0]['scale_agreement']='IGUAL_3_ATR'
        with self.assertRaisesRegex(ValueError,'Scale agreement'):h.enrich(*x,expected_universe=2)

    def test_unknown_family_fails_closed(self):
        x=fixtures();x[3][0]['family_0p5']='MAGIC'
        with self.assertRaisesRegex(ValueError,'Unknown family'):h.enrich(*x,expected_universe=2)

    def test_incomplete_is_not_unknown_success(self):
        x=fixtures();x[3][0]['status']='INCOMPLETE_PRE_96'
        with self.assertRaises(ValueError):h.enrich(*x,expected_universe=2)
        x=fixtures();x[3][0].update(status='INCOMPLETE_PRE_96',family_0p5='',family_1p0='',family_1p5='',scale_agreement='SIN_DATOS')
        x[4][0].update(status='INCOMPLETE_PRE_96',family_1p0='SIN_DATOS')
        at,_,_=h.enrich(*x,expected_universe=2)
        self.assertEqual(at[0]['scale_agreement'],'SIN_DATOS')

    def test_scenario_result_not_recomputed(self):
        x=fixtures();x[2][0]['state']='TIMEOUT';x[2][0]['net_pct']='-0.2'
        at,en,ou=h.enrich(*x,expected_universe=2)
        arts=h.outputs_from_enriched(at,en,ou,[])
        self.assertEqual(arts['entry_outcomes_family_v11.csv'][0]['net_pct'],'-0.2')
        m=arts['by_geometry_family_route_outcomes_v11.csv']
        self.assertEqual(sum(int(r['timeout']) for r in m if r['scale']=='1p0'),1)

    def test_comparison_matched_geometry_and_family(self):
        at,en,ou=h.enrich(*fixtures(),expected_universe=2)
        out=h.outputs_from_enriched(at,en,ou,[])
        pairs=out['family_geometry_flip_review_pairs_v11.csv']
        self.assertEqual(len(pairs),1)
        self.assertEqual(pairs[0]['family_1p0'],'REBOTE_TRAS_CORRECCION')
        self.assertEqual(pairs[0]['pre_pattern_96'],'ASCENDING_CLEAN')

    def test_pair_never_mix_holdout(self):
        x=fixtures();x[0][1]['split']='HOLDOUT_SYMBOL';x[1][1]['split']='HOLDOUT_SYMBOL';x[2][1]['split']='HOLDOUT_SYMBOL';x[2][0]['split']='HOLDOUT_SYMBOL';x[0][0]['split']='HOLDOUT_SYMBOL';x[0][1]['split']='HOLDOUT_SYMBOL';x[1][0]['split']='HOLDOUT_SYMBOL';x[1][1]['split']='HOLDOUT_SYMBOL'
        at,en,ou=h.enrich(*x,expected_universe=2)
        self.assertEqual(len(h.outputs_from_enriched(at,en,ou,[])['family_geometry_flip_review_pairs_v11.csv']),0)

    def test_no_cross_symbol_episode_same_level_groups(self):
        x=fixtures();x[0][1]['episode_id']='episode1';x[1][1]['episode_id']='episode1'
        x[4][1]['episode_id']='episode1';x[4][1]['episode_ordinal']='2';x[4][1]['is_first_in_episode']='0';x[0][1]['is_episode_first']='0'
        x[1][1]['episode_id']='episode1';x[0][1]['episode_ordinal']='2';x[1][1]['episode_ordinal']='2'
        x[5]['pilot_episodes']=1
        # one episode spanning unrelated symbols must be rejected by evolution.
        x[2][1]['episode_id']='episode1'
        at,en,ou=h.enrich(*x,expected_universe=2)
        with self.assertRaisesRegex(ValueError,'Episode crosses symbol'):h.outputs_from_enriched(at,en,ou,[])

    def test_ordinal_can_change_per_attempt_without_family_copy(self):
        x=fixtures();x[0][1].update(symbol='AAAUSDT',level_id='lev1',episode_id='episode1',episode_ordinal='2',is_episode_first='0')
        x[1][1].update(symbol='AAAUSDT',level_id='lev1',episode_id='episode1',episode_ordinal='2',is_episode_first='0')
        x[3][1].update(symbol='AAAUSDT',level_id='lev1',status='COMPLETE_PRE',family_0p5='REBOTE_TRAS_CORRECCION',family_1p0='ASCENSO_ESCALONADO',family_1p5='ASCENSO_ESCALONADO')
        x[4][1].update(symbol='AAAUSDT',level_id='lev1',episode_id='episode1',episode_ordinal='2',is_first_in_episode='0',family_1p0='ASCENSO_ESCALONADO')
        x[5]['pilot_episodes']=1
        x[2][1].update(symbol='AAAUSDT',episode_id='episode1')
        at,en,ou=h.enrich(*x,expected_universe=2)
        ep=h.outputs_from_enriched(at,en,ou,[])['episode_family_evolution_v11.csv'][0]
        self.assertEqual(ep['family_changes_1p0_retrospective'],1)
        self.assertEqual(at[0]['family_1p0'],'REBOTE_TRAS_CORRECCION')
        self.assertEqual(at[1]['family_1p0'],'ASCENSO_ESCALONADO')

    def test_exact_decimal_timestamp_float_style(self):
        self.assertEqual(h.exact_ts('1787292000000.0'),1787292000000)
        with self.assertRaises(ValueError):h.exact_ts('1787292000000.1')

    def test_no_overwrite_and_no_output_inside_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths={'v1':Path(tmp)/'src','discovery':Path(tmp)/'d', 'map':Path(tmp)/'m', 'output':Path(tmp)/'src'/'output'}
            with self.assertRaisesRegex(ValueError,'Refuse output'):h.run(paths)
            paths['output']=Path(tmp)/'out';paths['output'].mkdir()
            with self.assertRaises(FileExistsError):h.run(paths)

    def test_single_group_coverage_always_separates_not_covered(self):
        at,en,ou=h.enrich(*fixtures(),expected_universe=2)
        out=h.outputs_from_enriched(at,en,ou,[])
        row=out['by_geometry_family_coverage_v11.csv'][0]
        self.assertEqual(row['V29_CONTINUATION_30M_NO_SIGNAL'],1)
        self.assertEqual(row['V29_CONTINUATION_30M_NOT_COVERED'],1)


if __name__=='__main__':unittest.main()
