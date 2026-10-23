import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd
import audit_pivot_memory_first_touch as mod

class OutcomeReportTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.root=Path(self.t.name)
        self.pre=self.root/mod.PIVOT_DIR;self.pre.mkdir(parents=True)
        self.v1=self.root/mod.V1_DIR;self.v1.mkdir(parents=True)
        (self.v1/'event_labels.csv').write_text('source frozen v1')
        (self.v1/'candidate_levels.csv').write_text('zones frozen')
        self.ft=self.root/'ft.csv'
        self.rows=[];self.ftrows=[]
        all_groups=list(mod.ELIGIBLE)+['UNKNOWN_HISTORY','UNKNOWN_PIVOT']
        for i in range(6423):
            side='LONG' if i%2 else 'SHORT'
            g=all_groups[(i//2)%len(all_groups)]
            key=f'S{i}|{side}|{1230000000000+i}'
            self.rows.append(dict(event_key=key,symbol=f'S{i%17}',side=side,
                                  date_utc='2026-10-12',observation_type='LIVE_OBSERVED',
                                  memory_group_pre=g))
            if i<6420:
                self.ftrows.append(dict(event_key=key,symbol=f'S{i%17}',side=side,
                           observation_type='LIVE_OBSERVED',entry_mode='OBSERVED_OPEN',tp_pct=1.,sl_pct=1.,
                           result_180m='TP_FIRST' if i%3==0 else 'SL_FIRST',
                           result_360m='TP_FIRST' if i%3==0 else 'NO_HIT'))
        pd.DataFrame(self.rows).to_csv(self.pre/'pivot_memory_labels_pre.csv',index=False)
        pd.DataFrame(self.ftrows).to_csv(self.ft,index=False)
        aud={'success':True,'version':'pivot-historical-memory-pre-v1.0','events':6423,
             'no_outcomes_used':True,'labels_sha':mod.sha(self.pre/'pivot_memory_labels_pre.csv'),
             'groups':{k:int(v) for k,v in pd.DataFrame(self.rows).memory_group_pre.value_counts().items()},
             'source_hashes':{'v1_events_sha':mod.sha(self.v1/'event_labels.csv'),
                              'v1_zones_sha':mod.sha(self.v1/'candidate_levels.csv'),
                              'cohort_events':6423,'selected_events':6423}}
        (self.pre/'audit.json').write_text(json.dumps(aud))

    def tearDown(self):self.t.cleanup()
    def test_join_and_primary_side_specific(self):
        report=mod.analyze(self.root,self.ft)
        self.assertEqual(report['counts']['pre_total'],6423)
        self.assertEqual(report['counts']['scenario_matched'],6420)
        self.assertEqual(report['counts']['scenario_missing'],3)
        self.assertEqual(report['primary_comparison']['180']['LONG']['hypothesis_group'],'PRIOR_SUPPORT')
        self.assertEqual(report['primary_comparison']['360']['SHORT']['hypothesis_group'],'PRIOR_RESISTANCE')
        self.assertGreater(report['primary_comparison']['180']['LONG']['hypothesis']['events'],0)
        self.assertGreater(report['primary_comparison']['180']['SHORT']['hypothesis']['events'],0)
        self.assertEqual(sum(x['events'] for x in report['by_side']['180']),
                         report['counts']['eligible_matched'])
        self.assertNotIn('UNKNOWN_HISTORY',set(x['group'] for x in report['by_side']['180']))
        mod.write_json(self.pre/mod.OUTPUT_DIR/'report.json',report)
        self.assertTrue(json.loads((self.pre/mod.OUTPUT_DIR/'report.json').read_text()))
    def test_modified_pre_labels_fail_closed(self):
        with (self.pre/'pivot_memory_labels_pre.csv').open('a') as f:f.write('tampered')
        with self.assertRaisesRegex(ValueError,'SHA256'):
            mod.analyze(self.root,self.ft)
    def test_modified_frozen_v1_fails_closed(self):
        (self.v1/'candidate_levels.csv').write_text('different')
        with self.assertRaisesRegex(ValueError,'Frozen V1'):
            mod.analyze(self.root,self.ft)
    def test_duplicate_first_touch_scenarios_fail_closed(self):
        d=pd.read_csv(self.ft)
        d=pd.concat([d,d.iloc[[0]]],ignore_index=True)
        d.to_csv(self.ft,index=False)
        with self.assertRaisesRegex(ValueError,'Duplicate'):
            mod.analyze(self.root,self.ft)
    def test_symbol_side_mismatch_fails_closed(self):
        d=pd.read_csv(self.ft)
        d.loc[0,'side']='WRONG';d.to_csv(self.ft,index=False)
        with self.assertRaisesRegex(ValueError,'mismatch'):
            mod.analyze(self.root,self.ft)
    def test_other_scenarios_excluded(self):
        d=pd.read_csv(self.ft)
        d=pd.concat([d,d.iloc[[0]].assign(tp_pct=2)],ignore_index=True)
        d.to_csv(self.ft,index=False)
        r=mod.analyze(self.root,self.ft)
        self.assertEqual(r['counts']['scenario_matched'],6420)
    def test_unknown_status_refused(self):
        d=pd.read_csv(self.ft)
        d.loc[0,'result_360m']='WIN';d.to_csv(self.ft,index=False)
        with self.assertRaisesRegex(ValueError,'Unexpected First Touch'):
            mod.analyze(self.root,self.ft)
    def test_missing_scenarios_never_count_as_nohit(self):
        r=mod.analyze(self.root,self.ft)
        for h in ('180','360'):
            self.assertEqual(sum(v['events'] for v in r['by_side'][h]),r['counts']['eligible_matched'])
    def test_wilson_and_low_n(self):
        a=pd.DataFrame({'result_180m':['TP_FIRST','SL_FIRST','NO_HIT']})
        s=mod.summary(a,180)
        self.assertEqual(s['tp_rate_resolved'],50)
        self.assertIsNone(s['wilson95'])
        self.assertEqual(s['tp_rate_all'],round(100/3,2))

if __name__=='__main__':unittest.main()
