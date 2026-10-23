"""Test compact M/W PRE geometry, not just three touches of a price band."""
import sys
import unittest
from pathlib import Path
import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import reaction_runner_triple_top_pre_v1 as m

STEP=900_000
BASE=1_790_000_000_000


def candles(*, side='SHORT',peaks=(110,132,154),valleys=(121,143),
            valley2_override=None,truncate=170):
    rows=[]
    for i in range(truncate):
        if side=='SHORT':
            op,hi,lo,cl=118.,119.,117.,118.
            if i in peaks:hi,cl=121.,118.
            if i in valleys:lo=114. if valley2_override is None or i!=valleys[-1] else valley2_override
        else:
            op,hi,lo,cl=22.,23.,21.,22.
            if i in peaks:lo,cl=20.,22.
            if i in valleys:hi=27. if valley2_override is None or i!=valleys[-1] else valley2_override
        rows.append(dict(timestamp=BASE+i*STEP,open=op,high=hi,low=lo,close=cl))
    return pd.DataFrame(rows)


def event(side='SHORT', price=None):
    if price is None:price=121. if side=='SHORT' else 20.
    return {'event_key':f'ABCUSDT|{side}|{BASE+170*STEP+1}', 'symbol':'ABCUSDT',
            'side':side,'retest_close':price,'retest_ts':BASE+170*STEP+1}

class GeometryV2(unittest.TestCase):
    def test_exact_three_peaks_two_confirmed_valleys(self):
        o=m.classify_pre(event(),candles())
        self.assertEqual(o['pattern_pre'],'TRIPLE_TOP_CONFIRMED_PRE')
        self.assertLess(o['third_confirmation_ts'],o['retest_ts'])
        self.assertLess(o['valley_2_ts'],o['retest_ts'])
        self.assertAlmostEqual(o['neckline_price'],114.)

    def test_inverse_triple_bottom(self):
        o=m.classify_pre(event('LONG'),candles(side='LONG'))
        self.assertEqual(o['pattern_pre'],'TRIPLE_BOTTOM_CONFIRMED_PRE')
        self.assertEqual(o['neckline_price'],27.)

    def test_third_forming_pre(self):
        o=m.classify_pre(event(),candles(peaks=(110,132,168),valleys=(121,151)))
        self.assertEqual(o['pattern_pre'],'TRIPLE_TOP_FORMING_PRE')
        self.assertEqual(o['third_confirmation_ts'],'')

    def test_third_forming_bottom_pre(self):
        o=m.classify_pre(event('LONG'),candles(side='LONG',peaks=(110,132,168),valleys=(121,151)))
        self.assertEqual(o['pattern_pre'],'TRIPLE_BOTTOM_FORMING_PRE')

    def test_no_valleys_not_triple(self):
        x=candles()
        x.loc[x.index.isin([121,143]),'low']=117.
        self.assertEqual(m.classify_pre(event(),x)['pattern_pre'],'NO_PATTERN_PRE')

    def test_different_necklines_not_triple(self):
        self.assertEqual(m.classify_pre(event(),candles(valley2_override=108.))['pattern_pre'],'NO_PATTERN_PRE')

    def test_huge_crash_and_recovery_not_triple(self):
        self.assertEqual(m.classify_pre(event(),candles(valley2_override=99.))['pattern_pre'],'NO_PATTERN_PRE')

    def test_remote_three_touches_not_triple(self):
        self.assertEqual(m.classify_pre(event(),candles(peaks=(35,57,154),valleys=(45,106)))['pattern_pre'],'NO_PATTERN_PRE')

    def test_future_is_ignored(self):
        base=candles()
        future=candles(peaks=(0,),valleys=()).copy()
        future['timestamp']+=170*STEP
        self.assertEqual(m.classify_pre(event(),base),m.classify_pre(event(),pd.concat([base,future],ignore_index=True)))

    def test_third_not_confirmed_by_event_candle(self):
        x=candles(peaks=(110,132,168),valleys=(121,151))
        o=m.classify_pre(event(),x)
        future=pd.DataFrame([{'timestamp':BASE+170*STEP,'open':118.,'high':124.,'low':117.,'close':118.}])
        self.assertEqual(o,m.classify_pre(event(),pd.concat([x,future],ignore_index=True)))

    def test_lookback_gap_unknown(self):
        x=candles().drop(80)
        self.assertEqual(m.classify_pre(event(),x)['pattern_pre'],'UNKNOWN_HISTORY')

    def test_missing_or_invalid_price(self):
        self.assertEqual(m.classify_pre(event(price=float('nan')),candles())['pattern_pre'],'UNKNOWN_PRICE')

    def test_insufficient_history_unknown(self):
        self.assertEqual(m.classify_pre(event(),candles().tail(30))['pattern_pre'],'UNKNOWN_HISTORY')

    def test_version_new_directory(self):
        import inspect
        self.assertIn('geometry_pre_v2',inspect.getsource(m.label_path))

if __name__=='__main__':unittest.main()
