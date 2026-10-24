"""Offline regression tests for the exact ws_client.py bundled with this patch.

No Binance credentials, network, Redis, PM2, or installed python-binance needed.
"""
import asyncio
import importlib.util
import io
from contextlib import redirect_stdout
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

MODULE_PATH = Path(__file__).resolve().parent / 'engine' / 'live' / 'ws' / 'ws_client.py'


def load_ws_client():
    fake_binance = types.ModuleType('binance')
    fake_binance.ThreadedWebsocketManager = FakeTWM
    original = sys.modules.get('binance')
    sys.modules['binance'] = fake_binance
    try:
        spec = importlib.util.spec_from_file_location('_ws_guard_test_module', MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.WSClient
    finally:
        if original is None:
            sys.modules.pop('binance', None)
        else:
            sys.modules['binance'] = original


class FakeTWM:
    def __init__(self, loop=None, **kwargs):
        self.loop = loop
        self.stopped_keys = []
        self.callbacks = {}
        self.kwargs = kwargs
        self.running = False

    def start(self):
        self.running = True

    def stop_socket(self, key):
        self.stopped_keys.append(key)

    def start_futures_multiplex_socket(self, streams, callback):
        key = 'socket-' + str(len(self.callbacks) + 1)
        self.callbacks[key] = callback
        callback({'stream': streams[0], 'data': {'e': 'kline'}})
        return key

    def stop(self):
        self.running = False
        if self.loop is not None and not self.loop.is_closed():
            self.loop.close()

    def is_alive(self):
        return self.running


class WebsocketReadLoopGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.WSClient = load_ws_client()

    def setUp(self):
        self.on_message = Mock()
        self.ws = self.WSClient(
            on_message=self.on_message,
            timeframes=['1m'],
            symbols=['BTCUSDT'],
            enable_coverage_audit=False,
        )
        self.ws.running = True
        self.ws.twm = FakeTWM()
        self.ws._connection_generation = 9
        self.ws._group_socket_keys = {1: 'socket-btc', 2: 'socket-eth'}
        self.ws.is_connected = True

    def tearDown(self):
        if self.ws.twm is not None:
            self.ws.twm.stop()
        asyncio.set_event_loop(None)

    def send_error(self, group=1, generation=9, kind='ReadLoopClosed'):
        self.ws._handle_message(
            {'e': 'error', 'type': kind,
             'm': 'Read loop has been closed, please reset the websocket connection and listen to the message error.'},
            group_id=group,
            generation=generation,
        )

    def test_first_error_stops_only_failed_listener(self):
        with redirect_stdout(io.StringIO()) as out:
            self.send_error()
        self.assertEqual(self.ws.twm.stopped_keys, ['socket-btc'])
        self.assertFalse(self.ws.is_connected)
        self.assertTrue(self.ws._is_reconnecting)
        self.assertIn('group=1', out.getvalue())
        self.assertIn('ReadLoopClosed', out.getvalue())

    def test_1500_repeated_errors_do_not_repeat_stop_or_log(self):
        with redirect_stdout(io.StringIO()) as out:
            for _ in range(1500):
                self.send_error()
        self.assertEqual(self.ws.twm.stopped_keys, ['socket-btc'])
        self.assertEqual(out.getvalue().count('ReadLoopClosed'), 1)
        self.assertEqual(out.getvalue().count('listener stopped'), 1)

    def test_different_group_gets_independent_stop(self):
        with redirect_stdout(io.StringIO()):
            self.send_error(group=1)
            self.send_error(group=2, kind='BinanceWebsocketQueueOverflow')
        self.assertEqual(self.ws.twm.stopped_keys, ['socket-btc', 'socket-eth'])

    def test_stale_callback_cannot_stop_new_generation(self):
        with redirect_stdout(io.StringIO()):
            self.send_error(generation=8)
        self.assertEqual(self.ws.twm.stopped_keys, [])
        self.assertTrue(self.ws.is_connected)
        self.assertFalse(self.ws._is_reconnecting)

    def test_normal_kline_still_forwarded(self):
        event = {'stream':'btcusdt@kline_1m','data':{'e':'kline', 'k':{'x': True}}}
        with redirect_stdout(io.StringIO()):
            self.ws._handle_message(event, group_id=1, generation=9)
        self.on_message.assert_called_once_with(event)
        self.assertTrue(self.ws.is_connected)
        self.assertFalse(self.ws._is_reconnecting)

    def test_shutdown_callback_does_not_rearm_connection(self):
        self.ws.running = False
        with redirect_stdout(io.StringIO()):
            self.send_error()
        self.assertEqual(self.ws.twm.stopped_keys, [])
        self.assertFalse(self.ws._is_reconnecting)

    def test_missing_socket_key_still_arms_reconnect(self):
        self.ws._group_socket_keys.clear()
        with redirect_stdout(io.StringIO()):
            self.send_error()
        self.assertFalse(self.ws.is_connected)
        self.assertTrue(self.ws._is_reconnecting)
        self.assertEqual(self.ws.twm.stopped_keys, [])

    def test_stop_socket_exception_still_arms_reconnect(self):
        self.ws.twm.stop_socket = Mock(side_effect=RuntimeError('fake shutdown failure'))
        with redirect_stdout(io.StringIO()) as out:
            self.send_error()
        self.assertIn('stop_socket failed', out.getvalue())
        self.assertTrue(self.ws._is_reconnecting)

    def test_early_group_error_cannot_be_overwritten_by_ready(self):
        class FailedDuringInit(FakeTWM):
            def start_futures_multiplex_socket(self, streams, callback):
                key = super().start_futures_multiplex_socket(streams, callback)
                callback({'e': 'error', 'type': 'BinanceWebsocketQueueOverflow',
                          'm': 'The queue exceeded the allowed size'})
                return key

        class InjectingTWM(FailedDuringInit):
            pass

        self.ws.twm = None
        namespace = self.WSClient._connect.__globals__
        with patch.dict(namespace, {'ThreadedWebsocketManager': InjectingTWM}), \
             patch('time.sleep', return_value=None), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'terminated groups'):
                self.ws._connect()
        self.assertFalse(self.ws.is_connected)

    def test_new_connection_clears_failed_group_state(self):
        self.ws._failed_socket_groups.add(1)
        self.ws.twm = None
        with patch('time.sleep', return_value=None), redirect_stdout(io.StringIO()):
            self.ws._connect()
        self.assertEqual(self.ws._failed_socket_groups, set())
        self.assertTrue(self.ws.is_connected)
        self.assertFalse(self.ws._is_reconnecting)
        self.assertEqual(self.ws._connection_generation, 10)
        self.on_message.assert_called_once()


if __name__ == '__main__':
    unittest.main()
