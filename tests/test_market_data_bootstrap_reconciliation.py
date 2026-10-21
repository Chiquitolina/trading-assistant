"""Offline bootstrap integration tests from the actual MarketDataService methods.

No Redis connection, Binance API or live producer is started. Only the two
bootstrapping functions are extracted from the supplied service source.
"""
import ast
import copy
import pathlib
import threading
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'engine/live/data/market_data_service.py'
TF_MS = 60_000
MARKET_FLOW_TIMEFRAMES = ('1h', '4h')


def load_methods():
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    service = next(x for x in tree.body if isinstance(x, ast.ClassDef) and x.name == 'MarketDataService')
    names = {'_stage_bootstrap_missing_closes', '_flush_bootstrap_ws_buffer'}
    methods = [copy.deepcopy(x) for x in service.body if isinstance(x, ast.FunctionDef) and x.name in names]
    assert {x.name for x in methods} == names
    cls = ast.ClassDef(name='ExtractedBootstrapMethods', bases=[], keywords=[], body=methods, decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    namespace = {
        'MARKET_FLOW_TIMEFRAMES': MARKET_FLOW_TIMEFRAMES,
        'RuntimeError': RuntimeError,
        'fetch_closed_futures_candle': None,
    }
    exec(compile(module, str(SOURCE), 'exec'), namespace)
    return namespace, namespace['ExtractedBootstrapMethods']


class FakePublisher:
    def __init__(self, owner):
        self.owner = owner

    def publish_recovered_candle(self, *, symbol, timeframe, candle):
        assert timeframe == '1m'
        self.owner.publish('REST', symbol, candle['timestamp'])
        return {'timestamp': candle['timestamp']}


class FakeService:
    def __init__(self, cls, symbols, missing=None):
        self.__class__ = type('FakeServiceWithBootstrap', (cls, FakeService), {})
        self.symbols = symbols
        self.missing = set(missing or [])
        self.history_cutoff_ms = 150_000  # 2:30: prior seed includes 1:00
        self.history = {s: {60_000} for s in symbols}
        self.bootstrap_closed_messages = []
        self.bootstrap_buffer_sequence = 0
        self.bootstrap_buffering = True
        self.bootstrap_lock = threading.Lock()
        self.running = True
        self.stop_event = SimpleNamespace(wait=lambda _: False)
        self.ws = SimpleNamespace(is_connected=True)
        self.ws_was_connected = False
        self.publisher = FakePublisher(self)
        self.requests = []
        self.published = []
        self.resolved = []
        self.failures = set()
        self.inject_callback = None

        for ts in (120_000, 180_000, 240_000, 300_000):
            for sym in symbols:
                if (sym, ts) not in self.missing:
                    self.queue_ws(sym, ts)

    @staticmethod
    def _timeframe_ms(timeframe):
        assert timeframe == '1m', timeframe
        return TF_MS

    @staticmethod
    def _recovery_scan_until_ms():
        return 400_000  # Latest completed = 300_000

    def queue_ws(self, sym, ts):
        with self.bootstrap_lock:
            self.bootstrap_buffer_sequence += 1
            self.bootstrap_closed_messages.append((
                ts + TF_MS - 1, sym, '1m', ts,
                self.bootstrap_buffer_sequence,
                {'ws_symbol': sym, 'timestamp': ts},
            ))

    def _find_recovery_gaps(self, *, disconnected_at_ms, reconnected_at_ms):
        assert disconnected_at_ms == self.history_cutoff_ms
        assert reconnected_at_ms == 400_000
        pending = {
            (s, tf, ts)
            for _, s, tf, ts, _, _ in self.bootstrap_closed_messages
        }
        expected = (60_000, 120_000, 180_000, 240_000, 300_000)
        gaps = {}
        for ts in expected:
            missing = [
                sym for sym in self.symbols
                if ts not in self.history[sym] and (sym, '1m', ts) not in pending
            ]
            if missing:
                gaps[ts] = missing
        return {'1m': gaps} if gaps else {}

    def publish(self, kind, symbol, ts):
        self.published.append((ts, symbol, kind))
        self.history[symbol].add(ts)

    def _process_ws_message(self, message, *, publish_price):
        assert publish_price is False
        self.publish('WS', message['ws_symbol'], message['timestamp'])

    def _register_market_flow_close(self, **_kwargs):
        raise AssertionError('not a market flow close')


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.ns, self.cls = load_methods()

    def attach_fetcher(self, service):
        def fetch_closed_futures_candle(*, symbol, timeframe, candle_timestamp):
            service.requests.append((symbol, timeframe, candle_timestamp))
            if service.inject_callback:
                inject = service.inject_callback
                service.inject_callback = None
                inject()
            if (symbol, candle_timestamp) in service.failures:
                return None
            return {
                'timestamp': candle_timestamp,
                'close_timestamp': candle_timestamp + TF_MS - 1,
                'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0,
                'volume': 1.0, 'quoteVolume': 1.0,
            }
        self.ns['fetch_closed_futures_candle'] = fetch_closed_futures_candle

    def test_rest_fills_166_internal_gaps_while_preserving_close_order(self):
        syms = [f'S{i:03d}USDT' for i in range(290)]
        gaps = {(sym, 240_000) for sym in syms[:166]}
        service = FakeService(self.cls, syms, gaps)
        self.attach_fetcher(service)
        result = service._flush_bootstrap_ws_buffer()
        self.assertEqual(result['rest_recovered'], 166)
        self.assertEqual(result['published'], 1160)
        self.assertEqual(len(service.requests), 166)
        self.assertEqual(sum(len(x) for x in service.history.values()), 290 * 5)
        self.assertEqual([x[0] for x in service.published], sorted(x[0] for x in service.published))
        self.assertFalse(service.bootstrap_buffering)
        self.assertTrue(service.ws_was_connected)
        self.assertEqual(len({(ts, s) for ts, s, _ in service.published}), 1160)

    def test_complete_bootstrap_requires_no_rest(self):
        syms = ['BTCUSDT', 'ETHUSDT']
        service = FakeService(self.cls, syms)
        self.attach_fetcher(service)
        result = service._flush_bootstrap_ws_buffer()
        self.assertEqual(result['rest_recovered'], 0)
        self.assertEqual(service.requests, [])
        self.assertEqual(result['published'], 8)

    def test_late_ws_message_is_deduplicated_against_rest(self):
        service = FakeService(self.cls, ['AUSDT'], {('AUSDT', 240_000)})
        service.inject_callback = lambda: service.queue_ws('AUSDT', 240_000)
        self.attach_fetcher(service)
        result = service._flush_bootstrap_ws_buffer()
        self.assertEqual(result['published'], 4)
        # WS arrived first: we prefer that candle; REST becomes a duplicate.
        self.assertEqual(result['rest_recovered'], 0)
        self.assertEqual(result['duplicates'], 1)
        self.assertEqual(len(service.published), 4)

    def test_rest_failure_does_not_release_buffer_or_claim_ready(self):
        service = FakeService(self.cls, ['AUSDT'], {('AUSDT', 240_000)})
        service.failures.add(('AUSDT', 240_000))
        self.attach_fetcher(service)
        with self.assertRaisesRegex(RuntimeError, 'Bootstrap reconciliation failed'):
            service._flush_bootstrap_ws_buffer()
        self.assertTrue(service.bootstrap_buffering)
        self.assertFalse(service.ws_was_connected)
        self.assertEqual(service.requests.count(('AUSDT', '1m', 240_000)), 3)

    def test_ws_disconnect_blocks_handoff(self):
        service = FakeService(self.cls, ['AUSDT'])
        service.ws.is_connected = False
        self.attach_fetcher(service)
        with self.assertRaisesRegex(RuntimeError, 'WebSocket disconnected'):
            service._flush_bootstrap_ws_buffer()
        self.assertTrue(service.bootstrap_buffering)
        self.assertFalse(service.ws_was_connected)

    def test_no_dropped_close_which_arrives_during_flush(self):
        service = FakeService(self.cls, ['AUSDT'], {('AUSDT', 240_000)})
        # Simulate a WS close arriving while the initial sorted batch is being
        # processed.  The lock prevents releasing buffering before it drains.
        original = service._process_ws_message
        fired = []
        def during_publish(message, *, publish_price):
            if not fired:
                fired.append(1)
                service.queue_ws('AUSDT', 360_000)
                # This close is just after the settle horizon and should not
                # be synthesized by REST, but must still be flushed from WS.
            return original(message, publish_price=publish_price)
        service._process_ws_message = during_publish
        self.attach_fetcher(service)
        result = service._flush_bootstrap_ws_buffer()
        self.assertEqual(result['published'], 5)
        self.assertIn(('WS', 'AUSDT', 360_000), [(typ, sym, ts) for ts, sym, typ in service.published])

    def test_timestamp_mismatch_is_fatal(self):
        service = FakeService(self.cls, ['AUSDT'], {('AUSDT', 240_000)})
        def corrupted(**kwargs):
            return {'timestamp': 241_000, 'close_timestamp': 299_999}
        self.ns['fetch_closed_futures_candle'] = corrupted
        with self.assertRaisesRegex(RuntimeError, 'Bootstrap reconciliation failed'):
            service._flush_bootstrap_ws_buffer()
        self.assertTrue(service.bootstrap_buffering)


if __name__ == '__main__':
    unittest.main()
