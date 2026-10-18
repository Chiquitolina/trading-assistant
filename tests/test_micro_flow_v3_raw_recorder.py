"""Offline end-to-end tests for isolated asyncio-websockets recorder."""
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from research.micro_flow_v3.raw_recorder import run_live_raw
from research.micro_flow_v3.tape import replay_digest


def trade(agg_id, *, extra_space=False):
    stamp = int(time.time() * 1000)
    row = {'e': 'aggTrade', 'E': stamp, 's': 'ETHUSDT', 'a': agg_id,
           'p': '2500.000', 'q': '0.2000', 'T': stamp, 'm': False}
    return json.dumps(row, separators=(', ', ': ')) if extra_space else json.dumps(row)


class FakeSocket:
    def __init__(self, items):
        self.items = list(items)

    async def recv(self):
        if self.items:
            item = self.items.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        # A permanently live socket without new trades; wait_for cancels this.
        import asyncio
        await asyncio.sleep(20)


class FakeConnection:
    def __init__(self, items):
        self.items = items

    async def __aenter__(self):
        if isinstance(self.items, Exception):
            raise self.items
        return FakeSocket(self.items)

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeConnector:
    def __init__(self, *sessions):
        self.sessions = list(sessions)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.sessions:
            return FakeConnection([])
        return FakeConnection(self.sessions.pop(0))


class RawRecorderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'ethusdt.sqlite3'

    def tearDown(self):
        self.tmp.cleanup()

    def run_pilot(self, connector, duration=0.00008):
        return run_live_raw('ETHUSDT', self.path, max_hours=duration,
                            min_free_gb=0, reconnect_min=0,
                            reconnect_max=0, connect_factory=connector)

    def rows(self, sql):
        with sqlite3.connect(self.path) as c:
            return c.execute(sql).fetchall()

    def test_exact_text_and_clean_one_connection(self):
        text = trade(100, extra_space=True)
        factory = FakeConnector([text, trade(101)])
        result = self.run_pilot(factory)
        self.assertEqual(result['trades'], 2)
        self.assertEqual(result['missing_ids'], 0)
        self.assertEqual(result['reconnects'], 0)
        self.assertEqual(result['anomalies'], 0)
        self.assertEqual(self.rows("SELECT raw_json FROM tape WHERE kind='TRADE' ORDER BY seq")[0][0], text)
        self.assertEqual([r[0] for r in self.rows("SELECT quality FROM tape WHERE kind='TRADE'")], ['OK', 'OK'])
        self.assertEqual(replay_digest(self.path), replay_digest(self.path))
        self.assertTrue(factory.calls[0][0].endswith('/ethusdt@aggTrade'))
        self.assertEqual(factory.calls[0][1]['compression'], None)

    def test_error_reconnect_preserves_gap_and_generation(self):
        factory = FakeConnector(
            [trade(300), trade(301), ConnectionError('simulated cut')],
            [trade(305), trade(306)],
        )
        result = self.run_pilot(factory)
        self.assertEqual(result['trades'], 4)
        self.assertEqual(result['missing_ids'], 3)
        self.assertEqual(result['anomalies'], 1)
        self.assertEqual(result['reconnects'], 1)
        self.assertEqual(
            self.rows("SELECT agg_id,connection,quality FROM tape WHERE kind='TRADE' ORDER BY seq"),
            [(300, 1, 'OK'), (301, 1, 'OK'), (305, 2, 'ID_JUMP_SUSPECTED'), (306, 2, 'OK')],
        )
        reasons = [r[0] for r in self.rows("SELECT reason FROM tape WHERE kind='MARKER'")]
        self.assertTrue(any(r.startswith('WS_TRANSPORT_EXCEPTION:ConnectionError') for r in reasons))
        self.assertTrue(any('GAP_DETAILS:previous=301:next=305:missing_ids=3' == r for r in reasons))
        self.assertIn('RECONNECT_COVERAGE_UNVERIFIED', reasons)
        self.assertIn('PROCESS_STOP_DURATION_REACHED', reasons)

    def test_open_failure_and_retry_recorded(self):
        factory = FakeConnector(
            ConnectionRefusedError('open failed'),
            [trade(1000)],
        )
        result = self.run_pilot(factory)
        self.assertEqual(result['trades'], 1)
        self.assertGreaterEqual(result['reconnects'], 1)
        self.assertEqual(result['anomalies'], 0)
        reasons = [r[0] for r in self.rows("SELECT reason FROM tape WHERE kind='MARKER'")]
        self.assertTrue(any('WS_TRANSPORT_EXCEPTION:ConnectionRefusedError' in r for r in reasons))

    def test_same_generation_gap_also_detected(self):
        result = self.run_pilot(FakeConnector([trade(400), trade(405)]))
        self.assertEqual(result['missing_ids'], 4)
        self.assertEqual(result['anomalies'], 1)
        self.assertEqual(self.rows("SELECT quality FROM tape WHERE kind='TRADE' ORDER BY seq")[-1][0], 'ID_JUMP_SUSPECTED')

    def test_never_append_existing_or_overwrite(self):
        self.run_pilot(FakeConnector([trade(900)]))
        before = replay_digest(self.path)
        with self.assertRaises(FileExistsError):
            self.run_pilot(FakeConnector([trade(901)]))
        self.assertEqual(replay_digest(self.path), before)

    def test_invalid_data_stops_and_preserves_failure_marker(self):
        with self.assertRaisesRegex(RuntimeError, 'Invalid AggTrade'):
            self.run_pilot(FakeConnector(['{"e":"aggTrade","s":"ETHUSDT"}']))
        reasons = [r[0] for r in self.rows("SELECT reason FROM tape WHERE kind='MARKER'")]
        self.assertIn('INVALID_AGGTRADE_FAIL_CLOSED', reasons)
        self.assertTrue(any('PROCESS_FAILED:RuntimeError' in r for r in reasons))
        self.assertIn('PROCESS_STOP', reasons)


if __name__ == '__main__':
    unittest.main()
