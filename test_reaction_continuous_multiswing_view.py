"""Offline causal regression checks for the Continuous read-only swing viewer."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import sys
from dataclasses import dataclass

ROOT=Path(__file__).resolve().parent
if (ROOT/'engine/live/research/swing_detector.py').is_file():
    sys.path.insert(0,str(ROOT))
else:
    # When tests are installed into the VPS repo root this path is already importable.
    sys.path.insert(0,str(ROOT))
from dashboard.views.reaction_continuous_multiswing_view import (
    confirmed_points, normalized_snapshot, confluence_matches,
    read_frozen_event, assemble_chart_1m, simultaneous_journal_pivots,
)
from engine.live.research.swing_detector import SwingPoint

M=60_000


def pt(tf, ts, price=100, side='HIGH', confirmed=None):
    return SwingPoint(side=side, price=price,pivot_timestamp=ts,
                      confirmed_timestamp=(ts+M if confirmed is None else confirmed),
                      left_bars=2,right_bars=2,prominence_pct=.2)


class MultiSwingCausalTests(unittest.TestCase):
    def test_lookahead_never_counts_unconfirmed_swing(self):
        known=40*M
        data={'1m':[pt('1m',2*M,confirmed=18*M)],
              '5m':[pt('5m',0,confirmed=40*M)],
              '15m':[pt('15m',0,confirmed=15*M)]}
        result=confluence_matches(data,pivot_ts=0,pivot_price=100,side='HIGH',
                                  known_ts=known,time_bucket='15m',tolerance_pct=.2)
        self.assertEqual([x['timeframe'] for x in result],['1m','15m'])

    def test_two_swings_same_15m_bar_and_close_price(self):
        data={'1m':[pt('1m',7*M,100.03)],'5m':[pt('5m',5*M,100.05)],
              '15m':[pt('15m',0,100)]}
        result=confluence_matches(data,pivot_ts=0,pivot_price=100,side='HIGH',
                                  known_ts=60*M,tolerance_pct=.1)
        self.assertEqual(len(result),3)
        result5=confluence_matches(data,pivot_ts=0,pivot_price=100,side='HIGH',
                                   known_ts=60*M,tolerance_pct=.1,time_bucket='5m')
        self.assertEqual(len(result5),1)

    def test_same_candle_not_same_price(self):
        result=confluence_matches({'1m':[pt('1m',M,102)]},pivot_ts=0,
                                  pivot_price=100,side='HIGH',known_ts=20*M,tolerance_pct=.1)
        self.assertEqual(result,[])

    def test_wrong_side_cannot_count(self):
        result=confluence_matches({'5m':[pt('5m',0,100,'LOW')]},pivot_ts=0,
                                  pivot_price=100,side='HIGH',known_ts=40*M)
        self.assertEqual(result,[])

    def test_one_per_timeframe_even_multiple_1m_extremes(self):
        points={'1m':[pt('1m',M,100.01),pt('1m',5*M,100.05)]}
        result=confluence_matches(points,pivot_ts=0,pivot_price=100,side='HIGH',known_ts=30*M)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['pivot_ts'],M)

    def test_strict_closed_frame_no_future_and_no_partial(self):
        records=[{'timestamp':i*M,'open':100,'high':101,'low':99,'close':100}
                 for i in range(12)]
        frame=normalized_snapshot(records,'1m',10*M)
        self.assertEqual(frame.timestamp.astype(int).tolist(),list(range(0,10*M,M)))
        future=records+[{'timestamp':20*M,'open':100,'high':1000,'low':10,'close':999}]
        self.assertEqual(normalized_snapshot(future,'1m',10*M).to_dict('records'),frame.to_dict('records'))

    def test_gap_does_not_make_artificial_swing_neighbors(self):
        stamps=[0,M,2*M,8*M,9*M,10*M]
        highs=[100,102,101,101,102,101]
        rec=[{'timestamp':t,'open':99,'high':h,'low':98,'close':99} for t,h in zip(stamps,highs)]
        _,pts=confirmed_points(rec,'1m',11*M,1)
        # The two peaks are each valid inside their own segment, never bridged.
        self.assertEqual([p.pivot_timestamp for p in pts if p.side=='HIGH'],[M,9*M])

    def test_asof_future_mutation_cannot_change_detected_swings(self):
        rec=[]
        for i in range(24):
            high=100+(9-abs(i-9) if i<=18 else 0)
            rec.append({'timestamp':i*M,'open':99,'high':high,'low':98,'close':99})
        _,one=confirmed_points(rec,'1m',20*M,2)
        other=[dict(x) for x in rec]
        for i in range(20,24):
            other[i]['high']=10000
        _,two=confirmed_points(other,'1m',20*M,2)
        self.assertEqual(one,two)

    def test_original_context_identity_rejects_mismatched_timestamp(self):
        with tempfile.TemporaryDirectory() as temp:
            db=Path(temp)/'reports/reaction_lab/causal_context_v1.sqlite3'
            db.parent.mkdir(parents=True)
            with sqlite3.connect(db) as con:
                con.execute('CREATE TABLE contexts(event_key TEXT,known_ts INTEGER,status TEXT,candles_json TEXT)')
                con.execute('INSERT INTO contexts VALUES(?,?,?,?)',
                           ('X|SHORT|1',100,'READY',json.dumps({'1m':[{'timestamp':0}]})))
            rec,status=read_frozen_event(temp,'X|SHORT|1',101)
            self.assertIsNone(rec);self.assertEqual(status,'KNOWN_TS_MISMATCH')
            rec,status=read_frozen_event(temp,'X|SHORT|1',100)
            self.assertEqual(status,'READY');self.assertEqual(len(rec['1m']),1)

    def test_empty_archive_no_invented_candles(self):
        import pandas as pd
        out=assemble_chart_1m([],pd.DataFrame(),10*M,60,60)
        self.assertTrue(out.empty)

    def test_distinct_original_pivots_same_minute_are_not_collapsed(self):
        with tempfile.TemporaryDirectory() as temp:
            db=Path(temp)/'reports/reaction_lab/scanner_first_touches_v1.sqlite3'
            db.parent.mkdir(parents=True)
            with sqlite3.connect(db) as con:
                con.execute('CREATE TABLE first_touches(touch_key TEXT,symbol TEXT,side TEXT, '
                            'status TEXT,pivot_ts INTEGER,actionable_ts INTEGER, '
                            'retest_ts INTEGER,observation_type TEXT)')
                con.executemany('INSERT INTO first_touches VALUES(?,?,?,?,?,?,?,?)',[
                    ('pivotA','XUSDT','SHORT','REACTION',100,150,600,'LIVE_OBSERVED'),
                    ('pivotB','XUSDT','SHORT','REACTION',200,250,600,'LIVE_OBSERVED'),
                    ('pivotC','XUSDT','LONG','TOUCH_INDECISIVE',100,150,600,'LIVE_OBSERVED'),
                    ('other','XUSDT','SHORT','REACTION',300,350,660,'LIVE_OBSERVED'),
                ])
            hits=simultaneous_journal_pivots(temp,'XUSDT',600)
            self.assertEqual(len(hits),3)
            self.assertEqual(len([x for x in hits if x['side']=='SHORT']),2)
            self.assertEqual(len({x['touch_key'] for x in hits}),3)

    def test_app_hook_only_under_continuous_structure(self):
        app=(ROOT/'dashboard/app.py').read_text(encoding='utf8')
        self.assertIn('if lab_mode == "📍 Event swing structure":\n                from dashboard.views.reaction_continuous_multiswing_view',app)
        self.assertEqual(app.count('render_continuous_multiswing_view('),1)
        self.assertIn('render_cohort_research(',app)
        self.assertIn('chart_builder=build_volume_exhaustion_chart',app)

if __name__=='__main__':unittest.main()
