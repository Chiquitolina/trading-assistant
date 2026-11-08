"""Frozen audit inspector contract; no Streamlit server needed."""
import json
import tempfile
import unittest
from pathlib import Path
import pandas as pd
from hll_structural_behavior_inspector_v22 import (verified_load,episode_window,episode_marks,
                                                      available_runs,digest)

class InspectorV22(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.v1=self.root/'v1';self.v2=self.root/'v2'
        self.v1.mkdir();self.v2.mkdir()
        (self.v1/'manifest.json').write_text('{"v1":true}')
        (self.v2/'manifest.json').write_text('{"v2":true}')
        self.run=self.root/'reports/horizontal_level_structural_behavior_v22/runs/demo'
        self.run.mkdir(parents=True)
        pd.DataFrame([dict(episode_id='e',zone_id='z',first_test_closed_ts=1800000,
                   last_test_closed_ts=1800000,state='DEFENDED',side='ABOVE',resolved_at=3600000)]).to_csv(self.run/'episodes.csv',index=False)
        pd.DataFrame([dict(episode_id='e',zone_id='z',level_id='l',test_id='t',event_id='i',
                  event_type='TEST_FROM_ABOVE',observed_closed_ts=1800000,
                  transition_role='TEST')]).to_csv(self.run/'episode_transitions.csv',index=False)
        pd.DataFrame([dict(zone_id='z',episodes=1)]).to_csv(self.run/'behavior_summary.csv',index=False)
        m=dict(version='hll-structural-behavior-v2.2a-offline',cutoff_ms=7200000,
               v1_dir=str(self.v1.resolve()),v2_dir=str(self.v2.resolve()),
               v1_manifest_sha256=digest(self.v1/'manifest.json'),
               v2_manifest_sha256=digest(self.v2/'manifest.json'),episodes=1,transitions=1,
               files_sha256={n:digest(self.run/n) for n in ('episodes.csv','episode_transitions.csv','behavior_summary.csv')})
        (self.run/'manifest.json').write_text(json.dumps(m))
    def tearDown(self):self.tmp.cleanup()
    def test_verified_and_focus(self):
        _,ep,tr=verified_load(self.run,self.v1,self.v2,7200000)
        self.assertEqual(len(episode_marks(tr,'e',7200000)),1)
        self.assertEqual(episode_window(ep.iloc[0],7200000,1,1),(0,7200000))
        self.assertEqual(available_runs(self.root,self.v1,self.v2,7200000),[self.run])
    def test_wrong_cutoff_fails_closed(self):
        with self.assertRaisesRegex(ValueError,'corte'):
            verified_load(self.run,self.v1,self.v2,5400000)
        self.assertEqual(available_runs(self.root,self.v1,self.v2,5400000),[])
    def test_tampered_data_fails_closed(self):
        with (self.run/'episodes.csv').open('a') as f:f.write('tampered\n')
        with self.assertRaisesRegex(ValueError,'Hash'):
            verified_load(self.run,self.v1,self.v2,7200000)
    def test_wrong_sources_fail(self):
        other=self.root/'other';other.mkdir()
        (other/'manifest.json').write_text('{"v1":true}')
        with self.assertRaisesRegex(ValueError,'Fuentes'):
            verified_load(self.run,other,self.v2,7200000)
