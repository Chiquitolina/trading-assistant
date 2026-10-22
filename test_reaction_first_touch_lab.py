"""Offline deterministic tests for frozen full-universe first touch."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from reaction_lab_auto_paths import encode_path
import reaction_first_touch_lab as lab

BASE=1800000000000
M=60000

def event(symbol,origin='LIVE_OBSERVED',ts=BASE):
    return dict(event_key=f'{symbol}|LONG|{ts}',symbol=symbol,side='LONG',retest_ts=ts,
                known_ts=ts+M,first_seen_ms=ts+M+5000,observation_type=origin,
                source_hash='frozen',causal_json='{}',quality='READY')

def bars(start, first_tp=None,first_sl=None, same=None, count=360):
    arr=[]
    for i in range(count):
        o=100.;h=100.1;l=99.9;c=100.
        if i==first_tp:h=101.2
        if i==first_sl:l=99.2
        if i==same:h=101.2;l=99.2
        arr.append((start+i*M,o,h,l,c))
    return arr

class FirstTouchTests(unittest.TestCase):
    def test_first_touch_uses_absolute_horizon_and_rejects_ties(self):
        bs=bars(BASE+M,first_tp=10,first_sl=15)
        self.assertEqual(lab.first_touch(bs,0,30,'LONG',100,1,.5),('TP_FIRST',11))
        self.assertEqual(lab.first_touch(bs,0,5,'LONG',100,1,.5),('NO_HIT',None))
        self.assertEqual(lab.first_touch(bs,12,30,'LONG',100,1,.5),('SL_FIRST',16))
        self.assertEqual(lab.first_touch(bs,12,5,'LONG',100,1,.5),('NOT_ENTERED',None))
        bs=bars(BASE+M,same=0)
        self.assertEqual(lab.first_touch(bs,0,5,'LONG',100,1,.5),('AMBIGUOUS',1))
        bs[0]=(bs[0][0],100.,100.6,98.8,100.)
        self.assertEqual(lab.first_touch(bs,0,5,'SHORT',100,1,.5),('AMBIGUOUS',1))

    def test_closed_precontext_excludes_retest_and_uses_btc(self):
        with tempfile.TemporaryDirectory() as td:
            db=Path(td)/'x.db'
            with sqlite3.connect(db) as con:
                con.execute('CREATE TABLE candles(symbol TEXT,timeframe TEXT,open_ts INTEGER,high REAL,low REAL,close REAL,quote_volume REAL,trades INTEGER,taker_buy_quote REAL)')
                rows=[]
                for sym in ('BTCUSDT','AUSDT'):
                    for i in range(180):
                        rows.append((sym,'1m',BASE-(180-i)*M,100.2,99.8,100.,100.,4,50.))
                    rows.append((sym,'1m',BASE,200,1,100,100,4,50))
                con.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',rows)
                a=lab.get_pre_context(con,'AUSDT',BASE)
                self.assertAlmostEqual(a['range15'],0.4)
                self.assertEqual(a['trades5'],20)
                self.assertEqual(a['taker5'],0.5)
                self.assertEqual(a['return60'],0)
                self.assertEqual(a['vol60'],0)

    def test_short_tp_before_stop_and_invalid_path(self):
        bs=bars(BASE+M)
        bs[2]=(bs[2][0],100.,100.2,98.7,100.)
        bs[5]=(bs[5][0],100.,100.9,99.9,100.)
        self.assertEqual(lab.first_touch(bs,0,15,'SHORT',100,1,.5),('TP_FIRST',3))
        bs[8]=(bs[8][0]+M,100.,100.1,99.9,100.)
        e=event('AUSDT')
        self.assertEqual(lab.validate_path(bs,(BASE+M,'COMPLETE',360),e),'INVALID_CONTINUITY')

    def test_no_hit_and_open_observation_minute(self):
        bs=bars(BASE+M)
        e=event('AUSDT')
        e['first_seen_ms']=BASE+M
        self.assertEqual(lab.observed_index(bs,e),0)
        self.assertEqual(lab.first_touch(bs,0,180,'LONG',100,1,.5),('NO_HIT',None))

    def test_observed_recovered_distinction(self):
        bs=bars(BASE+M)
        self.assertEqual(lab.observed_index(bs,event('AUSDT')),1)
        self.assertIsNone(lab.observed_index(bs,event('BUSD','RECOVERED_WINDOW')))

    def test_read_only_plan_and_end_to_end(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); base=root/lab.REL;base.mkdir(parents=True)
            ctx=base/'reaction_full_context_v1.sqlite3'
            ap=base/'auto_paths_v1.sqlite3'
            events=[event('AUSDT'),event('BUSDT','RECOVERED_WINDOW'),event('CUSDT')]
            with sqlite3.connect(ctx) as con:
                con.executescript('''CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
                   CREATE TABLE events(event_key TEXT,symbol TEXT,side TEXT,retest_ts INTEGER,known_ts INTEGER,first_seen_ms INTEGER,observation_type TEXT,source_hash TEXT,causal_json TEXT,quality TEXT);
                   CREATE TABLE candles(symbol TEXT,timeframe TEXT,open_ts INTEGER,open REAL,high REAL,low REAL,close REAL,quote_volume REAL,trades INTEGER,taker_buy_quote REAL);''')
                con.executemany('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)',
                    [[e[k] for k in lab.INPUT_EVENT_COLS] for e in events])
                s='\n'.join(json.dumps(e,sort_keys=True,ensure_ascii=False,separators=(',',':')) for e in events)
                sha=hashlib.sha256(s.encode()).hexdigest()
                con.executemany('INSERT INTO meta VALUES (?,?)',[('cohort_sha256',sha),('event_count','3')])
                out=root/lab.REL/'reaction_full_context_v1';out.mkdir()
                (out/'audit.json').write_text(json.dumps({'success':True,
                    'frozen_cohort_sha256':sha,'events':3,'ready':3,
                    'incomplete_windows_total':0,'conflicts':0,
                    'aggregate':{'unverified_mismatches_total':0,'verified_provider_exceptions':[]}}))
                rows=[]
                for sym in ('BTCUSDT','AUSDT','BUSDT','CUSDT'):
                    for i in range(180):
                        rows.append((sym,'1m',BASE-(180-i)*M,100.,100.2,99.8,100.,100.,10,50.))
                con.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)',rows)
            with sqlite3.connect(ap) as con:
                con.executescript('CREATE TABLE events(event_key TEXT,entry_ts INTEGER,state TEXT,count INTEGER,path_blob BLOB);')
                con.executemany('INSERT INTO events VALUES (?,?,?,?,?)',[
                    (events[0]['event_key'],BASE+M,'COMPLETE',360,encode_path(bars(BASE+M,first_tp=3))),
                    (events[1]['event_key'],BASE+M,'COMPLETE',360,encode_path(bars(BASE+M,first_sl=1))),
                ])
            self.assertFalse((base/'reaction_first_touch_lab_v1').exists())
            p=lab.plan(root)
            self.assertEqual(p['frozen_events'],3)
            self.assertEqual(p['candidate_complete_360'],2)
            self.assertFalse((base/'reaction_first_touch_lab_v1').exists())
            r=lab.run(root)
            self.assertEqual(r['source_events'],3)
            self.assertEqual(r['scenario_rows'],20)
            self.assertEqual(r['path_quality'],{'COMPLETE_VALID':2,'PATH_MISSING':1})
            self.assertEqual(r['complete_by_observation'],{'LIVE_OBSERVED':1,'RECOVERED_WINDOW':1})
            with (base/'reaction_first_touch_lab_v1'/'first_touch_scenarios.csv').open() as f:
                import csv
                rows=list(csv.DictReader(f))
            self.assertEqual(len(rows),20)
            obs_recovered=[x for x in rows if x['entry_mode']=='OBSERVED_OPEN' and x['observation_type']=='RECOVERED_WINDOW']
            self.assertEqual(len(obs_recovered),5)
            self.assertTrue(all(x['result_180m']=='NOT_ENTERED' for x in obs_recovered))
            self.assertTrue(lab.audit(root)['success'])
            with (base/'reaction_first_touch_lab_v1'/'first_touch_scenarios.csv').open('a') as f:
                f.write('tampered\n')
            self.assertFalse(lab.audit(root)['success'])
            # Input unchanged; source export edits do not affect source frozen catalog.
            with sqlite3.connect(ctx) as con:
                con.execute("UPDATE events SET symbol='BROKEN' WHERE symbol='AUSDT'")
                con.commit()
            with self.assertRaises(RuntimeError):lab.plan(root)

if __name__=='__main__':unittest.main()
