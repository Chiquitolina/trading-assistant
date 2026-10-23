import hashlib,json,tempfile,unittest
from pathlib import Path
import pandas as pd
from audit_persistent_pattern_roles import classify_pre,read_pre,join_first_touch,report,MIN_HOURS,NEAR,FAR,PAT_LONG,PAT_SHORT,PAT_OTHER,G_FAR

class PatternRolesTest(unittest.TestCase):
    def setUp(self):
        self.events=pd.DataFrame([
            dict(event_key='A',symbol='AA',side='LONG',observation_type='LIVE_OBSERVED',group_pre=NEAR,chosen_role_pre='INSIDE_ZONE',chosen_kind='HIGH'),
            dict(event_key='B',symbol='BB',side='SHORT',observation_type='LIVE_OBSERVED',group_pre=NEAR,chosen_role_pre='OTRO_ROL_O_RUPTURA_PRE',chosen_kind='LOW'),
            dict(event_key='C',symbol='CC',side='SHORT',observation_type='LIVE_OBSERVED',group_pre=FAR,chosen_role_pre='UNCLASSIFIED',chosen_kind=''),
            dict(event_key='D',symbol='DD',side='LONG',observation_type='LIVE_OBSERVED',group_pre=NEAR,chosen_role_pre='INSIDE_ZONE',chosen_kind='LOW'),
        ])
        self.zones=pd.DataFrame([
            dict(event_key='A',timeframe='15m',zone_id='z1',origin_kind='HIGH',age_bars=160,independent_visits=4,confirmed_responses=4,distance_pre_atr=.05,relation_pre='INSIDE'),
            dict(event_key='A',timeframe='15m',zone_id='z2',origin_kind='LOW',age_bars=80,independent_visits=2,confirmed_responses=2,distance_pre_atr=.15,relation_pre='INSIDE'),
            dict(event_key='B',timeframe='30m',zone_id='z3',origin_kind='HIGH',age_bars=40,independent_visits=2,confirmed_responses=2,distance_pre_atr=.4,relation_pre='BELOW'),
            dict(event_key='D',timeframe='15m',zone_id='z4',origin_kind='LOW',age_bars=20,independent_visits=2,confirmed_responses=2,distance_pre_atr=.1,relation_pre='INSIDE'),
        ])
        self.zones['side']=self.zones.event_key.map({'A':'LONG','B':'SHORT','D':'LONG'})
        self.zones['symbol']=self.zones.event_key.map({'A':'AA','B':'BB','D':'DD'})
    def test_zone_side_disagreement_rejected(self):
        altered=self.zones.copy()
        altered.loc[altered.event_key=='A','side']='SHORT'
        with self.assertRaisesRegex(ValueError,'Zone side'):
            classify_pre(self.events,altered)
    def test_two_previous_not_three_and_any_level_not_nearest(self):
        labels=classify_pre(self.events,self.zones).set_index('event_key')
        self.assertEqual(labels.loc['A','pattern_pre'],PAT_LONG)
        self.assertEqual(labels.loc['A','pattern_zone_id'],'z2')
        self.assertEqual(labels.loc['A','pattern_prior_visits'],2)
        self.assertTrue(labels.loc['A','pattern_inside'])
    def test_30m_40_bars_same_twenty_hours_as_15m_80(self):
        labels=classify_pre(self.events,self.zones).set_index('event_key')
        self.assertEqual(labels.loc['A','pattern_age_hours'],MIN_HOURS)
        self.assertEqual(labels.loc['B','pattern_age_hours'],MIN_HOURS)
        self.assertEqual(labels.loc['B','pattern_pre'],PAT_LONG)
    def test_under20h_and_far(self):
        labels=classify_pre(self.events,self.zones).set_index('event_key')
        self.assertEqual(labels.loc['D','pattern_pre'],PAT_SHORT)
        self.assertEqual(labels.loc['C','pattern_pre'],G_FAR)
    def test_inside_high_not_compatible_long(self):
        ev=self.events.iloc[:1]
        zones=self.zones[self.zones.zone_id=='z1']
        labels=classify_pre(ev,zones)
        self.assertEqual(labels.iloc[0].pattern_pre,PAT_OTHER)
    def test_outcomes_missing_separate(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'ft.csv'
            pd.DataFrame([dict(event_key='A',symbol='AA',side='LONG',observation_type='LIVE_OBSERVED',entry_mode='OBSERVED_OPEN',tp_pct=1.0,sl_pct=1.0,result_180m='TP_FIRST',result_360m='TP_FIRST')]).to_csv(p,index=False)
            labels=classify_pre(self.events,self.zones)
            merged=join_first_touch(labels,p)
            r=report(labels,merged)
            self.assertEqual(r['n_first_touch_missing'],3)
            self.assertEqual(r['by_horizon']['360'][PAT_LONG]['TP'],1)
            self.assertEqual(r['groups_missing_first_touch'][PAT_LONG],1)
    def test_duplicate_first_touch_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'ft.csv'
            row=dict(event_key='A',symbol='AA',side='LONG',observation_type='LIVE_OBSERVED',entry_mode='OBSERVED_OPEN',tp_pct=1,sl_pct=1,result_180m='TP_FIRST',result_360m='TP_FIRST')
            pd.DataFrame([row,row]).to_csv(p,index=False)
            with self.assertRaisesRegex(ValueError,'duplicate'):
                join_first_touch(classify_pre(self.events,self.zones),p)
    def test_invalid_response_on_reaction_boundary_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            event=dict(event_key='X',symbol='XX',side='LONG',retest_ts=2000,group_pre=NEAR,
                       observation_type='LIVE_OBSERVED',chosen_role_pre='INSIDE_ZONE',chosen_kind='LOW')
            zone=dict(event_key='X',retest_ts=2000,timeframe='15m',zone_id='z',origin_kind='LOW',age_bars=80,independent_visits=2,confirmed_responses=2,distance_pre_atr=.2,relation_pre='INSIDE',formation_closed_ts=500,response_timestamps='1000;2000')
            pd.DataFrame([event]).to_csv(p/'event_labels.csv',index=False)
            pd.DataFrame([zone]).to_csv(p/'candidate_levels.csv',index=False)
            getsha=lambda name:hashlib.sha256((p/name).read_bytes()).hexdigest()
            (p/'audit.json').write_text(json.dumps(dict(success=True,events=1,audit_rule='strict_response_close_before_retest_v1.1',outputs_sha256={name:getsha(name) for name in ('event_labels.csv','candidate_levels.csv')})))
            with self.assertRaisesRegex(ValueError,'Noncausal'):
                read_pre(p)
    def test_csv_hash_modified_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            for name in ('event_labels.csv','candidate_levels.csv'):(p/name).write_text('bad')
            (p/'audit.json').write_text(json.dumps(dict(success=True,events=1,audit_rule='strict_response_close_before_retest_v1.1',outputs_sha256={'event_labels.csv':'x','candidate_levels.csv':'y'})))
            with self.assertRaisesRegex(ValueError,'changed'):
                read_pre(p)

if __name__=='__main__':unittest.main()

class EndToEndOfflineTest(unittest.TestCase):
    def test_outputs_pre_labels_and_aggregate_report_separately(self):
        from audit_persistent_pattern_roles import run
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);folder=p/'run';folder.mkdir()
            ev=pd.DataFrame([dict(event_key='A|LONG|2000',symbol='A',side='LONG',date_utc='2026-10-16',
                retest_ts=2000,observation_type='LIVE_OBSERVED',group_pre=NEAR,chosen_role_pre='INSIDE_ZONE',chosen_kind='LOW',
                chosen_timeframe='15m',chosen_visits=2,chosen_age_bars=80)])
            z=pd.DataFrame([dict(event_key='A|LONG|2000',symbol='A',side='LONG',retest_ts=2000,timeframe='15m',zone_id='zoneA',
                origin_kind='LOW',age_bars=80,independent_visits=2,confirmed_responses=2,distance_pre_atr=.1,
                relation_pre='INSIDE',formation_closed_ts=400,response_timestamps='500;1000')])
            ev.to_csv(folder/'event_labels.csv',index=False);z.to_csv(folder/'candidate_levels.csv',index=False)
            def checksum(name):return hashlib.sha256((folder/name).read_bytes()).hexdigest()
            (folder/'audit.json').write_text(json.dumps(dict(success=True,events=1,audit_rule='strict_response_close_before_retest_v1.1',
                outputs_sha256={name:checksum(name) for name in ('event_labels.csv','candidate_levels.csv')})))
            ft=p/'ft.csv';pd.DataFrame([dict(event_key='A|LONG|2000',symbol='A',side='LONG',observation_type='LIVE_OBSERVED',
                entry_mode='OBSERVED_OPEN',tp_pct=1,sl_pct=1,result_180m='TP_FIRST',result_360m='TP_FIRST')]).to_csv(ft,index=False)
            out=p/'out';res=run(folder,ft,out)
            self.assertEqual(res['by_horizon']['360'][PAT_LONG]['TP'],1)
            self.assertEqual(pd.read_csv(out/'pattern_labels_pre.csv').iloc[0].pattern_pre,PAT_LONG)
            self.assertFalse(any(x.startswith('result_') for x in pd.read_csv(out/'pattern_labels_pre.csv').columns))
            self.assertTrue((out/'pattern_report.json').is_file())
