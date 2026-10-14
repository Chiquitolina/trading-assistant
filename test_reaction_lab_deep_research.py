"""No-lookahead, append-only, as-of tape, outcomes, original analyzer integration."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from reaction_lab_deep_metrics import candle_tail, causal_features, forward_metrics
from reaction_lab_deep_collector import (run_once,status,load_deep_frame,load_deep_outcomes,
                                          telemetry_asof)
from reaction_lab_market_telemetry import derive_taker,derive_book
from reaction_lab_liquidations import parse as parse_liquidation
from reaction_lab_auto_paths import encode_path

M=60_000

class DeepResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.base=self.root/'reports/reaction_lab'
        self.base.mkdir(parents=True)
        self.ts=50_000_000*M
        self.known=self.ts+M
        self.key=f'SYMUSDT|LONG|{self.ts}'
        self.bars=[{'timestamp':self.known-(400-i)*M,'open':100+i*.01,
                    'high':101+i*.01,'low':99+i*.01,'close':100+i*.01,
                    'volume':40+i%9} for i in range(400)]
        self.event={'symbol':'SYMUSDT','signal':'LONG','retest_timestamp':self.ts,
             'retest_close':self.bars[-1]['close'],'swing_price':103,
             'actionable_timestamp':self.known-22*M,
             'departure_timestamp':self.known-10*M,'max_departure_pct':3.,
             'retest_distance_pct':.08,'retest_age_min':40}
        self.context={'Market alignment 1h':'HEADWIND','Market alignment 4h':'TAILWIND',
                      'market_breadth_1h':35,'breadth_delta_1h':-5,
                      'nearest_opposing_room_pct':5.1}

    def setup_db(self):
        scanner=sqlite3.connect(self.base/'scanner_events_v1.sqlite3')
        scanner.execute('CREATE TABLE events(event_key TEXT,symbol TEXT,side TEXT,retest_ts INT,known_ts INT,first_seen_ms INT,causal_json TEXT)')
        scanner.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?)',(self.key,'SYMUSDT','LONG',self.ts,self.known,self.known,json.dumps(self.event)))
        scanner.commit();scanner.close()
        ctx=sqlite3.connect(self.base/'causal_context_v1.sqlite3')
        ctx.execute('CREATE TABLE contexts(event_key TEXT,known_ts INT,status TEXT,context_json TEXT,candles_json TEXT,captured_at_ms INT)')
        ctx.execute('INSERT INTO contexts VALUES(?,?,?,?,?,?)',(self.key,self.known,'READY',json.dumps(self.context),json.dumps({'1m':self.bars}),self.known))
        ctx.commit();ctx.close()
        path=sqlite3.connect(self.base/'auto_paths_v1.sqlite3')
        path.execute('CREATE TABLE events(event_key TEXT,side TEXT,retest_ts INT,state TEXT,path_blob BLOB,updated_utc_ms INT)')
        rows=[(self.ts+(i+1)*M,104.+i*.01,104.1+i*.01,103.9+i*.01,104.02+i*.01) for i in range(360)]
        path.execute('INSERT INTO events VALUES (?,?,?,?,?,?)',(self.key,'LONG',self.ts,'COMPLETE',encode_path(rows),self.known+360*M))
        path.commit();path.close()

    def test_future_and_open_bars_excluded_from_frozen_metrics(self):
        causal=candle_tail(self.bars,self.known)
        self.assertEqual(len(causal),400)
        future=dict(self.bars[-1]);future['timestamp']=self.known;future['close']=99999
        same=causal_features(self.event,self.context,{'1m':self.bars+[future]},self.known)
        original=causal_features(self.event,self.context,{'1m':self.bars},self.known)
        self.assertEqual(same,original)
        self.assertGreater(original['departure_duration_min'],0)
        self.assertIsNotNone(original['atr_1m_pct'])
        self.assertEqual(original['microstructure_status'],'NOT_CAPTURED_AT_SIGNAL')
        self.assertIsNone(original['oi_contracts'])

    def test_outcomes_complete_and_same_bar_ambiguous(self):
        bars=[(self.ts+(i+1)*M,100.,103.,98.,100.) for i in range(360)]
        result=forward_metrics(bars,'LONG',100.,self.ts)
        self.assertEqual(result['outcome_180m_status'],'COMPLETE')
        self.assertEqual(result['tp_sl_2tp_1p0sl_180m'],'AMBIGUOUS_SL_FIRST')
        self.assertEqual(result['time_to_tp_2_180m'],1)
        self.assertEqual(result['mae_before_tp_2_180m_pct'],0.)
        self.assertGreaterEqual(result['mae_through_tp_bar_2_180m_pct'],2.)
        self.assertEqual(forward_metrics(bars[:50],'LONG',100.,self.ts)['outcome_180m_status'],'INCOMPLETE')
        broken=list(bars);broken[4]=(broken[4][0]+M,*broken[4][1:])
        self.assertEqual(forward_metrics(broken,'LONG',100.,self.ts)['valid_path_minutes'],4)

    def test_only_pre_observed_tape_can_join(self):
        con=sqlite3.connect(':memory:')
        con.execute('CREATE TABLE snapshots(symbol TEXT,observed_ms INT,received_ms INT,features_json TEXT)')
        con.execute('INSERT INTO snapshots VALUES (?,?,?,?)',('SYMUSDT',self.known-100_000,self.known-90_000,'{"oi_contracts":123}'))
        self.assertEqual(telemetry_asof(con,'SYMUSDT',self.known)['oi_contracts'],123)
        con.execute('INSERT INTO snapshots VALUES (?,?,?,?)',('SYMUSDT',self.known-500,self.known+100,'{"oi_contracts":987}'))
        self.assertEqual(telemetry_asof(con,'SYMUSDT',self.known)['oi_contracts'],123)
        con.close()

    def test_collector_idempotent_and_outcomes_unchanged_inputs(self):
        self.setup_db()
        first=run_once(self.root,activation=self.known-1,limit=10)
        self.assertEqual(first['causal_new'],1)
        self.assertEqual(first['outcomes_updated'],1)
        second=run_once(self.root,limit=10)
        self.assertEqual(second['causal_new'],0)
        self.assertEqual(second['outcomes_updated'],0)
        state=status(self.root)
        self.assertEqual(state['causal'],{'CAUSAL_READY':1})
        deep=load_deep_frame(self.root,[self.key]);self.assertEqual(len(deep),1)
        outcomes=load_deep_outcomes(self.root,[self.key],180)
        self.assertEqual(len(outcomes),1)
        self.assertIn('tp_sl_2tp_1p0sl_180m',outcomes)

    def test_liquidation_snapshot_and_heartbeat_reliability(self):
        msg={'e':'forceOrder','E':self.known-5000,'st':1,
            'o':{'s':'SYMUSDT','S':'SELL','q':'2','p':'101','T':self.known-5500}}
        records=parse_liquidation(msg,self.known)
        self.assertEqual(len(records),1)
        self.assertEqual(records[0][4],202.)
        connection=sqlite3.connect(':memory:')
        connection.execute('CREATE TABLE liquidations(symbol TEXT,event_ms INT,received_ms INT,side TEXT,notional_usdt REAL,trade_id TEXT)')
        connection.execute('CREATE TABLE liquidation_heartbeats(received_ms INT)')
        connection.execute('INSERT INTO liquidations VALUES (?,?,?,?,?,?)',records[0])
        for i in range(16):
            connection.execute('INSERT INTO liquidation_heartbeats VALUES (?)',
                               (self.known-15*M+i*M,))
        metrics=telemetry_asof(connection,'SYMUSDT',self.known)
        self.assertEqual(metrics['liquidations_15m_usdt'],202.)
        connection.execute('DELETE FROM liquidation_heartbeats WHERE received_ms BETWEEN ? AND ?',
                           (self.known-8*M,self.known-3*M))
        incomplete=telemetry_asof(connection,'SYMUSDT',self.known)
        self.assertIsNone(incomplete)
        connection.close()

    def test_readonly_audit_of_frozen_sidecar(self):
        from audit_reaction_lab_deep_research import audit
        self.setup_db()
        result=run_once(self.root,activation=self.known-1,limit=10)
        self.assertEqual(result['errors'],[])
        audit_result=audit(self.root,limit=2)
        self.assertEqual(audit_result['pass'],1)
        self.assertEqual(audit_result['different'],0)

    def test_public_taker_source_is_closed_only(self):
        now=self.known
        rows=[]
        for i in range(16):
            open_ts=now-(16-i)*M
            rows.append([open_ts,'','','','',10,open_ts+M-1,'','','6'])
        data=derive_taker(rows,now)
        self.assertAlmostEqual(data['taker_buy_ratio_5m'],.6)
        self.assertAlmostEqual(data['cvd_15m'],30.)
        # Open bar with huge buy cannot pollute signal.
        openbar=[now,'','','','',1_000_000,now+M-1,'','','1_000_000']
        self.assertEqual(data,derive_taker(rows+[openbar],now))
        book={'bids':[['100','2']], 'asks':[['101','1']]}
        self.assertGreater(derive_book(book)['orderbook_imbalance_5'],0)

if __name__=='__main__':unittest.main()
