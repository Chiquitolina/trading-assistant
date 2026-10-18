"""Offline regression checks for existing-project WSClient adapter."""
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from research.micro_flow_v3.recorder import _canonical_message, run_live


class FakeWSClient:
    def __init__(self, on_message, *, symbols, timeframes, include_agg_trades, **kwargs):
        assert symbols == ['ETHUSDT']
        assert timeframes == ()
        assert include_agg_trades is True
        self.on_message = on_message
        self.is_connected = False
        self._connection_generation = 0
        self.fatal_error = None

    def start(self):
        self._connection_generation = 1
        self.is_connected = True
        self.on_message({'stream':'ethusdt@aggTrade','data': {
            'e':'aggTrade','s':'ETHUSDT','a':100,
            'p':'2500.0','q':'0.1','T':1792353290000,
            'E':1792353290001,'m':False,
        }})

    def run(self):
        return

    def stop(self):
        self.is_connected = False


class WSClientTransportTests(unittest.TestCase):
    def test_binance_multiplex_payload_encoded_for_tape(self):
        msg = {'stream':'ethusdt@aggTrade', 'data':{'e':'aggTrade','s':'ETHUSDT'}}
        self.assertEqual(json.loads(_canonical_message(msg)), msg)

    def test_one_symbol_capture_on_existing_wsclient(self):
        modules = {}
        for path in ['engine', 'engine.live', 'engine.live.ws']:
            module = types.ModuleType(path)
            module.__path__ = []
            modules[path] = module
        client_mod = types.ModuleType('engine.live.ws.ws_client')
        client_mod.WSClient = FakeWSClient
        modules['engine.live.ws.ws_client'] = client_mod
        with tempfile.TemporaryDirectory() as tmp:
            tape = Path(tmp) / 'test.sqlite3'
            with patch.dict(sys.modules, modules):
                run_live('ETHUSDT', tape, min_free_gb=0, max_hours=0.00007)
            db = sqlite3.connect(tape)
            rows = db.execute('SELECT agg_id,quantity,connection FROM tape WHERE kind="TRADE"').fetchall()
            self.assertEqual(rows, [(100, '0.1', 1)])
            reasons = [x[0] for x in db.execute('SELECT reason FROM tape WHERE kind="MARKER"')]
            self.assertIn('TRANSPORT_PYTHON_BINANCE_DECODED_JSON_NOT_RAW_WIRE', reasons)
            self.assertIn('PROCESS_STOP', reasons)
            db.close()

if __name__=='__main__':
    unittest.main()
