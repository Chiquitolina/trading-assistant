import unittest
import pandas as pd
from horizontal_breakout_lab import TF, M
import horizontal_level_failed_retest_v16 as r


def bars(cross=105, later=None):
    if later is None:
        later=[(104,100,102),(102,99,97)]
    data=[dict(timestamp=0,open=100,high=108,low=99,close=cross)]
    for i,(op,lo,close) in enumerate(later,1):
        data.append(dict(timestamp=i*TF,open=op,high=max(op,close)+2,low=lo,close=close))
    return pd.DataFrame(data)


def src(top=103,bottom=101):
    return {'signal_closed_ts': TF, 'zone_high':top,'zone_low':bottom}


def minute(open_=100,n=360, tp_at=None, sl_at=None, both=None, close_=100):
    rows=[]
    for i in range(n):
        lo=open_*0.996;hi=open_*1.004
        if tp_at==i:lo=open_*0.96
        if sl_at==i:hi=open_*1.04
        if both==i:lo=open_*0.96;hi=open_*1.04
        rows.append(dict(timestamp=TF+i*M,open=open_,high=hi,low=lo,close=close_))
    return pd.DataFrame(rows)


class FailureResearchTests(unittest.TestCase):
    def test_first_contact_fails_and_short_signal(self):
        result,status=r.find_first_failure(src(),bars(),TF*9)
        self.assertEqual(status,'FAILED_RETEST')
        self.assertEqual(result['signal_closed_ts'],2*TF)
        self.assertEqual(result['location'],'INSIDE_ZONE')

    def test_first_touch_defended_does_not_pick_later_failure(self):
        result,status=r.find_first_failure(src(),bars(later=[(103,100,106),(104,99,98)]),TF*9)
        self.assertIsNone(result)
        self.assertEqual(status,'FIRST_CONTACT_NOT_FAILED')

    def test_no_touch_then_fails(self):
        result,status=r.find_first_failure(src(),bars(later=[(105,104,107),(104,99,100)]),TF*9)
        self.assertEqual(status,'FAILED_RETEST')
        self.assertEqual(result['entry_delay_min'],60)

    def test_first_contact_bearish_above_zone_is_not_a_short(self):
        result,status=r.find_first_failure(src(),bars(later=[(108,100,106)]),TF*9)
        self.assertIsNone(result)
        self.assertEqual(status,'FIRST_CONTACT_NOT_FAILED')

    def test_close_below_lower_zone_has_label(self):
        result,status=r.find_first_failure(src(),bars(later=[(102,97,98)]),TF*9)
        self.assertEqual(result['location'],'BELOW_ZONE_LOW')

    def test_cross_not_above_zone(self):
        _,status=r.find_first_failure(src(),bars(cross=102),TF*9)
        self.assertEqual(status,'CROSS_NOT_ABOVE_ZONE')

    def test_cutoff(self):
        _,status=r.find_first_failure(src(),bars(),TF*2)
        self.assertEqual(status,'CUTOFF')

    def test_retest_expired(self):
        _,status=r.find_first_failure(src(),bars(later=[(105,104,108)]*6),TF*9)
        self.assertEqual(status,'NO_CONTACT_WITHIN_WINDOW')

    def test_short_tp(self):
        x=r.short_trade({'signal_closed_ts':TF},minute(tp_at=4))
        self.assertEqual(x['exit_reason'],'TP_FIRST')
        self.assertGreater(x['net_pct'],1.7)

    def test_short_sl(self):
        x=r.short_trade({'signal_closed_ts':TF},minute(sl_at=2))
        self.assertEqual(x['exit_reason'],'SL_FIRST')
        self.assertLess(x['net_pct'],-1.6)

    def test_same_minute_ambiguity_conservative(self):
        x=r.short_trade({'signal_closed_ts':TF},minute(both=2))
        self.assertEqual(x['exit_reason'],'SL_FIRST')
        self.assertTrue(x['both_same_minute'])

    def test_timeout_and_slippage(self):
        x=r.short_trade({'signal_closed_ts':TF},minute(close_=100))
        self.assertEqual(x['exit_reason'],'TIMEOUT')
        self.assertLess(x['net_pct'],-0.19)

    def test_no_data_gap(self):
        m=minute(n=360)
        m.loc[200,'timestamp']+=M*2
        self.assertIsNone(r.short_trade({'signal_closed_ts':TF},m))

    def test_portfolio_one_slot(self):
        base=[dict(signal_closed_ts=TF,exit_ts=TF+60*M,net_pct=2,
                   symbol='A',attempt_id='x',exit_reason='TP_FIRST'),
              dict(signal_closed_ts=TF,exit_ts=TF+60*M,net_pct=-1.5,
                   symbol='B',attempt_id='y',exit_reason='SL_FIRST'),
              dict(signal_closed_ts=TF+60*M,exit_ts=TF+120*M,net_pct=-1.5,
                   symbol='C',attempt_id='z',exit_reason='SL_FIRST')]
        p,ledger=r.replay_short_portfolio(pd.DataFrame(base))
        self.assertEqual(p['trades'],2)
        self.assertEqual(p['skipped_slot_busy'],1)
        self.assertEqual([x['symbol'] for x in ledger],['A','C'])

if __name__=='__main__':unittest.main()
