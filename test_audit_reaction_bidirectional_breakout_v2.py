import unittest
import pandas as pd

from audit_reaction_bidirectional_breakout_v2 import (
    opposite, causal_context, first_opposite_breakout, outcome_at,
    analyze_touch, summarize)

T=60_000


def tape(rows,start=0):
    return pd.DataFrame([{'timestamp':start+i*T,'open':o,'high':h,'low':l,'close':c}
                         for i,(o,h,l,c) in enumerate(rows)])


def mirror(df):
    x=df.copy()
    x['open']=200-df['open']
    x['close']=200-df['close']
    x['high']=200-df['low']
    x['low']=200-df['high']
    return x


def event(side,known=71*T,status='REACTION'):
    return dict(symbol='TESTUSDT',event_key='T|'+side+'|70',status=status,side=side,
                pivot_ts=20*T,retest_ts=known-T,known_ts=known,
                retest_close=99.9 if side=='SHORT' else 100.1,
                swing_price=100.,prominence_pct=.3)


class BidirectionalBreakoutTests(unittest.TestCase):
    def setUp(self):
        self.short=tape([(100,100.08,99.94,100.03)]*70+
            [(100,100.07,99.9,99.95)]+
            [(99.95,100.03,99.85,99.94)]*4+
            [(99.94,100.17,99.92,100.12),
             (100.12,100.29,100.07,100.20)]+
            [(100.20,100.35,100.12,100.22)]*120)
        self.long=mirror(self.short)

    def test_invalid_side_rejected(self):
        with self.assertRaises(ValueError):opposite('INVALID')

    def test_short_up_break_2_closed_bars(self):
        r=first_opposite_breakout(self.short,71*T,100,'SHORT')
        self.assertEqual(r['opposite_breakout_side'],'LONG')
        self.assertTrue(r['opposite_breakout_confirmed'])
        self.assertEqual(r['opposite_breakout_confirm_ts'],77*T)

    def test_long_down_break_2_closed_bars(self):
        r=first_opposite_breakout(self.long,71*T,100,'LONG')
        self.assertEqual(r['opposite_breakout_side'],'SHORT')
        self.assertTrue(r['opposite_breakout_confirmed'])
        self.assertEqual(r['opposite_breakout_confirm_ts'],77*T)

    def test_one_close_alone_never_confirms_either_way(self):
        down=tape([(100,100.05,99.8,99.85),(99.85,100.02,99.80,100.01)]*35)
        up=mirror(down)
        self.assertFalse(first_opposite_breakout(down,0,100,'LONG')['opposite_breakout_confirmed'])
        self.assertFalse(first_opposite_breakout(up,0,100,'SHORT')['opposite_breakout_confirmed'])

    def test_scan_gap_and_tail_censor_both(self):
        x=self.long.copy()
        x.loc[72,'timestamp']+=2
        self.assertEqual(first_opposite_breakout(x,71*T,100,'LONG')['opposite_breakout_scan_coverage'],'GAP')
        y=self.short[self.short.timestamp<90*T]
        self.assertEqual(first_opposite_breakout(y,71*T,100,'SHORT')['opposite_breakout_scan_coverage'],'TAIL')
        self.assertIsNone(first_opposite_breakout(y,71*T,100,'SHORT')['opposite_breakout_confirmed'])

    def test_precontext_changes_never_look_ahead_short_and_long(self):
        for side,df in [('SHORT',self.short),('LONG',self.long)]:
            a=causal_context(df,71*T,100,side)
            x=df.copy()
            x.loc[x.timestamp>=71*T,'high']=99999
            x.loc[x.timestamp>=71*T,'low']=0.01
            x.loc[x.timestamp>=71*T,'close']=99999
            self.assertEqual(a,causal_context(x,71*T,100,side))
            self.assertEqual(a['context_30m_coverage'],'COMPLETE')
            self.assertEqual(a['adverse_pressure_direction'],opposite(side))

    def test_adverse_pressure_mirrors_lower_highs_long(self):
        short_rows=[]
        for i in range(70):
            b=99+i*.015
            short_rows.append((b,b+.04,b-.04,b+.025))
        pos=tape(short_rows)
        neg=mirror(pos)
        s=causal_context(pos,70*T,100,'SHORT')
        l=causal_context(neg,70*T,100,'LONG')
        self.assertTrue(s['pressure_adverse_structure'])
        self.assertTrue(s['pressure_adverse_momentum'])
        self.assertTrue(l['pressure_adverse_structure'])
        self.assertTrue(l['pressure_adverse_momentum'])
        self.assertTrue(l['pressure_two_of_three'])
        self.assertGreater(l['adverse_return_30m_pct'],0)
        self.assertLess(l['return_30m_pct'],0)

    def test_lack_of_context_is_unknown_not_no_pressure(self):
        z=causal_context(self.long,10*T,100,'LONG')
        self.assertIsNone(z['pressure_any'])
        self.assertNotEqual(z['context_30m_coverage'],'COMPLETE')

    def test_same_minute_outcome_collision_not_win(self):
        x=tape([(100,100.60,99.40,100)]*50)
        self.assertEqual(outcome_at(x,0,'LONG',100,30,'x_')['x_race_0_5'],'AMBIGUOUS_1M')
        self.assertEqual(outcome_at(x,0,'SHORT',100,30,'x_')['x_race_0_5'],'AMBIGUOUS_1M')

    def test_short_original_and_long_opposite_dont_relabel(self):
        x=analyze_touch(self.short,event('SHORT'),75*T)
        self.assertEqual(x['original_side'],'SHORT')
        self.assertEqual(x['original_status'],'REACTION')
        self.assertTrue(x['opposite_breakout_confirmed'])
        self.assertEqual(x['opposite_breakout_phase'],'POST_OUTCOME_ONLY')
        self.assertEqual(x['opposite_side'],'LONG')
        self.assertEqual(x['opposite_scenario'],'BULLISH_BREAKOUT')
        self.assertFalse(x['five_min_confirmed_pre'])

    def test_long_original_and_short_opposite_dont_relabel(self):
        x=analyze_touch(self.long,event('LONG'),75*T)
        self.assertEqual(x['original_side'],'LONG')
        self.assertEqual(x['original_status'],'REACTION')
        self.assertTrue(x['opposite_breakout_confirmed'])
        self.assertEqual(x['opposite_breakout_phase'],'POST_OUTCOME_ONLY')
        self.assertEqual(x['opposite_side'],'SHORT')
        self.assertEqual(x['opposite_scenario'],'BEARISH_BREAKDOWN')
        self.assertFalse(x['five_min_confirmed_pre'])

    def test_retest_initial_status_indecisive_stays(self):
        e=event('LONG',status='TOUCH_INDECISIVE')
        x=analyze_touch(self.long,e,75*T)
        self.assertFalse(x['original_reaction'])
        self.assertEqual(x['original_status'],'TOUCH_INDECISIVE')
        self.assertEqual(e['status'],'TOUCH_INDECISIVE')

    def test_pre_cutoff_hard_guard(self):
        with self.assertRaises(ValueError):
            analyze_touch(self.long,event('LONG'),70*T)

    def test_breakout_confirmed_pre_if_before_cutoff(self):
        x=analyze_touch(self.long,event('LONG'),80*T)
        self.assertEqual(x['opposite_breakout_phase'],'PRE')
        self.assertEqual(x['opposite_breakout_confirm_ts'],77*T)

    def test_summary_stratifies_side_and_original_status(self):
        a=analyze_touch(self.long,event('LONG'),80*T)
        b=analyze_touch(self.short,event('SHORT',status='TOUCH_INDECISIVE'),80*T)
        c=analyze_touch(self.short,event('SHORT'),80*T)
        c['retest_ts']-=T
        summary=summarize(pd.DataFrame([a,b,c]))
        all_rows=summary.loc[(summary.cohort=='ALL_FIRST_TOUCH')&(summary.flag=='ALL')]
        self.assertEqual(set(all_rows.original_side),{'LONG','SHORT'})
        self.assertEqual(int(all_rows.first_touch_n.sum()),3)
        orig=summary.loc[(summary.cohort=='ORIGINAL_REACTION')&(summary.flag=='ALL')]
        self.assertEqual(int(orig.first_touch_n.sum()),2)
        self.assertTrue((orig.original_30m_complete==1).all())


if __name__=='__main__':unittest.main()
