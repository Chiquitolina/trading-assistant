import unittest
import pandas as pd
import numpy as np

from horizontal_level_cross_research_v22 import (
    prior_counters, pre_volume, causal_signals_for_symbol,
    simulate_minute_path, stage_for_cross, bins
)
from horizontal_breakout_lab import TF, M


class HorizontalCrossV22(unittest.TestCase):
    def fixture(self):
        t0=1800000000000
        rows=[]
        for i in range(90):
            p=100+0.01*np.sin(i/5)
            rows.append(dict(timestamp=t0+i*TF,open=p,high=p+0.1,low=p-0.1,
                             close=p,volume=100+(i%7),quoteVolume=10000+i*12))
        # Three bars at the end make below, attack and CROSS_UP.
        rows[70].update(open=99.1,high=99.4,low=99,close=99.2)
        rows[71].update(open=99.2,high=100.12,low=99.1,close=99.8,volume=500)
        rows[72].update(open=99.8,high=100.6,low=99.7,close=100.5,volume=500)
        f=pd.DataFrame(rows)
        lv='levABC'
        formed=t0+55*TF
        forms=pd.DataFrame([dict(level_id=lv,symbol='TESTUSDT',origin='HIGH',center=100.,
                zone_low=99.9,zone_high=100.1,formation_closed_ts=formed,
                source_30m='SYNTH')])
        end=pd.DataFrame([dict(level_id=lv,end_closed_ts=t0+89*TF)])
        ev=pd.DataFrame([dict(level_id=lv,event_type='CROSS_UP',observed_closed_ts=t0+73*TF)])
        return f,forms,ev,end

    def test_all_cross_up_preserved_even_if_earlier_crosses_failed(self):
        f,forms,ev,ends=self.fixture()
        t0=int(f.timestamp.iloc[0]);
        ev=pd.concat([ev,pd.DataFrame([dict(level_id='levABC',event_type='CROSS_UP',
                   observed_closed_ts=t0+69*TF),dict(level_id='levABC',
                   event_type='RETURN_INSIDE',observed_closed_ts=t0+70*TF)])],ignore_index=True)
        x=causal_signals_for_symbol('TESTUSDT',f,forms,ev,ends)
        main=[v for v in x if v['stage']=='RESISTANCE_CROSS_UP' and v['known_closed_ts']==t0+73*TF]
        self.assertEqual(len(main),1)
        self.assertEqual(main[0]['prior_cross_up'],1)
        self.assertEqual(main[0]['prior_failed_cross_up'],1)

    def test_current_closed_bar_volume_and_no_future_feature_change(self):
        f,forms,ev,ends=self.fixture()
        x=causal_signals_for_symbol('TESTUSDT',f,forms,ev,ends)
        f2=f.copy()
        f2.loc[75:,'volume']*=1000
        f2.loc[75:,'quoteVolume']*=1000
        y=causal_signals_for_symbol('TESTUSDT',f2,forms,ev,ends)
        a=next(v for v in x if v['stage']=='RESISTANCE_CROSS_UP' and v['known_closed_ts']==int(f.timestamp.iloc[73]))
        b=next(v for v in y if v['event_id']==a['event_id'])
        for col in ('vol_ratio','prior_cross_up','prior_tests','distance_pre_atr','body_atr'):
            self.assertEqual(a[col],b[col])

    def test_event_at_current_time_not_counted_as_previous(self):
        f,forms,ev,ends=self.fixture()
        x=causal_signals_for_symbol('TESTUSDT',f,forms,ev,ends)
        c=next(v for v in x if v['stage']=='RESISTANCE_CROSS_UP')
        self.assertEqual(c['prior_cross_up'],0)

    def test_attack_is_not_confirmed_breakout(self):
        f,forms,ev,ends=self.fixture()
        x=causal_signals_for_symbol('TESTUSDT',f,forms,ev,ends)
        att=[v for v in x if v['stage']=='RESISTANCE_ATTACK_BELOW']
        self.assertTrue(att)
        self.assertTrue(all(a['close_relation']!='ABOVE' for a in att))

    def test_volume_pre_excludes_current(self):
        f,_,_,_=self.fixture()
        before=pre_volume(f,40)
        changed=f.copy();changed.loc[40,'quoteVolume']*=10;changed.loc[40,'volume']*=10
        after=pre_volume(changed,40)
        self.assertEqual(before['vol_pre20_median'],after['vol_pre20_median'])
        self.assertNotEqual(before['vol_ratio'],after['vol_ratio'])

    def test_no_entry_or_gap_censored(self):
        sig=dict(event_id='a',symbol='TESTUSDT',stage='RESISTANCE_CROSS_UP',direction='LONG',known_closed_ts=1800000000000)
        empty=simulate_minute_path(pd.DataFrame(),sig,3,1,1.5,.1)
        self.assertEqual(empty['path_state'],'NO_1M_ARCHIVE')
        t=1800000000000+M
        bars=pd.DataFrame([dict(timestamp=t,open=100.,high=101.,low=99.,close=100.),
                           dict(timestamp=t+M,open=100.,high=101.,low=99.,close=100.),
                           dict(timestamp=t+3*M,open=100.,high=101.,low=99.,close=100.)])
        outcome=simulate_minute_path(bars,sig,3,1,1.5,.1)
        self.assertEqual(outcome['path_state'],'GAP_1M')

    def test_same_minute_tp_sl_ambiguous(self):
        t=1800000000000;sig=dict(event_id='a',symbol='TESTUSDT',stage='RESISTANCE_CROSS_UP',direction='LONG',known_closed_ts=t)
        bars=pd.DataFrame([dict(timestamp=t+(i+1)*M,open=100.,high=103.,low=97.,close=101.) for i in range(3)])
        x=simulate_minute_path(bars,sig,3,1,1.5,.1)
        self.assertEqual(x['outcome'],'BOTH_SAME_1M')
        self.assertEqual(x['return_after_fee_pct'],'')

    def test_symmetric_controls(self):
        self.assertEqual(stage_for_cross('HIGH','CROSS_UP')[0],'RESISTANCE_CROSS_UP')
        self.assertEqual(stage_for_cross('LOW','CROSS_DOWN')[0],'SUPPORT_CROSS_DOWN')
        self.assertEqual(bins(2.1),'>=2')

if __name__=='__main__':unittest.main()
