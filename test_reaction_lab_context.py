import ast
import sys
import tempfile
import sqlite3
import unittest
from pathlib import Path
import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parent))
import reaction_lab_context as ctx
from reaction_lab_auto_paths import encode_path

class ContextTests(unittest.TestCase):
    def test_closed_candles_only_asof(self):
        f=pd.DataFrame({'timestamp':[0,60000,120000,180000],'close':[1,2,3,4]})
        got=ctx.closed_at(f,'1m',180000)
        self.assertEqual(got['timestamp'].tolist(),[0,60000,120000])
        self.assertEqual(ctx.last_boundary(180000,'1m'),120000)

    def test_market_return_uses_two_completed_bars(self):
        f=pd.DataFrame({'timestamp':[0,3600000,7200000], 'close':[100,110,121]})
        self.assertAlmostEqual(ctx.nearest_return(f,3600000,'1h'),10.0)
        self.assertIsNone(ctx.nearest_return(f,0,'1h'))

    def test_followup_never_uses_future_earlier_than_180_bars(self):
        b=[(i*60000,100.,100.1,99.9,100.) for i in range(180)]
        a=ctx.outcomes_from_bars(b,'LONG')
        self.assertEqual(a['tp_0_5_sl_3_h180_outcome'],'TIME_EXIT')
        self.assertIsNone(a['mfe_360m_pct'])
        self.assertIsNotNone(a['mfe_180m_pct'])
        b2=b[:2]
        b2[0]=(0,100,101,96,99)
        a=ctx.outcomes_from_bars(b2,'LONG')
        self.assertEqual(a['tp_0_5_sl_3_h180_outcome'],'AMBIGUOUS')

    def test_followup_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            d=Path(td)
            auto=d/'auto.sqlite3'
            with sqlite3.connect(auto) as con:
                con.executescript('''CREATE TABLE events(event_key TEXT PRIMARY KEY,side TEXT,state TEXT,count INTEGER,last_ts INTEGER,path_blob BLOB,first_seen_utc_ms INTEGER);
                CREATE TABLE event_sources(event_key TEXT,source TEXT);''')
                con.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?)',
                    ('ABC|LONG|0','LONG','ACTIVE',1,0,encode_path([(0,100,101,99,100)]),1))
                con.execute('INSERT INTO event_sources VALUES (?,?)',('ABC|LONG|0','continuous_scan'))
            with ctx.get_database(d/'context.sqlite3',write=True) as con:
                self.assertEqual(ctx.followup_once(con,auto)['updated'],1)
                self.assertEqual(ctx.followup_once(con,auto)['updated'],0)
                row=con.execute('SELECT followup_json FROM followups').fetchone()
                self.assertIn('entry_price',row[0])

    def test_canonical_source_functions_unchanged(self):
        app=Path(__file__).parent/'dashboard/app.py'
        source=app.read_text(encoding='utf-8')
        defs={n.name:n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef)}
        self.assertEqual(set(ctx.HELPERS)-set(defs),set())
        self.assertIn('reaction_lab_global_cohort_v2',source)
        self.assertIn('render_cohort_research',source)

if __name__=='__main__':unittest.main()

class CanonicalCompileTests(unittest.TestCase):
    def test_exact_dashboard_helpers_compile_without_streamlit(self):
        from unittest.mock import patch
        import types
        # An isolated module shim lets us verify AST extraction without importing
        # the real VPS engine or running app.py at module import time.
        detector=types.ModuleType('engine.live.research.swing_detector')
        class FakeSwingDetector:
            def __init__(self,**kwargs):pass
            def detect_all(self,rows):return []
        detector.SwingDetector=FakeSwingDetector
        engine=types.ModuleType('engine');engine.__path__=[]
        live=types.ModuleType('engine.live');live.__path__=[]
        research=types.ModuleType('engine.live.research');research.__path__=[]
        with patch.dict(sys.modules,{'engine':engine,'engine.live':live,
                'engine.live.research':research,'engine.live.research.swing_detector':detector}):
            helpers,digest=ctx.load_canonical(Path(__file__).parent/'dashboard/app.py')
            self.assertEqual(len(digest),20)
            f=pd.DataFrame({'timestamp':list(range(20)),'close':[float(100+i) for i in range(20)]})
            # No mutation of canonical user file and a usable causal RSI function.
            result=helpers['_reaction_driver_rsi_from_candles'](f,21,1,period=14)
            self.assertGreater(result,90)
