"""Full source SQLite -> V2 causal loader -> portfolio -> isolated ledger smoke test."""
import json
import sqlite3
import struct
import sys
import tempfile
import types
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from dashboard.services.reaction_lab_portfolio import run_comparison, export_comparison
from dashboard.services.reaction_lab_runner_optimizer import load_live

BAR = struct.Struct('<qdddd')
T = 1_790_000_000_000 // 60000 * 60000


class TestSQLiteIntegration(unittest.TestCase):
    def test_joins_all_sources_without_modifying_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            root = project/'reports'/'reaction_lab'
            root.mkdir(parents=True)
            scan_path = root/'scanner_events_v1.sqlite3'
            context_path = root/'causal_context_v1.sqlite3'
            paths_path = root/'auto_paths_v1.sqlite3'
            key = f'TESTUSDT|LONG|{T-60000}'
            source = ((T-60000), T, T+60000, T+2*60000+7000)
            retest, known, seen, cap = source
            candles = [(T+i*60000, 100., 100.15, 99.85, 100.) for i in range(360)]
            candles[3] = (T+3*60000, 99., 99.2, 97.8, 98.1)
            candles[4] = (T+4*60000, 98.1, 100.3, 98.0, 100.0)
            blob = zlib.compress(b''.join(BAR.pack(*bar) for bar in candles))
            with sqlite3.connect(scan_path) as c:
                c.execute('CREATE TABLE events(event_key TEXT, symbol TEXT, side TEXT, retest_ts INTEGER, known_ts INTEGER, first_seen_ms INTEGER, causal_json TEXT, observation_type TEXT)')
                c.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)',
                          (key, 'TESTUSDT','LONG',retest,known,seen,json.dumps({'max_departure_pct':2.}), 'LIVE_OBSERVED'))
            with sqlite3.connect(context_path) as c:
                c.execute('CREATE TABLE contexts(event_key TEXT,known_ts INTEGER,captured_at_ms INTEGER,status TEXT,context_json TEXT)')
                c.execute('CREATE TABLE v1_regime_events(event_key TEXT,known_ts INTEGER,boundary_ts INTEGER,captured_at_ms INTEGER,status TEXT,capture_method TEXT,metrics_json TEXT)')
                c.execute('INSERT INTO contexts VALUES (?,?,?,?,?)',
                          (key,known,cap,'READY',json.dumps({'opposing_room_1h_pct':3.})))
                c.execute('INSERT INTO v1_regime_events VALUES (?,?,?,?,?,?,?)',
                          (key,known,T-240000,cap,'READY','RECOMPUTED_ASOF_CLOSED_4H_REDIS',json.dumps({'volume_rank_pct_4h':70.,'return_rank_pct_4h':70.})))
            with sqlite3.connect(paths_path) as c:
                c.execute('CREATE TABLE events(event_key TEXT,state TEXT,entry_ts INTEGER,retest_ts INTEGER,count INTEGER,path_blob BLOB)')
                c.execute('INSERT INTO events VALUES (?,?,?,?,?,?)',
                          (key,'COMPLETE',T,retest,360,blob))
            initial = {p.name:p.read_bytes() for p in root.glob('*.sqlite3')}
            fake = types.ModuleType('reaction_lab_auto_paths')
            fake.decode_path = lambda b: [BAR.unpack_from(zlib.decompress(b), i) for i in range(0,len(zlib.decompress(b)),BAR.size)]
            with patch.dict(sys.modules, {'reaction_lab_auto_paths':fake}):
                records, quality = load_live(project, cutoff_ms=0)
            self.assertEqual(quality['usable'], 1)
            self.assertEqual(len(records), 1)
            comparison = run_comparison(records)
            self.assertEqual(comparison['Runner V1 Frozen']['summary']['resolved_trades'], 1)
            dest = export_comparison(comparison, records, project/'new_reports'/'run1')
            self.assertTrue((dest/'portfolio_ledger_v1.sqlite3').exists())
            self.assertEqual({p.name:p.read_bytes() for p in root.glob('*.sqlite3')},initial)


if __name__ == '__main__':
    unittest.main()
