"""Regression tests for exact causal HLL PRE event marker export and overlay."""
import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import sys

import plotly.graph_objects as go

from universal_runner_hll_pre_markers_v1 import (
    candidate_limits, extract_markers, run, VERSION,
)
from dashboard.views.reaction_hll_confluence_overlay_v1 import (
    load_pre_markers, load_case, decorate_chart, candidate_options, selected_candidates,
)


TF = 1_800_000


def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def csv_write(path, values):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=tuple(values[0]))
        w.writeheader()
        w.writerows(values)


class Markers(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project=Path(self.temp.name)
        self.join=self.project/'reports/reaction_lab/universal_runner_hll_lifecycle_join_v1/runs/pilot'
        self.join.mkdir(parents=True)
        self.hist=self.project/'reports/reaction_lab/verified_history'
        self.sym='BTCUSDT'
        self.folder=self.hist/'symbols'/self.sym
        self.folder.mkdir(parents=True)
        self.cutoff=10*TF
        self.formation=8*TF
        self.candidate=dict(touch_key='k',symbol=self.sym,status='REACTION',
            retest_ts=str(self.cutoff),level_id='L1',origin='HIGH',center='100',
            zone_low='99.5',zone_high='100.5',formation_closed_ts=str(self.formation),
            gap_atr='0.25',prior_tests='1',prior_defenses='0',prior_losses='0')
        self.label=dict(touch_key='k',symbol=self.sym,status='REACTION',
            retest_ts=str(self.cutoff),asof_cutoff_exclusive=str(self.cutoff),
            hll_coverage='COMPLETE',hll_persistent_group='HLL_ONLY',
            persistent_group_pre='NO_PERSISTENT_LEVEL_NEAR_PRE',
            persistent_zone_low='',persistent_zone_high='')
        csv_write(self.join/'event_hll_labels.csv',[self.label])
        csv_write(self.join/'hll_candidates_near_1atr.csv',[self.candidate])
        csv_write(self.folder/'30m.csv',[dict(timestamp=i*TF,open='100',high='102',low='99',
              close='100',volume='1',quoteVolume='1') for i in range(15)])
        manifest=dict(symbol=self.sym,start=0,end=15*TF,timeframes={
            '15m':{'validation':{'status':'FULL'}},
            '30m':{'validation':{'status':'FULL'}}})
        (self.folder/'manifest.json').write_text(json.dumps(manifest))
        self.audit=dict(version='universal-runner-hll-lifecycle-join-v1.0',
            no_post_outcomes_read=True,read_only_to_project=True,
            events=1,symbols=1,coverage={'COMPLETE':1},
            offline_history_dir=str(self.hist),
            offline_manifest_sha256={self.sym:digest(self.folder/'manifest.json')},
            hll_scanner='horizontal-level-lifecycle-v2.1-geometry',
            source_hashes={'event_hll_labels.csv':digest(self.join/'event_hll_labels.csv'),
                           'hll_candidates_near_1atr.csv':digest(self.join/'hll_candidates_near_1atr.csv')})
        (self.join/'audit.json').write_text(json.dumps(self.audit))
        self.formation_row=dict(level_id='L1',symbol=self.sym,origin='HIGH',
            formation_closed_ts=self.formation,center=100.,zone_low=99.5,zone_high=100.5)
        self.test_event=self.event('TEST_FROM_ABOVE',9*TF, 't1')
        self.future_event=self.event('DEFENSE_CONFIRMED',10*TF, 't2')

    def event(self,kind,when,event_id):
        return dict(event_id=event_id,level_id='L1',symbol=self.sym,origin='HIGH',
            event_type=kind,observed_closed_ts=when,bar_open_ts=when-TF,
            high=102.,low=99.,close=100.,test_id='test-1',test_from='ABOVE')

    def test_export_skips_observations_at_touch_and_later(self):
        limits=candidate_limits([self.label],[self.candidate])
        rows=extract_markers(self.sym,limits[self.sym],[self.formation_row],
                             [self.test_event,self.future_event])
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['event_type'],'TEST_FROM_ABOVE')

    def test_formation_mismatch_blocks_export(self):
        limits=candidate_limits([self.label],[self.candidate])
        changed=dict(self.formation_row,zone_high=102.)
        with self.assertRaisesRegex(ValueError,'geometry'):
            extract_markers(self.sym,limits[self.sym],[changed],[self.test_event])

    def test_future_candidate_rejected(self):
        candidate=dict(self.candidate,formation_closed_ts=str(self.cutoff))
        with self.assertRaisesRegex(ValueError,'future'):
            candidate_limits([self.label],[candidate])

    def export(self):
        fake_hll=SimpleNamespace(
            VERSION='horizontal-level-lifecycle-v2.1-geometry',
            Config=lambda: None,
            scan_symbol=lambda *args,**kwargs:(
                [self.formation_row], [self.test_event,self.future_event],[],
                {'status':'COMPLETE','gaps30':0}),
        )
        with patch.dict(sys.modules,{'horizontal_level_lifecycle_v21':fake_hll}), \
             patch('universal_runner_horizontal_history_v1.verify_symbol',return_value={
                 'timeframes':{'15m':{'validation':{'status':'FULL'}},
                               '30m':{'validation':{'status':'FULL'}}}}):
            return run(self.project,self.join,min_free_gb=0)

    def test_export_and_overlay_consistent(self):
        self.export()
        audit,row,candidates=load_case(self.project,self.join,'k')
        markers=load_pre_markers(self.project,self.join,row,candidates)
        self.assertEqual(len(markers),1)
        self.assertEqual(markers[0]['marker_price'],99.)
        self.assertLess(int(markers[0]['observed_closed_ts']),self.cutoff)
        self.assertEqual(selected_candidates(candidates,'NEAREST'),candidates[:1])
        self.assertEqual(selected_candidates(candidates,'ALL'),candidates)
        self.assertEqual(selected_candidates(candidates,'L1'),candidates)
        self.assertEqual(len(candidate_options(candidates)),3)
        pre=[((self.cutoff//TF-2)*TF,100.,102.,99.,100.)]
        fig=decorate_chart(go.Figure(),pre_bars=pre,row=row,candidates=candidates,
                           selected_hll='NEAREST',pre_markers=markers)
        self.assertEqual(len(fig.data),2)
        self.assertEqual(fig.data[-1].name,'Contacto confirmado PRE')
        self.assertEqual(len(fig.data[-1].x),1)
        self.assertEqual(len(fig.layout.shapes),3)
        with self.assertRaisesRegex(FileExistsError,'immutable'):
            self.export()

    def test_tamper_marker_file_fails_closed(self):
        self.export()
        output=self.project/'reports/reaction_lab/universal_runner_hll_pre_markers_v1/runs/pilot'
        filename=output/'symbols/BTCUSDT.csv'
        filename.write_text(filename.read_text()+'\n')
        _,row,cands=load_case(self.project,self.join,'k')
        with self.assertRaisesRegex(ValueError,'modificado'):
            load_pre_markers(self.project,self.join,row,cands)

    def test_mismatched_prior_counts_fails_closed(self):
        self.candidate['prior_defenses']='1'
        csv_write(self.join/'hll_candidates_near_1atr.csv',[self.candidate])
        self.audit['source_hashes']['hll_candidates_near_1atr.csv']=digest(
            self.join/'hll_candidates_near_1atr.csv')
        (self.join/'audit.json').write_text(json.dumps(self.audit))
        self.export()
        _,row,cands=load_case(self.project,self.join,'k')
        with self.assertRaisesRegex(ValueError,'recuento congelado'):
            load_pre_markers(self.project,self.join,row,cands)

    def test_future_markers_never_plotted_even_if_provided(self):
        row=dict(self.label)
        fig=decorate_chart(go.Figure(),pre_bars=[(8*TF,100.,102.,99.,100.)],row=row,
            candidates=[self.candidate],pre_markers=[{
              **self.future_event,'marker_price':100.}])
        self.assertEqual(len(fig.data),1) # 30m candles only, future point excluded

    def test_one_line_reduces_overplot(self):
        row=dict(self.label)
        second=dict(self.candidate,level_id='L2',origin='LOW',
                    center='102',zone_low='101.5',zone_high='102.5',gap_atr='0.5')
        single=decorate_chart(go.Figure(),pre_bars=[],row=row,
                              candidates=[self.candidate,second],selected_hll='NEAREST')
        all_lines=decorate_chart(go.Figure(),pre_bars=[],row=row,
                              candidates=[self.candidate,second],selected_hll='ALL')
        self.assertEqual(len(single.layout.shapes),2)
        self.assertEqual(len(all_lines.layout.shapes),4)


if __name__=='__main__':
    unittest.main()
