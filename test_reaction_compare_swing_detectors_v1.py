import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import pandas as pd
from reaction_compare_swing_detectors_v1 import (
    ONE, FIFTEEN, assert_full_minute_coverage, first_touch_trade_path,
    compare_canonical, match_pivots, find_variant_opportunities,
    evaluate_outcomes, summaries, run, audit,
)
from pre_reaction_opportunity_lab import contiguous_15m_from_1m, iso_ms


class Tests(unittest.TestCase):
    def test_complete_coverage(self):
        ts=iso_ms('2026-10-12T00:00:00Z')
        frame=pd.DataFrame({'timestamp':[ts,ts+ONE,ts+2*ONE]})
        assert_full_minute_coverage(frame,ts,ts+3*ONE,'AAA')
        with self.assertRaises(RuntimeError):assert_full_minute_coverage(frame.iloc[[0,2]],ts,ts+3*ONE,'AAA')

    def test_next_open_and_tp_long(self):
        base=iso_ms('2026-10-12T00:00:00Z')
        rows={base+ONE:{'open':100.,'high':102.2,'low':99.5}}
        self.assertEqual(first_touch_trade_path(rows,'LONG',base,2,1,180)['status'],'TP_FIRST')
        self.assertEqual(first_touch_trade_path(rows,'LONG',base,2,1,180)['entry_ts'],base+ONE)

    def test_short_sl(self):
        base=iso_ms('2026-10-12T00:00:00Z')
        rows={base+ONE:{'open':100.,'high':101.1,'low':99.1}}
        self.assertEqual(first_touch_trade_path(rows,'SHORT',base,2,1,180)['status'],'SL_FIRST')

    def test_both_ambiguous_not_win(self):
        base=iso_ms('2026-10-12T00:00:00Z')
        rows={base+ONE:{'open':100.,'high':102.1,'low':98.9}}
        self.assertEqual(first_touch_trade_path(rows,'LONG',base,2,1,180)['status'],'AMBIGUOUS_BOTH_IN_1M')

    def test_missing_no_entry(self):
        base=iso_ms('2026-10-12T00:00:00Z')
        self.assertEqual(first_touch_trade_path({},'LONG',base,2,1,180)['status'],'CENSORED_NO_ENTRY')

    def test_partial_path_not_no_hit(self):
        base=iso_ms('2026-10-12T00:00:00Z')
        rows={base+ONE:{'open':100.,'high':100.1,'low':99.9}}
        r=first_touch_trade_path(rows,'LONG',base,2,1,180)
        self.assertEqual(r['status'],'CENSORED_GAP_OR_END')
        self.assertEqual(r['bars_observed'],1)

    def test_all_no_hit(self):
        base=iso_ms('2026-10-12T00:00:00Z')
        rows={base+ONE*(1+i):{'open':100.,'high':100.1,'low':99.9} for i in range(180)}
        self.assertEqual(first_touch_trade_path(rows,'LONG',base,2,1,180)['status'],'NO_HIT')

    def test_canonical_exact_and_mismatch(self):
        r={'symbol':'AAA','side':'SHORT','pivot_ts':100,'actionable_ts':200,'status':'REACTION',
            'first_touch_ts':300,'opportunity_key':'AAA|key'}
        c={'symbol':'AAA','signal':'SHORT','pivot_timestamp':100,'actionable_timestamp':200,
            'retest_timestamp':300,'status':'REACTION'}
        parity,extra=compare_canonical([r],[c],3)
        self.assertEqual(parity['AAA|key'],'EXACT_TOUCH');self.assertFalse(extra)
        parity,_=compare_canonical([r],[{**c,'retest_timestamp':301}],3)
        self.assertEqual(parity['AAA|key'],'TOUCH_TS_DIFFERENCE')
        parity,_=compare_canonical([r],[],3)
        self.assertEqual(parity['AAA|key'],'MISSING_CANONICAL_TOUCH')

    def test_canonical_no_touch_uncertain(self):
        r={'symbol':'AAA','side':'SHORT','pivot_ts':100,'actionable_ts':200,'status':'ARMED_NO_RETEST',
            'first_touch_ts':None,'opportunity_key':'AAA|key'}
        p,extra=compare_canonical([r],[],3)
        self.assertEqual(p['AAA|key'],'BOTH_NO_TOUCH_UNRESOLVED')

    def test_matching_confirmation_delay(self):
        rows=[]
        for n in (2,3,5):
            rows.append({'pivot_identity':'AAA|LONG|1000','symbol':'AAA','side':'LONG','pivot_ts':1000,
                'detector':f'{n}x{n}','actionable_ts':1000+n*FIFTEEN+FIFTEEN,
                'status':'REACTION','first_touch_ts':1000+3*FIFTEEN,
                'canonical_parity':'EXACT_TOUCH','opportunity_key':f'AAA|{n}x{n}'})
        matches=match_pivots(rows)
        self.assertEqual(len(matches),1)
        self.assertEqual(matches[0]['confirmation_delay_5_minus_2_minutes'],45)
        self.assertTrue(matches[0]['2x2_touch_before_5x5_action'])

    def test_nested_strict_swings(self):
        from engine.live.research.swing_detector import SwingDetector
        bars=[]
        start=iso_ms('2026-10-12T00:00:00Z')
        highs=[1,2,3,4,5,8,5,4,3,2,1]
        for i,h in enumerate(highs):
            bars.append({'timestamp':start+i*FIFTEEN,'high':float(h),'low':0.5})
        for n in (2,3,5):
            highs_detected=[p for p in SwingDetector(n,n).detect_all(bars) if p.side=='HIGH']
            self.assertEqual(len(highs_detected),1)
            self.assertEqual(highs_detected[0].confirmed_timestamp,start+(5+n)*FIFTEEN)

    def test_plan_no_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'dashboard').mkdir();(root/'dashboard/app.py').write_text('anything')
            (root/'reaction_lab_continuous_scanner.py').write_text('anything')
            run(root,Path('cache'),root/'out',iso_ms('2026-10-12T00:00:00Z'),iso_ms('2026-10-13T00:00:00Z'),['AAA'],'plan',root/'none')
            self.assertFalse((root/'out').exists())


if __name__=='__main__':unittest.main()
