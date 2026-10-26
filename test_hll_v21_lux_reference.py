"""Tests for external screenshot comparison, no Streamlit server required."""
import sys
import types
import unittest

import numpy as np
import pandas as pd

# The container can test chart generation without the optional Streamlit server.
try:
    import streamlit  # noqa:F401
except ImportError:
    sys.modules['streamlit'] = types.ModuleType('streamlit')

from horizontal_breakout_lab import TF
from horizontal_level_lifecycle_v21_app import (
    REFERENCE_CASES, reference_clock, active_zones_at_reference, _chart
)


class ExternalReferenceTests(unittest.TestCase):
    def test_reference_close_does_not_leak_intrabar_tree(self):
        at, cutoff = reference_clock(REFERENCE_CASES['TREEUSDT'], 'UTC')
        self.assertEqual(pd.Timestamp(at,unit='ms',tz='UTC').strftime('%H:%M'), '10:50')
        self.assertEqual(pd.Timestamp(cutoff,unit='ms',tz='UTC').strftime('%H:%M'), '10:30')
        at2, cutoff2 = reference_clock(REFERENCE_CASES['INITUSDT'], 'UTC')
        self.assertEqual(at2, cutoff2)

    def test_timezone_is_explicit(self):
        utc, _ = reference_clock(REFERENCE_CASES['INITUSDT'], 'UTC')
        arg, _ = reference_clock(REFERENCE_CASES['INITUSDT'], 'America/Argentina/Buenos_Aires')
        self.assertEqual(arg-utc,3*60*60*1000)

    def test_levels_must_exist_at_reference_and_known_tests_only(self):
        _, cutoff = reference_clock(REFERENCE_CASES['INITUSDT'], 'UTC')
        levels = pd.DataFrame([
            dict(level_id='near', formation_closed_ts=cutoff-TF*4,
                 center=.13045,zone_low=.1301,zone_high=.1308),
            dict(level_id='far', formation_closed_ts=cutoff-TF*8,
                 center=.14,zone_low=.139,zone_high=.141),
            dict(level_id='future', formation_closed_ts=cutoff+TF,
                 center=.1304,zone_low=.1302,zone_high=.1306),
            dict(level_id='expired', formation_closed_ts=cutoff-TF*9,
                 center=.1304,zone_low=.1302,zone_high=.1306),
        ])
        ends = pd.DataFrame([
            dict(level_id='near',end_closed_ts=cutoff+3*TF),
            dict(level_id='far',end_closed_ts=cutoff+3*TF),
            dict(level_id='future',end_closed_ts=cutoff+3*TF),
            dict(level_id='expired',end_closed_ts=cutoff-TF),
        ])
        events = pd.DataFrame([
            dict(level_id='near',observed_closed_ts=cutoff-TF,event_type='TEST_FROM_BELOW'),
            dict(level_id='near',observed_closed_ts=cutoff+TF,event_type='TEST_FROM_BELOW'),
        ])
        out = active_zones_at_reference(levels,ends,events,cutoff,.1304)
        self.assertEqual(out.level_id.tolist(), ['near','far'])
        self.assertEqual(out.tests_known_pre.tolist(), [1,0])
        self.assertEqual(out.iloc[0].gap_price_pct,0.0)

    def test_reference_chart_does_not_show_future_candles(self):
        _, cutoff = reference_clock(REFERENCE_CASES['TREEUSDT'], 'UTC')
        timestamps=np.arange(cutoff-12*TF,cutoff+8*TF,TF,dtype=np.int64)
        df=pd.DataFrame({
            'timestamp':timestamps,'open':np.full(len(timestamps),.0396),
            'high':np.full(len(timestamps),.0401),
            'low':np.full(len(timestamps),.0391),
            'close':np.full(len(timestamps),.0397),
            'volume':np.full(len(timestamps),100.)})
        lv=pd.Series(dict(formation_closed_ts=cutoff-7*TF,
                          first_pivot_open_ts=cutoff-10*TF,
                          second_pivot_open_ts=cutoff-9*TF,
                          center=.0396, zone_low=.0392, zone_high=.04,
                          origin='HIGH'))
        ev=pd.DataFrame([
            dict(observed_closed_ts=cutoff-TF, event_type='CROSS_UP',close=.0398,visit_number=1),
            dict(observed_closed_ts=cutoff+TF, event_type='LOSS_CONFIRMED',close=.038,visit_number=1),
        ])
        ref,_=reference_clock(REFERENCE_CASES['TREEUSDT'],'UTC')
        fig=_chart(df,lv,ev,None,None,'REFERENCIA',12,8,
                   reference_ms=ref,reference_cutoff=cutoff,reference_price=.0396)
        self.assertIsNotNone(fig)
        candle=fig.data[0]
        self.assertLessEqual(max(pd.Timestamp(t).value//1_000_000+TF for t in candle.x),cutoff)
        self.assertTrue(any(t.name=='Entrada de captura (EXTERNA)' for t in fig.data))
        # Future loss is not charted in REFERENCIA view
        self.assertFalse(any(t.name=='Nivel perdido POST' for t in fig.data))


if __name__=='__main__':
    unittest.main()
