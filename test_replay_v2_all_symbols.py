#!/usr/bin/env python3
"""Offline test of headless canonical extraction, progress and interrupted resume."""
from pathlib import Path
import contextlib
import importlib.util
import json
import shutil
import sys
import tempfile
from unittest.mock import patch
import pandas as pd

from replay_v2_all_symbols import extract_canonical_engine, run


def main():
    with tempfile.TemporaryDirectory() as d:
        root=Path(d)
        (root/'dashboard').mkdir()
        current=Path(__file__).resolve().parent.parent/'candidate_calendar_month_replay/dashboard/app.py'
        if not current.exists():
            current=Path.cwd()/'dashboard/app.py'
        assert current.exists(), f'Missing fixture {current}'
        shutil.copyfile(current, root/'dashboard/app.py')
        swing_file=root/'engine/live/research/swing_detector.py'
        swing_file.parent.mkdir(parents=True)
        swing_file.write_text('class SwingDetector:\n    pass\n')
        symbols_file=root/'symbols.json'
        symbols_file.write_text(json.dumps(['AAAUSDT','BBBUSDT','CCCUSDT']))
        ns, digest=extract_canonical_engine(root/'dashboard/app.py',root)
        for name in ('_lh_symbol_month_events','_lh_run_id','_lh_append_events','_lh_month_ranges'):
            assert callable(ns[name]), name
        actual=[]
        def mocked(symbol, start, end, **kwargs):
            actual.append(symbol)
            n={'AAAUSDT':1,'BBBUSDT':1,'CCCUSDT':0}[symbol]
            return pd.DataFrame([{'candidate_v1_event_key':symbol+'|LONG|1','entry_timestamp':start+60000,
                                  'symbol':symbol, 'side':'LONG','Outcome':'TP','net_pnl_pct':.4}]*n)
        ns['_lh_symbol_month_events']=mocked
        # Exercise an error once; failed symbol must stay pending.
        with patch('replay_v2_all_symbols.extract_canonical_engine',return_value=(ns,digest)):
            a=run(root,start='2026-09-07',end='2026-09-30',limit=0,expect_symbols=3,
                  batch_size=2,symbols_path=symbols_file,execute=True)
            state=json.loads((root/'reports/candidate_long_horizon/runs'/a/'state.json').read_text())
            assert len(state['processed']['2026-09'])==2
            b=run(root,start='2026-09-07',end='2026-09-30',limit=0,expect_symbols=3,
                  batch_size=3,symbols_path=symbols_file,execute=True)
            assert a==b
            state=json.loads((root/'reports/candidate_long_horizon/runs'/b/'state.json').read_text())
            assert len(state['processed']['2026-09'])==3
            events=pd.read_csv(root/'reports/candidate_long_horizon/runs'/b/'events.csv')
            assert len(events)==2 and events['symbol'].nunique()==2
            assert actual==['AAAUSDT','BBBUSDT','CCCUSDT'],actual
            c=run(root,start='2026-09-07',end='2026-09-30',limit=0,expect_symbols=3,
                  batch_size=3,symbols_path=symbols_file,execute=True)
            assert c==a and len(actual)==3
    print('PASS: canonical engine AST extraction; offline per-symbol checkpoint, resume, dedup, zero-event symbols')

if __name__=='__main__': main()
