import unittest
import sys
from pathlib import Path
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parent))
import reaction_runner_triple_top_pre_v1 as m

STEP=900_000
BASE=1_790_000_000_000


def candles(peaks=(111,133,155), side='SHORT'):
    rows=[]
    for i in range(170):
        if side=='SHORT':
            o,hi,lo,cl=118.2,119.0,117.4,118.3
            if i in peaks:hi,cl=120.0,118.2
        else:
            o,hi,lo,cl=21.8,22.6,21.0,21.7
            if i in peaks:lo,cl=20.0,21.8
        rows.append(dict(timestamp=BASE+i*STEP,open=o,high=hi,low=lo,close=cl))
    return pd.DataFrame(rows)


def event(side='SHORT',p=120.0, ts=BASE+170*STEP+1):
    return {'event_key':f'XUSDT|{side}|{ts}','symbol':'XUSDT','side':side,
            'retest_ts':ts,'retest_close':p}


class TripleTopTests(unittest.TestCase):
    def test_confirmed_top(self):
        out=m.classify_pre(event(),candles())
        self.assertEqual(out['pattern_pre'],'TRIPLE_TOP_CONFIRMED_PRE')
        self.assertLess(out['third_confirmation_ts'],out['retest_ts'])
        self.assertLess(out['top_bottom_3_ts'],out['retest_ts'])

    def test_confirmed_bottom(self):
        out=m.classify_pre(event('LONG',20.),candles(side='LONG'))
        self.assertEqual(out['pattern_pre'],'TRIPLE_BOTTOM_CONFIRMED_PRE')

    def test_third_touch_unconfirmed_short(self):
        out=m.classify_pre(event(),candles(peaks=(110,135,168)))
        self.assertEqual(out['pattern_pre'],'TRIPLE_TOP_FORMING_PRE')
        self.assertEqual(out['third_confirmation_ts'],'')

    def test_third_touch_unconfirmed_long(self):
        out=m.classify_pre(event('LONG',20.),candles(peaks=(110,135,168),side='LONG'))
        self.assertEqual(out['pattern_pre'],'TRIPLE_BOTTOM_FORMING_PRE')

    def test_only_two_tops(self):
        self.assertEqual(m.classify_pre(event(),candles(peaks=(110,135)))['pattern_pre'],'NO_PATTERN_PRE')

    def test_far_from_reference_level(self):
        self.assertEqual(m.classify_pre(event(p=110.),candles())['pattern_pre'],'NO_PATTERN_PRE')

    def test_future_never_changes_classification(self):
        df=candles()
        first=m.classify_pre(event(),df)
        future=candles(peaks=(160,),side='SHORT')
        future['timestamp']+=170*STEP
        second=m.classify_pre(event(),pd.concat([df,future],ignore_index=True))
        self.assertEqual(first,second)

    def test_gap_unknown_not_negative(self):
        df=candles().drop(index=[75])
        self.assertEqual(m.classify_pre(event(),df)['pattern_pre'],'UNKNOWN_HISTORY')

    def test_not_enough_history(self):
        self.assertEqual(m.classify_pre(event(),candles().tail(45))['pattern_pre'],'UNKNOWN_HISTORY')

    def test_at_boundary_candle_not_used(self):
        df=candles(peaks=(110,135))
        c=dict(timestamp=BASE+170*STEP,open=118.3,high=120.0,low=117.4,close=118.2)
        with_future=pd.concat([df,pd.DataFrame([c])],ignore_index=True)
        self.assertEqual(m.classify_pre(event(),df)['pattern_pre'],
                         m.classify_pre(event(),with_future)['pattern_pre'])

    def test_invalid_price_unknown(self):
        self.assertEqual(m.classify_pre(event(p=None),candles())['pattern_pre'],'UNKNOWN_PRICE')

    def test_pivot_requires_three_right_confirmations(self):
        df=candles(peaks=(110,135,168))
        s=m.classify_pre(event(),df)
        self.assertEqual(s['pattern_pre'],'TRIPLE_TOP_FORMING_PRE')

if __name__=='__main__':unittest.main()
