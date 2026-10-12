"""Offline integration tests: canonical scanner, SQLite journal, collector, UI bridge."""
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd

sys.path.insert(0,str(Path(__file__).parent))
import reaction_lab_continuous_scanner as s
import reaction_lab_auto_paths as paths
import reaction_lab_scan_bridge as bridge

class FakeDetector:
    def __init__(self,*args,**kwargs):pass
    def detect_all(self, rows):
        if len(rows)<3:return []
        # A confirmed structural LOW, with causal actionable timestamp.
        return [types.SimpleNamespace(
            pivot_timestamp=int(rows[0]['timestamp']),
            confirmed_timestamp=int(rows[1]['timestamp']),
            side='LOW', price=100.,prominence_pct=0.0,
        )]

class RedisFake:
    def __init__(self, items):self.items=items
    def lrange(self,key,start,end):return self.items.get(key,[])[start:] if start<0 else self.items.get(key,[])[start:end+1]

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        root=Path(__file__).parent
        self.src=(root/'dashboard/app.py' if (root/'dashboard/app.py').exists()
                  else root/'app.py')
        self.db=self.root/'reports/reaction_lab/scanner_events_v1.sqlite3'
        self.pathdb=self.root/'reports/reaction_lab/auto_paths_v1.sqlite3'
        self.ts=1_800_000_000_000

    def loader(self,symbol,timeframe,limit):
        t=self.ts
        if timeframe=='15m':
            return pd.DataFrame([
                dict(timestamp=t-1_800_000,open=100,high=101,low=99,close=100,volume=10),
                dict(timestamp=t-900_000,open=101,high=103,low=101,close=102,volume=10),
                dict(timestamp=t,open=102,high=103,low=101,close=102,volume=10),
            ])
        if timeframe=='1m':
            base=t
            bars=[]
            for i in range(12):
                if i==5:
                    o,hi,lo,cl=100.15,100.25,99.95,100.2
                else:
                    o,hi,lo,cl=101.5,102.5,101.0,102.0
                bars.append(dict(timestamp=base+i*60_000,open=o,high=hi,low=lo,close=cl,volume=10))
            return pd.DataFrame(bars)
        return pd.DataFrame()

    def compiler(self):
        # Only testing the canonical extraction; fake detector is used in tests.
        fake=types.ModuleType('engine.live.research.swing_detector')
        fake.SwingDetector=FakeDetector
        with patch.dict(sys.modules,{'engine':types.ModuleType('engine'),
             'engine.live':types.ModuleType('engine.live'),
             'engine.live.research':types.ModuleType('engine.live.research'),
             'engine.live.research.swing_detector':fake}):
            return s.compile_canonical_scanner(self.src,self.loader)

    def test_canonical_reaction_and_close_gating(self):
        scan,fingerprint=self.compiler()
        result=scan(symbols=['BTCUSDT'],swing_timeframes=('15m',),
            swing_detector_items=(('15m','3x3'),),min_swing_prominence_pct=0,
            retest_tolerance_pct=.1,min_departure_pct=.2,
            max_age_minutes=360,max_retest_age_minutes=4320)
        self.assertFalse(result.empty)
        events=s.normalize_reactions(result,self.ts+8*60_000)
        self.assertEqual(len(events),1)
        event=list(events.values())[0]
        self.assertEqual(event['signal'],'LONG')
        self.assertEqual(event['retest_timestamp'],self.ts+5*60_000)
        self.assertFalse(any('future' in k or 'outcome' in k or 'first_touch' in k for k in event))
        before=s.normalize_reactions(result,self.ts+5*60_000)
        self.assertEqual(before,{})

    def test_event_journal_and_collector_dedup_and_bridge(self):
        key=f'BTCUSDT|LONG|{self.ts-60_000}'
        payload={'symbol':'BTCUSDT','signal':'LONG','retest_timestamp':self.ts-60_000,
                 'status':'REACTION','candidate_v1_event_key':key,
                 'candidate_v2_event_key':key}
        con=s.db_open(self.db)
        try:
            first=s.save_cycle(con,{key:payload},self.ts,self.ts-2000,1,[1,1],'testhash',self.ts-60_000)
            again=s.save_cycle(con,{key:payload},self.ts+60_000,self.ts,1,[1,1],'testhash',self.ts)
        finally:con.close()
        self.assertEqual((first,again),(1,0))
        self.assertEqual(len(bridge.scan_rows(self.db)),1)
        own=pd.DataFrame([dict(payload,retest_close=100)])
        mixed=bridge.merge_scanner_reactions(own,self.root,symbols=['BTCUSDT'],
            timeframe='15m',detector='3x3',min_prominence=0,tolerance=.1,departure=.2,max_age=360,
            now_ms=self.ts+60_000)
        self.assertEqual(len(mixed),1)
        self.assertEqual(float(mixed.iloc[0]['retest_close']),100.)
        found,memberships,_=paths.read_sources(self.root)
        self.assertEqual(set(found),{key})
        self.assertEqual(memberships[key],{'continuous_scan':'FORWARD'})
        candles=[]
        for i in range(360):
            ts=self.ts+i*60_000
            candles.append(json.dumps({'timestamp':ts,'close_timestamp':ts+59999,
              'open':100.,'high':101.,'low':99.,'close':100.5}))
        redis=RedisFake({'market-data:v1:history:BTCUSDT:1m':candles})
        cycle=paths.run_once(self.root,self.pathdb,redis,self.ts+360*60_000,0)
        self.assertEqual(cycle['registered']['new'],1)
        self.assertEqual(cycle['database']['saved_bars'],360)
        merged=bridge.merge_auto_paths(pd.DataFrame(),self.root,pd.DataFrame([{'candidate_v1_event_key':key}]))
        self.assertEqual(len(merged),360)
        self.assertEqual(int(merged.iloc[0]['timestamp']),self.ts)

    def test_app_patch_readonly_and_compiles(self):
        import install_reaction_lab_bridge as installer
        before=self.src.read_text()
        after=installer.modify(before)
        compile(after,'app.py','exec')
        self.assertIn('merge_scanner_reactions(',after)
        self.assertEqual(after.count('return merge_auto_paths('),3)
        self.assertEqual(before.count('merge_scanner_reactions('),0)
        with self.assertRaises(RuntimeError):installer.modify(after)

if __name__=='__main__':unittest.main()
