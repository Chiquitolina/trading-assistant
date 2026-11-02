"""Tests de similitud PRE; no score, notas visuales, POST ni anticipaciones."""
import copy
import csv
import json
import math
import tempfile
import unittest
from pathlib import Path

import hll_structural_similarity_v1 as sim
import hll_structural_fingerprint_v1 as fp
from test_hll_structural_fingerprint_evolution_v1 import make_data


class PureSimilarityTests(unittest.TestCase):
    def setUp(self):
        self.members,self.candles=make_data(5)
        for m in self.members: m["atr_pre"]=0.6
        self.a=sim.build_reference(self.members[0],self.candles)
        self.b=sim.build_reference(self.members[2],self.candles)
        self.a['episode_id']='epA'
        self.b['episode_id']='epB'
        self.b['symbol']='TEST2USDT'
        self.norm=sim.build_normalizers([self.a,self.b])

    def test_catalog_has_six_families_35_features(self):
        self.assertEqual(len(fp.FAMILIES),6)
        self.assertEqual(len(fp.FEATURES),35)
        self.assertEqual(set(self.norm),set(fp.FEATURES))

    def test_identity_has_zero_distance(self):
        r=sim.compare(self.a,self.a,self.norm,fp.FAMILIES)
        self.assertAlmostEqual(r['distance'],0)
        self.assertTrue(all(v==0 for v in r['family_distances'].values()))

    def test_selected_reference_not_replaced_with_first(self):
        self.assertEqual(sim.build_reference(self.members[2],self.candles)['attempt_id'],'a3')
        self.assertEqual(sim.build_reference(self.members[2],self.candles)['episode_ordinal_report_only'],3)

    def test_later_candles_cannot_change_reference(self):
        ref=sim.build_reference(self.members[2],self.candles)
        future=copy.deepcopy(self.candles)
        pre=self.members[2]['pre_closed_ts']
        for ts in future:
            if ts>=pre:future[ts].update(open=999,close=999,high=1000,low=998)
        self.assertEqual(ref,sim.build_reference(self.members[2],future))

    def test_future_labels_and_episode_count_do_not_affect_features(self):
        ref=sim.build_reference(self.members[2],self.candles)
        hacked=copy.deepcopy(self.members[2]);hacked.update(visual_grade='A',post_pnl=999,episode_attempt_count=100)
        self.assertEqual(ref,sim.build_reference(hacked,self.candles))

    def test_missing_288_cannot_be_ref(self):
        bad=copy.deepcopy(self.candles)
        bad.pop(self.members[2]['pre_closed_ts']-288*fp.anatomy.BAR_MS)
        with self.assertRaisesRegex(ValueError,'96 y 288'):
            sim.build_reference(self.members[2],bad)

    def test_equal_weight_per_family_not_per_feature(self):
        r=sim.compare(self.a,self.b,self.norm,['geometry','movement'])
        self.assertAlmostEqual(r['distance'],(r['family_distances']['geometry']+r['family_distances']['movement'])/2)
        q=sim.compare(self.a,self.b,self.norm,['geometry','movement'],mode='strict')
        self.assertAlmostEqual(q['distance'],max(q['family_distances'].values()))

    def test_missing_family_not_silently_zero(self):
        b=dict(self.b)
        for fid in fp.FEATURES:
            if fp.FEATURES[fid][0]=='compression':b[fid]=None
        self.assertIsNone(sim.compare(self.a,b,self.norm,['compression']))
        self.assertIsNotNone(sim.compare(self.a,b,self.norm,['geometry']))

    def test_exclude_same_episode_and_distinct_symbols(self):
        same=dict(self.a)
        other=dict(self.b)
        duplicate=dict(self.b,episode_id='epC',attempt_id='a4')
        r=sim.rank(self.a,[same,other,duplicate],self.norm,['geometry'],top=5)
        self.assertEqual(len(r['results']),1)
        self.assertEqual(r['excluded_same_episode'],1)
        self.assertEqual(r['results'][0]['candidate']['episode_id'],'epB')

    def test_strict_may_rank_differently_from_balanced(self):
        # Test mode definition, not desired ordering of market data.
        x=sim.compare(self.a,self.b,self.norm,fp.FAMILIES,mode='balanced')
        y=sim.compare(self.a,self.b,self.norm,fp.FAMILIES,mode='strict')
        self.assertGreaterEqual(y['distance'],x['distance'])

    def test_reject_empty_and_unknown_family(self):
        with self.assertRaises(ValueError):sim.compare(self.a,self.b,self.norm,[])
        with self.assertRaises(ValueError):sim.compare(self.a,self.b,self.norm,['bad'])
        with self.assertRaises(ValueError):sim.compare(self.a,self.b,self.norm,['geometry','geometry'])

    def test_no_quality_or_label_features(self):
        self.assertTrue(all('quality' not in k.lower() and 'post' not in k.lower() for k in fp.FEATURES))
        self.assertNotIn('quality_score',sim.compare(self.a,self.b,self.norm,['geometry']))


class ReferenceIndexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root=Path(__file__).resolve().parent
        cls.pilot=cls.root/'reports/horizontal_level_structural_similarity_v1/pilot_references_5_v1'

    def test_load_valid_pilot_checks_manifest_hashes(self):
        if not self.pilot.is_dir():self.skipTest('No se incluye piloto en paquete de instalación')
        rows,n,man=sim.load_index(self.pilot)
        self.assertEqual(len(rows),407)
        self.assertEqual(man['complete_288'],376)
        self.assertEqual(len(set(r['episode_id'] for r in rows)),len(rows))
        self.assertEqual(set(n),set(fp.FEATURES))

    def test_reject_tampered_index(self):
        if not self.pilot.is_dir():self.skipTest('No se incluye piloto en paquete de instalación')
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)
            for fn in ['manifest.json','fingerprints_first_pre.csv','normalizers.json']:
                (p/fn).write_bytes((self.pilot/fn).read_bytes())
            (p/'fingerprints_first_pre.csv').write_text('modificado',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'alterados'):
                sim.load_index(p)

    def test_first_attempts_of_full_canonical_map(self):
        if not sim.DEFAULT_MAP.exists() or not sim.DEFAULT_OVERLAP_MANIFEST.exists():
            self.skipTest('Mapa canónico no está incluido en paquete de instalación')
        first=sim.load_episode_first()
        self.assertEqual(len(first),22674)
        self.assertTrue(all(r['episode_ordinal']==1 for r in first))
        self.assertTrue(all(r['episode_first_attempt_id']==r['attempt_id'] for r in first))

if __name__=='__main__':unittest.main()
