#!/usr/bin/env python3
"""Strict V2.1 CROSS_UP compatibility under CSV precision boundaries."""
import tempfile
import unittest
from pathlib import Path
import pandas as pd
from hll_v26_structure_lab import _validate_v21_cross_up


class V26CrossPrecision(unittest.TestCase):
    def test_round_trip_parser_retains_cross_boundary(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'events.csv'
            p.write_text('zone_high,close,previous_relation,close_relation\n'
                         '0.002545000000000003,0.002545000000000004,INSIDE,ABOVE\n')
            default=pd.read_csv(p)
            exact=pd.read_csv(p,float_precision='round_trip')
            self.assertFalse(float(default.iloc[0]['close']) > float(default.iloc[0]['zone_high']))
            self.assertTrue(float(exact.iloc[0]['close']) > float(exact.iloc[0]['zone_high']))

    def test_consistent_cross_up_preserved(self):
        z=0.002545000000000003
        c=0.002545000000000004
        event=dict(previous_relation='INSIDE',close_relation='ABOVE',
                   close=c,zone_low=0.0024,zone_high=z)
        self.assertEqual(_validate_v21_cross_up(
            symbol='XUSDT',level_id='L1',known_ts=100,
            event=event,previous_close=z,close=c,
            zone_low=0.0024,zone_high=z),'INSIDE')

    def test_genuine_mismatch_still_aborts_with_trace(self):
        event=dict(previous_relation='INSIDE',close_relation='ABOVE',
                   close=100.9,zone_low=99,zone_high=101)
        with self.assertRaisesRegex(RuntimeError,r'symbol=XUSDT.*zone_high=101'):
            _validate_v21_cross_up(symbol='XUSDT',level_id='L2',known_ts=200,
                event=event,previous_close=100,close=100.9,
                zone_low=99,zone_high=101)

    def test_changed_level_band_is_not_silently_accepted(self):
        event=dict(previous_relation='INSIDE',close_relation='ABOVE',
                   close=102,zone_low=99,zone_high=100)
        with self.assertRaisesRegex(RuntimeError,r'level_id=L3'):
            _validate_v21_cross_up(symbol='XUSDT',level_id='L3',known_ts=300,
                event=event,previous_close=99.5,close=102,
                zone_low=99,zone_high=101)

    def test_invalid_prior_above_rejected(self):
        event=dict(previous_relation='ABOVE',close_relation='ABOVE',
                   close=102,zone_low=99,zone_high=101)
        with self.assertRaisesRegex(RuntimeError,'v21_prev=ABOVE'):
            _validate_v21_cross_up(symbol='XUSDT',level_id='L4',known_ts=400,
                event=event,previous_close=102,close=102,
                zone_low=99,zone_high=101)


if __name__=='__main__': unittest.main()
