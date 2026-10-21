import csv
import json
import sqlite3
import zlib
from contextlib import closing
import math
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import runner_precursor_validation_v2 as v

class ValidationV2Tests(unittest.TestCase):
    def event(self,i,label='RUNNER_3',symbol=None):
        retest=1_790_000_000_000//v.v1.MIN*v.v1.MIN+i*v.v1.MIN
        sym=symbol or f'S{i}USDT'
        return {'event_key':f'{sym}|LONG|{retest}','symbol':sym,'side':'LONG',
             'retest_ts':retest,'known_ts':retest+v.v1.MIN,
             'wave_minute':retest//v.v1.MIN+1,'episode_id':f'EP{i}',
             'class_label':label,'mfe_180_pct':5 if label=='RUNNER_3' else 0,
             'mae_180_pct':1 if label=='RUNNER_3' else 2,'first_3m':10 if label=='RUNNER_3' else None}

    def test_blind_selection_ignores_all_future_labels(self):
        base=[self.event(i, 'RUNNER_3' if i%2 else 'CONTROL_LT3') for i in range(40)]
        a=v.choose_blind(base,15,seed=42)
        rev=[{**x,'class_label':'CONTROL_LT3' if x['class_label']=='RUNNER_3' else 'RUNNER_3',
               'mfe_180_pct':-99,'mae_180_pct':999,'first_3m':None} for x in base]
        b=v.choose_blind(rev,15,seed=42)
        self.assertEqual([x['event_key'] for x in a],[x['event_key'] for x in b])
        self.assertEqual(len(set(x['event_key'] for x in a)),15)

    def test_caps_episodes_symbol_and_waves(self):
        cases=[self.event(i,symbol='AUSDT') for i in range(20)]
        # 1 per symbol, and with a single same-symbol-side trajectory at 1m separation.
        with self.assertRaisesRegex(ValueError,'only'):
            v.choose_blind(cases,2,seed=4,max_symbol=1)
        cases=[self.event(i) for i in range(10)]
        for e in cases:e['wave_minute']=100
        self.assertEqual(len(v.choose_blind(cases,2,max_wave=2)),2)
        with self.assertRaises(ValueError):v.choose_blind(cases,3,max_wave=2)

    def test_no_overlap_with_v1_events_and_episodes(self):
        prior=[self.event(0),self.event(50,'CONTROL_LT3',symbol='S10USDT')]
        same_episode={**self.event(1), 'episode_id':prior[0]['episode_id']}
        too_close=self.event(51,symbol='S10USDT')
        good=self.event(450)
        with patch.object(v.v1,'source_candidates',return_value=([prior[0],same_episode,too_close,good],{})):
            remaining,reasons=v.historical_candidates(Path('/empty'),prior)
        self.assertEqual([x['event_key'] for x in remaining],[good['event_key']])
        self.assertEqual(reasons['frozen_pilot_keys'],1)
        self.assertEqual(reasons['frozen_pilot_episode'],1)
        self.assertEqual(reasons['near_frozen_pilot_same_symbol_side_180m'],1)

    def test_first_touch_and_same_candle_ambiguity(self):
        path=[(i*60000,100.,100.,100.,100.) for i in range(180)]
        path[1]=(60000,100.,103.,99.5,102.)
        self.assertEqual(v.first_touch(path,'LONG',3),( 'TP_FIRST',2))
        self.assertEqual(v.first_touch(path,'SHORT',3,1),('SL_FIRST',2))
        path[1]=(60000,100.,103.,98.,100.)
        self.assertEqual(v.first_touch(path,'LONG',3),('AMBIGUOUS_SAME_1M',2))
        path[1]=(60000,100.,100.,98.,99.)
        self.assertEqual(v.first_touch(path,'LONG',3),('SL_FIRST',2))
        self.assertEqual(v.first_touch(path,'SHORT',2),('TP_FIRST',2))

    def test_percentiles_and_auc(self):
        self.assertEqual(v.percentile([1,2,3,4],.75),3.25)
        self.assertEqual(v.auc([{'runner':True,'score':3},{'runner':False,'score':1}]),1)
        self.assertEqual(v.auc([{'runner':True,'score':2},{'runner':False,'score':2}]),.5)
        self.assertEqual(v.auc([{'runner':True,'score':1},{'runner':False,'score':2}]),0)

    def test_thresholds_are_frozen_from_pilot_only(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);data=root/v.BASE/'runner_precursor_pilot';data.mkdir(parents=True)
            with (data/'features_before_retest.csv').open('w',newline='') as f:
                wr=csv.DictWriter(f,['event_key',v.VOL]);wr.writeheader()
                for i in range(200):wr.writerow({'event_key':f'E{i}','asset_prer_1m_realized_vol_60_pct':i/10})
            got=v.freeze_thresholds(root)
            self.assertAlmostEqual(got['volatility_pilot_p75'],14.925)
            self.assertEqual(len(got['pilot_features_sha256']),64)
            with (data/'features_before_retest.csv').open('a') as f:f.write('E201,99\n')
            with self.assertRaisesRegex(ValueError,'200-row'):v.freeze_thresholds(root)


    def test_end_to_end_offline_export_and_first_touch_report(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            basedir=root/v.BASE
            basedir.mkdir(parents=True)
            anchor=1_790_000_000_000//(15*v.v1.MIN)*(15*v.v1.MIN)
            events=[]
            with closing(sqlite3.connect(basedir/'scanner_events_v1.sqlite3')) as sc, closing(sqlite3.connect(basedir/'auto_paths_v1.sqlite3')) as ap:
                sc.execute('CREATE TABLE events (event_key TEXT PRIMARY KEY, symbol TEXT, side TEXT, retest_ts INTEGER, known_ts INTEGER, first_seen_ms INTEGER, observation_type TEXT, causal_json TEXT)')
                ap.execute('CREATE TABLE events (event_key TEXT PRIMARY KEY, entry_ts INTEGER, retest_ts INTEGER, state TEXT, count INTEGER, path_blob BLOB)')
                for i,(sym,side,label) in enumerate((('TESTLONGUSDT','LONG','RUNNER_3'),('TESTSHORTUSDT','SHORT','CONTROL_LT3'))):
                    retest=anchor+(i+8)*v.v1.MIN
                    known=retest+v.v1.MIN
                    key=f'{sym}|{side}|{retest}'
                    path=[]
                    for n in range(360):
                        high=104. if n==20 and side=='LONG' else 102. if n==20 and side=='SHORT' else 100.
                        path.append((known+n*v.v1.MIN,100.,high,100.,100.))
                    blob=zlib.compress(b''.join(v.v1.BAR.pack(*x) for x in path))
                    ap.execute('INSERT INTO events VALUES (?,?,?,?,?,?)',(key,known,retest,'COMPLETE',360,blob))
                    sc.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)',(key,sym,side,retest,known,known,'LIVE_OBSERVED','{}'))
                    events.append({'event_key':key,'symbol':sym,'side':side,'retest_ts':retest,
                        'known_ts':known,'first_seen_ms':known,'observation_type':'LIVE_OBSERVED',
                        'episode_id':None,'wave_minute':known//v.v1.MIN,'entry_open':100.,
                        'class_label':label,'mfe_180_pct':4. if side=='LONG' else 0.,
                        'mae_180_pct':0. if side=='LONG' else 2.,
                        'first_2m':21 if side=='LONG' else None,
                        'first_3m':21 if side=='LONG' else None,'first_5m':None})
                sc.commit();ap.commit()
            db=basedir/v.NAME
            args=SimpleNamespace(pre1=120,pre15=96,post_audit=15,seed=1,cases=2,controls=0,
                min_free_gb=0,max_requests=1000,max_failures=1,pause=0)
            def fake_fetch(symbol,tf,start,end):
                step=v.v1.TFS[tf]
                factor=1 if tf=='1m' else 15
                return [(ts,100.,100.,100.,100.,10*factor,1000*factor,3*factor,5*factor,500*factor)
                        for ts in range(start,end+1,step)], 'fake'
            v.v1.run(root,db,args,events,fake_fetch)
            with closing(v.v1.connect(db)) as con:
                with con:
                    con.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('purpose','UNBALANCED_HISTORICAL_LABEL_BLIND_REPLICATION'))
                    con.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('volatility_pilot_p75','1'))
                    con.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('pilot_features_sha256','pinnedsha'))
            self.assertTrue(v.v1.audit(root,db)['success'])
            destination=basedir/v.OUT
            v.v1.export(root,db,destination)
            result=v.analyze(root,db,destination)
            self.assertEqual(result['overall']['n'],2)
            self.assertEqual(result['overall']['runners'],1)
            self.assertEqual(result['overall']['tp3_sl1_first']['TP_FIRST'],1)
            self.assertEqual(result['overall']['tp3_sl1_first']['SL_FIRST'],1)
            self.assertTrue((destination/'historical_validation_events.csv').is_file())
            rows=v.read_csv(destination/'historical_validation_events.csv')
            self.assertEqual(set(x['tp3_sl1_state'] for x in rows),{'TP_FIRST','SL_FIRST'})


    def test_v2_13_verified_binance_anomalies_are_exact_and_pinned(self):
        # V2 cross-TF discrepancies were verified against both Binance REST
        # timeframes on 2026-10-21. A new case or mutated observation fails.
        case_specs = [
            ('ACUUSDT', '2026-10-13 21:15'),
            ('ALICEUSDT', '2026-10-17 20:15'),
            ('ASRUSDT', '2026-10-15 23:45'),
            ('ASRUSDT', '2026-10-19 04:30'),
            ('DRIFTUSDT', '2026-10-13 21:30'),
            ('JOEUSDT', '2026-10-13 06:30'),
            ('JOEUSDT', '2026-10-17 20:45'),
            ('KSMUSDT', '2026-10-13 20:30'),
            ('MELANIAUSDT', '2026-10-20 03:30'),
            ('OGUSDT', '2026-10-15 22:45'),
            ('REDUSDT', '2026-10-20 21:45'),
            ('TAUSDT', '2026-10-13 23:45'),
            ('XNYUSDT', '2026-10-16 06:45'),
        ]
        keys = {
            (sym, int(datetime.strptime(day, '%Y-%m-%d %H:%M')
                      .replace(tzinfo=timezone.utc).timestamp() * 1000))
            for sym, day in case_specs
        }
        self.assertEqual(len(keys), 13)
        self.assertTrue(keys.issubset(v.v1.BINANCE_VERIFIED_15M_ANOMALIES))
        for symbol, ts in keys:
            proof = v.v1.BINANCE_VERIFIED_15M_ANOMALIES[(symbol, ts)]
            self.assertIsNotNone(v.v1.exact_verified_provider_anomaly(
                symbol, ts, proof['one_min_aggregate'], proof['binance_15m']))
            self.assertIsNone(v.v1.exact_verified_provider_anomaly(
                symbol, ts + 15 * v.v1.MIN,
                proof['one_min_aggregate'], proof['binance_15m']))
            changed = list(proof['binance_15m'])
            changed[0] += 0.0001
            self.assertIsNone(v.v1.exact_verified_provider_anomaly(
                symbol, ts, proof['one_min_aggregate'], changed))
            changed = list(proof['one_min_aggregate'])
            changed[6] += 1
            self.assertIsNone(v.v1.exact_verified_provider_anomaly(
                symbol, ts, changed, proof['binance_15m']))


if __name__=='__main__':unittest.main()
