import unittest
import pandas as pd
from reaction_horizontal_level_research import (
    A,B,C,UNKNOWN,build,important,outcome,group_summary,flag,finite,MODEL_VERSION,
)


def event(key='e1',complete=True):
    return dict(event_key=key,all_covered=int(complete),retest_ts=60_000_000,
                symbol='TESTUSDT',side='LONG',date_utc='2026-10-21',
                outcome_norm='TP_FIRST')


def level(key='e1',name='z1',*,touch=False,near=0.1,episodes=1,pivots=2,tf='15m',known=True):
    return dict(event_key=key,zone_id=name,pivot_count=pivots,
        defense_episodes_pre=episodes,formation_closed_ts=10_000_000,
        retest_ts=60_000_000,timeframe=tf,gap_to_zone_atr=near,
        known_touch=touch if known else None,zone_low=99.0,zone_high=101.0,
        atr_pre=3.0,pre_last_close=101.5,level_state_pre='ACCEPTED_DEFENDED')


class HorizontalLevelResearchTests(unittest.TestCase):
    def test_known_touch_a_and_no_touch_near_b(self):
        e=pd.DataFrame([event('a'),event('b')]);z=pd.DataFrame([level('a',touch=True),level('b',touch=False)])
        out,_=build(e,z)
        self.assertEqual(list(out.level_group),[A,B])

    def test_far_or_no_level_class_c_only_complete(self):
        e=pd.DataFrame([event('a'),event('b'),event('c',False)])
        z=pd.DataFrame([level('a',near=1.8),level('c',near=1.8)])
        out,_=build(e,z)
        self.assertEqual(list(out.level_group),[C,C,UNKNOWN])

    def test_missing_known_when_zone_exists_is_not_c(self):
        e=pd.DataFrame([event()]);z=pd.DataFrame([level(near=0.1,known=False)])
        out,_=build(e,z)
        self.assertEqual(out.iloc[0].level_group,UNKNOWN)

    def test_importance_two_pivots_and_pre_defense(self):
        self.assertTrue(important(level()))
        self.assertFalse(important(level(episodes=0)))
        self.assertFalse(important(level(pivots=1)))
        self.assertFalse(important(level(episodes=None)))

    def test_ignore_not_important_close_and_known_touch(self):
        e=pd.DataFrame([event()]);z=pd.DataFrame([level(touch=True,episodes=0)])
        out,valid=build(e,z)
        self.assertEqual(out.iloc[0].level_group,C)
        self.assertTrue(valid.empty)

    def test_do_not_select_on_future_outcome_or_known_hold(self):
        e=pd.DataFrame([event()]);z=pd.DataFrame([level(name='a',touch=True,episodes=1),
                   level(name='b',touch=True,episodes=2)])
        z['known_hold']=[False,True]
        out,_=build(e,z)
        self.assertEqual(out.iloc[0].relevant_zone_id,'b')
        e['outcome_norm']='SL_FIRST'
        z['known_hold']=[True,False]
        second,_=build(e,z)
        self.assertEqual(second.iloc[0].relevant_zone_id,'b')

    def test_unknown_coverage_blocks_any_contact(self):
        e=pd.DataFrame([event(complete=False)]);z=pd.DataFrame([level(touch=True)])
        out,_=build(e,z)
        self.assertEqual(out.iloc[0].level_group,UNKNOWN)
        self.assertEqual(out.iloc[0].pre_bucket,'NO_EVALUABLE')

    def test_strict_clock_no_future(self):
        e=pd.DataFrame([event()]);z=pd.DataFrame([level()])
        z['formation_closed_ts']=z['retest_ts']
        with self.assertRaisesRegex(ValueError,'Future'):
            build(e,z)

    def test_no_planted_good_results_in_outcomes(self):
        e=pd.DataFrame([event()]);z=pd.DataFrame([level()])
        out,_=build(e,z)
        self.assertNotIn('TP_FIRST',out.columns)
        self.assertEqual(out.iloc[0].level_group,B)

    def test_tpsl_summary_without_missing_in_denominator(self):
        x=pd.DataFrame([{'level_group':A,'outcome_norm':outcome(v)} for v in
                        ['TP_FIRST','SL_FIRST','NO_HIT',None,float('nan'),'AMBIGUOUS']])
        summary=group_summary(x,min_resolved=2)
        r=summary.iloc[0]
        self.assertEqual(r['REACTIONs'],6)
        self.assertEqual(r['SIN_RESULTADO'],2)
        self.assertEqual(r['N resueltos'],2)
        self.assertEqual(r['TP resueltos (%)'],50)

    def test_no_rates_when_fewer_than_30_resolved(self):
        x=pd.DataFrame([{'level_group':B,'outcome_norm':'TP_FIRST'}])
        self.assertTrue(pd.isna(group_summary(x).iloc[0]['TP resueltos (%)']))

    def test_timeframe_filter_does_not_change_event_denominator(self):
        e=pd.DataFrame([event('a'),event('b')]);z=pd.DataFrame([level('a',tf='15m'),level('b',tf='30m')])
        out,z15=build(e,z,'15m')
        self.assertEqual(len(out),2)
        self.assertEqual(len(z15),1)
        self.assertEqual(out.loc[out.event_key=='b','level_group'].iloc[0],C)

    def test_requires_stable_schema(self):
        e=pd.DataFrame([event()]);z=pd.DataFrame([level()]).drop(columns=['known_touch'])
        with self.assertRaisesRegex(ValueError,'columns missing'):
            build(e,z)

    def test_pre_group_independent_of_known_touch(self):
        e=pd.DataFrame([event()])
        a,_=build(e,pd.DataFrame([level(touch=False)]))
        b,_=build(e,pd.DataFrame([level(touch=True)]))
        self.assertEqual(a.iloc[0].pre_bucket,b.iloc[0].pre_bucket)
        self.assertEqual(a.iloc[0].pre_bucket,'PRE_CERCA')


if __name__=='__main__': unittest.main()
