"""Pure tests for optional Discovery structural fingerprint evolution view."""
import unittest
import pandas as pd
from dashboard.views import hll_fingerprint_evolution_view as view
import hll_structural_fingerprint_evolution_v1 as ev


class EvolutionViewTests(unittest.TestCase):
    def test_all_six_families_expose_full_feature_catalog(self):
        self.assertEqual(len(view.FEATURES_BY_FAMILY),6)
        self.assertEqual(sum(len(x) for x in view.FEATURES_BY_FAMILY.values()),35)
        self.assertEqual(set(k for keys in view.FEATURES_BY_FAMILY.values() for k in keys),set(view.fingerprint.FEATURES))

    def test_tabular_values_not_score(self):
        members,candles=self.demo()
        res=ev.build_timeline(members,candles,'a2')
        df=view.evolution_feature_table(res,'movement')
        self.assertIn('Δ vs #1',df)
        self.assertNotIn('Calidad',df)
        self.assertGreater(len(df),0)

    def test_plot_excludes_future_in_causal_and_marks_historical(self):
        members,candles=self.demo()
        a=ev.build_timeline(members,candles,'a2',mode='CAUSAL')
        b=ev.build_timeline(members,candles,'a2',mode='HISTORICAL')
        k='movement_net_advance_48_atr_96'
        p=view.evolution_plot(a,k)
        q=view.evolution_plot(b,k)
        self.assertEqual(len(p.data),1)
        self.assertEqual(len(q.data),2)
        self.assertEqual(len(p.data[0].x),2)
        self.assertEqual(len(q.data[1].x),3)

    @staticmethod
    def demo():
        from test_hll_structural_fingerprint_evolution_v1 import make_data
        return make_data(4)

if __name__=='__main__':unittest.main()
