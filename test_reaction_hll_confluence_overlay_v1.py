"""Tests: frozen, strictly PRE HLL overlays under Runner Discovery."""
import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from dashboard.views.reaction_hll_confluence_overlay_v1 import (
    available_runs, audited_keys, load_case, read_pre_30m, decorate_chart,
)


def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def rows(path, labels, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as f:
        w=csv.DictWriter(f, fieldnames=labels)
        w.writeheader()
        w.writerows(values)


class HLLOverlay(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project=Path(self.temp.name)
        self.run=self.project/'reports/reaction_lab/universal_runner_hll_lifecycle_join_v1/runs/pilot'
        self.run.mkdir(parents=True)
        self.history=self.project/'reports/reaction_lab/hist'
        self.symbol='AAVEUSDT'
        self.folder=self.history/'symbols'/self.symbol
        self.folder.mkdir(parents=True)
        self.cutoff=18000000
        candles=[]
        for i in range(12):
            candles.append(dict(timestamp=i*1800000,open='100',high='101',low='99',close='100',volume='1',quoteVolume='100'))
        # Explicitly exclude last 30m candle if it closes at cutoff or after.
        rows(self.folder/'30m.csv',('timestamp','open','high','low','close','volume','quoteVolume'),candles)
        manifest=dict(symbol=self.symbol, start=0,end=30000000,
                      timeframes={'30m': {'sha256':digest(self.folder/'30m.csv'),
                                          'validation': {'status':'FULL'}}})
        (self.folder/'manifest.json').write_text(json.dumps(manifest))
        self.record=dict(touch_key='key-a',symbol=self.symbol,status='REACTION',
            retest_ts=str(self.cutoff),asof_cutoff_exclusive=str(self.cutoff),
            hll_persistent_group='BOTH_NEAR_OVERLAPPING_BANDS',hll_coverage='COMPLETE',
            persistent_group_pre='PERSISTENT_LEVEL_NEAR_PRE',persistent_zone_low='99.1',
            persistent_zone_high='99.3',hll_near_05atr_count='1')
        self.candidate=dict(touch_key='key-a',symbol=self.symbol,status='REACTION',
            retest_ts=str(self.cutoff),level_id='abc',origin='HIGH',center='100',
            zone_low='99.9',zone_high='100.1',formation_closed_ts='14400000',gap_atr='0.1')
        self.labels_path=self.run/'event_hll_labels.csv'
        self.cand_path=self.run/'hll_candidates_near_1atr.csv'
        rows(self.labels_path,self.record.keys(),[self.record])
        rows(self.cand_path,self.candidate.keys(),[self.candidate])
        self.audit=dict(version='universal-runner-hll-lifecycle-join-v1.0',
                        no_post_outcomes_read=True,read_only_to_project=True,
                        events=1,coverage={'COMPLETE':1},
                        offline_history_dir=str(self.history),
                        offline_manifest_sha256={self.symbol:digest(self.folder/'manifest.json')},
                        source_hashes={'event_hll_labels.csv':digest(self.labels_path),
                                       'hll_candidates_near_1atr.csv':digest(self.cand_path)})
        (self.run/'audit.json').write_text(json.dumps(self.audit))

    def test_verified_pilot_discovered(self):
        self.assertEqual(len(available_runs(self.project)),1)
        self.assertEqual(audited_keys(self.project,self.run),frozenset(('key-a',)))

    def test_causal_lines_and_pre_candles(self):
        audit,row,candidates=load_case(self.project,self.run,'key-a')
        self.assertEqual(len(candidates),1)
        pre=read_pre_30m(self.project,audit,self.symbol,self.cutoff,max_bars=40)
        self.assertEqual(len(pre),9)
        self.assertTrue(all(p[0]+1800000 < self.cutoff for p in pre))

    def test_missing_event_is_not_invented(self):
        self.assertIsNone(load_case(self.project,self.run,'nonexistent'))

    def test_integrity_fails_closed(self):
        self.labels_path.write_text(self.labels_path.read_text()+'\n')
        with self.assertRaises(ValueError):
            load_case(self.project,self.run,'key-a')

    def test_future_level_rejected(self):
        self.candidate['formation_closed_ts']=str(self.cutoff)
        rows(self.cand_path,self.candidate.keys(),[self.candidate])
        self.audit['source_hashes']['hll_candidates_near_1atr.csv']=digest(self.cand_path)
        (self.run/'audit.json').write_text(json.dumps(self.audit))
        with self.assertRaises(ValueError):
            load_case(self.project,self.run,'key-a')

    def test_fig_contains_pre_and_bands(self):
        import plotly.graph_objects as go
        audit,row,candidates=load_case(self.project,self.run,'key-a')
        pre=read_pre_30m(self.project,audit,self.symbol,self.cutoff,max_bars=40)
        fig=decorate_chart(go.Figure(),pre_bars=pre,row=row,candidates=candidates)
        self.assertEqual(len(fig.data),1)
        self.assertGreaterEqual(len(fig.layout.shapes),4)


if __name__=='__main__':
    unittest.main()
