import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
import types

import numpy as np
import pandas as pd

import audit_persistent_recovery_robustness_v4 as mod
import audit_persistent_level_lifecycle_v3 as v3

STEP=900000


def zone(formation=STEP,tf='15m'):
    return dict(event_key='E1',zone_id='z1',origin_kind='LOW',timeframe=tf,
        zone_low=99.,zone_high=101.,formation_closed_ts=formation)


def frame(closes):
    return pd.DataFrame([dict(timestamp=i*STEP,open=c,close=c,low=c-.1,high=c+.1)
                         for i,c in enumerate(closes)])


def label(key,symbol,ts,group,visits='',side='LONG',tf='15m'):
    return dict(event_key=key,symbol=symbol,side=side,retest_ts=ts,
        support_zone_id=('z'+key if group!=mod.NO_LEVEL else ''),
        support_tf=(tf if group!=mod.NO_LEVEL else ''),
        support_visits=visits,support_state_pre='RECOVERED_ORIGINAL' if group==mod.RECOVERED else 'INTACT_ORIGINAL',
        date_utc='2026-10-20',v3_group_pre=group)


class RecoveryClockTests(unittest.TestCase):
    def test_last_recovery_and_strict_boundary(self):
        z=zone()
        f=frame([103,103,97,97,103,103,103,103])
        self.assertEqual(mod.last_recovery_closed(f,z,10*STEP),6*STEP)
        self.assertEqual(mod.last_recovery_closed(f,z,8*STEP),6*STEP)
        with self.assertRaisesRegex(ValueError,'mismatch'):
            mod.last_recovery_closed(f,z,6*STEP)  # final confirmation at reaction excluded

    def test_last_not_first_recovery(self):
        f=frame([103,103,97,97,103,103,97,97,103,103,103])
        self.assertEqual(mod.last_recovery_closed(f,zone(),12*STEP),10*STEP)

    def test_reject_unknown_gap(self):
        f=frame([103,103,97,97,103,103])
        f.loc[2,'timestamp']+=STEP//2
        with self.assertRaisesRegex(ValueError,'path mismatch'):
            mod.last_recovery_closed(f,zone(),9*STEP)

    def test_recency_pre_features_no_outcomes(self):
        e=pd.DataFrame([label('E1','XYZ',10*STEP,mod.RECOVERED,3)])
        features=mod.build_recency_rows(e,{'E1':zone()}, {'15m':frame([103,103,97,97,103,103,103,103])})
        self.assertEqual(features.hours_since_recovery_pre.iloc[0],1.)
        self.assertNotIn('result_180m',features.columns)
        self.assertEqual(mod.recency_bin(6),'<=6h')
        self.assertEqual(mod.recency_bin(24),'6-24h')
        self.assertEqual(mod.recency_bin(30),'>24h')

    def test_role_visits_no_other_group_loss(self):
        events=pd.DataFrame([label('a','AAA',10*STEP,mod.RECOVERED,3),
            label('b','AAA',11*STEP,mod.RECOVERED,2),
            label('c','AAA',12*STEP,mod.DEFENDED,3),
            label('d','AAA',13*STEP,mod.DEFENDED,2),
            label('e','AAA',14*STEP,mod.NO_LEVEL,''),
            label('f','AAA',15*STEP,mod.RECOVERED,3,side='SHORT')])
        got=mod.add_group(events)
        self.assertEqual(got.analysis_group.tolist(),['RECOVERED_3PLUS','RECOVERED_2',
            'DEFENDED_3PLUS','DEFENDED_2','NO_LEVEL','EXCLUDED'])

    def test_cluster_sampling_preserves_all_symbol_rows(self):
        x=pd.DataFrame([dict(event_key=f'{s}-{i}-{arm}',symbol_pre=s,analysis_group=arm,
                    result_180m=result) for s in ('A','B','C','D')
                    for arm,result in (('RECOVERED_3PLUS','TP_FIRST'),('DEFENDED_3PLUS','SL_FIRST'))
                    for i in (1,2)])
        a=mod._bootstrap(x,'symbol_pre',h=180,rounds=200)
        b=mod._bootstrap(x,'symbol_pre',h=180,rounds=200)
        self.assertEqual(a,b)
        self.assertEqual(a['clusters'],4)
        self.assertEqual(a['ci95_pp'],[100.,100.])


class OfflineReportTests(unittest.TestCase):
    def test_join_robustness_missing_scenarios_and_holdout_language(self):
        with tempfile.TemporaryDirectory() as d:
            run=Path(d)
            out=mod.path_for(run)
            out.mkdir(parents=True)
            tags=[label('a','AAA',10*STEP,mod.RECOVERED,3),
                  label('b','BBB',11*STEP,mod.RECOVERED,3),
                  label('c','CCC',12*STEP,mod.DEFENDED,3),
                  label('d','DDD',13*STEP,mod.DEFENDED,3),
                  label('e','EEE',14*STEP,mod.RECOVERED,2),
                  label('f','FFF',15*STEP,mod.NO_LEVEL,'')]
            tags[1]['retest_ts']=10*STEP+86_400_000
            tags[3]['retest_ts']=12*STEP+86_400_000
            labels=pd.DataFrame(tags)
            rec=pd.DataFrame([dict(event_key=e['event_key'],symbol=e['symbol'],
                retest_ts=e['retest_ts'],support_zone_id=e['support_zone_id'],support_tf=e['support_tf'],
                last_recovery_close_ts=e['retest_ts']-7_200_000,
                hours_since_recovery_pre=2.,recency_pre_status='COMPLETE')
                for e in tags if e['v3_group_pre']==mod.RECOVERED and e['side']=='LONG'])
            mod.write_csv(out/'recovery_recency_pre.csv',rec)
            hashes={'v1_event':'a','v1_zone':'b','v3_labels':'c'}
            mod.write_json(out/'audit.json',dict(success=True,version=mod.VERSION,
                n_long_recovered=3,source_hashes=hashes,
                recovery_recency_sha=mod.sha(out/'recovery_recency_pre.csv')))
            ft=run/'ft.csv'
            scenarios=[]
            results={'a':'TP_FIRST','b':'SL_FIRST','c':'SL_FIRST','d':'TP_FIRST','f':'TP_FIRST'}
            for e in tags:
                if e['event_key'] not in results:continue
                scenarios.append(dict(event_key=e['event_key'],symbol=e['symbol'],side=e['side'],
                    observation_type='LIVE_OBSERVED',entry_mode='OBSERVED_OPEN',tp_pct=1.,sl_pct=1.,
                    result_180m=results[e['event_key']],result_360m=results[e['event_key']]))
            pd.DataFrame(scenarios).to_csv(ft,index=False)
            with patch.object(mod,'read_inputs',return_value=(labels,None,hashes)):
                r=mod.report(run,ft,rounds=100)
            self.assertEqual(r['included_long_pre']['RECOVERED_3PLUS'],2)
            self.assertEqual(r['missing_first_touch_by_group']['RECOVERED_2'],1)
            self.assertEqual(r['summary']['180']['RECOVERED_3PLUS']['events'],2)
            self.assertEqual(r['summary']['180']['RECOVERED_3PLUS']['TP'],1)
            self.assertEqual(r['summary']['180']['DEFENDED_3PLUS']['SL'],1)
            self.assertEqual(r['recovery_recency']['360']['<=6h']['events'],2)
            self.assertEqual(len(r['time_halves']['180']),2)
            self.assertIn('no independent holdout',r['cautions'][0].lower())
            self.assertTrue((out/'robustness_report.json').is_file())
            self.assertTrue((out/'daily_breakdown.csv').is_file())
            # Corruption refused before joining outcomes
            with (out/'recovery_recency_pre.csv').open('a') as f:f.write('\n')
            with patch.object(mod,'read_inputs',return_value=(labels,None,hashes)):
                with self.assertRaisesRegex(ValueError,'hash mismatch'):
                    mod.report(run,ft,rounds=100)

    def test_audit_pre_rejects_noncausal_clock(self):
        with tempfile.TemporaryDirectory() as d:
            run=Path(d);out=mod.path_for(run);(out/'shards').mkdir(parents=True)
            labels=pd.DataFrame([label('a','AAA',10*STEP,mod.RECOVERED,3)])
            hashes={'v1_event':'a','v1_zone':'b','v3_labels':'c'}
            mod.write_json(out/'config.json',mod.config(hashes))
            data=pd.DataFrame([dict(event_key='a',symbol='AAA',retest_ts=10*STEP,
                support_zone_id='za',support_tf='15m',last_recovery_close_ts=10*STEP,
                hours_since_recovery_pre=0.,recency_pre_status='COMPLETE')])
            p=out/'shards/AAA.csv';mod.write_csv(p,data)
            mod.write_json(out/'shards/AAA.json',dict(keys=['a'],sha256=mod.sha(p)))
            with patch.object(mod,'read_inputs',return_value=(labels,None,hashes)):
                with self.assertRaisesRegex(ValueError,'Non-causal'):
                    mod.audit_pre(run)

    def test_end_to_end_pre_only_mocked_history(self):
        class FakeConnection:
            def close(self): pass
            def __enter__(self):return self
            def __exit__(self,*args): self.close()
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);run=root/'study'
            ev=pd.DataFrame([label('E1','XYZ',10*STEP,mod.RECOVERED,3)])
            z=pd.DataFrame([dict(zone(),event_key='E1')])
            hashes={'v1_event':'a','v1_zone':'b','v3_labels':'c'}
            line=types.SimpleNamespace(ROOT_REL=Path('any'),CTX_NAME='db',HISTORY_NAME='hist',ro=lambda x:FakeConnection())
            atlas=types.ModuleType('reaction_structural_level_atlas');atlas.lineage=line
            v4=types.ModuleType('reaction_compression_breakout_retest_v4')
            candles=frame([103,103,97,97,103,103,103,103]).set_index('timestamp')
            v4._merge_tf=lambda ctx,hist,symbol:(candles,candles,'MOCK')
            with patch.object(mod,'read_inputs',return_value=(ev,z,hashes)):
                with patch.dict(sys.modules,{'reaction_structural_level_atlas':atlas,
                         'reaction_compression_breakout_retest_v4':v4}):
                    result=mod.run_pre(root,run)
                    self.assertTrue(result['success'])
                    self.assertEqual(result['recency_complete'],1)
                    rerun=mod.run_pre(root,run)
                    self.assertEqual(result['recovery_recency_sha'],rerun['recovery_recency_sha'])
            self.assertFalse((mod.path_for(run)/'first_touch_report.json').exists())


class WithinStrataTests(unittest.TestCase):
    def test_within_date_compares_only_dates_with_both_arms(self):
        cases=[]
        for day in ('2026-10-18','2026-10-19'):
            for a,result in [('RECOVERED_3PLUS','TP_FIRST'),('DEFENDED_3PLUS','SL_FIRST')]:
                for i in range(3):
                    cases.append(dict(event_key=f'{day}-{a}-{i}',day_utc=day,
                          analysis_group=a,result_180m=result))
        for i in range(20):
            cases.append(dict(event_key=f'x{i}',day_utc='2026-10-20',
                analysis_group='RECOVERED_3PLUS',result_180m='SL_FIRST'))
        got=mod.within_strata(pd.DataFrame(cases),'day_utc',180)
        self.assertEqual(got['strata_used'],2)
        self.assertEqual(got['within_strata_delta_pp'],100.)
        self.assertEqual(got['positive_strata'],2)

class FrozenSourcesTests(unittest.TestCase):
    def test_read_inputs_matches_frozen_v3_and_refuses_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d);lbl=label('E1','XYZ',10*STEP,mod.RECOVERED,3)
            lbl['support_zone_id']='zE1'
            events=pd.DataFrame([dict(event_key='E1',symbol='XYZ',side='LONG',
                retest_ts=10*STEP,observation_type='LIVE_OBSERVED',
                group_pre='PERSISTENT_LEVEL_NEAR_PRE',
                chosen_role_pre='INSIDE_ZONE',chosen_kind='LOW')])
            zones=pd.DataFrame([dict(event_key='E1',symbol='XYZ',side='LONG',
                retest_ts=10*STEP,timeframe='15m',zone_id='zE1',origin_kind='LOW',
                age_bars=40,independent_visits=3,confirmed_responses=3,
                distance_pre_atr=.15,relation_pre='INSIDE',formation_closed_ts=STEP,
                response_timestamps=f'{2*STEP};{3*STEP};{4*STEP}',
                zone_low=99.,zone_high=101.)])
            events.to_csv(path/'event_labels.csv',index=False)
            zones.to_csv(path/'candidate_levels.csv',index=False)
            hashes={p:mod.sha(path/p) for p in ('event_labels.csv','candidate_levels.csv')}
            (path/'audit.json').write_text(json.dumps(dict(success=True,events=1,
                audit_rule='strict_response_close_before_retest_v1.1',outputs_sha256=hashes)))
            dst=path/'level_lifecycle_v3/all_live';dst.mkdir(parents=True)
            pd.DataFrame([lbl]).to_csv(dst/'lifecycle_labels_pre.csv',index=False)
            (dst/'audit.json').write_text(json.dumps(dict(success=True,
                version=v3.VERSION,events=1,input_event_sha=hashes['event_labels.csv'],
                input_zone_sha=hashes['candidate_levels.csv'],
                output_labels_sha=mod.sha(dst/'lifecycle_labels_pre.csv'))))
            labels,selected,source=mod.read_inputs(path)
            self.assertEqual(len(labels),1)
            self.assertEqual(selected.zone_id.iloc[0],'zE1')
            self.assertEqual(source['v3_labels'],mod.sha(dst/'lifecycle_labels_pre.csv'))
            with (dst/'lifecycle_labels_pre.csv').open('a') as f:f.write('\n')
            with self.assertRaisesRegex(ValueError,'SHA mismatch'):
                mod.read_inputs(path)

if __name__ == '__main__':
    unittest.main()
