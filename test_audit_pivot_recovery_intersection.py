import json
import tempfile
import unittest
from pathlib import Path
import pandas as pd
import numpy as np

import audit_pivot_recovery_intersection as m


class IntersectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name)
        v1=cls.root/m.BASE;v3=cls.root/m.V3;v4=cls.root/m.V4;pivot=cls.root/m.PIVOT
        for d in (v1,v3,v4,pivot):d.mkdir(parents=True,exist_ok=True)
        events=[];lifecycle=[];pivots=[];ft=[];rec=[]
        start=1791820800000
        for i in range(6423):
            side='LONG' if i<3200 else 'SHORT';key=f'S{i%97}|{side}|{start+i*60000}'
            symbol=f'S{i%97}';date=pd.Timestamp(start+i*60000,unit='ms',tz='UTC').strftime('%Y-%m-%d')
            v3grp='NO_PERSISTENT_LEVEL_NEAR_PRE';visits=np.nan
            pg='NO_PRIOR_LEVEL'
            if i in (0,1,8):v3grp='RECOVERED_ORIGINAL_PRE';visits=3;pg='PRIOR_MIXED_SR'
            if i in (2,7):v3grp='RECOVERED_ORIGINAL_PRE';visits=3
            if i==3:v3grp='DEFENDED_CONTINUOUS_PRE';visits=3;pg='PRIOR_MIXED_SR'
            if i==4:v3grp='DEFENDED_CONTINUOUS_PRE';visits=3
            if i==5:v3grp='UNKNOWN_HISTORY';pg='PRIOR_MIXED_SR'
            if i==6:v3grp='RECOVERED_ORIGINAL_PRE';visits=3;pg='UNKNOWN_HISTORY'
            if i==9:v3grp='RECOVERED_ORIGINAL_PRE';visits=2;pg='PRIOR_MIXED_SR'
            if i==10:pg='PRIOR_MIXED_SR'
            if i==11:v3grp='UNKNOWN_PATH';pg='NO_PRIOR_LEVEL'
            if i==12:pg='UNKNOWN_PIVOT'
            events.append(dict(event_key=key,symbol=symbol,side=side,retest_ts=start+i*60000,date_utc=date,observation_type='LIVE_OBSERVED'))
            lifecycle.append(dict(event_key=key,symbol=symbol,side=side,retest_ts=start+i*60000,date_utc=date,v3_group_pre=v3grp,support_visits=visits))
            pivots.append(dict(event_key=key,symbol=symbol,side=side,retest_ts=start+i*60000,date_utc=date,observation_type='LIVE_OBSERVED',memory_group_pre=pg))
            if v3grp=='RECOVERED_ORIGINAL_PRE' and side=='LONG':rec.append(dict(event_key=key,hours_since_recovery_pre=5.0))
            if i<18 and i!=7:
                tp=(i in (0,3,6,8,9,10))
                ft.append(dict(event_key=key,symbol=symbol,side=side,observation_type='LIVE_OBSERVED',entry_mode='OBSERVED_OPEN',tp_pct=1.,sl_pct=1.,result_180m='TP_FIRST' if tp else 'SL_FIRST',result_360m='TP_FIRST' if tp else 'NO_HIT'))
        cls.events=pd.DataFrame(events);cls.v3=pd.DataFrame(lifecycle);cls.pivots=pd.DataFrame(pivots);cls.ftdf=pd.DataFrame(ft)
        cls.events.to_csv(v1/'event_labels.csv',index=False)
        (v1/'candidate_levels.csv').write_text('frozen candidate zones\n',encoding='utf8')
        h1=m.digest(v1/'event_labels.csv');hz=m.digest(v1/'candidate_levels.csv')
        m.json_write(v1/'audit.json',dict(success=True,version='persistent-horizontal-pre-v1.0',events=6423,audit_rule='strict_response_close_before_retest_v1.1',outputs_sha256={'event_labels.csv':h1,'candidate_levels.csv':hz}))
        cls.v3.to_csv(v3/'lifecycle_labels_pre.csv',index=False)
        h3=m.digest(v3/'lifecycle_labels_pre.csv')
        m.json_write(v3/'audit.json',dict(success=True,version='persistent-level-lifecycle-pre-v3.0',events=6423,input_event_sha=h1,input_zone_sha=hz,output_labels_sha=h3,no_outcomes_used=True,groups={k:int(v) for k,v in cls.v3.v3_group_pre.value_counts().items()}))
        cls.pivots.to_csv(pivot/'pivot_memory_labels_pre.csv',index=False)
        m.json_write(pivot/'audit.json',dict(success=True,version='pivot-historical-memory-pre-v1.0',events=6423,labels_sha=m.digest(pivot/'pivot_memory_labels_pre.csv'),no_outcomes_used=True,source_hashes={'v1_events_sha':h1,'v1_zones_sha':hz,'cohort_events':6423,'selected_events':6423},groups={k:int(v) for k,v in cls.pivots.memory_group_pre.value_counts().items()}))
        pd.DataFrame(rec).to_csv(v4/'recovery_recency_pre.csv',index=False)
        m.json_write(v4/'audit.json',dict(success=True,version='persistent-recovery-robustness-v4.0',events_v3=6423,n_long_recovered=len(rec),recency_complete=len(rec),no_outcomes_used=True,source_hashes={'v1_event':h1,'v1_zone':hz,'v3_labels':h3},recovery_recency_sha=m.digest(v4/'recovery_recency_pre.csv')))
        cls.ftpath=cls.root/'first_touch_scenarios.csv';cls.ftdf.to_csv(cls.ftpath,index=False)
        m.run(cls.root)

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def test_four_exclusive_groups(self):
        src,_=m.load_sources(self.root);p=m.classify_pre(src)
        found=p.set_index('event_key').loc[self.events.event_key[:13],'intersection_group_pre'].tolist()
        self.assertEqual(found[:5],['BOTH','BOTH','RECOVERED_ONLY','MIXED_ONLY','NEITHER'])
        self.assertEqual(found[5:7],['EXCLUDED_UNKNOWN_PRE','EXCLUDED_UNKNOWN_PRE'])
        self.assertEqual(found[7:13],['RECOVERED_ONLY','BOTH','MIXED_ONLY','MIXED_ONLY','EXCLUDED_UNKNOWN_PRE','EXCLUDED_UNKNOWN_PRE'])
        self.assertEqual(len(p),6423)
        self.assertEqual(set(p.intersection_group_pre.unique())-set(m.GROUPS),{'EXCLUDED_UNKNOWN_PRE','EXCLUDED_OTHER_SIDE'})

    def test_pre_has_no_outcome_columns(self):
        x,_=m.load_sources(self.root);p=m.classify_pre(x)
        self.assertFalse(any(c.startswith('result_') for c in p))
        self.assertTrue(p.loc[p.intersection_group_pre=='BOTH','recovered_3plus_pre'].all())
        self.assertTrue(p.loc[p.intersection_group_pre=='BOTH','pivot_mixed_pre'].all())

    def test_run_audit_and_integrity(self):
        saved=m.run(self.root)
        self.assertTrue(saved['success']);self.assertTrue(saved['no_outcomes_used'])
        audited,frame=m.audit(self.root)
        self.assertEqual(audited['labels_sha'],saved['labels_sha'])
        self.assertEqual(len(frame),6423)

    def test_real_ft_join_no_duplicate_or_missing_as_loss(self):
        p=m.audit(self.root)[1];r,known=m.analyze(p,self.ftpath,100)
        self.assertEqual(r['total_pre'],6423)
        self.assertEqual(r['long_pre'],3200)
        self.assertEqual(r['groups_matched']['BOTH'],3)
        self.assertEqual(r['missing_scenario_by_group']['RECOVERED_ONLY'],1)
        self.assertEqual(r['groups_matched']['RECOVERED_ONLY'],1)
        self.assertEqual(r['groups_matched']['MIXED_ONLY'],3)
        self.assertEqual(r['horizons']['180']['groups']['BOTH']['TP'],2)
        self.assertEqual(r['horizons']['180']['groups']['BOTH']['SL'],1)
        self.assertNotIn('UNKNOWN_HISTORY',set(known.intersection_group_pre.unique()))
        self.assertEqual(r['horizons']['180']['groups']['RECOVERED_ONLY']['NO_HIT'],0)

    def test_report_produces_files_and_strict_json(self):
        r=m.report(self.root,self.ftpath,100)
        out=self.root/m.OUT
        self.assertTrue((out/'intersection_report.json').exists())
        self.assertTrue((out/'intersection_first_touch_POST.csv').exists())
        got=json.loads((out/'intersection_report.json').read_text())
        self.assertEqual(got['total_pre'],6423)
        self.assertTrue(r['horizons']['180']['early_late'])

    def test_status_summary_denominator(self):
        x=pd.DataFrame({'result_180m':['TP_FIRST','SL_FIRST','NO_HIT','AMBIGUOUS']})
        s=m.stats(x,180)
        self.assertEqual(s['tp_first_resolved_pct'],50)
        self.assertEqual(s['tp_all_pct'],25)

    def test_missing_and_corrupt_ft_refused(self):
        p=m.audit(self.root)[1]
        corrupted=self.ftdf.copy();corrupted=pd.concat([corrupted,corrupted.iloc[[0]]],ignore_index=True)
        bad=self.root/'duplicates.csv';corrupted.to_csv(bad,index=False)
        with self.assertRaisesRegex(ValueError,'Duplicate'):
            m.ft_join(p,bad)
        corrupted=self.ftdf.copy();corrupted.loc[0,'side']='SHORT'
        bad=self.root/'wrong_side.csv';corrupted.to_csv(bad,index=False)
        with self.assertRaisesRegex(ValueError,'mismatch'):
            m.ft_join(p,bad)
        corrupted=self.ftdf.copy();corrupted.loc[0,'result_180m']='WIN'
        bad=self.root/'bad_status.csv';corrupted.to_csv(bad,index=False)
        with self.assertRaisesRegex(ValueError,'Unexpected'):
            m.ft_join(p,bad)

    def test_changed_pre_refused_even_if_csv_still_valid(self):
        original=(self.root/m.OUT/'intersection_labels_pre.csv').read_bytes()
        try:
            with (self.root/m.OUT/'intersection_labels_pre.csv').open('ab') as f:f.write(b'\n')
            with self.assertRaisesRegex(ValueError,'not-audited'):
                m.audit(self.root)
        finally:(self.root/m.OUT/'intersection_labels_pre.csv').write_bytes(original)

    def test_bad_v4_provenance_refused(self):
        path=self.root/m.V4/'audit.json';original=path.read_text()
        try:
            data=json.loads(original);data['source_hashes']['v3_labels']='corrupted'
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError,'V4 frozen'):
                m.load_sources(self.root)
        finally:path.write_text(original)

    def test_bootstrap_and_interaction_deterministic(self):
        p=m.audit(self.root)[1];r1,k=m.analyze(p,self.ftpath,100);r2,_=m.analyze(p,self.ftpath,100)
        self.assertEqual(r1['horizons']['180']['interaction_cluster_symbol'],r2['horizons']['180']['interaction_cluster_symbol'])
        self.assertIsNotNone(r1['horizons']['180']['interaction_difference_in_differences_pp'])

    def test_within_day_requires_comparable_resolved_events(self):
        df=pd.DataFrame({
            'day_utc':['2026-10-12']*6 + ['2026-10-13']*6,
            'intersection_group_pre':['BOTH']*3+['RECOVERED_ONLY']*3+['BOTH']*3+['RECOVERED_ONLY']*3,
            'result_180m':['TP_FIRST']*3+['SL_FIRST']*3+['SL_FIRST']*3+['SL_FIRST']*3,
        })
        check=m.within_strata(df,'day_utc','BOTH','RECOVERED_ONLY',180)
        self.assertEqual(check['strata_used'],2)
        self.assertEqual(check['positive_strata'],1)
        self.assertEqual(check['weighted_delta_pp'],50.0)

    def test_mismatched_pivot_membership_refused(self):
        path=self.root/m.PIVOT/'pivot_memory_labels_pre.csv'
        original=path.read_bytes()
        try:
            df=pd.read_csv(path)
            df.loc[0,'symbol']='OTHER_SYMBOL'
            df.to_csv(path,index=False)
            with self.assertRaisesRegex(ValueError,'Pivot PRE provenance'):
                m.load_sources(self.root)
        finally:path.write_bytes(original)

    def test_missing_pre_unknown_never_in_neither(self):
        x,_=m.load_sources(self.root);p=m.classify_pre(x)
        unknown=p[p.intersection_group_pre.eq('EXCLUDED_UNKNOWN_PRE')]
        self.assertEqual(len(unknown),4)
        self.assertFalse(unknown.intersection_group_pre.eq('NEITHER').any())


if __name__=='__main__':unittest.main()
