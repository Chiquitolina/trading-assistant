import hashlib
import json
import tempfile
import unittest
from pathlib import Path
import pandas as pd
import audit_persistent_level_lifecycle_v3 as v3

STEP=v3.TF_MS['15m']

def mkzone(kind='LOW',lo=99,hi=101,formed=STEP):
    return dict(timeframe='15m',origin_kind=kind,zone_low=lo,zone_high=hi,independent_visits=2,
        formation_closed_ts=formed,zone_id='z',age_hours=20)

def mkframe(closes,overlap=()):
    bars=[]
    for i,c in enumerate(closes):
        if i in overlap:
            low=min(c,100.0);high=max(c,100.0)
        else:
            low=c-.2;high=c+.2
        bars.append(dict(timestamp=i*STEP,open=c,close=c,low=low,high=high))
    return pd.DataFrame(bars)

class LifecycleUnitTests(unittest.TestCase):
    def test_support_never_lost_even_with_wick(self):
        df=mkframe([103,103,103,103,103],overlap=(1,2))
        self.assertEqual(v3.evaluate_zone(df,mkzone(),7*STEP)['state'],'INTACT_ORIGINAL')
    def test_support_two_closed_breaks_recovered(self):
        df=mkframe([103,103,97,97,103,103,103])
        out=v3.evaluate_zone(df,mkzone(),8*STEP)
        self.assertEqual(out['state'],'RECOVERED_ORIGINAL')
        self.assertEqual(out['wrong_confirmations'],1)
    def test_breakout_without_retest_is_not_flip(self):
        df=mkframe([97,97,103,103,103,103,103])
        out=v3.evaluate_zone(df,mkzone(kind='HIGH'),8*STEP)
        self.assertEqual(out['state'],'OPPOSITE_CONFIRMED_NO_RETEST')
    def test_high_flips_to_support_only_after_later_retest_and_holds(self):
        df=mkframe([97,97,103,103,103,103,103,103],overlap=(4,))
        out=v3.evaluate_zone(df,mkzone(kind='HIGH'),10*STEP)
        self.assertEqual(out['state'],'OPPOSITE_FLIP_SUSTAINED')
        self.assertEqual(out['flip_retest_closed_ts'],5*STEP)
    def test_low_flips_to_resistance_for_short(self):
        df=mkframe([103,103,97,97,97,97,97,97],overlap=(4,))
        out=v3.evaluate_zone(df,mkzone(kind='LOW'),10*STEP)
        self.assertEqual(out['state'],'OPPOSITE_FLIP_SUSTAINED')
    def test_retest_in_reaction_boundary_not_used(self):
        df=mkframe([97,97,103,103,103,103,103],overlap=(4,))
        # Only bar ending at 5*STEP would retest. With retest_ts=5*STEP
        # it must not be used for any historical confirmation.
        out=v3.evaluate_zone(df,mkzone(kind='HIGH'),5*STEP)
        self.assertNotEqual(out['state'],'OPPOSITE_FLIP_SUSTAINED')
    def test_gap_is_unknown_not_defended(self):
        df=mkframe([103]*5)
        df.loc[2,'timestamp']=df.loc[2,'timestamp']+STEP//2
        self.assertEqual(v3.evaluate_zone(df,mkzone(),8*STEP)['state'],'UNKNOWN_PATH')
    def test_cannot_use_formed_after_event(self):
        self.assertEqual(v3.evaluate_zone(mkframe([103]*5),mkzone(formed=9*STEP),8*STEP)['state'],'UNKNOWN_PATH')
    def test_flipped_then_regained_is_not_still_flipped(self):
        df=mkframe([97,97,103,103,103,103,103,97,97,97],overlap=(4,))
        out=v3.evaluate_zone(df,mkzone(kind='HIGH'),12*STEP)
        self.assertEqual(out['state'],'RECOVERED_ORIGINAL')
    def test_close_on_boundary_does_not_confirm_break(self):
        df=mkframe([103,103,97,97])
        self.assertEqual(v3.evaluate_zone(df,mkzone(),4*STEP)['state'],'PENDING_BREAK')
        self.assertEqual(v3.evaluate_zone(df,mkzone(),5*STEP)['state'],'OPPOSITE_CONFIRMED_NO_RETEST')
    def test_deterministic_choice_before_lifecycle(self):
        events=pd.DataFrame([dict(event_key='a')]);zones=pd.DataFrame([
            dict(event_key='a',zone_id='A',origin_kind='LOW',timeframe='15m',distance_pre_atr=.3,confirmed_responses=4,age_bars=80),
            dict(event_key='a',zone_id='B',origin_kind='LOW',timeframe='15m',distance_pre_atr=.1,confirmed_responses=2,age_bars=40),
            dict(event_key='a',zone_id='C',origin_kind='HIGH',timeframe='30m',distance_pre_atr=.25,confirmed_responses=3,age_bars=40)])
        pick=v3.deterministic_zones(events,zones)
        self.assertEqual(set(pick.zone_id),{'B','C'})
    def test_event_precedence_and_no_outcomes(self):
        e=dict(event_key='e',symbol='XX',side='LONG',date_utc='2026-10-20',
               retest_ts=10*STEP,group_pre=v3.v2.NEAR)
        z=[dict(mkzone('LOW'),zone_id='supportA'),dict(mkzone('HIGH'),zone_id='flipB')]
        for x in z:x['age_hours']=20
        df=mkframe([97,97,103,103,103,103,103,103],overlap=(4,))
        out=v3.label_one(e,z,{'15m':df})
        self.assertEqual(out['v3_group_pre'],'FLIP_CONFIRMED_SUSTAINED_PRE')
        self.assertNotIn('TP',out)

class RunAuditTests(unittest.TestCase):
    def test_sha_input_verified_before_processing(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            event=dict(event_key='Z|LONG|9000000',symbol='Z',side='LONG',date_utc='2026-10-20',
                       retest_ts=9000000,observation_type='LIVE_OBSERVED',group_pre=v3.v2.NEAR,
                       chosen_role_pre='INSIDE_ZONE',chosen_kind='LOW')
            zone=dict(event_key=event['event_key'],symbol='Z',side='LONG',retest_ts=9000000,
                      timeframe='15m',zone_id='zz',origin_kind='LOW',age_bars=40,
                      independent_visits=2,confirmed_responses=2,distance_pre_atr=.1,
                      relation_pre='INSIDE',formation_closed_ts=900000,
                      response_timestamps='1800000;2700000',zone_low=99,zone_high=101)
            pd.DataFrame([event]).to_csv(p/'event_labels.csv',index=False)
            pd.DataFrame([zone]).to_csv(p/'candidate_levels.csv',index=False)
            def digest(n):return hashlib.sha256((p/n).read_bytes()).hexdigest()
            (p/'audit.json').write_text(json.dumps(dict(success=True,events=1,
               audit_rule='strict_response_close_before_retest_v1.1',
               outputs_sha256={n:digest(n) for n in ('event_labels.csv','candidate_levels.csv')})))
            ev,zones,audit=v3.prepare(p)
            self.assertEqual(len(zones),1)
            self.assertEqual(len(ev),1)
            with (p/'candidate_levels.csv').open('a') as fh:fh.write('\n')
            with self.assertRaisesRegex(ValueError,'Invalid or changed'):
                v3.prepare(p)

if __name__=='__main__':unittest.main()

class EndToEndMockedSources(unittest.TestCase):
    def test_offline_run_resume_audit_and_outcome_join(self):
        import sys
        import types
        from unittest.mock import patch
        class FakeConn:
            def close(self):pass
            def __enter__(self):return self
            def __exit__(self,*args):self.close()
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);folder=root/'in';folder.mkdir()
            retest=10*STEP
            event=dict(event_key=f'XYZ|LONG|{retest}',symbol='XYZ',side='LONG',date_utc='2026-10-20',
                retest_ts=retest,observation_type='LIVE_OBSERVED',group_pre=v3.v2.NEAR,
                chosen_role_pre='INSIDE_ZONE',chosen_kind='LOW')
            zone=dict(event_key=event['event_key'],symbol='XYZ',side='LONG',retest_ts=retest,
                timeframe='15m',zone_id='zz',origin_kind='LOW',age_bars=40,independent_visits=2,
                confirmed_responses=2,distance_pre_atr=.10,relation_pre='INSIDE',
                formation_closed_ts=STEP,response_timestamps='1800000;2700000',zone_low=99,zone_high=101)
            pd.DataFrame([event]).to_csv(folder/'event_labels.csv',index=False)
            pd.DataFrame([zone]).to_csv(folder/'candidate_levels.csv',index=False)
            getsha=lambda name:hashlib.sha256((folder/name).read_bytes()).hexdigest()
            (folder/'audit.json').write_text(json.dumps(dict(success=True,events=1,
                audit_rule='strict_response_close_before_retest_v1.1',
                outputs_sha256={name:getsha(name) for name in ('event_labels.csv','candidate_levels.csv')})))
            fake_lineage=types.SimpleNamespace(ROOT_REL=Path('foo'),CTX_NAME='ctx',HISTORY_NAME='hist',ro=lambda p: FakeConn())
            fake_atlas=types.ModuleType('reaction_structural_level_atlas');fake_atlas.lineage=fake_lineage
            fake_v4=types.ModuleType('reaction_compression_breakout_retest_v4')
            frame=mkframe([103]*8).set_index('timestamp')
            fake_v4._merge_tf=lambda ctx,hist,symbol:(frame,frame,'MOCK')
            with patch.dict(sys.modules,{'reaction_structural_level_atlas':fake_atlas,
                                         'reaction_compression_breakout_retest_v4':fake_v4}):
                r=v3.run(root,folder,max_events=0)
                self.assertEqual(r['groups']['DEFENDED_CONTINUOUS_PRE'],1)
                r2=v3.run(root,folder,max_events=0)
                self.assertEqual(r2['output_labels_sha'],r['output_labels_sha'])
                self.assertTrue(v3.verify(folder)['success'])
            ft=root/'ft.csv'
            pd.DataFrame([dict(event_key=event['event_key'],symbol='XYZ',side='LONG',
                observation_type='LIVE_OBSERVED',entry_mode='OBSERVED_OPEN',tp_pct=1.0,sl_pct=1.0,
                result_180m='TP_FIRST',result_360m='TP_FIRST')]).to_csv(ft,index=False)
            report=v3.first_touch_report(folder,ft_path=ft)
            self.assertEqual(report['summary']['360']['DEFENDED_CONTINUOUS_PRE']['TP'],1)
            out=folder/'level_lifecycle_v3/all_live/lifecycle_labels_pre.csv'
            self.assertFalse(any(c.startswith('result_') for c in pd.read_csv(out).columns))
