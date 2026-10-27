import unittest
import pandas as pd

try:
    from dashboard.views.hll_v29_continuation_lab import (
        TF, Params, first_continuation, reclaim_continuation,
        events_for_attempt, validate_candles,
    )
except ModuleNotFoundError:
    from hll_v29_continuation_lab import (
        TF, Params, first_continuation, reclaim_continuation,
        events_for_attempt, validate_candles,
    )


def bars(closes, lows=None, highs=None):
    lows=lows or [x-.3 for x in closes]
    highs=highs or [x+.25 for x in closes]
    rows=[]
    for i,close in enumerate(closes):
        o=closes[max(0,i-1)]
        rows.append(dict(timestamp=i*TF,open=o,high=max(highs[i],close,o),
                         low=min(lows[i],close,o),close=close))
    return pd.DataFrame(rows)


class ContinuationLabTests(unittest.TestCase):
    def test_direct_confirms_at_closed_bar_with_follow_through(self):
        # Zone 100-101, ATR 1. Post-break no low touches 101.
        b=bars([100.5,101.2,101.65,102.1,102.5],
               lows=[100.0,101.1,101.30,101.75,102.1],
               highs=[100.6,101.4,101.85,102.3,102.7])
        result=first_continuation(b,2*TF,100,101,1)
        self.assertEqual(result['status'],'CONFIRMED')
        self.assertEqual(result['confirmed_ts'],3*TF)
        self.assertEqual(result['next_open_ts'],3*TF)
        self.assertEqual(result['signal_open_ts'],2*TF)

    def test_future_does_not_change_past_signal(self):
        initial=bars([100.5,101.2,101.65,102.1,102.5],
                  lows=[100,101.1,101.3,101.75,102.1])
        change=initial.copy()
        change.loc[3,'close']=103.0
        change.loc[3,'high']=103.2
        r1=first_continuation(initial,2*TF,100,101,1)
        r2=first_continuation(change,2*TF,100,101,1)
        self.assertEqual(r1['confirmed_ts'],r2['confirmed_ts'])
        self.assertEqual(r1['signal_close'],r2['signal_close'])

    def test_contact_followed_by_momentum_is_confirmable(self):
        b=bars([100.3,101.1,101.6,102.1],lows=[100,101.05,100.95,101.7],
               highs=[100.6,101.4,101.85,102.4])
        r=first_continuation(b,2*TF,100,101,1)
        self.assertEqual(r['status'],'CONFIRMED')
        self.assertTrue(r['contact_before_confirmation'])
        self.assertEqual(r['confirmed_ts'],3*TF)

    def test_previously_confirmed_signal_stays_when_later_retest_arrives(self):
        b=bars([100.3,101.2,101.7,101.9,102],lows=[100,101.1,101.3,100.9,101.7])
        r=first_continuation(b,2*TF,100,101,1)
        self.assertEqual(r['status'],'CONFIRMED')
        self.assertFalse(r['contact_before_confirmation'])

    def test_failure_blocks_direct(self):
        b=bars([100.3,101.1,99.8,101.5],lows=[100,101.02,99.5,101.1])
        r=first_continuation(b,2*TF,100,101,1)
        self.assertEqual(r['status'],'LOST_ZONE_BEFORE_CONTINUATION')

    def test_new_reclaim_after_failed_attempt(self):
        b=bars([100.4,101.2,99.5,100.5,101.3,101.8,102.4],
               lows=[100,101.05,99.1,100.1,101.15,101.4,102],
               highs=[100.8,101.5,100,100.9,101.55,102.05,102.7])
        result,anchor=reclaim_continuation(b,2*TF,3*TF,100,101,1)
        self.assertEqual(anchor,5*TF)
        self.assertEqual(result['status'],'CONFIRMED')
        self.assertEqual(result['confirmed_ts'],6*TF)

    def test_does_not_use_reclaim_if_v26_was_flip_then_lost(self):
        b=bars([100.4,101.2,99.5,100.5,101.3,101.8,102.4],
               lows=[100,101.05,99.1,100.1,101.15,101.4,102])
        r=dict(attempt_id='a',symbol='TEST',level_id='l',pre_pattern_48='ASCENDING_CLEAN',
               v26_status='FLIP_CONFIRMED_LATER_LOST',failed_closed_ts=3*TF,
               breakout_closed_ts=2*TF,zone_low=100,zone_high=101,atr_pre=1)
        ev=events_for_attempt(r,b)
        self.assertEqual(len(ev),2)
        self.assertEqual(ev[1]['status'],'NO_ORIGINAL_FAILURE')

    def test_censored_gap(self):
        b=bars([100.4,101.2,101.65,102.1],lows=[100,101.1,101.3,101.9])
        b.loc[2,'timestamp']=4*TF
        r=first_continuation(b,2*TF,100,101,1)
        self.assertEqual(r['status'],'CENSORED_CONTINUATION')

    def test_missing_or_malformed_ohlc_guard(self):
        b=bars([100.5,101.2])
        b.loc[1,'low']=105
        with self.assertRaises(ValueError):validate_candles(b)

    def test_no_confirmation_without_displacement(self):
        b=bars([100.4,101.1,101.18,101.22,101.24,101.3,101.25,101.2],
               lows=[100.0,101.04,101.05,101.08,101.09,101.1,101.1,101.1])
        result=first_continuation(b,2*TF,100,101,1)
        self.assertNotEqual(result['status'],'CONFIRMED')

if __name__=='__main__':unittest.main()
