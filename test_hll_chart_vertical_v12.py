import unittest
import pandas as pd
from hll_chart_vertical_v12 import price_axis_window, zone_in_price_window

class ChartVerticalV12(unittest.TestCase):
    def test_price_window_uses_real_extrema_and_padding(self):
        bars=pd.DataFrame({'low':[75500,75200,75700],'high':[76000,76100,76300]})
        lo,hi=price_axis_window(bars)
        self.assertLess(lo,75200)
        self.assertGreater(hi,76300)
        self.assertLess(hi,77000)
    def test_far_zones_do_not_enter_local_view(self):
        viewport=price_axis_window(pd.DataFrame({'low':[75400], 'high':[76200]}))
        self.assertFalse(zone_in_price_window({'low':80500,'high':80800},viewport))
        self.assertTrue(zone_in_price_window({'low':75900,'high':76100},viewport))
    def test_flat_candles_still_have_height(self):
        lo,hi=price_axis_window(pd.DataFrame({'low':[76000], 'high':[76000]}))
        self.assertLess(lo,76000)
        self.assertGreater(hi,76000)
    def test_broken_candles_fail_closed(self):
        with self.assertRaises(ValueError):
            price_axis_window(pd.DataFrame({'low':[0], 'high':[1]}))
        with self.assertRaises(ValueError):
            price_axis_window(pd.DataFrame({'low':[float('nan')], 'high':[1]}))
if __name__=='__main__': unittest.main()
