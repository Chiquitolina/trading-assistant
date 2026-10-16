"""Pure data/SQLite integration tests for the read-only Streamlit portfolio dashboard.

No Binance/Redis connections or source-journal writes. Run with:
    python3 -m unittest -v test_runner_portfolio_dashboard.py
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dashboard.services.reaction_lab_portfolio_reader import (
    RUN_ROOT, active_slot_intervals, discover_runs, explain_skipped_signals,
    load_run_metadata, read_table, resolve_run, table_counts, verify_consistency,
)

STRAT = 'Runner Challenger 220'
KEY = 'EXAMPLEUSDT|SHORT|1000'
SKIP = 'SECONDUSDT|LONG|1010'


class PortfolioReadOnlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.root = self.project / RUN_ROOT
        self.run = self.root / '20261016T181941604037Z_8885eaaddf'
        self.run.mkdir(parents=True)
        self.manifest = {
            'engine_version': 'runner-portfolio-v1.0', 'source_records': 2,
            'created_at_utc': '2026-10-16T18:19:41Z',
            'provenance': {'data_quality': {'live':2,'usable':2}},
            'profiles': {STRAT: {'tp':2,'sl':2}},
        }
        self.summary = {STRAT:{'equity_initial_usdt':200,
                               'equity_final_realized_usdt':209.16,
                               'resolved_trades':1, 'orders_placed':1,
                               'skipped_slot_busy':1}}
        (self.run/'manifest.json').write_text(json.dumps(self.manifest))
        (self.run/'summary.json').write_text(json.dumps(self.summary))
        self.data = {
            'decisions': [
                {'strategy':STRAT,'event_key':KEY,'ready_ms':1010,'decision':'ORDER_SUBMITTED'},
                {'strategy':STRAT,'event_key':SKIP,'ready_ms':3000,'decision':'SKIPPED_SLOT_BUSY',
                 'reason':'slot_busy_through_61000'},
            ],
            'events': [
                {'strategy':STRAT,'event_key':KEY,'kind':'ORDER_PLACED','timestamp_ms':1000},
                {'strategy':STRAT,'event_key':KEY,'kind':'ORDER_FILLED','timestamp_ms':2000},
                {'strategy':STRAT,'event_key':KEY,'kind':'POSITION_EXIT','timestamp_ms':61000},
            ],
            'trades': [
                {'strategy':STRAT,'event_key':KEY,'net_pnl_usdt':9.16,'exit_ts':1000,
                 'fill_ts':2000,'wallet_after_usdt':209.16,'status':'TP'},
            ],
            'equity': [
                {'strategy':STRAT,'event_key':KEY,'timestamp_ms':1000,'wallet_usdt':200},
                {'strategy':STRAT,'event_key':KEY,'timestamp_ms':61000,'wallet_usdt':209.16},
            ],
            'path_traces': [
                {'strategy':STRAT,'event_key':KEY,'timestamp_ms':2000,'open':1.0,
                 'high':1.1,'low':0.9,'close':1.0, 'stage':'IN_POSITION'},
            ],
        }
        self.db=self.run/'portfolio_ledger_v1.sqlite3'
        with sqlite3.connect(self.db) as c:
            for name, rows in self.data.items():
                c.execute(f'CREATE TABLE "{name}" (id INTEGER PRIMARY KEY, strategy TEXT, event_key TEXT, timestamp_ms INTEGER, payload_json TEXT NOT NULL)')
                for row in rows:
                    c.execute(f'INSERT INTO "{name}" (strategy,event_key,timestamp_ms,payload_json) VALUES (?,?,?,?)',
                              (row.get('strategy'),row.get('event_key'),
                               row.get('timestamp_ms',row.get('ready_ms')),json.dumps(row)))

    def test_run_discovery(self):
        runs=discover_runs(self.project)
        self.assertEqual(len(runs),1)
        self.assertEqual(runs[0]['source_records'],2)

    def test_missing_root_is_empty(self):
        self.assertEqual(discover_runs(self.project/'missing'), [])

    def test_correct_resolved_folder(self):
        self.assertEqual(resolve_run(self.project, self.run.name), self.run)

    def test_prevent_relative_escape(self):
        for candidate in ('../etc', '/tmp', '..', './run', 'a/b'):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                resolve_run(self.project, candidate)

    def test_prevent_symlink_folder(self):
        symlink=self.root/'20261016_link'
        symlink.symlink_to(self.run, target_is_directory=True)
        self.assertNotIn('20261016_link',[x['run_id'] for x in discover_runs(self.project)])
        with self.assertRaises(ValueError):
            resolve_run(self.project,'20261016_link')

    def test_manifest_and_summary(self):
        a,b=load_run_metadata(self.project,self.run.name)
        self.assertEqual(a,self.manifest)
        self.assertEqual(b,self.summary)

    def test_read_each_table(self):
        for name, rows in self.data.items():
            self.assertEqual(read_table(self.project,self.run.name,name), rows)

    def test_strategies_filter(self):
        self.assertEqual(read_table(self.project,self.run.name,'trades',STRAT),self.data['trades'])
        self.assertEqual(read_table(self.project,self.run.name,'trades','NONEXISTENT'),[])

    def test_invalid_table(self):
        for n in ('sqlite_master','trades; DROP TABLE trades','metadata'):
            with self.assertRaises(ValueError):
                read_table(self.project,self.run.name,n)

    def test_read_does_not_modify_database(self):
        original=self.db.read_bytes()
        for n in self.data:read_table(self.project,self.run.name,n)
        self.assertEqual(original,self.db.read_bytes())

    def test_table_counts(self):
        counts=table_counts(self.project,self.run.name)
        self.assertEqual(counts['events'],3)
        self.assertEqual(counts['trades'],1)

    def test_slot_blocker_in_position(self):
        blocked=explain_skipped_signals(self.data['decisions'],self.data['events'],STRAT)
        self.assertEqual(len(blocked),1)
        self.assertEqual(blocked[0]['blocker_event_key'],KEY)
        self.assertEqual(blocked[0]['blocker_stage'],'POSITION')
        self.assertTrue(blocked[0]['blocker_release_matches_reason'])

    def test_pending_order_blocks_slot(self):
        dec=[{**self.data['decisions'][1], 'ready_ms':1500}]
        blocked=explain_skipped_signals(dec,self.data['events'],STRAT)
        self.assertEqual(blocked[0]['blocker_stage'],'PENDING_ORDER')

    def test_slot_remains_busy_until_exit_event_not_bar_open(self):
        dec=[{**self.data['decisions'][1],'ready_ms':60000}]
        blocked=explain_skipped_signals(dec,self.data['events'],STRAT)
        self.assertEqual(blocked[0]['blocker_event_key'],KEY)
        dec[0]['ready_ms']=61000
        blocked=explain_skipped_signals(dec,self.data['events'],STRAT)
        self.assertIsNone(blocked[0]['blocker_event_key'])

    def test_outcome_missing_blocker_not_guessed(self):
        skipped=[{**self.data['decisions'][1],'ready_ms':80000}]
        blocked=explain_skipped_signals(skipped,self.data['events'],STRAT)
        self.assertIsNone(blocked[0]['blocker_event_key'])
        self.assertEqual(blocked[0]['blocker_matches'],0)

    def test_integrity_clean(self):
        result=verify_consistency(self.summary,self.data)
        self.assertTrue(result['ok'],result['issues'])
        self.assertEqual(result['by_strategy'][STRAT]['blocked_attributed'],1)

    def test_integrity_detects_wrong_equity(self):
        bad={STRAT:{**self.summary[STRAT],'equity_final_realized_usdt':250}}
        result=verify_consistency(bad,self.data)
        self.assertFalse(result['ok'])
        self.assertTrue(any('PnL' in err for err in result['issues']))

    def test_invalid_json_rejected(self):
        with sqlite3.connect(self.db) as c:
            c.execute('UPDATE trades SET payload_json=?',('invalid-json',))
        with self.assertRaises(json.JSONDecodeError):
            read_table(self.project,self.run.name,'trades')

    def test_sources_untouched(self):
        ghost=self.project/'live_journal.sqlite3'
        ghost.write_text('immutable')
        discover_runs(self.project)
        read_table(self.project,self.run.name,'trades')
        self.assertEqual(ghost.read_text(),'immutable')


if __name__=='__main__':
    unittest.main()
