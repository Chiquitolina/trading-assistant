"""Regression checks: reconciled with the actual VPS Discovery POST Visual V1."""
import ast
from pathlib import Path
import tempfile
import csv
import unittest
from dashboard.views.hll_resistance_universal_audit_view import prepare, LEVELS
from hll_resistance_universal_audit_v1 import TF

BASE=1_800_000_000_000

def bars(n=12):
    out={}
    for i in range(-2,n):
        ts=BASE+i*TF
        out[ts]=dict(open_ts=ts,close_ts=ts+TF,open=11.1,high=11.3,low=10.9,close=11.1)
    return out

class ReconciliationTests(unittest.TestCase):
    def test_latest_discovery_preserves_post_entry_overlay_and_similarity(self):
        src=Path('dashboard/views/hll_structural_discovery_view.py').read_text()
        self.assertIn('overlay_ui.add_post_markers(fig, mechanism_rows',src)
        self.assertIn('overlay_ui.mechanism_table(mechanism_rows)',src)
        self.assertIn('render_fingerprint_evolution(members, attempt, candles, scale)',src)
        self.assertIn('render_structural_similarity(r, candles, root)',src)
        self.assertIn('render_structural_similarity(member, candles, root)',src)
        self.assertIn("st.plotly_chart(fig,use_container_width=True,key='hll_discovery_chart')",src)
        self.assertIn('resistance_ui.add_behavior_markers(',src)
        self.assertIn('resistance_ui.render_audit(universal_result)',src)
        ast.parse(src)

    def test_without_formation_atr_still_provides_behavior_no_fabricated_detector(self):
        with tempfile.TemporaryDirectory() as tmp:
            selected=dict(attempt_id='attempt1',symbol='AAAUSDT',level_id='some',
                 pre_closed_ts=BASE,breakout_closed_ts=BASE+TF,
                 zone_low=10,zone_high=11,atr_pre=1)
            result=prepare(Path(tmp),selected,bars(),None,visible_post_bars=12)
            self.assertEqual(result['behavior']['summary']['observed_post_bars'],12)
            self.assertIsNone(result['formation_atr'])
            self.assertIsNone(result['detector'])
            self.assertIn('ATR de formación',result['detector_unavailable_reason'])

    def test_with_formation_atr_behavior_preserves_original_exact_zone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); path=root/LEVELS;path.parent.mkdir(parents=True)
            with path.open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=['symbol','level_id','zone_low','zone_high','atr_at_formation'])
                w.writeheader();w.writerow(dict(symbol='AAAUSDT',level_id='some',zone_low=10,zone_high=11,atr_at_formation=1))
            selected=dict(attempt_id='attempt1',symbol='AAAUSDT',level_id='some',
                 pre_closed_ts=BASE,breakout_closed_ts=BASE+TF,
                 zone_low=10,zone_high=11,atr_pre=1)
            result=prepare(root,selected,bars(),None,visible_post_bars=12)
            self.assertEqual(result['formation_atr'],1)
            self.assertIn('Overlay de mecanismos',result['detector_unavailable_reason'])
            self.assertEqual(result['behavior']['summary']['observed_post_bars'],12)

if __name__=='__main__':unittest.main()
