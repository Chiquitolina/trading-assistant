"""Offline tests for isolated Reaction Lab auto 1m path collector."""
import csv
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import reaction_lab_auto_paths as p


class RedisFake:
    def __init__(self, items):
        self.items = items
        self.calls = []

    def lrange(self, key, start, end):
        self.calls.append((key, start, end))
        return self.items.get(key, [])


def candle(ts, price, close_ts=None):
    return json.dumps({
        'timestamp': ts, 'close_timestamp': ts + 59_999 if close_ts is None else close_ts,
        'open': price, 'high': price + 1, 'low': price - 1, 'close': price + 0.1,
    })


def write_monitor(path, events):
    with path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=[
            'candidate_v2_event_key', 'candidate_v2_cohort', 'symbol', 'signal',
            'retest_timestamp', 'status'])
        writer.writeheader()
        for s, side, ts in events:
            writer.writerow({'candidate_v2_event_key': f'{s}|{side}|{ts}',
                'candidate_v2_cohort': 'FORWARD', 'symbol': s,
                'signal': side, 'retest_timestamp': ts, 'status': 'REACTION'})


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.root = Path(self.t.name)
        self.db_path = self.root / 'reports/reaction_lab/auto_paths_v1.sqlite3'
        self.monitor = self.root / 'candidate_v2_monitor.csv'
        self.base = 1_800_000_000_000
        # Make entry_ts exactly base; retest minute is previous.
        self.retest = self.base - p.ONE_MINUTE_MS

    def status(self, event_key):
        con = sqlite3.connect(self.db_path)
        try:
            row = con.execute('SELECT state,count,path_blob FROM events WHERE event_key=?', (event_key,)).fetchone()
        finally:
            con.close()
        return row[0], row[1], p.decode_path(row[2])

    def test_check_has_no_writes(self):
        write_monitor(self.monitor, [('BTCUSDT', 'LONG', self.retest)])
        self.assertEqual(p.main(['--project', str(self.root), '--check']), 0)
        self.assertFalse(self.db_path.exists())

    def test_closed_contiguous_start_and_completion(self):
        write_monitor(self.monitor, [('BTCUSDT', 'LONG', self.retest)])
        first = [candle(self.base + i*60_000, 10.0 + i*0.001)
                 for i in range(180)]
        first += [candle(self.base + 180*60_000, 11.0,
                         close_ts=self.base + 181*60_000 + 50_000)]
        redis = RedisFake({'market-data:v1:history:BTCUSDT:1m': first})
        now = self.base + 180*60_000
        result = p.run_once(self.root, self.db_path, redis, now, 0)
        self.assertEqual(result['registered']['new'], 1)
        key = f'BTCUSDT|LONG|{self.retest}'
        state, count, path = self.status(key)
        self.assertEqual((state,count), ('ACTIVE',180))
        self.assertEqual(path[0][0], self.base)
        self.assertEqual(path[-1][0], self.base + 179*60_000)
        self.assertEqual(len(redis.calls), 1)
        # Now complete and verify idempotence.
        redis.items['market-data:v1:history:BTCUSDT:1m'] = [
            candle(self.base + i*60_000, 10+i*0.001) for i in range(360)
        ]
        second_now = self.base + 361*60_000
        p.run_once(self.root, self.db_path, redis, second_now, 0)
        self.assertEqual(self.status(key)[:2], ('COMPLETE',360))
        before_calls = len(redis.calls)
        p.run_once(self.root, self.db_path, redis, second_now, 0)
        self.assertEqual(len(redis.calls), before_calls)
        self.assertEqual(self.status(key)[:2], ('COMPLETE',360))
        self.assertEqual(len(p.read_auto_paths_dataframe(self.db_path, {key})), 360)

    def test_gap_is_not_bridged(self):
        write_monitor(self.monitor, [('BTCUSDT','SHORT',self.retest)])
        prices = [candle(self.base, 10), candle(self.base+60_000,10.1),
                  candle(self.base+180_000,10.3)]
        redis = RedisFake({'market-data:v1:history:BTCUSDT:1m': prices})
        p.run_once(self.root, self.db_path, redis, self.base+240_000, 0)
        state, count, bars = self.status(f'BTCUSDT|SHORT|{self.retest}')
        self.assertEqual((state,count), ('ACTIVE',2))
        self.assertEqual([row[0] for row in bars], [self.base, self.base+60_000])
        # Next cycle flags the hole, never bridges to offset 3.
        p.run_once(self.root, self.db_path, redis, self.base+300_000, 0)
        self.assertEqual(self.status(f'BTCUSDT|SHORT|{self.retest}')[:2], ('GAP_WAITING',2))
        # Producer can repair the missing bar while it is still in Redis.
        redis.items['market-data:v1:history:BTCUSDT:1m'] = [
            candle(self.base+i*60_000,10+i*0.1) for i in range(4)
        ]
        p.run_once(self.root, self.db_path, redis, self.base+360_000, 0)
        self.assertEqual(self.status(f'BTCUSDT|SHORT|{self.retest}')[:2], ('ACTIVE',4))

    def test_missing_entry_and_stale_ignored(self):
        write_monitor(self.monitor, [
            ('OLDUSDT','LONG', self.retest - 500*60_000),
            ('NEWUSDT','LONG', self.retest),
        ])
        redis = RedisFake({'market-data:v1:history:NEWUSDT:1m':
            [candle(self.base+60_000, 20), candle(self.base+120_000, 21)]})
        now = self.base+120_000
        p.run_once(self.root, self.db_path, redis, now, 0)
        self.assertEqual(self.status(f'OLDUSDT|LONG|{self.retest - 500*60_000}')[:2],
                         ('STALE_BEFORE_COLLECTOR', 0))
        self.assertEqual(self.status(f'NEWUSDT|LONG|{self.retest}')[:2],
                         ('MISSING_ENTRY', 0))
        self.assertEqual(len(redis.calls), 1)

    def test_monitor_alias_and_invalid_events(self):
        with self.monitor.open('w', newline='') as f:
            w=csv.DictWriter(f, fieldnames=['candidate_v1_event_key','symbol','side','retest_timestamp'])
            w.writeheader()
            w.writerow({'candidate_v1_event_key':f'BTCUSDT|LONG|{self.retest}', 'symbol':'BTCUSDT','side':'LONG','retest_timestamp':self.retest})
            w.writerow({'candidate_v1_event_key':'BAD', 'symbol':'BTCUSDT','side':'LONG','retest_timestamp':self.retest})
        result=p.monitor_events(self.monitor)
        self.assertEqual(len(result), 1)

    def test_cross_profile_shared_identity_single_path(self):
        write_monitor(self.monitor, [('BTCUSDT','LONG',self.retest)])
        short = self.root / 'candidate_v1_frozen_monitor.csv'
        with short.open('w', newline='', encoding='utf-8') as out:
            w = csv.DictWriter(out, fieldnames=[
                'candidate_v1_event_key','candidate_v1_cohort',
                'symbol','signal','retest_timestamp','status'])
            w.writeheader()
            w.writerow({'candidate_v1_event_key':f'BTCUSDT|LONG|{self.retest}',
                        'candidate_v1_cohort':'DISCOVERY', 'symbol':'BTCUSDT',
                        'signal':'LONG', 'retest_timestamp':self.retest, 'status':'REACTION'})
        raw = self.root / p.SOURCE_FILES['v1_raw']
        raw.parent.mkdir(parents=True, exist_ok=True)
        with raw.open('w', newline='', encoding='utf-8') as out:
            w = csv.DictWriter(out, fieldnames=['symbol','signal','retest_timestamp','status'])
            w.writeheader()
            w.writerow({'symbol':'BTCUSDT','signal':'LONG',
                        'retest_timestamp':self.retest,'status':'REACTION'})
        found, memberships, diagnostics = p.read_sources(self.root)
        self.assertEqual(len(found), 1)
        self.assertEqual(set(memberships[f'BTCUSDT|LONG|{self.retest}']),
                         {'v1_raw','v1_short_frozen','v2'})
        redis=RedisFake({'market-data:v1:history:BTCUSDT:1m':[candle(self.base,10)]})
        result=p.run_once(self.root,self.db_path,redis,self.base+60_000,0)
        self.assertEqual(result['registered']['new'],1)
        con=sqlite3.connect(self.db_path)
        self.assertEqual(con.execute('SELECT COUNT(*) FROM events').fetchone()[0],1)
        self.assertEqual(con.execute('SELECT COUNT(*) FROM event_sources').fetchone()[0],3)
        con.close()
        self.assertEqual(len(p.read_auto_paths_dataframe(self.db_path,source='v2')),1)
        self.assertEqual(len(p.read_auto_paths_dataframe(self.db_path,source='v1_short_frozen')),1)
        self.assertEqual(len(p.read_auto_paths_dataframe(self.db_path,source='v1_long_frozen')),0)

    def test_all_sources_appear_even_if_no_v2(self):
        self.monitor.unlink(missing_ok=True)
        write_monitor(self.root / 'candidate_v1_long_frozen_monitor.csv',
                      [('ETHUSDT','SHORT',self.retest)])
        events, memberships, _ = p.read_sources(self.root)
        self.assertEqual(len(events),1)
        self.assertEqual(set(memberships[f'ETHUSDT|SHORT|{self.retest}']),{'v1_long_frozen'})

    def test_check_reports_missing_sources_readonly(self):
        write_monitor(self.monitor,[('BTCUSDT','LONG',self.retest)])
        self.assertEqual(p.main(['--project',str(self.root),'--check']),0)
        self.assertFalse(self.db_path.exists())

    def test_does_not_touch_dashboard_pickle(self):
        legacy = self.root / 'reports/candidate_v2_analysis/paths_1m.pkl'
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_bytes(b'LEGACY-CONTENT')
        write_monitor(self.monitor, [('BTCUSDT','LONG',self.retest)])
        redis=RedisFake({'market-data:v1:history:BTCUSDT:1m':[candle(self.base,10)]})
        p.run_once(self.root, self.db_path, redis, self.base+60_000, 0)
        self.assertEqual(legacy.read_bytes(), b'LEGACY-CONTENT')


if __name__ == '__main__':
    unittest.main(verbosity=2)
