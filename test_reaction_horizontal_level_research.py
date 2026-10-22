import sys
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from reaction_horizontal_level_research import (
    A,B,C,UNKNOWN,build,important,outcome,group_summary,load_retest_candles,
    candle_zone_gap,valid_retest_candle,MODEL_VERSION,
)


def event(key='e1',complete=True,price=100,low=None,high=None):
    lo=price-.2 if low is None else low
    hi=price+.2 if high is None else high
    return dict(event_key=key,all_covered=int(complete),retest_ts=60_000_000,
                symbol='TESTUSDT',side='LONG',date_utc='2026-10-21',
                known_open=price,known_high=hi,known_low=lo,known_close=price)


def level(key='e1',name='z1',*,touch=False,near=.1,episodes=1,
          pivots=2,tf='15m',bounds=(99.8,100.2),atr=1):
    return dict(event_key=key,zone_id=name,pivot_count=pivots,
        defense_episodes_pre=episodes,formation_closed_ts=10_000_000,
        retest_ts=60_000_000,timeframe=tf,gap_to_zone_atr=near,
        known_touch=touch,zone_low=bounds[0],zone_high=bounds[1],
        atr_pre=atr,pre_last_close=100.0,level_state_pre='ACCEPTED_DEFENDED')


class HorizontalLevelResearchVerifiedTouchTests(unittest.TestCase):
    def test_verified_actual_overlap_a_ignores_atlas_false(self):
        e=pd.DataFrame([event()]);z=pd.DataFrame([level(touch=False)])
        out,_=build(e,z)
        self.assertEqual(out.iloc[0].level_group,A)
        self.assertTrue(out.iloc[0].known_touch_verified)
        self.assertEqual(out.iloc[0].relevant_retest_gap_atr,0)

    def test_short_distant_support_false_touch_4_6atr_is_c(self):
        e=pd.DataFrame([event(price=.0225,low=.02248,high=.02255)])
        e.loc[0,'side']='SHORT'
        z=pd.DataFrame([level(touch=True,bounds=(.021206,.021334),atr=.00025,near=4.624)])
        out,_=build(e,z)
        self.assertEqual(out.iloc[0].level_group,C)
        self.assertIsNone(out.iloc[0].relevant_zone_id)
        self.assertFalse(out.iloc[0].known_touch_verified)

    def test_short_distant_support_7atr_is_not_touch(self):
        e=pd.DataFrame([event(price=1.29,low=1.288,high=1.291)])
        e.loc[0,'side']='SHORT'
        out,_=build(e,pd.DataFrame([level(touch=True,bounds=(1.2407,1.2463),atr=.006,near=7.2)]))
        self.assertEqual(out.iloc[0].level_group,C)

    def test_long_distant_resistance_is_not_touch(self):
        e=pd.DataFrame([event(price=.047,low=.0468,high=.0472)])
        out,_=build(e,pd.DataFrame([level(touch=True,bounds=(.063,.064),atr=.0004,near=40)]))
        self.assertEqual(out.iloc[0].level_group,C)

    def test_real_near_without_touch_b_even_if_pre_far(self):
        e=pd.DataFrame([event(price=100,low=99.8,high=100.2)])
        out,_=build(e,pd.DataFrame([level(bounds=(100.3,100.5),atr=1,near=2,touch=True)]))
        self.assertEqual(out.iloc[0].level_group,B)
        self.assertEqual(out.iloc[0].pre_bucket,'PRE_LEJOS_O_SIN_NIVEL')
        self.assertAlmostEqual(out.iloc[0].relevant_retest_gap_atr,.1)

    def test_not_near_at_1m_no_group_b_even_if_pre_near(self):
        e=pd.DataFrame([event(price=100)])
        out,_=build(e,pd.DataFrame([level(bounds=(102,103),atr=1,near=.1,touch=True)]))
        self.assertEqual(out.iloc[0].level_group,C)
        self.assertEqual(out.iloc[0].pre_bucket,'PRE_CERCA')

    def test_missing_retest_is_unknown_and_still_pre_near(self):
        x=event();x['known_low']=float('nan');
        out,_=build(pd.DataFrame([x]),pd.DataFrame([level(touch=True)]))
        self.assertEqual(out.iloc[0].level_group,UNKNOWN)
        self.assertEqual(out.iloc[0].pre_bucket,'PRE_CERCA')

    def test_bad_retest_high_low_fails_closed(self):
        x=event();x['known_high']=99.0
        out,_=build(pd.DataFrame([x]),pd.DataFrame([level(touch=True)]))
        self.assertEqual(out.iloc[0].level_group,UNKNOWN)

    def test_missing_atr_no_touch_is_unknown(self):
        out,_=build(pd.DataFrame([event()]),pd.DataFrame([level(bounds=(102,103),atr=None)]))
        self.assertEqual(out.iloc[0].level_group,UNKNOWN)

    def test_no_levels_and_complete_c(self):
        e=pd.DataFrame([event('a'),event('b',complete=False)])
        out,_=build(e,pd.DataFrame([level('other')]))
        self.assertEqual(list(out.level_group),[C,UNKNOWN])

    def test_secondary_zone_real_touch_is_selected(self):
        e=pd.DataFrame([event()])
        z=pd.DataFrame([level(name='far',touch=True,episodes=8,bounds=(105,106),atr=1),
                        level(name='near',touch=False,episodes=1)])
        out,_=build(e,z)
        self.assertEqual(out.iloc[0].level_group,A)
        self.assertEqual(out.iloc[0].relevant_zone_id,'near')

    def test_no_future_to_choose_importance(self):
        z=level()
        self.assertTrue(important(z))
        z['formation_closed_ts']=z['retest_ts']
        self.assertFalse(important(z))
        with self.assertRaisesRegex(ValueError,'Future'):
            build(pd.DataFrame([event()]),pd.DataFrame([z]))

    def test_missing_1m_fields_raise_error(self):
        e=pd.DataFrame([event()]).drop(columns=['known_high'])
        with self.assertRaisesRegex(ValueError,'columns missing'):
            build(e,pd.DataFrame([level()]))

    def test_same_known_candle_long_short_has_same_overlap(self):
        a=event('long');b=event('short');b['side']='SHORT'
        aout,_=build(pd.DataFrame([a,b]),pd.DataFrame([level('long'),level('short')]))
        self.assertEqual(list(aout.level_group),[A,A])

    def test_no_outcome_in_research_group(self):
        e=pd.DataFrame([event()]);z=pd.DataFrame([level()])
        first,_=build(e,z)
        e['outcome_norm']='SL_FIRST';z['known_hold']=False
        second,_=build(e,z)
        self.assertEqual(list(first.level_group),list(second.level_group))

    def test_null_outcomes_not_losses(self):
        x=pd.DataFrame([{'level_group':A,'outcome_norm':outcome(v)} for v in
                        ['TP_FIRST','SL_FIRST','NO_HIT',None,float('nan'),'AMBIGUOUS']])
        s=group_summary(x,min_resolved=2).iloc[0]
        self.assertEqual(s['SIN_RESULTADO'],2)
        self.assertEqual(s['N resueltos'],2)
        self.assertEqual(s['TP resueltos (%)'],50)

    def test_sqlite_readonly_exact_1m_candle(self):
        with tempfile.TemporaryDirectory() as root:
            db=Path(root)/'reports/reaction_lab/reaction_full_context_v1.sqlite3'
            db.parent.mkdir(parents=True)
            with sqlite3.connect(db) as c:
                c.execute('CREATE TABLE candles (symbol TEXT,timeframe TEXT,open_ts INTEGER,open REAL,high REAL,low REAL,close REAL)')
                c.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?)',('TESTUSDT','1m',60000000,100,101,99,100.5))
                c.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?)',('TESTUSDT','1m',60060000,101,109,95,101))
            fake=types.ModuleType('reaction_structural_level_atlas_v2')
            fake.v1=types.SimpleNamespace(lineage=types.SimpleNamespace(ROOT_REL='reports/reaction_lab',CTX_NAME='reaction_full_context_v1.sqlite3'))
            with patch.dict(sys.modules,{'reaction_structural_level_atlas_v2':fake}):
                got=load_retest_candles(root,pd.DataFrame([event('x')]))
            self.assertEqual(got.iloc[0].known_high,101)
            self.assertEqual(got.iloc[0].known_low,99)
            with sqlite3.connect(db) as c:
                self.assertEqual(c.execute('SELECT COUNT(*) FROM candles').fetchone()[0],2)

if __name__=='__main__':unittest.main()
