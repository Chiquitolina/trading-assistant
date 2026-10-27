"""V2.9.1 synthetic regression: causal trendline diagnostics + POST visibility."""
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from dashboard.views.hll_v291_structure_integrity import (
        TF, trendline_break, audit_cohort, BROKEN,
    )
    from dashboard.views.hll_v282_pre_followup_view import (
        make_chart, filtered, load_v291_audit,
    )
except ModuleNotFoundError:
    from hll_v291_structure_integrity import TF, trendline_break, audit_cohort, BROKEN
    from hll_v282_pre_followup_view import make_chart, filtered, load_v291_audit


def make_bars():
    idx=np.arange(160)
    return pd.DataFrame(dict(timestamp=idx * TF,
        open=np.full(160, 105.), high=np.full(160, 106.),
        low=np.full(160, 99.), close=np.full(160, 105.)))


def make_pivots():
    return json.dumps([dict(open_ts=i*TF,low=v,known_closed_ts=(i+3)*TF)
                       for i,v in [(5,100.),(15,101.),(25,102.)]])


class TrendlineAuditTest(unittest.TestCase):
    def test_two_confirmed_closes_break_line(self):
        b=make_bars(); b.loc[40:41,'close']=102.
        result=trendline_break(b,make_pivots(),50*TF,48,1.)
        self.assertTrue(result['broken'])
        self.assertEqual(result['broken_closed_ts'],42*TF)
        self.assertEqual(result['anchor_confirmed_ts'],28*TF)
        self.assertGreater(result['break_depth_atr'],.35)

    def test_one_wick_does_not_break_line(self):
        b=make_bars();b.loc[40:41,'low']=90.
        r=trendline_break(b,make_pivots(),50*TF,48,1.)
        self.assertFalse(r['broken'])
        self.assertIsNotNone(r['anchor_first_ts'])

    def test_one_close_below_line_does_not_break(self):
        b=make_bars();b.loc[40,'close']=102.
        r=trendline_break(b,make_pivots(),50*TF,48,1.)
        self.assertFalse(r['broken'])

    def test_future_prices_cannot_change_pre_flag(self):
        b=make_bars();b.loc[40:41,'close']=102.
        r=trendline_break(b,make_pivots(),50*TF,48,1.)
        changed=b.copy()
        changed.loc[50:,'close']=50.
        self.assertEqual(r,trendline_break(changed,make_pivots(),50*TF,48,1.))

    def test_unconfirmed_pivot_not_used(self):
        pivots=json.loads(make_pivots())
        pivots[2]['known_closed_ts']=51*TF
        b=make_bars();b.loc[40:41,'close']=102.
        r=trendline_break(b,json.dumps(pivots),50*TF,48,1.)
        self.assertFalse(r['broken'])

    def test_incomplete_history_no_false_break(self):
        b=make_bars().drop(index=30)
        r=trendline_break(b,make_pivots(),50*TF,48,1.)
        self.assertFalse(r['broken'])
        self.assertIsNone(r['anchor_first_ts'])

    def test_only_clean_a_is_reclassified(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp); cd=base/'candles';cd.mkdir()
            b=make_bars();b.loc[40:41,'close']=102.
            b.to_csv(cd/'TEST_30m.csv',index=False)
            common=dict(symbol='TEST',pre_closed_ts=50*TF,atr_pre=1.,
                        **{f'pre{w}_pivot_lows_json':make_pivots() for w in (24,48,96)},
                        **{f'pre_{w}_coverage':True for w in (24,48,96)})
            rows=[dict(attempt_id='a',**common,
                       **{f'pre_pattern_{w}':'ASCENDING_CLEAN' for w in (24,48,96)}),
                  dict(attempt_id='b',**common,
                       **{f'pre_pattern_{w}':'ASCENDING_CHOPPY' for w in (24,48,96)})]
            tax=pd.DataFrame(rows)
            original=tax.copy(deep=True)
            audited=audit_cohort(tax,cd)
            revised=tax.merge(audited,on='attempt_id',validate='one_to_one')
            self.assertEqual(revised.loc[0,'pre_pattern_v291_48'],BROKEN)
            self.assertEqual(revised.loc[1,'pre_pattern_v291_48'],'ASCENDING_CHOPPY')
            self.assertEqual(revised.loc[0,'pre_pattern_48'],'ASCENDING_CLEAN')
            self.assertTrue(tax.equals(original))
            self.assertEqual(len(filtered(revised,48,pattern=BROKEN,
                pattern_field='pre_pattern_v291_48')),1)
            self.assertEqual(len(filtered(revised,48,pattern='ASCENDING_CLEAN')),1)

    def test_chart_post_minimum_window_and_no_fake_bars(self):
        b=make_bars()
        record=pd.Series(dict(pre_closed_ts=50*TF,breakout_closed_ts=51*TF,
           flip_confirmed_closed_ts=np.nan,retest_closed_ts=np.nan,
           failed_closed_ts=np.nan,zone_low=100,zone_high=101,
           v26_status='DIRECT_NO_RETEST_24B',pre48_pivot_lows_json='[]'))
        short=make_chart(record,b,48,show_post=True,post_bars=24)
        long=make_chart(record,b,48,show_post=True,post_bars=96)
        self.assertGreater(len(long.data[0].x),len(short.data[0].x))
        self.assertLessEqual(max(int(pd.Timestamp(t).timestamp()*1000)
                                 for t in long.data[0].x),b.timestamp.max())
        with self.assertRaises(ValueError):
            make_chart(record,b,48,show_post=True,post_bars=999)

    def test_trendline_label_and_marker_on_chart(self):
        b=make_bars()
        record=pd.Series(dict(pre_closed_ts=50*TF,breakout_closed_ts=51*TF,
           flip_confirmed_closed_ts=np.nan,retest_closed_ts=np.nan,
           failed_closed_ts=np.nan,zone_low=100,zone_high=101,
           v26_status='DIRECT_NO_RETEST_24B',pre48_pivot_lows_json='[]',
           trendline48_anchor_first_ts=5*TF,trendline48_anchor_last_ts=25*TF,
           trendline48_line_price_first=100,trendline48_line_price_at_pre=104.4,
           trendline48_broken_closed_ts=42*TF))
        fig=make_chart(record,b,48,show_post=False)
        names=[tr.name for tr in fig.data]
        self.assertIn('Directriz PRE (3 pivotes confirmados)',names)
        self.assertIn('Directriz rota en PRE (2 cierres)',names)
        self.assertFalse(any('FLIP confirmado' in str(x) for x in names))

if __name__=='__main__':unittest.main()
