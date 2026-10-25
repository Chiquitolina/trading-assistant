import unittest
import sys
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parent))
from horizontal_breakout_lab import TF
BASE = (1790000000000 // TF)*TF
from horizontal_level_lifecycle_v2 import Config, _segment_bounds, _pivots_confirmed_now, scan_symbol


def make_bars(n=100,change_at=None, side='HIGH'):
    prices=np.full(n,100.)
    highs=np.full(n,100.18)
    lows=np.full(n,99.82)
    # Two separate confirmed highs AND lows.
    for j in (40,60):
        if j < n:
            highs[j]=101.0
            lows[j]=99.0
    if change_at is not None:
        if side=='HIGH':
            highs[change_at]=102.;prices[change_at]=101.8
        else:
            lows[change_at]=98.;prices[change_at]=98.3
    return pd.DataFrame(dict(timestamp=BASE+np.arange(n,dtype='int64')*TF,
                             open=prices,high=np.maximum(highs,prices),
                             low=np.minimum(lows,prices),close=prices,
                             volume=np.full(n,150.),quoteVolume=np.full(n,15000.)))

class LevelLifecycleV2(unittest.TestCase):
    def test_confirmed_pivot_requires_right_bars(self):
        bars=make_bars()
        cfg=Config()
        self.assertFalse(any(p[2]==40 for p in _pivots_confirmed_now(bars,41,cfg)))
        self.assertTrue(any(p[2]==40 for p in _pivots_confirmed_now(bars,42,cfg)))

    def test_both_support_and_resistance(self):
        f,ev,end,diag=scan_symbol('FAKEUSDT',make_bars())
        self.assertTrue({'LOW','HIGH'}.issubset({a['origin'] for a in f}))
        self.assertEqual(len(f),len(end))
        self.assertTrue(all(x['formation_closed_ts']==BASE+(60+2+1)*TF for x in f))

    def test_prefix_invariance(self):
        b=make_bars(120,change_at=84)
        p1,e1,_,_=scan_symbol('FAKEUSDT',b.iloc[:95])
        b.loc[100:,'high']+=5; b.loc[100:,'close']+=2; b.loc[100:,'open']+=2
        p2,e2,_,_=scan_symbol('FAKEUSDT',b)
        self.assertEqual(p1,[x for x in p2 if x['formation_closed_ts']<=BASE+95*TF])
        self.assertEqual(e1,[x for x in e2 if x['observed_closed_ts']<=BASE+95*TF])

    def test_gap_censors_and_no_pivots_span_gap(self):
        b=make_bars(100)
        b.loc[70:,'timestamp']+=TF
        f,e,end,diag=scan_symbol('FAKEUSDT',b)
        self.assertEqual(diag['gaps30'],1)
        self.assertTrue(any(x['end_state']=='GAP_CENSORED' for x in end))
        self.assertTrue(all(x['formation_closed_ts']<BASE+70*TF for x in f))

    def test_causal_cross_events(self):
        b=make_bars(100,change_at=84)
        f,e,_,_=scan_symbol('FAKEUSDT',b)
        highs=[x for x in e if x['origin']=='HIGH' and x['event_type']=='CROSS_UP']
        self.assertGreater(len(highs),0)
        self.assertTrue(all(x['observed_closed_ts']>BASE+(84)*TF for x in highs))
        self.assertTrue(all(x['level_id'] in {r['level_id'] for r in f} for x in e))

    def test_cross_down_is_symmetric(self):
        b=make_bars(105,change_at=84,side='LOW')
        f,e,_,_=scan_symbol('FAKEUSDT',b)
        down=[x for x in e if x['origin']=='LOW' and x['event_type']=='CROSS_DOWN']
        self.assertTrue(down)
        self.assertTrue(all(x['observed_closed_ts'] == BASE+85*TF for x in down))

    def test_level_is_frozen_after_crossing(self):
        b=make_bars(110,change_at=84)
        f,e,end,_=scan_symbol('FAKEUSDT',b)
        by_id={x['level_id']:x for x in f}
        for event in e:
            level=by_id[event['level_id']]
            self.assertAlmostEqual(event['center'],level['center'])
            self.assertAlmostEqual(event['zone_low'],level['zone_low'])
            self.assertGreater(event['observed_closed_ts'],level['formation_closed_ts'])

    def test_empty_is_insufficient(self):
        b=make_bars(10)
        f,e,end,diag=scan_symbol('FAKEUSDT',b)
        self.assertEqual(f,[])
        self.assertEqual(diag['status'],'INSUFFICIENT_30M')

if __name__=='__main__':unittest.main()
