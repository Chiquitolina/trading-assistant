"""Offline checks for V3-only transport error markers and generation tagging."""
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from research.micro_flow_v3.recorder import run_live


class SimulatedWSClient:
    def __init__(self, on_message, *, symbols, timeframes, include_agg_trades, **kwargs):
        assert symbols == ['ETHUSDT']
        assert timeframes == ()
        assert include_agg_trades
        self.options = kwargs
        self.on_message = on_message
        self._connection_generation = 0
        self.is_connected = False
        self.fatal_error = None
        self._is_reconnecting = False

    def _handle_message(self, msg, group_id=None, generation=None):
        if generation != self._connection_generation:
            return
        if isinstance(msg, dict) and msg.get('e') == 'error':
            self.is_connected = False
            self._is_reconnecting = True
            return
        self.on_message(msg)

    def start(self):
        now_ms = int(time.time() * 1000)
        def trade(agg_id):
            return {'stream':'ethusdt@aggTrade', 'data': {
                'e':'aggTrade','s':'ETHUSDT', 'a':agg_id,
                'p':'2500','q':'1','T':now_ms,'E':now_ms,'m':False,
            }}
        self._connection_generation = 1
        self.is_connected = True
        self._handle_message(trade(200), generation=1)
        error = {'e':'error','type':'ReadLoopClosed','m':'Read loop has been closed'}
        self._handle_message(error, generation=1)
        self._handle_message(error, generation=1)  # dedup error storm
        self._connection_generation = 2
        self.is_connected = True
        self._handle_message(trade(204), generation=2)
        self._handle_message(error, generation=1)  # stale error not recorded

    def run(self):
        return

    def stop(self):
        self.is_connected = False


class RecorderDiagnosticsTests(unittest.TestCase):
    def test_records_first_transport_error_and_original_generation(self):
        modules = {}
        for module_name in ('engine','engine.live','engine.live.ws'):
            m = types.ModuleType(module_name)
            m.__path__ = []
            modules[module_name] = m
        ws_mod = types.ModuleType('engine.live.ws.ws_client')
        ws_mod.WSClient = SimulatedWSClient
        modules['engine.live.ws.ws_client'] = ws_mod
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'tape.sqlite3'
            with patch.dict(sys.modules, modules):
                run_live('ETHUSDT', path, max_hours=0.00005, min_free_gb=0,
                         min_reconnect_interval=0, reconnect_delay_floor=1)
            with sqlite3.connect(path) as con:
                trades = con.execute(
                    "SELECT agg_id,connection,quality FROM tape WHERE kind='TRADE' ORDER BY seq"
                ).fetchall()
                errors = con.execute(
                    "SELECT connection,reason FROM tape WHERE kind='MARKER' AND reason LIKE 'WS_TRANSPORT_ERROR_OBSERVED:%'"
                ).fetchall()
                self.assertEqual([(x[0],x[1]) for x in trades], [(200,1),(204,2)])
                self.assertEqual(trades[-1][-1], 'ID_JUMP_SUSPECTED')
                self.assertEqual(len(errors),1)
                self.assertEqual(errors[0][0], 1)
                self.assertIn('ReadLoopClosed', errors[0][1])


if __name__ == '__main__':
    unittest.main()
