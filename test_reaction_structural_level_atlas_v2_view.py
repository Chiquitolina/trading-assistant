import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parent))
from dashboard.views.reaction_structural_level_atlas_v2_view import _post_figure, _stats
import pandas as pd

class AtlasV2ViewTests(unittest.TestCase):
    def test_post_chart_has_tp_sl_and_entry_from_observed_open(self):
        item=SimpleNamespace(side='LONG',entry_ts=60000,entry_price=100.,outcome='TP_FIRST',first_touch_minute=2)
        bars=[(i*60000,100.,101.,99.,100.) for i in range(1,361)]
        fig=_post_figure(item,bars,15,1.,.5)
        self.assertIsNotNone(fig)
        self.assertEqual(len(fig.data[0].x),15)
        self.assertTrue(len(fig.layout.shapes)>=4)

    def test_group_summary_ignores_no_hit_in_resolved_denominator(self):
        f=pd.DataFrame([dict(label='a',outcome=x,date_utc='2026-10-12',symbol='X')
                        for x in ('TP_FIRST','SL_FIRST','NO_HIT','AMBIGUOUS')])
        r=_stats(f,'label',min_n=2)
        self.assertEqual(int(r.iloc[0]['N resueltos']),2)
        self.assertEqual(float(r.iloc[0]['TP entre resueltos (%)']),50.)

if __name__=='__main__':unittest.main()
