"""Tests offline del contrato gráfico/UI de similitud PRE."""
import unittest
import hll_structural_fingerprint_labels_v1 as labels
from dashboard.views import hll_structural_similarity_view as view
from dashboard.views import hll_fingerprint_evolution_view as evol_view
import hll_structural_similarity_v1 as sim
import hll_structural_fingerprint_v1 as fp
from test_hll_structural_fingerprint_evolution_v1 import make_data

class SimilarityViewTests(unittest.TestCase):
    def setUp(self):
        members,candles=make_data(4)
        for m in members: m["atr_pre"]=0.6
        self.ref=sim.build_reference(members[0],candles)
        self.cand=sim.build_reference(members[2],candles)
        self.cand['episode_id']='ep2'
        self.norm=sim.build_normalizers([self.ref,self.cand])

    def test_human_name_above_technical_code_all_35(self):
        self.assertEqual(len(labels.LABELS),35)
        self.assertTrue(labels.check_catalog())
        for key in fp.FEATURES:
            display=labels.dual_label(key)
            self.assertIn(key,display)
            self.assertIn(labels.label(key),display)
            self.assertLess(display.find(labels.label(key)),display.find(key))

    def test_evolution_table_keeps_both_names(self):
        import hll_structural_fingerprint_evolution_v1 as ev
        members,candles=make_data(3)
        timeline=ev.build_timeline(members,candles,'a2')
        table=evol_view.evolution_feature_table(timeline,'geometry')
        self.assertIn('Variable',table)
        self.assertIn('ID técnico',table)
        self.assertIn('Proporción de mínimos ascendentes',table['Variable'].iloc[0])
        self.assertEqual(table['ID técnico'].iloc[0],'geo_low_up_share_96')

    def test_source_selected_ordinal_not_forced_first(self):
        members,candles=make_data(5)
        x=view.source_row_for_reference(members[3])
        self.assertEqual(x['episode_ordinal'],4)
        self.assertEqual(x['attempt_id'],'a4')

    def test_candidate_metadata_valid_for_original_chart(self):
        meta=view.candidate_meta(self.cand)
        self.assertEqual(meta['attempt_id'],self.cand['attempt_id'])
        self.assertEqual(meta['status'],'COMPLETE_PRE')
        self.assertEqual(meta['zone_low'],float(self.cand['zone_low_report_only']))

    def test_ranking_has_full_ids_and_no_quality_score(self):
        result=sim.rank(self.ref,[self.cand],self.norm,['geometry','movement'],top=5)
        tb=view.summary_table(result)
        self.assertIn('Attempt ID',tb)
        self.assertIn('Episode ID',tb)
        self.assertIn('Distancia',tb)
        self.assertNotIn('Calidad',tb)
        self.assertEqual(tb['Attempt ID'].iloc[0],self.cand['attempt_id'])
        explanations=view.feature_explanations(result['results'][0])
        self.assertIn('ID técnico',explanations)
        self.assertIn('Diferencia normalizada',explanations)

if __name__=='__main__':unittest.main()
