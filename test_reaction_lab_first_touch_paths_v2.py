"""Offline tests. No Redis, HTTP, PM2, LIVE or real journal modified."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import reaction_lab_first_touches_v1 as ft
import reaction_lab_first_touch_paths_v2 as p
from dashboard.views import reaction_lab_first_touch_metrics_view as v


class FakeRedis:
    def __init__(self, bars):self.bars=bars;self.calls=[]
    def lrange(self,key,start,end):
        self.calls.append((key,start,end))
        return self.bars.get(key,[])[start:] if start<0 else self.bars.get(key,[])[start:end+1]


class TestFirstTouchPaths(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        self.now=1_800_000_000_000//p.MINUTE*p.MINUTE
        self.retest=self.now-361*p.MINUTE
        self.journal=ft.default_db(self.project)
        self.db=p.destination_db(self.project)
        self.payloads={}
        for i,(side,status,symbol) in enumerate((
            ('LONG','TOUCH_FAILED','BTCUSDT'),
            ('SHORT','TOUCH_INDECISIVE','ETHUSDT'),
            ('LONG','REACTION','XRPUSDT'),
        )):
            row=dict(symbol=symbol,signal=side,status=status,timeframe='15m',detector='3x3',
                     pivot_timestamp=self.retest-60*p.MINUTE,
                     actionable_timestamp=self.retest-45*p.MINUTE,
                     retest_timestamp=self.retest,retest_close=100.0,swing_price=101.)
            key=ft.touch_identity(row)[0]
            row['legacy_event_key']=ft.touch_identity(row)[1]
            self.payloads[key]=row
        with ft.db_open(self.journal) as con:
            ft.save_cycle(con,self.payloads,self.now,self.now,'src')
        self.by_symbol={}
        for row in self.payloads.values():
            self.by_symbol[f"market-data:v1:history:{row['symbol']}:1m"]=[
                json.dumps(dict(timestamp=self.retest+(j+1)*p.MINUTE,
                                close_timestamp=self.retest+(j+2)*p.MINUTE-1,
                                open=101.0 if j==0 else 100.,
                                high=102.,low=98.,close=100.)) for j in range(360)]
        self.redis=FakeRedis(self.by_symbol)

    def tearDown(self):self.tmp.cleanup()

    def test_store_all_three_and_360_complete(self):
        out=p.run_once(self.project,self.db,self.redis,self.now)
        self.assertEqual(out['registration']['registered'],3)
        self.assertEqual(out['progress']['completed_360'],3)
        s=p.status(self.db)
        self.assertEqual(s['paths'].get('COMPLETE'),3)
        self.assertEqual(s['evaluable']['LEGACY_ORIGINAL_60'],3)
        self.assertEqual(s['evaluable']['INVERSE_360'],3)
        self.assertEqual(s['stored_1m_bars'],1080)
        self.assertEqual(p.run_once(self.project,self.db,self.redis,self.now)['registration']['registered'],0)
        with sqlite3.connect(self.journal) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM first_touches').fetchone()[0],3)

    def test_metric_baselines_different(self):
        m0,a0,r0=p.evaluate([(0,101.,102.,98.,100.)],'LONG',100.)
        self.assertAlmostEqual(m0,2.0)
        self.assertAlmostEqual(a0,2.0)
        self.assertAlmostEqual(r0,0.0)
        mfe,mae,ret=p.evaluate([(0,101.,102.,98.,100.)],'LONG',101.)
        self.assertAlmostEqual(mfe,0.990099,places=5)
        self.assertAlmostEqual(mae,2.970297,places=5)
        self.assertNotEqual(mfe,2.0)
        mfe2,mae2,_=p.evaluate([(0,101.,102.,98.,100.)],'SHORT',101.)
        self.assertAlmostEqual(mfe2,mae,places=5)
        self.assertAlmostEqual(mae2,mfe,places=5)

    def test_ambiguous_tp_sl_is_sl(self):
        oc,minute,gross=p.simulate([(0,100.,102.,98.,101.)],'LONG',1.,1.)
        self.assertEqual((oc,minute,gross),('SL_AMBIGUOUS',1,-1.))
        self.assertEqual(p.simulate([(0,100.,100.5,98.0,99.)],'SHORT',1.,1.)[0],'TP_FIRST')

    def test_truncated_horizons_not_fabricated(self):
        del self.by_symbol['market-data:v1:history:BTCUSDT:1m'][45:]
        p.run_once(self.project,self.db,self.redis,self.now)
        with sqlite3.connect(self.db) as con:
            key=next(k for k,r in self.payloads.items() if r['symbol']=='BTCUSDT')
            self.assertEqual(con.execute('SELECT count FROM paths WHERE touch_key=?',(key,)).fetchone()[0],45)
            self.assertEqual(con.execute('SELECT count(*) FROM metrics WHERE touch_key=? AND horizon=60',(key,)).fetchone()[0],0)
            self.assertEqual(con.execute('SELECT count(*) FROM metrics WHERE touch_key=? AND horizon=30',(key,)).fetchone()[0],3)

    def test_missing_first_minute_not_zero_return(self):
        key=next(k for k,r in self.payloads.items() if r['symbol']=='BTCUSDT')
        self.by_symbol['market-data:v1:history:BTCUSDT:1m']=self.by_symbol['market-data:v1:history:BTCUSDT:1m'][10:]
        p.run_once(self.project,self.db,self.redis,self.now)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute('SELECT state FROM paths WHERE touch_key=?',(key,)).fetchone()[0],'MISSING_ENTRY')
            self.assertEqual(con.execute('SELECT COUNT(*) FROM metrics WHERE touch_key=?',(key,)).fetchone()[0],0)

    def test_overview_and_per_event_with_no_writes(self):
        p.run_once(self.project,self.db,self.redis,self.now)
        before=self.journal.read_bytes()
        s=v.overview(self.journal,self.db,scope='all',mode='LEGACY_ORIGINAL',horizon=60)
        self.assertEqual(s['total'],3)
        self.assertEqual(len([r for r in s['metrics'] if r['horizon']==60]),4)
        self.assertEqual(next(r for r in s['metrics'] if r['status']=='TODOS' and r['horizon']==60)['N'],3)
        key=next(k for k,r in self.payloads.items() if r['status']=='TOUCH_FAILED')
        page=v.page_metrics(self.journal,self.db,[key],60,'INVERSE')
        self.assertIn('MFE_pct',page[key])
        self.assertIn('TP_SL',page[key])
        self.assertEqual(self.journal.read_bytes(),before)
        self.assertEqual(v.overview(self.journal,self.db,scope='filtered',status='TOUCH_FAILED')['total'],1)
        self.assertEqual(v.overview(self.journal,self.db,scope='last')['total'],3)

    def test_corrupt_and_invalid_bars_rejected(self):
        good=json.dumps(dict(timestamp=self.now-p.MINUTE,open=100,high=101,low=99,close=100))
        bad=json.dumps(dict(timestamp=self.now,open=100,high=1,low=99,close=100))
        self.assertEqual(len(p.normalize_bars([good,bad,b'invalid'],self.now)),1)
        with self.assertRaises((ValueError,p.zlib.error)):p.decode(b'nozlib')

    def test_project_store_independent(self):
        self.assertNotEqual(self.db,p.source_db(self.project))
        with self.assertRaises(ValueError):p.run_once(self.project,self.journal,self.redis,self.now)

if __name__=='__main__':unittest.main()
