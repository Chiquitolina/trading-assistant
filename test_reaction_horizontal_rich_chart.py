import unittest
from datetime import datetime, timezone, timedelta

import pandas as pd
import plotly.graph_objects as go

from dashboard.views.reaction_horizontal_rich_chart import (
    confirmed_swing_lows, enhance_pre_candles, diagnostic_text,
)


class RichChartFilledCandlesTests(unittest.TestCase):
    def chart(self, tf='30m', lows=None, doji=False):
        interval = 30 if tf=='30m' else 15
        x=pd.date_range('2026-10-22T00:00:00Z',periods=21,freq=f'{interval}min')
        lo=lows or [10,10,10,9,10,10,10,10,10,9.8,10,10,10,10,10,9.9,10,10,10,10,10]
        op=[v+.2 for v in lo]
        cl=[v+.2 if doji else v+.5 for v in lo]
        hi=[max(a,b)+.1 for a,b in zip(op,cl)]
        fig=go.Figure(go.Candlestick(x=x,open=op,high=hi,low=lo,close=cl,name=tf))
        fig.add_trace(go.Scatter(x=[x[-1]+timedelta(minutes=1)],y=[10.2],name='REACTION'))
        return fig,x,op,hi,lo,cl

    def test_30m_filled_bar_width_kept_when_1m_post_added(self):
        fig,x,op,hi,lo,cl=self.chart('30m')
        enhanced,diag=enhance_pre_candles(fig,'30m')
        bodies=next(tr for tr in enhanced.data if tr.type=='bar')
        self.assertEqual(bodies.width, int(30*60_000*.76))
        self.assertEqual(list(bodies.y),[abs(a-b) for a,b in zip(op,cl)])
        self.assertEqual(list(bodies.base),[min(a,b) for a,b in zip(op,cl)])
        self.assertTrue(any(tr.type=='scatter' and tr.name=='REACTION' for tr in enhanced.data))
        self.assertFalse(any(tr.type=='candlestick' and tr.name=='30m' for tr in enhanced.data))
        enhanced.add_trace(go.Candlestick(x=[x[-1]+timedelta(minutes=i) for i in range(1,10)],
                          open=[10]*9,high=[10.5]*9,low=[9.5]*9,close=[10.1]*9,name='1m POST'))
        self.assertEqual(bodies.width, 1_368_000)
        self.assertEqual(enhanced.layout.barmode,'overlay')

    def test_15m_width_kept(self):
        fig,*_=self.chart('15m')
        enhanced,_=enhance_pre_candles(fig,'15m')
        bodies=next(tr for tr in enhanced.data if tr.type=='bar')
        self.assertEqual(bodies.width, 684_000)

    def test_higher_lows_only_confirmed_with_three_future_pre_bars(self):
        lows=[10,10,10,9,10,10,10,10,10,9.5,10,10,10,10,10,10,10,10,10,10,8]
        fig,x,*_=self.chart(lows=lows)
        _, diag=enhance_pre_candles(fig,'30m')
        self.assertEqual(diag['status'],'HIGHER_LOWS')
        self.assertEqual(diag['count'],2)
        self.assertEqual(tuple(x[3:4]),tuple([diag['last_two'][0][0]]))
        self.assertFalse(any(float(y)==8.0 for _,y in diag['last_two']))
        self.assertIn('No confirma un triángulo', diagnostic_text(diag,'30m'))

    def test_no_lookahead_in_markers(self):
        first=[10,10,10,9,10,10,10,10,10,9.5,10,10,10,10,10,10,10,10,10,10,10]
        fig,x,*_=self.chart(lows=first)
        _,a=enhance_pre_candles(fig,'30m')
        fig2,x,*_=self.chart(lows=first[:-1]+[1])
        _,b=enhance_pre_candles(fig2,'30m')
        self.assertEqual(a['last_two'],b['last_two'])

    def test_less_than_two_pre_pivots_is_not_claimed_as_hl(self):
        lo=[10]*21
        fig,*_=self.chart(lows=lo)
        _,diag=enhance_pre_candles(fig,'30m')
        self.assertEqual(diag['status'],'INSUFFICIENT_PIVOTS')

    def test_doji_keeps_true_ohlc_and_visual_tick(self):
        fig,*_=self.chart('30m',doji=True)
        enhanced,_=enhance_pre_candles(fig,'30m')
        bodies=next(tr for tr in enhanced.data if tr.type=='bar')
        self.assertTrue(all(v==0 for v in bodies.y))
        self.assertTrue(any(tr.name=='Doji PRE' for tr in enhanced.data))



class RichChartHighsSupportTests(unittest.TestCase):
    def test_support_marks_lower_highs_in_filled_htf(self):
        x=pd.date_range('2026-10-22T00:00:00Z',periods=22,freq='30min')
        high=[12.0]*22
        high[4]=15.0
        high[12]=14.0
        op=[11.0]*22
        close=[11.1]*22
        lo=[10.0]*22
        f=go.Figure(go.Candlestick(x=x,open=op,high=high,low=lo,close=close,name='30m'))
        enhanced,diag=enhance_pre_candles(f,'30m',zone_role='SOPORTE',visible_hours=24)
        self.assertEqual(diag['status'],'LOWER_HIGHS')
        self.assertIn('máximos descendentes',diagnostic_text(diag,'30m'))
        self.assertTrue(any(t.name=='Máximos PRE 3×3' for t in enhanced.data))

if __name__ == "__main__":
    unittest.main()
