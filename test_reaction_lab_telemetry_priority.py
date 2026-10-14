import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import reaction_lab_market_telemetry as market
from reaction_lab_structural_priority import priority_watches
import reaction_lab_telemetry_link as link

MIN=60_000

class Frame:
    def __init__(self, bars): self.bars=bars;self.empty=not bool(bars)
    def to_dict(self,kind):return self.bars

def make_bars():
    arr=[]
    for i in range(12):
        low=90.0 if i==3 else 98.0
        arr.append({'timestamp':i*15*MIN,'open':99,'high':102,'low':low,'close':99})
    # 15m 3x3 pivot idx=3 confirmed after idx=6 close -> at 105m.
    minutely=[]
    for i in range(105,125):
        minutely.append({'timestamp':i*MIN,'open':90.5,'high':90.6,
                          'low':90.2,'close':90.5})
    return Frame(arr),Frame(minutely),125*MIN

class PriorityTest(unittest.TestCase):
    def test_uses_closed_confirmed_and_pending_retest(self):
        a,b,now=make_bars()
        result=priority_watches(a,b,now)
        self.assertTrue(any(w['side']=='LONG' and w['level']==90 for w in result))
        self.assertFalse(priority_watches(a,b,105*MIN))
        # A first touch after departure must withdraw the watch.
        b.bars[9]['low']=90.05
        self.assertFalse(any(w['side']=='LONG' for w in priority_watches(a,b,now)))

    def test_missing_1m_bars_must_not_prove_unspent_watch(self):
        a,b,now=make_bars()
        b.bars.pop(5)
        self.assertFalse(priority_watches(a,b,now))

    def test_priority_and_fair_rotation(self):
        with tempfile.TemporaryDirectory() as d:
            with market.open_store(Path(d)/'t.sqlite3') as c:
                moment=1_000_000
                for symbol in ('S01','S02','S03','S04'):
                    c.execute('INSERT INTO priority_watches VALUES (?,?,?,?)',
                        (symbol,100,'{}',moment))
                c.commit()
                universe=[f'S{i:02d}' for i in range(20)]
                chosen,after,n=market.select_symbols(c,universe,8,moment,max_priority=4)
                self.assertEqual(n,4)
                self.assertEqual(len(chosen),8)
                self.assertEqual(len(set(chosen)),8)
                self.assertTrue(any(s not in ('S01','S02','S03','S04') for s in chosen))
                self.assertGreater(after,0)

    def test_missing_fields_marked_unavailable(self):
        def partial_get(path,params):
            if path.endswith('openInterest'):return {'openInterest':'123'}
            if path.endswith('premiumIndex'):return {'lastFundingRate':None}
            if path.endswith('depth'):return {'bids':[],'asks':[]}
            return []
        features,quality=market.poll_symbol('BTCUSDT',partial_get)
        self.assertTrue(quality['oi']['available'])
        self.assertFalse(quality['funding']['available'])
        self.assertFalse(quality['depth']['available'])
        self.assertFalse(quality['taker']['available'])

class LinkTests(unittest.TestCase):
    def add_snapshot(self,c,rec,values,quality):
        c.execute('INSERT INTO snapshots VALUES (?,?,?,?,?)',
                  ('ETHUSDT',rec,rec,json.dumps(values),json.dumps(quality)))
        c.commit()

    def test_each_source_has_own_receipt_age_and_future_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            with market.open_store(Path(d)/'t.sqlite3') as c:
                k=1_800_000
                self.add_snapshot(c,k-50_000,{'oi_contracts':100.0,'funding_rate':.001,
                  'orderbook_spread_bps':2.0,'orderbook_imbalance_5':.3},
                  {'oi':{'observed_ms':k-50_000,'available':True},
                   'funding':{'observed_ms':k-61*MIN,'available':True},
                   'depth':{'observed_ms':k-50_000,'available':True}})
                self.add_snapshot(c,k+1000,{'oi_contracts':10000},
                  {'oi':{'observed_ms':k+1000,'available':True}})
                f,q,e=link.market_asof(c,'ETHUSDT',k)
                self.assertEqual(f['oi_contracts'],100)
                self.assertNotIn('funding_rate',f)
                self.assertEqual(f['orderbook_spread_bps'],2)
                self.assertFalse(q['funding']['available'])
                self.assertLessEqual(q['depth']['source_observed_ms'],k)

    def test_liquidation_zero_only_with_full_coverage(self):
        with tempfile.TemporaryDirectory() as d:
            with market.open_store(Path(d)/'t.sqlite3') as c:
                c.executescript('CREATE TABLE liquidations(symbol TEXT,event_ms INTEGER,received_ms INTEGER,side TEXT,notional_usdt REAL,trade_id TEXT);'
                   'CREATE TABLE liquidation_heartbeats(received_ms INTEGER PRIMARY KEY);')
                known=2_000_000
                f,q,e=link.liquidation_asof(c,'ETHUSDT',known)
                self.assertFalse(q['available']);self.assertNotIn('liquidations_15m_usdt',f)
                for t in range(known-15*MIN,known+1,30_000):
                    c.execute('INSERT INTO liquidation_heartbeats VALUES (?)',(t,))
                c.commit()
                f,q,e=link.liquidation_asof(c,'ETHUSDT',known)
                self.assertTrue(q['available']);self.assertEqual(f['liquidations_15m_usdt'],0)
                c.execute('INSERT INTO liquidations VALUES (?,?,?,?,?,?)',('ETHUSDT',known-30_000,known-10_000,'SELL',200.,'a'))
                c.execute('INSERT INTO liquidations VALUES (?,?,?,?,?,?)',('ETHUSDT',known+1000,known+1000,'SELL',999.,'b'))
                c.commit()
                f,q,e=link.liquidation_asof(c,'ETHUSDT',known)
                self.assertEqual(f['liquidations_15m_usdt'],200)

    def test_frozen_link_is_idempotent_and_auditable(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);folder=root/'reports/reaction_lab';folder.mkdir(parents=True)
            with sqlite3.connect(folder/'scanner_events_v1.sqlite3') as c:
                c.execute('CREATE TABLE events(event_key TEXT,symbol TEXT,known_ts INTEGER)')
                c.execute('INSERT INTO events VALUES (?,?,?)',('ETHUSDT|LONG|1000000','ETHUSDT',1_060_000))
                c.commit()
            db=folder/'telemetry_link_v1.sqlite3'
            a=link.run_once(root,db=db,activation=1_000_000,settle_seconds=0)
            self.assertEqual(a['inserted'],1)
            b=link.run_once(root,db=db,settle_seconds=0)
            self.assertEqual(b['inserted'],0)
            self.assertEqual(link.audit(root,db=db)['different'],0)
            with sqlite3.connect(db) as c:
                self.assertEqual(c.execute('SELECT COUNT(*) FROM event_telemetry').fetchone()[0],1)

if __name__=='__main__':unittest.main()

# Additional isolated compatibility checks; no live Redis or HTTP used.
class IntegrationTests(unittest.TestCase):
    def test_rate_limit_persists_cooldown(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with patch('reaction_lab_continuous_scanner.get_symbols',return_value=('AAAUSDT','BBBUSDT')):
                def fail_get(path,params):
                    raise market.BinanceRateLimited(429,600)
                r=market.poll_once(root,root/'throttle.sqlite3',2,get=fail_get)
                self.assertTrue(r['rate_limited'])
                self.assertEqual(r['symbols_attempted'],0)
                again=market.poll_once(root,root/'throttle.sqlite3',2,get=fail_get)
                self.assertGreater(again['cooldown_remaining_s'],0)

    def test_deep_read_merge_uses_frozen_telemetry_without_rewriting(self):
        import reaction_lab_deep_collector as deep
        import reaction_lab_telemetry_link as link_mod
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            dp=deep.output_db(root)
            with deep.open_writer(dp,activation=1) as c:
                c.execute('INSERT INTO event_features VALUES (?,?,?,?,?,?,?)',
                  ('EVT',100,200,'CAUSAL_READY',json.dumps({'oi_contracts':None,'atr_1m_pct':1.5}),
                   'abc','{}'))
                c.commit()
            tp=link_mod.output(root)
            with link_mod.open_writer(tp,activation=1) as c:
                e={'features':{'oi_contracts':42.0},'quality':{},'evidence':{}}
                import hashlib
                digest=hashlib.sha256(json.dumps(e,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                c.execute('INSERT INTO event_telemetry VALUES (?,?,?,?,?,?,?,?)',
                    ('EVT',100,200,'PARTIAL',json.dumps(e['features']),json.dumps({}),json.dumps({}),digest))
                c.commit()
            f=deep.load_deep_frame(root,['EVT'])
            self.assertEqual(f.iloc[0]['oi_contracts'],42.0)
            self.assertEqual(f.iloc[0]['atr_1m_pct'],1.5)
            with sqlite3.connect(dp) as c:
                old=json.loads(c.execute('SELECT features_json FROM event_features').fetchone()[0])
                self.assertIsNone(old['oi_contracts'])
