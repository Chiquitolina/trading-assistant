import csv
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

import legacy_immediate_fail_reversal_v19 as m


def candles(rows, start=0):
    return {k:np.array([r[i] for r in rows],dtype=np.int64 if k=='timestamp' else float)
            for i,k in enumerate(('timestamp','open','high','low','close'))}


class TestInversion(unittest.TestCase):
    def test_side_reverse(self):
        self.assertEqual(m.inverted('LONG'),'SHORT')
        self.assertEqual(m.inverted('SHORT'),'LONG')
        with self.assertRaises(ValueError):m.inverted('BOTH')

    def test_short_profit_after_failed_long(self):
        x=candles([[0,100,100,99.8,99.9],[60000,99.9,99.9,98.3,98.5]])
        r=m._constant_tpsl_evaluator(x,0,'SHORT',1.,1.,2)
        self.assertEqual(r['status'],'TP_FIRST')
        self.assertGreater(r['net_pct'],0)
        o=m._constant_tpsl_evaluator(x,0,'LONG',1.,1.,2)
        self.assertEqual(o['status'],'SL_FIRST')

    def test_long_profit_after_failed_short(self):
        x=candles([[0,100,100.15,100,100.1],[60000,100,101.5,99.9,101.4]])
        r=m._constant_tpsl_evaluator(x,0,'LONG',1,1,2)
        self.assertEqual(r['status'],'TP_FIRST')

    def test_conservative_ambiguity(self):
        x=candles([[0,100,102,98,100]])
        r=m._constant_tpsl_evaluator(x,0,'LONG',1,1,1)
        self.assertEqual(r['status'],'SL_AMBIGUOUS')
        self.assertLess(r['net_pct'],0)

    def test_censored_never_timeout(self):
        x=candles([[0,100,100.1,99.9,100],[60000,100,100.1,99.9,100]])
        r=m._constant_tpsl_evaluator(x,0,'LONG',1,1,3)
        self.assertEqual(r['status'],'CENSORED_END')
        self.assertIsNone(r['net_pct'])

    def test_timeout_exit(self):
        x=candles([[0,100,100.1,99.9,100.05],[60000,100.05,100.1,99.9,100.08]])
        r=m._constant_tpsl_evaluator(x,0,'LONG',2,1,2)
        self.assertEqual(r['status'],'TIMEOUT')
        self.assertAlmostEqual(r['held_minutes'],2)

    def test_gap_detected(self):
        x=candles([[0,100,100.1,99.9,100],[120000,100,100.1,99.9,100]])
        r=m._constant_tpsl_evaluator(x,0,'LONG',1,1,2)
        self.assertEqual(r['status'],'CENSORED_GAP')
        self.assertIsNone(r['net_pct'])

    def test_intrabar_doesnt_enter_before_failed_bar_closed(self):
        x={'timestamp':np.array([0,60000],dtype=np.int64),'open':np.array([100,99.9]),
           'high':np.array([100,100]),'low':np.array([98,98.5]),'close':np.array([99,99])}
        r=m._constant_tpsl_evaluator(x,1,'SHORT',0.5,0.5,1)
        self.assertEqual(r['status'],'TP_FIRST')
        self.assertEqual(r['exit_ts'],60000)

    def test_portfolio_one_slot(self):
        base=dict(period='september',trade_direction='INVERSE',tp_pct=1.0,sl_pct=1.0,
                  horizon_min=60,net_pct=1.0,status='TP_FIRST',symbol='BTCUSDT')
        a=dict(base,entry_ts=60000,exit_ts=180000,event_id='a')
        b=dict(base,entry_ts=120000,exit_ts=180000,event_id='b')
        c=dict(base,entry_ts=240000,exit_ts=270000,event_id='c')
        out=m.portfolio([b,c,a],'september','INVERSE',(1.0,1.0,60))
        self.assertEqual(out['trades'],2)
        self.assertEqual(out['skipped_slot_busy'],1)
        self.assertEqual(out['selected_event_ids'],['a','c'])

    def test_native_csv_and_15m(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'c.csv'
            with p.open('w',newline='') as h:
                w=csv.writer(h);w.writerow(m.CANDLE_COLS)
                for i in range(45):w.writerow([i*60000,10,11,9,10,2,20])
            frame=m.read_candles(p)
            fif=m.aggregate_15m(frame)
            self.assertEqual(len(fif),3)
            self.assertEqual(int(fif.volume.iloc[0]),30)
            self.assertEqual(int(fif.timestamp.iloc[-1]),30*60000)

    def test_native_missing_gap_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'c.csv'
            with p.open('w',newline='') as h:
                w=csv.writer(h);w.writerow(m.CANDLE_COLS)
                w.writerow([0,10,11,9,10,2,20]);w.writerow([120000,10,11,9,10,2,20])
            with self.assertRaisesRegex(ValueError,'Gap'):
                m.read_candles(p)

    def test_persisted_grid_columns_no_collisions(self):
        r=dict(period='october',trade_direction='INVERSE',tp_pct=1.0,sl_pct=1.0,horizon_min=30,
               net_pct=0.1,status='TP_FIRST')
        grid=m.report_grid([r])
        self.assertEqual(grid[0]['tp'],1.0)
        self.assertEqual(grid[0]['tp_first'],1)
        self.assertEqual(grid[0]['sl_first'],0)

    def test_fail_classification_uses_canonical_helper(self):
        # Run the *installed* AST compiler + Legacy functions in an isolated subprocess.
        import sys, subprocess
        root=Path(__file__).resolve().parent
        # In a development fixture without the VPS sources, create a standalone source tree.
        if not (root/'dashboard/app.py').is_file():
            candidate=Path('/mnt/data/files/FAIL_TOUCH_ANALYZER_LEGACY_V1_ARCHIVOS_COMPLETOS.zip')
            original=Path('/mnt/data/files/legacy_continuous_first_touch_sources.zip')
            engine=Path('/mnt/data/files/pre_reaction_opportunity_sources.zip')
            if not all(p.exists() for p in (candidate,original,engine)):
                self.skipTest('Legacy app and canonical scanner unavailable in local test fixture')
            import zipfile
            tmp=tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            root=Path(tmp.name)
            with zipfile.ZipFile(engine) as z:
                target=root/'engine/live/research/swing_detector.py'
                target.parent.mkdir(parents=True)
                target.write_bytes(z.read('engine/live/research/swing_detector.py'))
            for d in (root/'engine',root/'engine/live',root/'engine/live/research'):
                (d/'__init__.py').touch()
            with zipfile.ZipFile(original) as z:
                (root/'reaction_lab_continuous_scanner.py').write_bytes(z.read('reaction_lab_continuous_scanner.py'))
            with zipfile.ZipFile(candidate) as z:
                (root/'dashboard').mkdir()
                (root/'dashboard/app.py').write_bytes(z.read('dashboard/app.py'))
        check='''\
import sys
sys.path.insert(0,sys.argv[1])
from reaction_lab_continuous_scanner import compile_canonical_scanner
scan, fp=compile_canonical_scanner(__import__('pathlib').Path(sys.argv[1])/'dashboard/app.py',lambda *a,**kw: None)
find=scan.__globals__['_find_confirmed_swing_retest']
import pandas as pd
rows=[]
for i,(op,hi,lo,cl) in enumerate([(100,100.4,100,100.3),(100.3,100.3,99.6,99.75)]):
 rows.append(dict(timestamp=i*60000,open=op,high=hi,low=lo,close=cl,volume=1))
r=find(pd.DataFrame(rows), dict(actionable_timestamp=0,pivot_timestamp=-900000, pivot_price=100, signal='LONG'),0.1,0.2,360)
assert r['status']=='TOUCH_FAILED',r
assert r['retest_timestamp']==60000
'''
        x=subprocess.run([sys.executable,'-c',check,str(root)],capture_output=True,text=True)
        self.assertEqual(x.returncode,0,x.stderr)

if __name__=='__main__': unittest.main()
