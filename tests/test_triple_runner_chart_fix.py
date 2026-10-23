"""Self-contained tests: AST extraction avoids importing unrelated Streamlit/Redis app code."""
import ast
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd

SOURCE = Path(__file__).resolve().parents[1] / 'dashboard/views/reaction_lab_runner_discovery_v2_view.py'
code = SOURCE.read_text(encoding='utf-8')
node = next(n for n in ast.parse(code).body if isinstance(n, ast.FunctionDef) and n.name == '_read_triple_pre_candles')
ns = {'Path':Path}
exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec'), ns)
reader = ns['_read_triple_pre_candles']

class FakeConn:
    def __init__(self, calls):self.calls=calls
    def close(self):self.calls.append('close')

class PreReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        dbdir=self.root/'reports'/'reaction_lab'
        dbdir.mkdir(parents=True)
        (dbdir/'ctx.sqlite3').touch()
        (dbdir/'hist.sqlite3').touch()
        self.calls=[]
        self.atlas=types.ModuleType('reaction_structural_level_atlas')
        self.atlas.lineage=types.SimpleNamespace(ROOT_REL='reports/reaction_lab',
                    CTX_NAME='ctx.sqlite3',HISTORY_NAME='hist.sqlite3',
                    ro=lambda path: FakeConn(self.calls))
        self.v4=types.ModuleType('reaction_compression_breakout_retest_v4')
        base=1_700_000_000_000
        frame=pd.DataFrame([dict(timestamp=base+i*900_000,open=10.,high=11.,low=9.,close=10.)
                            for i in range(180)]).set_index('timestamp')
        self.frame=frame
        self.v4._merge_tf=lambda ctx,hist,symbol:(self.frame,None,None)

    def run_reader(self,ts,max_bars=170):
        with patch.dict(sys.modules,{'reaction_structural_level_atlas':self.atlas,
                                     'reaction_compression_breakout_retest_v4':self.v4}):
            return reader(self.root,'ABCUSDT',ts,max_bars=max_bars)

    def test_strict_before_reaction_close(self):
        start=1_700_000_000_000
        event=start+175*900_000
        x=self.run_reader(event)
        self.assertEqual(len(x),170)
        self.assertTrue(((x.timestamp+900_000)<event).all())
        self.assertEqual(x.timestamp.iloc[-1],start+173*900_000)
        self.assertEqual(x.timestamp.iloc[0],start+4*900_000)
        self.assertEqual(self.calls.count('close'),2)

    def test_missing_db_no_network(self):
        (self.root/'reports/reaction_lab/ctx.sqlite3').unlink()
        with self.assertRaisesRegex(FileNotFoundError,'SQLite Full Context'):
            self.run_reader(1_700_000_000_000+175*900_000)

    def test_no_history_returns_empty(self):
        self.frame=self.frame.iloc[:0]
        out=self.run_reader(1_700_000_000_000+175*900_000)
        self.assertTrue(out.empty)

    def test_duplicate_candles_refused(self):
        self.frame=pd.concat([self.frame,self.frame.iloc[[45]]]).sort_index()
        with self.assertRaisesRegex(ValueError,'duplicados'):
            self.run_reader(1_700_000_000_000+175*900_000)

    def test_no_old_gallery_dependency(self):
        self.assertNotIn('from dashboard.views.reaction_recovered_support_gallery_view import read_ohlc_pre',code)
        self.assertIn('def _read_triple_pre_candles',code)
        self.assertIn('Acercar al patrón',code)

if __name__=='__main__':unittest.main()
