import hashlib
import json
from pathlib import Path
import sqlite3
import unittest
import zlib

from test_reaction_lab_runner_discovery_all_touches_v1 import IntegrationTests, candles, blob
from reaction_lab_runner_discovery_all_touches_v1 import build_dataset, iter_events, recovery_index


class HistoricalOverlayTests(IntegrationTests):
    # Inherit setup/fixture and existing tests, exercising parity of originals too.
    def write_batch(self,key,status,old_ts=0,bars=None,name='pilot_a'):
        bars=bars or candles()
        folder=self.root/'first_touch_historical_recovery_v11'/name
        folder.mkdir(parents=True)
        path=folder/'recovered_paths.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('''CREATE TABLE recovered_paths(touch_key TEXT PRIMARY KEY,symbol TEXT,side TEXT,
                original_status TEXT,retest_ts INTEGER,entry_ts INTEGER,path_state TEXT,count INTEGER,
                path_blob BLOB,provider TEXT)''')
            db.execute('INSERT INTO recovered_paths VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (key,'BTCUSDT','LONG',status,old_ts,old_ts+60000,
                        'FULL_360',360,blob(bars),'BINANCE_USDM_PUBLIC_REST_1M_OFFLINE'))
        sha=hashlib.sha256(path.read_bytes()).hexdigest()
        (folder/'manifest.json').write_text(json.dumps({
            'schema':'FIRST_TOUCH_HISTORICAL_RECOVERY_V11',
            'files_sha256':{'recovered_paths.sqlite3':sha}}))
        return folder

    def test_overlay_gives_complete_to_failed(self):
        self.ft_event('K','BTCUSDT','LONG','TOUCH_FAILED')
        self.write_batch('K','TOUCH_FAILED',bars=candles(n=360,high=103.0))
        row=next(iter_events(self.project,recovery=recovery_index(self.root/'first_touch_historical_recovery_v11')[0]))
        self.assertEqual(row['path_source'],'HISTORICAL_BINANCE_V11')
        self.assertEqual(row['path_count'],360)
        self.assertEqual(row['retest_time_to_run_2pct'],1)
        manifest=build_dataset(self.project)
        self.assertEqual(manifest['counts']['evaluable_180_TOUCH_FAILED'],1)
        self.assertEqual(manifest['historical_recovery']['full_360_indexed'],1)

    def test_history_must_match_stored_prefix(self):
        p=candles(n=2)
        self.ft_event('K','BTCUSDT','LONG','TOUCH_FAILED',bars=p)
        bs=candles(n=360)
        bs[0]=(bs[0][0],101,101.2,100.8,101)
        self.write_batch('K','TOUCH_FAILED',bars=bs)
        row=next(iter_events(self.project,recovery=recovery_index(self.root/'first_touch_historical_recovery_v11')[0]))
        self.assertEqual(row['path_source'],'HISTORICAL_OHLC_CONFLICT')
        self.assertEqual(row['path_count'],2)
        self.assertEqual(row['path_quality'],'SOURCE_REVIEW_REQUIRED')

    def test_wrong_status_rejected(self):
        self.ft_event('K','BTCUSDT','LONG','TOUCH_FAILED')
        self.write_batch('K','TOUCH_INDECISIVE')
        with self.assertRaisesRegex(ValueError,'identity mismatch'):
            list(iter_events(self.project,recovery=recovery_index(self.root/'first_touch_historical_recovery_v11')[0]))

    def test_duplicate_batches_refused(self):
        self.write_batch('K','TOUCH_FAILED',name='a')
        self.write_batch('K','TOUCH_FAILED',name='b')
        with self.assertRaisesRegex(ValueError,'Duplicate historical touch_key'):
            recovery_index(self.root/'first_touch_historical_recovery_v11')

    def test_corrupt_manifest_refused(self):
        folder=self.write_batch('K','TOUCH_FAILED')
        x=json.loads((folder/'manifest.json').read_text())
        x['files_sha256']['recovered_paths.sqlite3']='bad'
        (folder/'manifest.json').write_text(json.dumps(x))
        with self.assertRaisesRegex(ValueError,'SHA256 mismatch'):
            recovery_index(self.root/'first_touch_historical_recovery_v11')

    def test_complete_ft_kept_over_history(self):
        self.ft_event('K','BTCUSDT','LONG','TOUCH_FAILED',bars=candles())
        self.write_batch('K','TOUCH_FAILED')
        row=next(iter_events(self.project,recovery=recovery_index(self.root/'first_touch_historical_recovery_v11')[0]))
        self.assertEqual(row['path_source'],'FIRST_TOUCH_V2')


if __name__=='__main__':unittest.main()
