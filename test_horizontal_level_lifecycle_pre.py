"""Causal regression suite: lost support, rejected retest and recovery."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd

from reaction_horizontal_level_lifecycle import (
  ACTIVE,LOST,FAILED,RECLAIMED,UNKNOWN,NO_NEAR,
  classify_zone_history, classify_one, build_lifecycle, STEPS,
)

S=STEPS['15m']
T=20*S


def mkbar(i,price,wick_high=None,wick_low=None):
    high=max(price+0.12,wick_high if wick_high is not None else price)
    low=min(price-0.12,wick_low if wick_low is not None else price)
    return {'timestamp':T+i*S-S,'open':price,'high':high,'low':low,'close':price}


def make_bars(prices,extra=None):
    return pd.DataFrame([mkbar(i,float(price),**((extra or {}).get(i,{})))
                         for i,price in enumerate(prices,start=1)])


def sample_zone():
    return {'zone_low':99.7,'zone_high':100.3,'timeframe':'15m',
            'formation_closed_ts':T}


def read(prices,extra=None,add_later=None,form=None):
    df=make_bars(prices,extra)
    if add_later:
        df=pd.concat([df,make_bars(prices+add_later).iloc[len(prices):]],ignore_index=True)
    zone=sample_zone()
    if form is not None:zone['formation_closed_ts']=form
    retest=T+len(prices)*S+60_000
    return classify_zone_history(zone,df,retest),df,retest


class LifecycleCausalTests(unittest.TestCase):
    def test_initial_support_respected(self):
        got,_,_=read([101,101.1,100.8,101.0,101.2])
        self.assertEqual(got['lifecycle_state'],ACTIVE)
        self.assertEqual(got['initial_side'],'ABOVE')

    def test_support_lost_two_closes_not_single_wick(self):
        one,_,_=read([101,101,98.9,101])
        self.assertNotEqual(one['lifecycle_state'],LOST)
        two,_,_=read([101,101,99,98.8])
        self.assertEqual(two['lifecycle_state'],LOST)
        self.assertEqual(two['current_confirmed_side'],'BELOW')

    def test_jelly_like_lost_support_failed_retest_pre(self):
        # Classic case: two accepted closes above, two below, then touch
        # underside and close under it; no future bars consulted.
        data=[101,101,98.8,98.7,98.6,98.8,98.5]
        got,_,_=read(data,extra={6:{'wick_high':100.08}})
        self.assertEqual(got['lifecycle_state'],FAILED)
        self.assertIsNotNone(got['last_break_close_ts'])
        self.assertIsNotNone(got['last_failed_retest_close_ts'])
        self.assertLess(got['last_break_close_ts'],got['last_failed_retest_close_ts'])
        self.assertEqual(got['independent_failed_retests'],1)

    def test_reclaim_after_loss(self):
        got,_,_=read([101,101,98.5,98.2,100.5,100.8])
        self.assertEqual(got['lifecycle_state'],RECLAIMED)
        self.assertIsNotNone(got['last_reclaim_close_ts'])

    def test_broken_resistance_retest_rejected_symmetry(self):
        # Resistance originally keeps price below; two closes break above.
        seq=[98,98.2,101,101.2,102,101]
        got,_,_=read(seq,extra={6:{'wick_low':100.0}})
        self.assertEqual(got['initial_side'],'BELOW')
        self.assertEqual(got['lifecycle_state'],FAILED)

    def test_loss_transition_bar_cannot_be_own_retest(self):
        got,_,_=read([101,101,98,98],extra={4:{'wick_high':100.0}})
        self.assertEqual(got['lifecycle_state'],LOST)
        self.assertIsNone(got['last_failed_retest_close_ts'])

    def test_one_inside_last_bar_does_not_confirm_reclaim(self):
        got,_,_=read([101,101,98,98,100.0])
        self.assertEqual(got['lifecycle_state'],UNKNOWN)
        self.assertEqual(got['reason'],'LAST_BAR_CONTESTS_CONFIRMED_SIDE')

    def test_missing_bar_cannot_be_interpreted_as_valid_history(self):
        got,df,r=read([101,101,98,98,98])
        dropped=df.drop(index=[3])
        bad=classify_zone_history(sample_zone(),dropped,r)
        self.assertEqual(bad['lifecycle_state'],UNKNOWN)
        self.assertEqual(bad['reason'],'GAPPED_HTF_HISTORY')

    def test_future_bars_do_not_change_pre_label(self):
        seq=[101,101,98.8,98.7,98.6]
        got,df,r=read(seq)
        more=pd.concat([df, pd.DataFrame([{'timestamp':r+60_000,'open':100.9,
                                           'high':101.2,'low':100.5,'close':100.9}])],ignore_index=True)
        newer=classify_zone_history(sample_zone(),more,r)
        self.assertEqual(got,newer)

    def test_no_selection_status_and_role_outcome_invariance(self):
        event={'event_key':'foo','symbol':'XUSDT','side':'SHORT','date_utc':'2026-10-21',
               'retest_ts':T+8*S,'all_covered':1,'outcome_norm':'TP_FIRST'}
        role={'role_zone_id':None,'role_timeframe':None,'role_zone_low':None,
              'role_zone_high':None,'pre_role':'PRE_SIN_NIVEL_CERCANO','role_distance_atr':None}
        a=classify_one(event,role,None,None)
        self.assertEqual(a['lifecycle_state'],NO_NEAR)
        event['outcome_norm']='SL_FIRST'
        self.assertEqual(a,classify_one(event,role,None,None))

    def test_more_than_one_tf_selection_keeps_unique_event_tf(self):
        # Cases that contain NO_NEAR do not require actual historical bars.
        from reaction_horizontal_level_research import ROLE_NO_NEAR
        event=pd.DataFrame([{'event_key':'foo','symbol':'XUSDT','side':'SHORT',
                              'date_utc':'2026-10-21','retest_ts':T+8*S,'all_covered':1}])
        zones=pd.DataFrame([{'event_key':'foo','symbol':'XUSDT','zone_id':'z',
             'pivot_count':2,'defense_episodes_pre':1,'formation_closed_ts':T,
             'retest_ts':T+8*S,'timeframe':'15m','pre_last_close':100,
             'atr_pre':0.1,'zone_low':105,'zone_high':106}])
        ans=build_lifecycle(event,zones,{},'TODOS')
        self.assertEqual(len(ans),1)
        self.assertEqual(ans.loc[0,'lifecycle_state'],NO_NEAR)

if __name__=='__main__':unittest.main()

class OfflineRunAuditTests(unittest.TestCase):
    def test_cli_run_audit_and_exact_zone_cache_across_all_tf_filters(self):
        """Fake the upstream read-only SQLite provider; test only derived writes."""
        import json
        import sys
        import types
        from contextlib import nullcontext
        from reaction_horizontal_level_lifecycle import run,audit,read_checked_cache,VERSION
        from reaction_horizontal_level_research import build_pre_roles

        closes=[101.0,101.0,98.8,98.7,98.6,98.8,98.5]
        bars=make_bars(closes,{6:{'wick_high':100.05}})
        r=T+len(closes)*S+60_000
        event=pd.DataFrame([{'event_key':'e1','symbol':'TESTUSDT','side':'LONG',
                 'date_utc':'2026-10-21','retest_ts':r,'all_covered':1}])
        zone=pd.DataFrame([{'event_key':'e1','symbol':'TESTUSDT','zone_id':'LEVEL_15',
             'timeframe':'15m','pivot_count':2,'defense_episodes_pre':2,
             'formation_closed_ts':T,'retest_ts':r,'pre_last_close':98.5,
             'atr_pre':3.0,'zone_low':99.7,'zone_high':100.3}])
        class FakeConnection:
            def close(self):pass
        class FakeLineage:
            ROOT_REL=Path('reports/reaction_lab')
            CTX_NAME='test_context.db';HISTORY_NAME='nonexistent_history.db'
            ro=staticmethod(lambda path:FakeConnection())
        def merged(ctx,cache,symbol):
            return (bars.set_index('timestamp'),pd.DataFrame(columns=bars.columns).set_index('timestamp'),'NATIVE')
        atlas=types.ModuleType('reaction_structural_level_atlas_v2')
        atlas.OUTPUT='reaction_structural_level_atlas_v2'
        atlas.v1=types.SimpleNamespace(lineage=FakeLineage,
                                       v4=types.SimpleNamespace(_merge_tf=merged))
        view=types.ModuleType('dashboard.views.reaction_structural_level_atlas_v2_view')
        view.load=lambda root:(zone,event,{'version':'structural-level-atlas-v2.0'})
        with tempfile.TemporaryDirectory() as root:
            src=Path(root)/'reports/reaction_lab/reaction_structural_level_atlas_v2'
            src.mkdir(parents=True)
            event.to_csv(src/'event_map.csv',index=False)
            zone.to_csv(src/'candidate_levels.csv',index=False)
            with patch.dict(sys.modules,{'reaction_structural_level_atlas_v2':atlas,
                 'dashboard.views.reaction_structural_level_atlas_v2_view':view}):
                meta=run(root)
                self.assertEqual(meta['events'],1)
                self.assertEqual(meta['rows'],3)
                self.assertTrue(audit(root)['success'])
                roles=build_pre_roles(event,zone,'TODOS')
                selected=read_checked_cache(root,event,roles,'TODOS')
                self.assertEqual(len(selected),1)
                self.assertEqual(selected.lifecycle_state.iloc[0],FAILED)
                role30=build_pre_roles(event,zone,'30m')
                self.assertEqual(read_checked_cache(root,event,role30,'30m').lifecycle_state.iloc[0],NO_NEAR)
                # Atlas file changed after research: fail closed (no stale results).
                with (src/'candidate_levels.csv').open('a') as f:f.write('\n#modified')
                with self.assertRaisesRegex(ValueError,'Atlas V2 cambió'):
                    read_checked_cache(root,event,roles,'TODOS')
