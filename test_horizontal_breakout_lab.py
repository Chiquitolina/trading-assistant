"""Offline causal/gap tests: no Binance, Redis or VPS access."""
import unittest

import pandas as pd

from horizontal_breakout_lab import (Config, TF, M, analyze_symbol, features,
                                     normalize, path_outcome)


EPOCH = 1_790_000_000_000 // TF * TF

def sample_series(total=90, break_bar=63):
    rows=[]
    for i in range(total):
        # Local resistance hit on separated candles, base maintained until breakout.
        high=102 if i>=10 and i%6==0 and i < break_bar else 100.6
        low=98.5 + i*.001
        op=100
        close=100
        if i == break_bar:
            high,close=105,104
        if i > break_bar:
            high,low,op,close=106,102,103,104
        rows.append((EPOCH+i*TF,op,high,low,close,100+i*0.2,10000))
    return pd.DataFrame(rows,columns=['timestamp','open','high','low','close','volume','quoteVolume'])


def minutes(total=100*30):
    rows=[]
    for i in range(total):
        open_=100
        high=100.15
        low=99.95
        close=100
        if i >= 64*30:
            open_,high,low,close=104,107,103.9,106
        rows.append((EPOCH+i*M,open_,high,low,close,100,10000))
    return pd.DataFrame(rows,columns=['timestamp','open','high','low','close','volume','quoteVolume'])


class HorizontalBreakoutTests(unittest.TestCase):
    def test_forming_detector_ignores_future(self):
        data=sample_series()
        cfg=Config(windows=(12,24))
        pre=features(data.iloc[20:44],24,cfg)
        self.assertIsNotNone(pre)
        altered=data.copy()
        altered.loc[63:,'high']=1000
        self.assertEqual(pre,features(altered.iloc[20:44],24,cfg))

    def test_gap_does_not_create_causal_formations(self):
        data=sample_series().iloc[30:54].reset_index(drop=True)
        data=data.drop(index=[10]).reset_index(drop=True)
        self.assertIsNone(features(data,24,Config(windows=(24,))))

    def test_detect_breakout_and_next_minute_fill(self):
        cfg=Config(windows=(12,24),max_wall_distance_atr=5)
        setups,breakouts,outcomes,diag=analyze_symbol('TESTUSDT',sample_series(),minutes(),cfg)
        self.assertGreaterEqual(len(setups),1)
        self.assertGreaterEqual(len(breakouts),1)
        self.assertEqual(len(outcomes),len(breakouts))
        for event,outcome in zip(breakouts,outcomes):
            self.assertGreater(event['breakout_ts'],event['formation_closed_ts'])
            self.assertEqual(outcome['entry_ts'],event['breakout_ts'])
            self.assertEqual(outcome['entry_price'],104)

    def test_1m_gap_is_censored_not_stop(self):
        minute=minutes(500)
        minute=minute[minute.timestamp!=EPOCH+10*M]
        r=path_outcome(minute,EPOCH,102,Config())
        self.assertEqual(r['path_state'],'GAPPED_OR_SHORT')
        self.assertEqual(r['tp2_h180'],'CENSORED')
        self.assertEqual(r['minutes_observed'],10)

    def test_tp_sl_same_minute_is_ambiguous(self):
        minute=minutes(400)
        minute.loc[0,['high','low']]=[104,97]
        r=path_outcome(minute,EPOCH,102,Config())
        self.assertEqual(r['tp2_h180'],'BOTH_SAME_1M')

    def test_first_close_future_does_not_change_pre(self):
        base=sample_series()
        cfg=Config(windows=(12,24))
        a=features(base.iloc[30:54],24,cfg)
        base.loc[54,'high']=150
        b=features(base.iloc[30:54],24,cfg)
        self.assertEqual(a,b)

    def test_earlier_1m_breakout_survives_later_gap(self):
        cfg=Config(windows=(12,24),trigger='1m')
        minute=minutes()
        # Break at the fourth minute of the 30m candle. A missing later
        # minute must not retroactively suppress an earlier observable break.
        t=EPOCH+(63*30+3)*M
        minute.loc[minute.timestamp==t,['open','high','low','close']]=[103,104,102.8,103.5]
        minute=minute[minute.timestamp!=EPOCH+(63*30+15)*M]
        setups,breakouts,outcomes,_=analyze_symbol('TESTUSDT',sample_series(),minute,cfg)
        self.assertTrue(any(x['breakout_ts']==t+M for x in breakouts))

    def test_no_fill_is_not_misclassified_as_sl(self):
        minute=minutes()
        r=path_outcome(minute,EPOCH+3001*M,102,Config())
        self.assertEqual(r['path_state'],'NO_FILL_1M')
        self.assertEqual(r['tp2_h360'],'CENSORED')
        self.assertIsNone(r['net_tp2_h360_pct'])

    def test_invalid_ohlc_fails_closed(self):
        base=sample_series()
        base.loc[3,'high']=90
        with self.assertRaises(ValueError):normalize(base,TF)

    def test_1m_trigger_requires_full_observation(self):
        cfg=Config(windows=(12,24),trigger='1m',max_wall_distance_atr=5)
        truncated=minutes(300)
        setups,breakouts,outcomes,_=analyze_symbol('TESTUSDT',sample_series(),truncated,cfg)
        self.assertEqual(len(breakouts),0)
        self.assertTrue(any(x['end_state']=='UNOBSERVABLE_1M' for x in setups))


if __name__=='__main__':unittest.main()
