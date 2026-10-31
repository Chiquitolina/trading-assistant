"""Tests of the read-only HLL family comparison against frozen PRE snapshots."""
from __future__ import annotations
import copy
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch
import hll_pre_family_audit_v1 as audit


class AuditCoreTests(unittest.TestCase):
    def test_review_ids_are_unique_and_provenance_explicit(self):
        d=audit.review_reference_map()
        self.assertEqual(len(d),17)
        self.assertEqual(d['a419c2ae10918faceff87cc4']['review_provenance'],'USUARIO')
        self.assertEqual(d['a3015ca2f41fa79ef27de2c1']['review_provenance'],'ASISTENTE_PROVISIONAL')

    def test_no_intercontact_is_not_negative(self):
        self.assertEqual(audit.compare_sign(''),'SIN_DATOS')
        self.assertEqual(audit.compare_sign('nan'),'SIN_DATOS')
        self.assertEqual(audit.compare_sign('0'),'IGUAL')

    def test_discrepancy_flag_is_not_a_quality_label(self):
        r={'review_visual':'NO_CONVENCE','family_1p0':'RECONSTRUCCION_ENTRE_CONTACTOS',
           'intercontact_direction_48':'BAJA','net_return_pre48_pct':'1.2',
           'scales_agree':1,'low_progression_1atr':'CRECIENTE'}
        flags=audit.classify_audit_flags(r)
        self.assertIn('NO_CONVENCE_CON_FAMILIA_ASCENDENTE',flags)
        self.assertIn('PIVOTES_Y_CONTACTOS_DIVERGENTES',flags)
        self.assertNotIn('ESTRUCTURA_MALA',flags)

    def test_unknown_visual_is_not_treated_as_bad(self):
        r={'review_visual':'SIN_EVALUACION','family_1p0':'ASCENSO_ESCALONADO',
           'intercontact_direction_48':'SUBE','net_return_pre48_pct':'2',
           'scales_agree':1,'low_progression_1atr':'CRECIENTE'}
        flags=audit.classify_audit_flags(r)
        self.assertFalse(any(f.startswith('NO_CONVENCE') for f in flags))

    @classmethod
    def setUpClass(cls):
        try:
            cls.rows, cls.summary=audit.build_from_paths()
        except FileNotFoundError:
            cls.rows=None

    def test_real_50_cohort_and_no_missing_or_duplicate(self):
        if self.rows is None:self.skipTest('Necesita reportes CORE15 reales')
        self.assertEqual(len(self.rows),50)
        self.assertEqual(len({r['attempt_id'] for r in self.rows}),50)
        self.assertEqual(self.summary['scales_agree'],22)
        self.assertEqual(self.summary['intercontact_direction_48'].get('SIN_DATOS'),7)
        self.assertEqual(sum(r['review_visual']!='SIN_EVALUACION' for r in self.rows),17)

    def test_1atr_families_match_frozen_outputs(self):
        if self.rows is None:self.skipTest('Necesita reportes CORE15 reales')
        self.assertEqual(Counter(r['family_1p0'] for r in self.rows),
            {'RECONSTRUCCION_ENTRE_CONTACTOS':20,'OSCILACION_EN_RESISTENCIA':13,
             'REBOTE_TRAS_CORRECCION':12,'ASCENSO_ESCALONADO':4,'DETERIORO_DESDE_MINIMO':1})

    def test_mismatched_cutoff_refused(self):
        if self.rows is None:self.skipTest('Necesita reportes CORE15 reales')
        families=audit.read_csv(audit.FAMILIES)
        resistance=audit.read_csv(audit.RESISTANCE)
        ohlc=audit.read_csv(audit.OHLC)
        resistance[0]['pre_closed_ts']=str(int(resistance[0]['pre_closed_ts']) + 1800000)
        with self.assertRaisesRegex(ValueError,'corte PRE'):
            audit.make_matrix(families,resistance,ohlc)

    def test_missing_scale_refused(self):
        if self.rows is None:self.skipTest('Necesita reportes CORE15 reales')
        families=audit.read_csv(audit.FAMILIES)
        with self.assertRaisesRegex(ValueError,'150'):
            audit.make_matrix(families[:-1], audit.read_csv(audit.RESISTANCE),audit.read_csv(audit.OHLC))

    def test_visual_label_changes_do_not_change_frozen_families(self):
        if self.rows is None:self.skipTest('Necesita reportes CORE15 reales')
        families=audit.read_csv(audit.FAMILIES)
        v3=audit.read_csv(audit.RESISTANCE)
        v2=audit.read_csv(audit.OHLC)
        before=audit.make_matrix(families,v3,v2)
        with patch.object(audit,'REVIEW_REFERENCES', tuple((s,a,'DUDOSA',p,b)
                    for s,a,_,p,b in audit.REVIEW_REFERENCES)):
            after=audit.make_matrix(families,v3,v2)
        for x,y in zip(before,after):
            self.assertEqual(x['family_1p0'],y['family_1p0'])
            self.assertEqual(x['intercontact_direction_48'],y['intercontact_direction_48'])
        self.assertTrue(any(x['review_visual'] != y['review_visual'] for x,y in zip(before,after)))

    def test_family_manifest_tamper_fails_closed(self):
        if self.rows is None:self.skipTest('Necesita reportes CORE15 reales')
        m=json.loads(audit.FAMILY_MANIFEST.read_text())
        m['guardrails']['reads_post']=True
        with self.assertRaisesRegex(ValueError,'aislamiento PRE'):
            audit.verify_family_provenance(m)

    def test_output_manifest_and_csv_exist(self):
        if self.rows is None:self.skipTest('Necesita reportes CORE15 reales')
        output=audit.OUTPUT
        if not (output/'audit_matrix_pre.csv').is_file():self.skipTest('Ejecutar --run para probar export')
        r=audit.read_csv(output/'audit_matrix_pre.csv')
        self.assertEqual(len(r),50)
        self.assertEqual(set(r[0]),set(audit.MATRIX_COLUMNS))
        self.assertEqual(json.loads((output/'manifest.json').read_text())['reads_post'],False)

if __name__=='__main__':unittest.main()
