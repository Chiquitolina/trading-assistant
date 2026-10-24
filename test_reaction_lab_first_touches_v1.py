#!/usr/bin/env python3
"""Offline tests: no Redis, no Binance, no Streamlit, no real DB modifications."""
import ast
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

import pandas as pd
import reaction_lab_first_touches_v1 as journal
import reaction_lab_continuous_scanner as scanner

SOURCE_APP = Path(__file__).resolve().parents[0]/'dashboard/app.py'


def make_row(status='REACTION', side='LONG', retest_ts=None,
             pivot_ts=None, symbol='ETHUSDT'):
    if retest_ts is None:
        retest_ts=(int(time.time()*1000)//60_000 - 3)*60_000
    if pivot_ts is None:
        pivot_ts=retest_ts-10*60_000
    return {
        'symbol':symbol, 'signal':side, 'status':status,
        'timeframe':'15m', 'detector':'3x3',
        'pivot_timestamp':pivot_ts,
        'actionable_timestamp':retest_ts-5*60_000,
        'retest_timestamp':retest_ts,
        'retest_close':3210.5,'swing_price':3215.0,
        'retest_high':3220.0,'retest_low':3205.0,
        'reaction':status=='REACTION',
        'future_mfe_pct':95.0,
    }


@contextmanager
def patched_modules(replacements):
    with patch.dict(sys.modules,replacements):
        yield


class TestFirstTouches(unittest.TestCase):
    def setUp(self):
        self.now=int(time.time()*1000)

    def normalize(self, rows):
        return journal.normalize_first_touches(
            pd.DataFrame(rows),self.now,scanner.CAUSAL_FIELDS,scanner.coerce_jsonable)

    def test_all_three_statuses_without_forward_fields(self):
        rows=[make_row('REACTION',symbol='ETHUSDT'),
              make_row('TOUCH_FAILED',symbol='BTCUSDT'),
              make_row('TOUCH_INDECISIVE',symbol='SOLUSDT')]
        touches=self.normalize(rows)
        self.assertEqual(len(touches),3)
        self.assertEqual(set(t['status'] for t in touches.values()),set(journal.STATUSES))
        self.assertTrue(all('future_mfe_pct' not in t for t in touches.values()))
        self.assertTrue(all(t['known_timestamp']==t['retest_timestamp']+60_000 for t in touches.values()))
        self.assertTrue(all('candidate_v1_event_key' not in t for t in touches.values()))

    def test_unclosed_and_draft_ignored(self):
        last=(self.now//60_000)*60_000
        touches=self.normalize([make_row('TOUCH_FAILED',retest_ts=last),
                                make_row('ARMED_NO_RETEST',symbol='ADAUSDT')])
        self.assertFalse(touches)

    def test_multiple_swings_same_minute_preserved(self):
        a=make_row('TOUCH_FAILED')
        b=make_row('TOUCH_INDECISIVE',pivot_ts=a['pivot_timestamp']-60_000)
        touches=self.normalize([a,b])
        self.assertEqual(len(touches),2)
        self.assertEqual(len(set(t['legacy_event_key'] for t in touches.values())),1)

    def test_bad_timestamp_order_ignored(self):
        a=make_row('TOUCH_FAILED')
        a['actionable_timestamp']=a['retest_timestamp']+60_000
        self.assertEqual(self.normalize([a]),{})

    def test_duplicate_same_touch_not_reinserted(self):
        touch=self.normalize([make_row('TOUCH_FAILED')])
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'touches.sqlite3'
            with journal.db_open(db) as con:
                v=journal.save_cycle(con,touch,self.now,self.now-1000,'abc')
                self.assertEqual(v['new'],1)
                first=con.execute('SELECT first_seen_ms FROM first_touches').fetchone()[0]
                v=journal.save_cycle(con,touch,self.now+60000,self.now+59000,'abc')
                self.assertEqual(v['new'],0)
                self.assertEqual(con.execute('SELECT COUNT(*) FROM first_touches').fetchone()[0],1)
                self.assertEqual(con.execute('SELECT first_seen_ms FROM first_touches').fetchone()[0],first)
                self.assertEqual(journal.status(db)['status_counts']['TOUCH_FAILED'],1)

    def test_status_classification_frozen_and_conflict_audited(self):
        a=make_row('TOUCH_FAILED')
        original=self.normalize([a])
        a['status']='REACTION'
        revised=self.normalize([a])
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'touches.sqlite3'
            con=journal.db_open(db)
            try:
                journal.save_cycle(con,original,self.now,self.now,'old')
                result=journal.save_cycle(con,revised,self.now+1,self.now+1,'new')
                self.assertEqual(result['classification_conflicts'],1)
                self.assertEqual(con.execute('SELECT status FROM first_touches').fetchone()[0],'TOUCH_FAILED')
                self.assertEqual(journal.status(db)['classification_conflicts'],1)
            finally:con.close()

    def test_legacy_scanner_retains_only_reactions(self):
        rows=[make_row('REACTION',symbol='ETHUSDT'),
              make_row('TOUCH_FAILED',symbol='BTCUSDT'),
              make_row('TOUCH_INDECISIVE',symbol='SOLUSDT')]
        reactions=scanner.normalize_reactions(pd.DataFrame(rows),self.now)
        self.assertEqual(len(reactions),1)
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'scanner_events_v1.sqlite3'
            con=scanner.db_open(db)
            try:
                scanner.save_cycle(con,reactions,self.now,self.now-1000,3,[3,3],'hash',self.now-60_000)
                self.assertEqual(con.execute('SELECT COUNT(*) FROM events').fetchone()[0],1)
                self.assertEqual(json.loads(con.execute('SELECT causal_json FROM events').fetchone()[0])['status'],'REACTION')
            finally:con.close()

    def test_status_and_recent_readonly_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'missing.sqlite3'
            self.assertFalse(journal.status(db)['database_exists'])
            self.assertEqual(journal.recent(db,None,10),[])
            self.assertFalse(db.exists())

    def test_same_cycle_conflicting_source_fails_fast(self):
        a=make_row('TOUCH_FAILED')
        b=make_row('REACTION')
        with self.assertRaises(ValueError):
            self.normalize([a,b])

    def test_integrated_scan_persists_both_journals_and_idempotent(self):
        now=self.now
        ts=(now//60_000-5)*60_000
        events=[make_row('REACTION',symbol='ETHUSDT',retest_ts=ts),
                make_row('TOUCH_FAILED',symbol='BTCUSDT',retest_ts=ts),
                make_row('TOUCH_INDECISIVE',symbol='SOLUSDT',retest_ts=ts)]
        def fake_scan(**kwargs):
            for symbol in kwargs['symbols']:
                for timeframe in ('1m','15m'):
                    load_volume_exhaustion_research_candles(
                        symbol=symbol,timeframe=timeframe,limit=400)
            return pd.DataFrame(events)
        class FakeService:
            def __init__(self,redis_client):pass
            def get_closed_candles(self,symbol,timeframe,limit):
                return pd.DataFrame([{'timestamp':ts}])
        geometry=types.ModuleType('dashboard.services.geometry_scanner_data_service')
        geometry.GeometryScannerDataService=FakeService
        data=types.ModuleType('engine.live.data.redis_market_data_protocol')
        data.history_key=lambda *a:None
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            db=root/'scanner_events_v1.sqlite3'
            first=root/'scanner_first_touches_v1.sqlite3'
            with patched_modules({'dashboard.services.geometry_scanner_data_service':geometry,
                                  'engine.live.data.redis_market_data_protocol':data}):
                r=scanner.scan_once(root,object(),fake_scan,'hash',db,
                                   ('ETHUSDT','BTCUSDT','SOLUSDT'),dry=True,
                                   first_touches_db=first)
                self.assertEqual(r['first_touches_seen'],3)
                self.assertFalse(first.exists())
                for expected in (1,0):
                    r=scanner.scan_once(root,object(),fake_scan,'hash',db,
                                        ('ETHUSDT','BTCUSDT','SOLUSDT'),
                                        min_free_mb=0, first_touches_db=first)
                    self.assertEqual(r['new_first_touches'],3*expected)
                    self.assertEqual(r['new_events'],expected)
            self.assertEqual(journal.status(first)['total_touches'],3)
            self.assertEqual(scanner.status(db)['total_events'],1)

    @unittest.skipUnless(SOURCE_APP.is_file(),'Real app.py absent')
    def test_actual_canonical_first_touch_statuses(self):
        class FakeDetector: pass
        swing=types.ModuleType('engine.live.research.swing_detector')
        swing.SwingDetector=FakeDetector
        with patched_modules({'engine.live.research.swing_detector':swing}):
            compiled,_=scanner.compile_canonical_scanner(SOURCE_APP,None)
        detect=compiled.__globals__['_find_confirmed_swing_retest']
        start=1_800_000_000_000
        swing_row={'actionable_timestamp':start,
                   'pivot_timestamp':start-60_000,
                   'pivot_price':100.0, 'signal':'LONG',
                   'timeframe':'15m','detector':'3x3'}
        first={'timestamp':start,'open':100.1,'high':100.35,'low':100.05,'close':100.2}
        candidates={
            'REACTION': {'open':100.16,'high':100.18,'low':99.98,'close':100.12},
            'TOUCH_FAILED': {'open':100.16,'high':100.18,'low':99.70,'close':99.70},
            'TOUCH_INDECISIVE': {'open':100.16,'high':100.18,'low':99.95,'close':100.01},
        }
        for expected,candle in candidates.items():
            rows=[first,{'timestamp':start+60_000,**candle}]
            actual=detect(pd.DataFrame(rows),swing_row,0.10,0.20,360)
            self.assertIsNotNone(actual)
            self.assertEqual(actual['status'],expected)

    @unittest.skipUnless(SOURCE_APP.is_file(),'Real app.py absent')
    def test_app_ast_strips_forward_only_legacy_metrics(self):
        class FakeDetector:pass
        swing=types.ModuleType('engine.live.research.swing_detector')
        swing.SwingDetector=FakeDetector
        with patched_modules({'engine.live.research.swing_detector':swing}):
            compiled,fingerprint=scanner.compile_canonical_scanner(SOURCE_APP,None)
        self.assertEqual(len(fingerprint),20)
        names=compiled.__code__.co_names
        for post in ('_legacy_attach_fail_touch_metrics',
                     '_attach_confirmed_swing_reaction_run',
                     '_attach_confirmed_swing_order_recovery_analysis'):
            self.assertNotIn(post,names)
        self.assertIn('_find_confirmed_swing_retest',names)


if __name__=='__main__':unittest.main()
