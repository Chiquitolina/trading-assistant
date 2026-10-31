"""Pruebas del nuevo inspector dentro del V2.8 existente."""
import unittest
import csv
import json
import tempfile
from pathlib import Path
from dashboard.views.hll_structural_discovery_view import drawable_bars, load_discovery
from hll_structural_discovery_v1 import sha

BASE=1_787_184_000_000
TF=1_800_000

class DiscoveryViewTests(unittest.TestCase):
    def setUp(self):
        self.data={BASE+i*TF:{'open_ts':BASE+i*TF,'close_ts':BASE+(i+1)*TF}
                   for i in range(150)}
    def test_post_toggle_does_not_change_pre(self):
        cutoff=BASE+120*TF
        a,p=drawable_bars(self.data,cutoff,96,0)
        b,q=drawable_bars(self.data,cutoff,96,24)
        self.assertEqual(a,b)
        self.assertEqual(len(a),96)
        self.assertEqual(len(q),24)
    def test_missing_post_is_explicit(self):
        before,after=drawable_bars(self.data,BASE+140*TF,96,96)
        self.assertEqual(len(before),96)
        self.assertEqual(len(after),10)
    def test_gap_stops_not_fills(self):
        del self.data[BASE+118*TF]
        before,_=drawable_bars(self.data,BASE+120*TF,96,0)
        self.assertEqual(len(before),1)
    def test_tampered_attempts_csv_fails_closed(self):
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t); p=folder/'attempts_pre.csv'
            fields=['attempt_id','symbol','level_id','pre_closed_ts','zone_low','zone_high',
                    'atr_pre','status','family_0p5','family_1p0','family_1p5']
            with p.open('w',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
                writer.writerow(dict.fromkeys(fields,'x') | {'attempt_id':'id1','symbol':'TESTUSDT'})
            (folder/'manifest.json').write_text(json.dumps({
                'version':'HLL_STRUCTURAL_DISCOVERY_V1','reads_post':False,'attempts':1,'symbols':1,
                'output_sha256':{'attempts_pre.csv':sha(p)}}))
            self.assertEqual(len(load_discovery(folder)[0]),1)
            with p.open('a') as f:f.write('altered')
            with self.assertRaisesRegex(ValueError,'Hash de CSV'):
                load_discovery(folder)

    def test_main_view_wires_without_blind_mode_leak(self):
        p=Path(__file__).with_name('hll_v28_structure_dashboard_view.py')
        text=p.read_text(encoding='utf8')
        self.assertIn("if st.session_state.get('hll_phase_mode') == 'Revisión visual ciega (PRE)':\n            return",text)
        self.assertEqual(text.count('render_hll_structural_discovery(root)'),1)
        self.assertIn('hll_discovery_enabled',text)

if __name__=='__main__':unittest.main()
