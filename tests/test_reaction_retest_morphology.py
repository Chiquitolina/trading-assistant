"""Temporal discipline and symmetry tests for the three morphologies."""
import sys
import unittest
from pathlib import Path
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parent))
from reaction_retest_morphology import calculate_morphology, MORPH_FIELDS
from dashboard.views.reaction_morphology_view import summarize_morphology, compare_dates
MIN=60_000


def path_closes(closes,side='LONG',recent_quote=1000):
    rows=[]
    for i,close in enumerate(closes):
        prev=closes[i-1] if i else close
        q=1000 if i<len(closes)-5 else recent_quote
        rows.append((i*MIN,prev,max(close,prev)+.03,min(close,prev)-.03,close,10,q,12,6,q*.45))
    return rows


class MorphTests(unittest.TestCase):
    def rapid_pre(self):
        prices=[100.0]*180
        for i in range(164,175):
            prices[i]=100 - (i-163)*.025
        for i in range(175,180):
            prices[i]=99.725-(i-174)*.14
        return path_closes(prices)

    def test_fast_pre_and_retest_close(self):
        pre=self.rapid_pre()
        ret=(180*MIN,99.04,99.12,98.55,99.10,12,2000,34,12,600)
        d=calculate_morphology('LONG',pre,ret,swing_level=98.90)
        self.assertEqual(d['pre_morphology'],'FAST_APPROACH')
        self.assertEqual(d['known_morphology'],'FAST_REJECTION')
        self.assertGreater(d['pre_adverse_5m_pct'],0)
        self.assertEqual(d['post_morphology'],'NOT_DEEP_BREAK')

    def test_pre_cannot_see_retest_and_future(self):
        pre=self.rapid_pre()
        x=calculate_morphology('LONG',pre)
        # Huge favorable retest and future do not change a single PRE variable.
        ret=(180*MIN,99,109,98,107,12,100,7,3,80)
        post=[((181+i)*MIN,99,102,98,101,10) for i in range(15)]
        y=calculate_morphology('LONG',pre,ret,swing_level=100.0,post=post)
        for key in MORPH_FIELDS:
            if key.startswith('pre_'):
                self.assertEqual(x[key],y[key],key)

    def test_slow_volume_decay(self):
        closes=[100.0]*180
        # 15m total adverse 0.6%, previous 5m adverse .35%, recent 5m adverse .08%
        for i in range(164,170):closes[i]=100-.02*(i-163)
        for i in range(170,175):closes[i]=99.88-.075*(i-169)
        for i in range(175,180):closes[i]=99.505-.015*(i-174)
        pre=path_closes(closes,recent_quote=200)
        x=calculate_morphology('LONG',pre, (180*MIN,99.43,99.55,99.32,99.51,10,200,10,3,130),swing_level=99)
        self.assertEqual(x['pre_morphology'],'SLOW_FADING')
        self.assertEqual(x['known_morphology'],'SLOW_EXHAUSTION')
        self.assertLess(x['pre_quote_recent5_vs_previous5'],.85)

    def test_deep_break_and_late_recovery_no_lookahead(self):
        pre=path_closes([100.]*180)
        ret=(180*MIN,99.80,99.92,99.25,99.50,100,4000,100,40,2000)
        post=[]
        for i in range(15):
            close=99.5 if i<4 else 100.05
            post.append(((181+i)*MIN,99.5,close+.1,99.4,close))
        x=calculate_morphology('LONG',pre,ret,swing_level=100.,post=post)
        self.assertEqual(x['known_morphology'],'DEEP_BREAK_OUTSIDE')
        self.assertEqual(x['post_reclaim_minute_15m'],5)
        self.assertEqual(x['post_morphology'],'POST_LATE_RECLAIM_3_TO_15M')
        altered=post[:]
        for i in range(4,8):
            altered[i]=((181+i)*MIN,99.5,99.8,99.2,99.5)
        altered[8]=((189)*MIN,99.5,100.2,99.3,100.1)
        y=calculate_morphology('LONG',pre,ret,swing_level=100.,post=altered)
        for k in MORPH_FIELDS:
            if not k.startswith('post_'):
                self.assertEqual(x[k],y[k],k)
        self.assertEqual(y['post_reclaim_minute_15m'],9)

    def test_short_deep_break_and_no_recovery(self):
        pre=path_closes([100.]*180)
        ret=(180*MIN,100.1,100.50,100,100.25,50,3000,50,30,1700)
        post=[((181+i)*MIN,100.3,100.5,100.1,100.2) for i in range(15)]
        x=calculate_morphology('SHORT',pre,ret,swing_level=100.,post=post)
        self.assertEqual(x['known_morphology'],'DEEP_BREAK_OUTSIDE')
        self.assertEqual(x['post_morphology'],'POST_NO_RECLAIM_15M')
        self.assertIsNone(x['post_reclaim_minute_15m'])

    def test_missing_level_cannot_invent_breakout(self):
        pre=path_closes([100.]*180)
        ret=(180*MIN,99.8,100.5,99,99.6,10,100,10,5,50)
        x=calculate_morphology('LONG',pre,ret,swing_level=None)
        self.assertIsNone(x['known_swing_penetration_pct'])
        self.assertNotEqual(x['known_morphology'],'DEEP_BREAK_OUTSIDE')

    def test_gap_blocks_post_label(self):
        pre=path_closes([100.]*180)
        ret=(180*MIN,99.8,100.05,99.4,99.5,10,100,10,5,50)
        post=[((181+i)*MIN,99.8,100.3,99.4,100.1) for i in range(15)]
        post[6]=((181+7)*MIN,99.8,100.3,99.4,100.1)
        x=calculate_morphology('LONG',pre,ret,swing_level=100.,post=post)
        self.assertEqual(x['post_morphology'],'NO_VALID_POST')

    def test_group_denominator_and_small_n(self):
        frame=pd.DataFrame([{'event_key':str(i),'date_utc':'2026-10-13' if i<30 else '2026-10-20',
            'pre_morphology':'FAST_APPROACH' if i<30 else 'OTHER_PRE',
            'outcome':('TP_FIRST' if i%3==0 else 'SL_FIRST') if i%4 else 'NO_HIT'}
            for i in range(60)])
        m=summarize_morphology(frame,'pre_morphology',min_group=20)
        self.assertEqual(int(m.oportunidades.sum()),60)
        self.assertEqual(int(m.resueltos.sum()),45)
        future,_,_=compare_dates(frame,'pre_morphology')
        self.assertTrue(future.empty)
        dup=pd.concat([frame,frame.iloc[:1]],ignore_index=True)
        with self.assertRaises(ValueError):summarize_morphology(dup,'pre_morphology')

if __name__=='__main__':unittest.main()
