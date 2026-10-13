"""Offline tests: no Redis server, no app.py import, no writes to production."""
import json
import math
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import reaction_lab_btc_context as mod


class FakeRedis:
    def __init__(self, data):
        self.data=data
        self.calls=0
    def lrange(self,key,a,b):
        self.calls+=1
        return self.data.get(key,[])[a:] if a<0 else self.data.get(key,[])[a:b+1]


def fixture_data(known):
    data={}
    for sym in ('BTCUSDT','ALTUSDT'):
        for tf,period in mod.TF_MS.items():
            newest=(known//period-1)*period
            start=newest-period*(400-1)
            candles=[]
            for i in range(400):
                ts=start+i*period
                btc=100 + .018*i + math.sin(i*.16)*1.2 + math.cos(i*.044)*.8
                px=btc if sym=='BTCUSDT' else 5 + btc*.04 + math.sin(i*.29)*.22
                candles.append(json.dumps({'timestamp':ts,'close':px}).encode())
            data[f'market-data:v1:history:{sym}:{tf}']=candles
    return data


def install_protocol():
    module=types.ModuleType('engine.live.data.redis_market_data_protocol')
    module.history_key=lambda sym,tf:f'market-data:v1:history:{sym}:{tf}'
    return patch.dict(sys.modules,{'engine':types.ModuleType('engine'),
            'engine.live':types.ModuleType('engine.live'),
            'engine.live.data':types.ModuleType('engine.live.data'),
            'engine.live.data.redis_market_data_protocol':module})


class BTCContextTests(unittest.TestCase):
    def setUp(self):
        self.known=(2_000_000_000_000//mod.TF_MS['4h'])*mod.TF_MS['4h']
        self.redis=FakeRedis(fixture_data(self.known))

    def test_asof_excludes_open_and_future_candles(self):
        with install_protocol():
            tf='5m';step=mod.TF_MS[tf]
            key='market-data:v1:history:ALTUSDT:5m'
            self.redis.data[key].extend([
                json.dumps({'timestamp':self.known,'close':99999}).encode(),
                json.dumps({'timestamp':self.known+step,'close':99999}).encode()])
            buf=mod.RedisAsofBuffer(self.redis,self.known)
            candles=buf.get_candles('ALTUSDT','5m')
            self.assertEqual(candles[-1]['timestamp'],self.known-step)
            self.assertTrue(all(c['close']!=99999 for c in candles))
            proof,quality=buf.evidence('ALTUSDT')
            self.assertEqual(proof['ALTUSDT']['5m'][-1][0],self.known-step)
            self.assertEqual(quality['5m']['ALTUSDT']['bars'],398)  # 400 Redis slots include 2 still-open/future rows

    def test_missing_latest_bar_returns_no_stale_context(self):
        period=mod.TF_MS['1m']
        rows=[json.dumps({'timestamp':self.known-2*period,'close':50})]
        self.assertEqual(mod.contiguous_asof(rows,'1m',self.known),[])

    def test_gap_restricts_valid_return_samples(self):
        with install_protocol():
            key='market-data:v1:history:ALTUSDT:5m'
            del self.redis.data[key][-40]
            buf=mod.RedisAsofBuffer(self.redis,self.known)
            self.assertEqual(len(buf.get_candles('ALTUSDT','5m')),39)
            state,features,quality,blob=mod.compute_event(
                ('ALTUSDT|LONG|1','ALTUSDT','LONG',self.known,'LIVE_OBSERVED'),buf,'vtest')
            self.assertEqual(state,'PARTIAL')
            self.assertFalse(features['btc_corr_available_5m_24h'])
            self.assertEqual(features['btc_context_known_ts'],self.known)

    def test_canonical_corr_velocity_and_evidence(self):
        with install_protocol():
            buf=mod.RedisAsofBuffer(self.redis,self.known)
            state,features,quality,blob=mod.compute_event(
                ('ALTUSDT|LONG|1','ALTUSDT','LONG',self.known,'LIVE_OBSERVED'),buf,'vtest')
            self.assertEqual(state,'READY')
            self.assertTrue(features['btc_corr_available_5m_24h'])
            self.assertEqual(features['btc_velocity_state'],
                mod.BTCVelocityContextService().evaluate(buf,self.known).state)
            self.assertEqual(features['btc_context_status'],'READY')
            restored=json.loads(mod.zlib.decompress(blob).decode('utf-8'))
            self.assertEqual(restored['known_ts'],self.known)
            for sym,tf_dict in restored['price_closes_asof'].items():
                for tf,bars in tf_dict.items():
                    self.assertTrue(all(ts+mod.TF_MS[tf]<=self.known for ts,close in bars))
            # original references used, not guessed coefficients
            ref=mod.BTCCorrelationAnalyzer(buf).analyze('ALTUSDT')
            self.assertEqual(features['btc_beta_5m_4h'],ref['btc_beta_5m_4h'])
            self.assertEqual(features['btc_corr_1h'],ref['btc_corr_1h'])

    def test_no_lookahead_after_future_price_changes(self):
        with install_protocol():
            first=mod.compute_event(('e','ALTUSDT','LONG',self.known,'LIVE_OBSERVED'),
                            mod.RedisAsofBuffer(self.redis,self.known),'test')[1]
            for tf,step in mod.TF_MS.items():
                for sym in ('BTCUSDT','ALTUSDT'):
                    key=f'market-data:v1:history:{sym}:{tf}'
                    self.redis.data[key].extend([
                        json.dumps({'timestamp':self.known+i*step,'close':999999}).encode()
                        for i in range(3)])
            second=mod.compute_event(('e','ALTUSDT','LONG',self.known,'LIVE_OBSERVED'),
                            mod.RedisAsofBuffer(self.redis,self.known),'test')[1]
            self.assertEqual(first,second)

    def test_prospective_start_and_idempotent_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp, install_protocol():
            root=Path(tmp)
            for name in ('reaction_lab_btc_correlation_reference.py',
                         'reaction_lab_btc_velocity_reference.py',
                         'reaction_lab_btc_context.py'):
                (root/name).write_bytes((Path(__file__).parent/name).read_bytes())
            scanner=root/'scanner.sqlite3'
            with sqlite3.connect(scanner) as con:
                con.execute('CREATE TABLE events(event_key TEXT,symbol TEXT,side TEXT,known_ts INTEGER,observation_type TEXT)')
                con.executemany('INSERT INTO events VALUES (?,?,?,?,?)',[
                    ('older','ALTUSDT','LONG',self.known-1,'LIVE_OBSERVED'),
                    ('future','ALTUSDT','SHORT',self.known+1,'RECOVERED_WINDOW')])
            db=root/'pilot.sqlite3'
            with mod.open_writer(db,activation_ms=self.known) as con:
                pass
            a=mod.run_once(root,db,scanner,limit=10,redis_client=self.redis,min_free_mb=0)
            self.assertEqual(a['inserted'],1)
            self.assertEqual(a['ready']+a['partial'],1)
            b=mod.run_once(root,db,scanner,limit=10,redis_client=self.redis,min_free_mb=0)
            self.assertEqual(b['inserted'],0)
            with sqlite3.connect(db) as con:
                keys=con.execute('SELECT event_key FROM btc_events').fetchall()
                self.assertEqual(keys,[('future',)])
                self.assertEqual(int(con.execute("SELECT value FROM meta WHERE key='feature_start_ms'").fetchone()[0]),self.known)
            self.assertEqual(mod.status(db)['total'],1)

    def test_view_reads_sidecar_by_event_key(self):
        import reaction_lab_research_view as view
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'reports/reaction_lab/btc_context_v1.sqlite3'
            with mod.open_writer(path,activation_ms=self.known) as con:
                con.execute('INSERT INTO btc_events VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    ('evt',self.known,self.known,'ALTUSDT','LONG','LIVE_OBSERVED','READY',
                     'test',mod.dumps({'btc_corr_5m_4h':.88,'btc_velocity_state':'HEALTHY',
                         'btc_context_status':'READY'}),'{}',b'blob'))
                con.commit()
            cohort_module=types.ModuleType('reaction_lab_cohort_view')
            cohort_module.load_reaction_cohort=lambda _root,mode='Continuous V1':{
                 'rows':[{'event_key':'evt','symbol':'ALTUSDT','signal':'LONG',
                          'cohort':'Continuous V1','known_ts':self.known}], 'errors':[]}
            with patch.dict(sys.modules,{'reaction_lab_cohort_view':cohort_module}):
                df,_=view.continuous_research_frame(root)
            self.assertEqual(df.loc[0,'btc_corr_5m_4h'],.88)
            self.assertEqual(df.loc[0,'btc_velocity_state'],'HEALTHY')

    def test_frozen_evidence_replays_without_redis(self):
        from audit_reaction_lab_btc_context import replay_one
        with install_protocol():
            event=('ALTUSDT|LONG|1','ALTUSDT','LONG',self.known,'LIVE_OBSERVED')
            state,features,quality,blob=mod.compute_event(event,
                        mod.RedisAsofBuffer(self.redis,self.known),'fingerprint')
            record=replay_one(event[0],event[1],event[2],event[3],
                              mod.dumps(features),blob,'fingerprint','fingerprint')
            self.assertEqual(record['result'],'PASS')
            self.assertGreater(record['compared'],30)
            broken=dict(features);broken['btc_beta_5m_4h']=99
            record=replay_one(event[0],event[1],event[2],event[3],
                              mod.dumps(broken),blob,'fingerprint','fingerprint')
            self.assertEqual(record['result'],'DIFF')
            self.assertEqual(replay_one(event[0],event[1],event[2],event[3],
                              mod.dumps(features),blob,'fingerprint','other')['result'],
                             'VERSION_MISMATCH')

    def test_analyzer_exposes_btc_velocity_factors(self):
        from reaction_lab_runner_analyzer import NUMERIC_FACTORS,CATEGORICAL_FACTORS
        self.assertEqual(NUMERIC_FACTORS['BTC velocidad 15m (%)'][0],'btc_velocity_15m')
        self.assertEqual(CATEGORICAL_FACTORS['BTC trade alignment'],'btc_trade_alignment')

if __name__=='__main__':unittest.main()
