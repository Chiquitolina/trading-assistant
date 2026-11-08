import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import universal_runner_horizontal_history_v1 as m


class HistoricalSource(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out=Path(self.tmp.name)
        self.start=m.to_ms('2026-10-22T00:00:00Z')
        self.end=m.to_ms('2026-10-23T00:00:00Z')

    @staticmethod
    def fetch(sym,tf,cursor,end,sleep):
        # Local fixture, no real network; pages may contain at most 20 rows.
        ans=[]
        for t in range(cursor,min(end,cursor+20*m.TF[tf]),m.TF[tf]):
            ans.append([t,'100','101','99','100','100',t+m.TF[tf]-1,'10000'])
        return ans

    def test_batch_download_and_verify_both_timeframes(self):
        self.assertEqual(m.download_symbol(self.out,'BTCUSDT',self.start,self.end,0,fetcher=self.fetch),
                         'DOWNLOADED_VERIFIED')
        meta=m.verify_symbol(self.out,'BTCUSDT',self.start,self.end)
        self.assertEqual(meta['timeframes']['15m']['validation']['rows'],96)
        self.assertEqual(meta['timeframes']['30m']['validation']['rows'],48)
        self.assertEqual(meta['timeframes']['15m']['validation']['status'],'FULL')
        self.assertEqual(m.download_symbol(self.out,'BTCUSDT',self.start,self.end,0,fetcher=self.fetch),
                         'ALREADY_VERIFIED')

    def test_tamper_is_detected(self):
        m.download_symbol(self.out,'BTCUSDT',self.start,self.end,0,fetcher=self.fetch)
        f=self.out/'symbols/BTCUSDT/15m.csv'
        f.write_text(f.read_text().replace('100.0','200.0'))
        with self.assertRaisesRegex(RuntimeError,'Checksum mismatch'):
            m.verify_symbol(self.out,'BTCUSDT',self.start,self.end)

    def test_internal_gap_rejected(self):
        def bad(sym,tf,cursor,end,sleep):
            if tf=='15m':
                return [[cursor,'100','101','99','100','100',cursor+1,'10000'],
                        [cursor+2*m.TF[tf],'100','101','99','100','100',cursor+3*m.TF[tf]-1,'10000']]
            return self.fetch(sym,tf,cursor,end,sleep)
        with self.assertRaisesRegex(ValueError,'Internal Binance gap'):
            m.download_symbol(self.out,'BTCUSDT',self.start,self.end,0,fetcher=bad)
        self.assertFalse((self.out/'symbols/BTCUSDT/manifest.json').exists())

    def test_partial_listing_is_explicit(self):
        def late(sym,tf,cursor,end,sleep):
            return self.fetch(sym,tf,max(cursor,self.start+3*m.TF[tf]),end,sleep)
        m.download_symbol(self.out,'NEWUSDT',self.start,self.end,0,fetcher=late)
        result=m.verify_symbol(self.out,'NEWUSDT',self.start,self.end)
        self.assertEqual(result['timeframes']['15m']['validation']['status'],'PARTIAL_LISTING_OR_END')
        self.assertEqual(result['timeframes']['30m']['validation']['status'],'PARTIAL_LISTING_OR_END')

    def test_invalid_ohlc_rejected(self):
        with self.assertRaisesRegex(ValueError,'Bad OHLCV'):
            m.validate_row({'timestamp':self.start,'open':100,'high':99,
                            'low':99,'close':100,'volume':1,'quoteVolume':2},'X','15m')

    def test_universe_read_only(self):
        import sqlite3
        r=self.out/m.TOUCHES
        r.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(r) as db:
            db.execute('CREATE TABLE first_touches(symbol TEXT,status TEXT)')
            db.executemany('INSERT INTO first_touches VALUES (?,?)',
                           [('BTCUSDT','TOUCH_FAILED'),('ETHUSDT','REACTION'),('BTCUSDT','REACTION')])
            db.commit()
        self.assertEqual(m.symbols_for(self.out),['BTCUSDT','ETHUSDT'])
        self.assertEqual(m.symbols_for(self.out,'AAVEUSDT,BTCUSDT'),['AAVEUSDT','BTCUSDT'])


class HftOfflineAdapter(unittest.TestCase):
    def test_offline_verified_and_event_cutoff(self):
        import horizontal_first_touch_research_v1 as h
        import sqlite3
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'frozen'
            touchdb=root/h.SOURCE_REL;touchdb.parent.mkdir(parents=True,exist_ok=True)
            event=m.to_ms('2026-10-25T22:19:00Z')
            with sqlite3.connect(touchdb) as db:
                db.execute('''CREATE TABLE first_touches(touch_key TEXT PRIMARY KEY,
                    legacy_event_key TEXT,symbol TEXT,side TEXT,status TEXT,
                    observation_type TEXT,retest_ts INTEGER,known_ts INTEGER)''')
                for i,status in enumerate(('TOUCH_FAILED','TOUCH_INDECISIVE','REACTION')):
                    t=event+i*60_000
                    db.execute('INSERT INTO first_touches VALUES (?,?,?,?,?,?,?,?)',
                               (f'k{i}',f'old{i}','BTCUSDT','LONG',status,'LIVE_OBSERVED',t,t+60_000))
                db.commit()
            start=m.to_ms('2026-10-23T00:00:00Z')
            end=m.to_ms('2026-10-26T00:00:00Z')
            HistoricalSource.fetch('BTCUSDT','15m',start,end,0)
            m.download_symbol(source,'BTCUSDT',start,end,0,fetcher=HistoricalSource.fetch)
            rep=h.run(root,0,False,offline_history_dir=source,
                      event_end_exclusive=event+2*60_000,output_dir=root/'frozen_labels')
            self.assertEqual(rep['events'],2)
            self.assertEqual(rep['source_mode'],'OFFLINE_FROZEN')
            self.assertEqual(rep['source_error_symbols'],0)
            self.assertEqual(rep['history_coverage_pairs'],{'COMPLETE|COMPLETE':2})
            self.assertEqual(len(rep['offline_manifest_sha256']),1)
            with (root/'frozen_labels/event_labels.csv').open() as f:
                labels=list(csv.DictReader(f))
            self.assertTrue(all('OFFLINE_VERIFIED' in r['history_source_15m'] for r in labels))

if __name__=='__main__':unittest.main()
