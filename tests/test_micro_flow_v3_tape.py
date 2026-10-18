import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from research.micro_flow_v3.tape import TapeStore,replay_digest


def msg(a, T=1000, s='ETHUSDT', p='4000.01', q='0.001', m=False):
    return json.dumps({'e':'aggTrade','E':T+5,'s':s,'a':a,'p':p,'q':q,'T':T,'m':m})


class TapeTests(unittest.TestCase):
    def setUp(self):
        self.td=tempfile.TemporaryDirectory()
        self.path=Path(self.td.name)/'tape.sqlite3'

    def tearDown(self):
        self.td.cleanup()

    def test_roundtrip_replay_is_deterministic(self):
        w=TapeStore(self.path,batch_size=2)
        w.marker('CONNECTED')
        self.assertEqual(w.ingest(msg(100),wall_ns=1_000_000_000_000,mono_ns=1),'TRADE')
        self.assertEqual(w.ingest(msg(101,T=1100,m=True),wall_ns=1_100_000_000_000,mono_ns=2),'TRADE')
        w.close()
        self.assertEqual(replay_digest(self.path),replay_digest(self.path))
        c=sqlite3.connect(self.path)
        self.assertEqual(c.execute('select count(*) from tape where kind="TRADE"').fetchone()[0],2)
        self.assertEqual(c.execute('select quality from tape where kind="TRADE"').fetchall(),[('OK',),('OK',)])
        self.assertEqual(c.execute('select price from tape where kind="TRADE" limit 1').fetchone()[0],'4000.01')
        c.close()

    def test_reconnect_gap_and_duplicates_not_erased(self):
        w=TapeStore(self.path,batch_size=2)
        w.ingest(msg(50),wall_ns=2_000_000_000_000,mono_ns=1)
        w.marker('DISCONNECTED:ConnectionError')
        w.close()
        w=TapeStore(self.path,batch_size=2)
        w.marker('CONNECTED')
        w.ingest(msg(53,T=2000),wall_ns=3_000_000_000_000,mono_ns=2)
        w.ingest(msg(53,T=2000),wall_ns=3_000_000_000_001,mono_ns=3)
        w.close()
        c=sqlite3.connect(self.path)
        self.assertEqual([r[0] for r in c.execute('select quality from tape where kind="TRADE"')],
                         ['OK','ID_JUMP_SUSPECTED','ID_NONINCREASING'])
        self.assertEqual(c.execute('select count(*) from tape where kind="MARKER"').fetchone()[0],4)
        c.close()

    def test_symbol_scoping_and_bad_payload(self):
        w=TapeStore(self.path)
        self.assertEqual(w.ingest(msg(1,s='BTCUSDT')), 'INVALID')
        self.assertEqual(w.ingest(msg(2)), 'TRADE')
        w.close()
        with self.assertRaises(ValueError):
            TapeStore(self.path,'BTCUSDT')
        c=sqlite3.connect(self.path)
        self.assertEqual(c.execute('select count(*) from tape where kind="TRADE"').fetchone()[0],1)
        c.close()

    def test_out_of_order_trade_time_flags(self):
        w=TapeStore(self.path)
        w.ingest(msg(4,T=5000),wall_ns=10_000_000_000_000,mono_ns=4)
        w.ingest(msg(5,T=4000),wall_ns=10_000_000_000_001,mono_ns=5)
        w.close()
        con=sqlite3.connect(self.path)
        self.assertIn('TRADE_TIME_REGRESSION',con.execute('select quality from tape where agg_id=5').fetchone()[0])
        con.close()


if __name__=='__main__': unittest.main()
