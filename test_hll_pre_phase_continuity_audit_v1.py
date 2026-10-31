"""Research-only causal and integrity regression tests: Phase Continuity V1."""
from __future__ import annotations

import copy
import unittest
from pathlib import Path

import hll_pre_phase_continuity_audit_v1 as mod

AAVE='2221ee173c945cf17b026f2d'
PEPE='5c16416a996220d47fe32c96'
TWOZ='412e50a892f6e90f492ceaaa'
INCH='917bcd9be0ad18833727dc3d'
BONK='a419c2ae10918faceff87cc4'
ROOT=Path(__file__).resolve().parent


class ContinuityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = ROOT/'reports/horizontal_level_second_ascent_pre/core15_phase_reconstruction_pre_v1'
        matrix=ROOT/'reports/horizontal_level_second_ascent_pre/core15_pre_family_audit_v1/audit_matrix_pre.csv'
        # If the test is bundled without historical reports, assert the
        # algorithm via pure fixtures in the separate unit tests below.
        cls.have_sources = matrix.is_file() and (source/'phase_summary_pre.csv').is_file()
        if cls.have_sources:
            cls.audit=mod.read_csv(matrix)
            cls.summary=mod.read_csv(source/'phase_summary_pre.csv')
            cls.legs=mod.read_csv(source/'phase_legs_pre.csv')
            cls.candidates=mod.read_csv(source/'phase_recovery_candidates_pre.csv')

    def need_data(self):
        if not self.have_sources:self.skipTest('Requiere los reportes CORE15 en la VPS')

    def selection(self,aid,scale=1.0):
        self.need_data()
        ss=next(r for r in self.summary if r['attempt_id']==aid and float(r['scale_atr'])==scale)
        legs=[r for r in self.legs if r['attempt_id']==aid and float(r['scale_atr'])==scale]
        cand=[r for r in self.candidates if r['attempt_id']==aid and float(r['scale_atr'])==scale]
        return copy.deepcopy(ss),copy.deepcopy(legs),copy.deepcopy(cand)

    def test_real_cohort_50_x_3_and_cycles(self):
        self.need_data()
        a,b,c=mod.build_data(self.audit,self.summary,self.legs,self.candidates)
        self.assertEqual((len(a),len(b)),(150,50))
        self.assertEqual({r['attempt_id'] for r in b},{r['attempt_id'] for r in self.audit})
        self.assertTrue(all(r['status']=='DESCRIPTIVE_PRE_ONLY' for r in a))
        self.assertTrue(all(r['terminal_leg_is_provisional']==1 for r in a))
        self.assertTrue(all(r['scale_atr'] in mod.SCALES for r in a))

    def test_2z_recent_rebound_does_not_hide_previous_giveback(self):
        s,legs,cand=self.selection(TWOZ)
        r,cycles=mod.analyze_one(s,legs,cand)
        self.assertAlmostEqual(r['latest_recovery_retention_frac'],0.86153846,places=6)
        self.assertAlmostEqual(r['preserved_fraction_last3'],0.0640394,places=5)
        self.assertEqual(r['complete_givebacks_last3'],1)
        self.assertEqual(r['left_censored_cycles'],1)
        self.assertEqual(r['latest_confirmed_low_age_bars'],3)
        self.assertEqual(r['completed_up_down_cycles'],2)
        self.assertTrue(any(c['giveback_ratio']>2 for c in cycles))
        self.assertTrue(any(c['left_censored'] for c in cycles))

    def test_aave_pepe_bonk_not_separated_by_binary_giveback_alone(self):
        self.need_data()
        found={}
        for aid in (AAVE,PEPE,INCH,BONK):
            found[aid]=mod.analyze_one(*self.selection(aid))[0]
        self.assertGreater(found[AAVE]['preserved_fraction_last3'],0.4)
        self.assertGreater(found[PEPE]['preserved_fraction_last3'],0.5)
        self.assertGreater(found[INCH]['preserved_fraction_last3'],0)
        self.assertLess(found[BONK]['preserved_fraction_last3'],-0.8)
        # PEPE is visually constructive but also has two >100% giving-back cycles.
        self.assertEqual(found[PEPE]['complete_givebacks_last3'],2)

    def test_latest_anchor_chosen_by_confirmation_not_best_gain(self):
        s,legs,cand=self.selection(TWOZ)
        r,_=mod.analyze_one(s,legs,cand)
        latest=max(cand,key=lambda c:int(c['anchor_low_open_ts']))
        self.assertEqual(int(latest['is_latest_confirmed_low']),1)
        self.assertEqual(r['latest_recovery_net_atr'],float(latest['net_change_from_low_atr']))
        self.assertGreater(max(float(c['max_favorable_close_from_low_atr']) for c in cand),
                           r['latest_recovery_max_favorable_atr'])

    def test_future_fields_do_not_change_metrics(self):
        ss,legs,cand=self.selection(TWOZ)
        base=mod.analyze_one(ss,legs,cand)[0]
        for r in [ss,*legs,*cand]:
            r['future_post_price'] = 9999999
            r['visual_label'] = 'ALTERADO'
        self.assertEqual(mod.analyze_one(ss,legs,cand)[0],base)

    def test_future_confirmation_is_rejected(self):
        ss,legs,cand=self.selection(TWOZ)
        cand[-1]['anchor_low_known_closed_ts']=str(int(ss['pre_closed_ts'])+mod.TF_MS)
        with self.assertRaisesRegex(ValueError,'posterior a PRE'):
            mod.analyze_one(ss,legs,cand)

    def test_provisional_leg_cannot_be_confirmed(self):
        ss,legs,cand=self.selection(TWOZ)
        legs[-1]['status']='CONFIRMED_PRE'
        with self.assertRaisesRegex(ValueError,'provisionales'):
            mod.analyze_one(ss,legs,cand)

    def test_tampered_retention_is_rejected(self):
        ss,legs,cand=self.selection(TWOZ)
        cand[-1]['last_close_retention_fraction']='0.99'
        with self.assertRaisesRegex(ValueError,'Retención'):
            mod.analyze_one(ss,legs,cand)

    def test_dropped_latest_low_is_rejected(self):
        ss,legs,cand=self.selection(TWOZ)
        with self.assertRaisesRegex(ValueError,'Cantidad de mínimos'):
            mod.analyze_one(ss,legs,cand[:-1])

    def test_mismatched_cutoff_and_duplicate_audit_rejected(self):
        self.need_data()
        at=copy.deepcopy(self.audit)
        at[0]['pre_closed_ts']=str(int(at[0]['pre_closed_ts'])+mod.TF_MS)
        with self.assertRaisesRegex(ValueError,'Corte'):
            mod.build_data(at,self.summary,self.legs,self.candidates)
        at=copy.deepcopy(self.audit)
        at[0]=copy.deepcopy(at[1])
        with self.assertRaisesRegex(ValueError,'Duplicado'):
            mod.build_data(at,self.summary,self.legs,self.candidates)

    def test_visual_changes_do_not_change_metrics(self):
        self.need_data()
        old=mod.build_data(self.audit,self.summary,self.legs,self.candidates)[0]
        changed=copy.deepcopy(self.audit)
        for r in changed:r['review_visual']='TEST_VISUAL_FUTURE'
        fresh=mod.build_data(changed,self.summary,self.legs,self.candidates)[0]
        self.assertEqual(old,fresh)

    def test_bad_parent_manifests_rejected(self):
        bad_parent={'inputs_are_pre_only':False}
        bad_phase={'lookback_closed_30m_bars':96}
        with self.assertRaisesRegex(ValueError,'no certifica'):
            mod.verify_parent_manifests(bad_parent,bad_phase)

    def test_no_candidate_or_cycle_is_not_zero_retention(self):
        # Bare in-memory snapshot, with no confirmed pivot/leg, still a
        # descriptive row and missing metrics, never 'bad structure'.
        s={'attempt_id':'synthetic','symbol':'TEST','level_id':'LV',
           'pre_closed_ts':str(96*mod.TF_MS),'scale_atr':'1.0',
           'status':'COMPLETE_PRE','closed_bars':'96','confirmed_pivots':'0',
           'recovery_candidates':'0','terminal_leg_provisional':'0',
           'terminal_leg_direction':'UNRESOLVED','confirmed_up_legs':'0',
           'confirmed_down_legs':'0'}
        row,cycles=mod.analyze_one(s,[],[])
        self.assertIsNone(row['latest_recovery_retention_frac'])
        self.assertIsNone(row['preserved_fraction_last3'])
        self.assertIsNone(row['complete_givebacks_last3'])
        self.assertEqual(cycles,[])
        self.assertIn('SIN_MINIMO_CONFIRMADO',row['data_limitations'])


if __name__=='__main__':unittest.main()
