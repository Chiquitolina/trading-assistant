import json
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
import reaction_lab_v1_regime_adapter as adapter
import reaction_lab_context as ctx


class StubPipe:
    def __init__(self, histories):
        self.histories=histories; self.keys=[]
    def lrange(self,key,start,end):
        self.keys.append(key)
        return self
    def execute(self):
        return [self.histories.get(k,[]) for k in self.keys]


class StubRedis:
    def __init__(self, histories):
        self.histories=histories; self.pipelines=0
    def pipeline(self,transaction=False):
        self.pipelines+=1
        return StubPipe(self.histories)


class CanonicalStub:
    @staticmethod
    def _universe_signature(universe):
        return 'v1:' + ','.join(universe)

    @staticmethod
    def _parse_histories(universe,raw_histories):
        result={}
        for symbol,raw in zip(universe,raw_histories):
            candles=[json.loads(row) if isinstance(row,str) else dict(row) for row in raw]
            candles=sorted(candles,key=lambda c:c['timestamp'])
            result[symbol]=(candles,{c['timestamp']:i for i,c in enumerate(candles)})
        return result

    @staticmethod
    def _build_boundary_snapshot(boundary,histories,universe,sectors,baseline,min_sector,signature):
        import pandas as pd
        result=[]
        for symbol in universe:
            candles,index=histories[symbol]
            i=index.get(boundary)
            if i is None or i<baseline:continue
            result.append({'symbol':symbol,'return_pct_4h':1.5,
                           'relative_volume_4h':2.25,'return_rank_pct_4h':70.0,
                           'volume_rank_pct_4h':90.0,'market_breadth_4h':50.0,
                           'primary_sector':'DeFi','sector_return_pct_4h':0.3,
                           'sector_breadth_4h':55,'sector_relative_volume_4h':1.3,
                           'sector_return_rank_pct_4h':80,
                           'sector_strength_vs_btc_4h':0.2,
                           'symbol_strength_vs_sector_4h':0.5,
                           'sector_coverage_pct':100.0})
        return pd.DataFrame(result)


class V1RegimeTests(unittest.TestCase):
    def test_boundary_strictly_closed(self):
        ms=adapter.FOUR_H_MS
        self.assertEqual(adapter.event_boundary(ms*51),ms*50)
        self.assertEqual(adapter.event_boundary(ms*51+1000),ms*50)
        self.assertEqual(adapter.event_boundary(ms*50+1000),ms*49)

    def test_missing_sector_keeps_ranks_independent(self):
        row={'return_pct_4h':1,'relative_volume_4h':2,'return_rank_pct_4h':80,
             'volume_rank_pct_4h':60,'market_breadth_4h':45,'primary_sector':'Other'}
        self.assertTrue(adapter.required_rank_available(row))
        self.assertFalse(adapter.sector_available(row))
        row['relative_volume_4h']=None
        self.assertFalse(adapter.required_rank_available(row))

    def test_noncontiguous_history_rejected(self):
        ms=adapter.FOUR_H_MS
        candles=[{'timestamp':i*ms} for i in range(52)]
        candles[9]['timestamp']=100*ms
        histories={'AAA':(candles,{50*ms:50})}
        with self.assertRaisesRegex(RuntimeError,'Non-contiguous'):
            adapter.check_contiguous_reference(histories,50*ms,['AAA'])

    def test_snapshot_dedup_idempotence_and_no_core_rewrite(self):
        ms=adapter.FOUR_H_MS
        symbols=('AAAUSDT','BTCUSDT')
        rows=[{'timestamp':i*ms,'open':10,'high':11,'low':9,'close':10+i/100,
               'volume':20,'quoteVolume':1000+i*2} for i in range(55)]
        raw=[json.dumps(row).encode() for row in rows]
        histories={f'history:{symbol}:4h':raw for symbol in symbols}
        redis=StubRedis(histories)
        with tempfile.TemporaryDirectory() as td:
            project=Path(td)
            mfile=project/'dashboard/analytics/candidate_v1_market_regime.py'
            mfile.parent.mkdir(parents=True)
            mfile.write_text('# frozen reference hash')
            db=project/'context.db'
            with ctx.get_database(db,write=True) as con:
                for symbol in symbols:
                    con.execute('''INSERT INTO contexts VALUES (?,?,?,?,?,?,?,?,?)''',
                                (f'{symbol}|LONG|1',51*ms,1,1,'READY','hash',
                                 json.dumps({'symbol':symbol,'return_pct_4h':7.123}),
                                 '{}','[]'))
                con.commit()
                original=con.execute('SELECT context_json FROM contexts ORDER BY event_key').fetchall()
                protocol=types.ModuleType('engine.live.data.redis_market_data_protocol')
                protocol.history_key=lambda sym,tf:f'history:{sym}:{tf}'
                engine=types.ModuleType('engine');engine.__path__=[]
                live=types.ModuleType('engine.live');live.__path__=[]
                data=types.ModuleType('engine.live.data');data.__path__=[]
                market_config=types.ModuleType('config.market_sectors')
                market_config.MIN_SECTOR_SYMBOLS=1
                config=types.ModuleType('config');config.__path__=[]
                with patch.dict(sys.modules,{'engine':engine,'engine.live':live,
                        'engine.live.data':data,
                        'engine.live.data.redis_market_data_protocol':protocol,
                        'config':config,'config.market_sectors':market_config}),\
                     patch.object(adapter,'canonical_module',return_value=CanonicalStub):
                    result=adapter.enrich_once(con,redis,project,symbols,
                                               {'AAAUSDT':'DeFi','BTCUSDT':'DeFi'},2)
                    self.assertEqual(result['ready'],2)
                    self.assertEqual(result['new_snapshots'],1)
                    self.assertEqual(redis.pipelines,1)
                    self.assertEqual(con.execute('SELECT COUNT(*) FROM v1_regime_snapshots').fetchone()[0],1)
                    self.assertEqual(con.execute('SELECT COUNT(*) FROM v1_regime_events').fetchone()[0],2)
                    enrich=con.execute('SELECT metrics_json FROM v1_regime_events LIMIT 1').fetchone()[0]
                    self.assertEqual(json.loads(enrich)['relative_volume_4h'],2.25)
                    self.assertEqual(con.execute('SELECT context_json FROM contexts ORDER BY event_key').fetchall(),original)
                    rerun=adapter.enrich_once(con,redis,project,symbols,
                                              {'AAAUSDT':'DeFi','BTCUSDT':'DeFi'},2)
                    self.assertEqual(rerun['checked'],0)
                    self.assertEqual(redis.pipelines,1)
                    self.assertEqual(con.execute('SELECT COUNT(*) FROM v1_regime_events').fetchone()[0],2)

    def test_failed_event_is_deferred_and_does_not_starve_next(self):
        ms=adapter.FOUR_H_MS
        with tempfile.TemporaryDirectory() as td:
            project=Path(td)
            mfile=project/'dashboard/analytics/candidate_v1_market_regime.py'
            mfile.parent.mkdir(parents=True);mfile.write_text('frozen')
            with ctx.get_database(project/'db.sqlite3',write=True) as con:
                for symbol,known in [('NEWUSDT',51*ms),('OLDUSDT',50*ms)]:
                    con.execute('INSERT INTO contexts VALUES (?,?,?,?,?,?,?,?,?)',
                                (symbol+'|LONG|1',known,1,1,'READY','hash',
                                 json.dumps({'symbol':symbol}),'{}','[]'))
                con.commit()
                cfg=types.ModuleType('config.market_sectors');cfg.MIN_SECTOR_SYMBOLS=1
                config=types.ModuleType('config');config.__path__=[]
                complete={'return_pct_4h':1,'relative_volume_4h':2,
                    'return_rank_pct_4h':50,'volume_rank_pct_4h':80,
                    'market_breadth_4h':55,'primary_sector':'Other'}
                def reference(_client,_canonical,_universe,_sectors,boundary,_min_sector):
                    if boundary==ms*50:return {}  # transient missing newest event
                    return {'OLDUSDT':dict(complete)}
                with patch.dict(sys.modules,{'config':config,'config.market_sectors':cfg}),\
                     patch.object(adapter,'canonical_module',return_value=CanonicalStub),\
                     patch.object(adapter,'regime_snapshot',side_effect=reference):
                    one=adapter.enrich_once(con,object(),project,('NEWUSDT','OLDUSDT'),{},1)
                    self.assertEqual(one['missing_history'],1)
                    self.assertEqual(one['missing_boundaries'],1)
                    self.assertEqual(con.execute('SELECT COUNT(*) FROM v1_regime_attempts').fetchone()[0],1)
                    two=adapter.enrich_once(con,object(),project,('NEWUSDT','OLDUSDT'),{},1)
                    self.assertEqual(two['enriched'],1)
                    self.assertEqual(two['sector_unavailable'],1)
                    self.assertEqual(con.execute('SELECT COUNT(*) FROM v1_regime_events').fetchone()[0],1)

    def test_sector_catalog_change_never_overwrites_cache(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)
            with ctx.get_database(p/'db.sqlite3',write=True) as con:
                con.executescript(adapter.SCHEMA)
                con.execute('INSERT INTO v1_regime_snapshots VALUES (?,?,?,?,?,?,?)',
                            (adapter.FOUR_H_MS,1,'old','old','old',2,'{}'))
                con.execute('INSERT INTO contexts VALUES (?,?,?,?,?,?,?,?,?)',
                            ('AAA|LONG|1',adapter.FOUR_H_MS*2,1,1,'READY','hash',
                             '{"symbol":"AAA"}','{}','[]'))
                pth=p/'dashboard/analytics/candidate_v1_market_regime.py'
                pth.parent.mkdir(parents=True);pth.write_text('module')
                config=types.ModuleType('config');config.__path__=[]
                cfg=types.ModuleType('config.market_sectors');cfg.MIN_SECTOR_SYMBOLS=1
                with patch.dict(sys.modules,{'config':config,'config.market_sectors':cfg}),\
                     patch.object(adapter,'canonical_module',return_value=CanonicalStub):
                    with self.assertRaisesRegex(RuntimeError,'versioned migration'):
                        adapter.enrich_once(con,StubRedis({}),p,('AAA',),{'AAA':'DeFi'},1)
                self.assertEqual(con.execute('SELECT COUNT(*) FROM v1_regime_events').fetchone()[0],0)


if __name__=='__main__':unittest.main()
