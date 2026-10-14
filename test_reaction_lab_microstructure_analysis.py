"""Microstructure in Runner Analyzer: read-only, no lookahead, equal controls."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import pandas as pd

import reaction_lab_telemetry_link as link
from reaction_lab_microstructure_analysis import (
    enrich_runner_microstructure, source_coverage_table,
    winner_control_microstructure,
)
from reaction_lab_runner_analyzer import factor_buckets, factor_coverage
from reaction_lab_runner_patterns import EXCLUDED_SEARCH_FACTORS, DEFAULT_PATTERN_FACTORS

T0 = 1_800_000_000_000


def frame(rows=None):
    rows = rows or [
        ('EVT_LONG',T0,'LONG',True),
        ('EVT_SHORT',T0+60_000,'SHORT',False),
        ('EVT_NO_LINK',T0+120_000,'LONG',False),
    ]
    return pd.DataFrame([
        {'event_key':key, 'known_ts':known,'signal':side,
         'cohort':'Continuous V1','hit_2_0pct':hit,
         'run_horizon_pct':3. if hit else .4,
         # Legacy deep metric MUST NOT fill the missing link factor.
         'oi_change_5m_pct':999.,'taker_buy_ratio_5m':.99,
         'liquidations_15m_usdt':987654321.,'microstructure_status':'OBSERVED'}
        for key,known,side,hit in rows])


def quality(known, oi=True, depth=True, taker=True, liq=True):
    q = {s:{'available':True,'source_observed_ms':known-20_000,
            'received_ms':known-10_000}
         for s in ('oi','funding','depth','taker')}
    if not oi:q['oi']={'available':False,'reason':'no_fresh_pre_signal_observation'}
    if not depth:q['depth']={'available':False,'reason':'no_fresh_pre_signal_observation'}
    if not taker:q['taker']={'available':False,'reason':'no_fresh_pre_signal_observation'}
    q['liquidations'] = ({'available':True,'window_start_ms':known-900_000,
                         'window_end_ms':known,'heartbeats':30}
                         if liq else {'available':False,'reason':'no_contiguous_15m_stream'})
    return q


def freeze(root,key,known,values=None,q=None,alter_hash=False,stored_known=None):
    path=link.output(root)
    path.parent.mkdir(parents=True,exist_ok=True)
    if values is None:
        values = {
            'oi_change_5m_pct': .12, 'oi_change_15m_pct':-.2,
            'funding_rate':.0001,'oi_contracts':50,
            'taker_buy_ratio_5m':.7,'taker_buy_volume_5m':70,
            'taker_sell_volume_5m':30,'taker_buy_ratio_15m':.6,
            'orderbook_imbalance_5':.4,'orderbook_spread_bps':2.0,
            'liquidations_15m_usdt':0.0, 'liquidations_snapshot_count_15m':0,
            'telemetry_link_known_ts': known,
        }
    if q is None:q=quality(known)
    evidence={}
    payload=json.dumps({'features':values,'quality':q,'evidence':evidence},
                       sort_keys=True,separators=(',',':'),allow_nan=False)
    digest=hashlib.sha256(payload.encode()).hexdigest()
    if alter_hash:digest='a'*64
    with link.open_writer(path,activation=T0-1) as con:
        con.execute('INSERT INTO event_telemetry VALUES (?,?,?,?,?,?,?,?)',
                    (key,known if stored_known is None else stored_known,known+1000,
                     'OBSERVED',json.dumps(values),json.dumps(q),json.dumps(evidence),digest))
        con.commit()


class LinkAndComparisonTests(unittest.TestCase):
    def test_all_events_retained_no_fallback_and_side_adjustment(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            freeze(root,'EVT_LONG',T0)
            freeze(root,'EVT_SHORT',T0+60_000)
            original=frame()
            result=enrich_runner_microstructure(original,root).set_index('event_key')
            self.assertEqual(len(result),3)
            self.assertEqual(result.loc['EVT_NO_LINK','telemetry_link_integrity'],'NOT_LINKED')
            self.assertTrue(pd.isna(result.loc['EVT_NO_LINK','oi_change_5m_pct']))
            self.assertTrue(pd.isna(result.loc['EVT_NO_LINK','liquidations_15m_usdt']))
            self.assertEqual(result.loc['EVT_LONG','microstructure_status'],'OBSERVED')
            self.assertAlmostEqual(result.loc['EVT_LONG','taker_pressure_side_5m'],.4)
            self.assertAlmostEqual(result.loc['EVT_SHORT','taker_pressure_side_5m'],-.4)
            self.assertAlmostEqual(result.loc['EVT_LONG','orderbook_imbalance_side_5'],.4)
            self.assertAlmostEqual(result.loc['EVT_SHORT','orderbook_imbalance_side_5'],-.4)
            self.assertAlmostEqual(result.loc['EVT_LONG','taker_imbalance_ratio_5m'],.4)
            self.assertAlmostEqual(result.loc['EVT_LONG','telemetry_depth_age_sec'],20)
            self.assertEqual(result.loc['EVT_LONG','liquidations_15m_usdt'],0)
            self.assertEqual(original['oi_change_5m_pct'].tolist(),[999.]*3)

    def test_invalid_digest_known_timestamp_and_future_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            freeze(root,'EVT_LONG',T0,alter_hash=True)
            freeze(root,'EVT_SHORT',T0+60_000,stored_known=T0+61_000)
            result=enrich_runner_microstructure(frame(),root).set_index('event_key')
            self.assertEqual(result.loc['EVT_LONG','telemetry_link_integrity'],'INVALID_DIGEST_OR_PAYLOAD')
            self.assertEqual(result.loc['EVT_SHORT','telemetry_link_integrity'],'KNOWN_TS_MISMATCH')
            self.assertTrue(pd.isna(result.loc['EVT_LONG','orderbook_imbalance_5']))
            self.assertTrue(pd.isna(result.loc['EVT_SHORT','oi_change_5m_pct']))
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            q=quality(T0)
            q['depth']['received_ms']=T0+10_000
            freeze(root,'EVT_LONG',T0,q=q)
            result=enrich_runner_microstructure(frame(),root).set_index('event_key')
            self.assertEqual(result.loc['EVT_LONG','telemetry_link_integrity'],'PASS')
            self.assertFalse(bool(result.loc['EVT_LONG','telemetry_depth_available']))
            self.assertTrue(pd.isna(result.loc['EVT_LONG','orderbook_imbalance_5']))
            self.assertTrue(bool(result.loc['EVT_LONG','telemetry_oi_available']))

    def test_declared_available_with_missing_or_stale_measurement_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            q=quality(T0)
            q['taker']['source_observed_ms']=T0-181_000  # link max 180s
            values={'telemetry_link_known_ts':T0,'oi_contracts':10,
                    'taker_buy_ratio_5m':.6,'taker_buy_ratio_15m':.6,
                    'orderbook_imbalance_5':0.,'orderbook_spread_bps':1.,
                    'liquidations_15m_usdt':0.,'liquidations_snapshot_count_15m':0}
            # Funding's quality flag is true, but its value was absent.
            freeze(root,'EVT_LONG',T0,values=values,q=q)
            r=enrich_runner_microstructure(frame(),root).set_index('event_key')
            self.assertFalse(bool(r.loc['EVT_LONG','telemetry_funding_available']))
            self.assertFalse(bool(r.loc['EVT_LONG','telemetry_taker_available']))
            self.assertTrue(pd.isna(r.loc['EVT_LONG','taker_buy_ratio_5m']))
            self.assertTrue(bool(r.loc['EVT_LONG','telemetry_depth_available']))
            self.assertEqual(r.loc['EVT_LONG','microstructure_status'],'PARTIAL')

    def test_no_liquidation_stream_is_missing_not_zero(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            q=quality(T0,liq=False)
            freeze(root,'EVT_LONG',T0,q=q)
            result=enrich_runner_microstructure(frame(),root).set_index('event_key')
            self.assertTrue(pd.isna(result.loc['EVT_LONG','liquidations_15m_usdt']))
            self.assertFalse(bool(result.loc['EVT_LONG','telemetry_liquidations_available']))
            self.assertEqual(result.loc['EVT_LONG','microstructure_status'],'PARTIAL')

    def test_no_linker_preserves_previous_analyzer_compatibility(self):
        with tempfile.TemporaryDirectory() as d:
            result=enrich_runner_microstructure(frame(),d)
            self.assertEqual(result['telemetry_link_integrity'].tolist(),['NO_SIDECAR']*3)
            self.assertEqual(result['oi_change_5m_pct'].tolist(),[999.]*3)

    def test_coverage_comparable_denominator_and_missingness(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            freeze(root,'EVT_LONG',T0)
            freeze(root,'EVT_SHORT',T0+60_000,q=quality(T0+60_000,oi=False,taker=False))
            eligible=enrich_runner_microstructure(frame(),root)
            coverage=source_coverage_table(eligible,2.).set_index('Fuente')
            self.assertEqual(int(coverage.loc['Open Interest','Con dato']),1)
            self.assertEqual(int(coverage.loc['Open Interest','Total elegible']),3)
            self.assertEqual(int(coverage.loc['Open Interest','Runners con dato']),1)
            self.assertEqual(int(coverage.loc['Open Interest','Controles con dato']),0)
            self.assertEqual(int(coverage.loc['Liquidaciones observadas 15m','Con dato']),2)
            self.assertEqual(coverage.loc['Open Interest','Tasa runner con dato %'],100.)
            self.assertEqual(coverage.loc['Open Interest','Tasa runner sin dato %'],0.)
            buckets=factor_buckets(eligible,'OI cambio 5m (%)',2.,min_n=1)
            self.assertEqual(int(buckets['n'].sum()),1)
            cov=factor_coverage(eligible).set_index('Factor')
            self.assertEqual(int(cov.loc['OI cambio 5m (%)','Con dato']),1)
            self.assertTrue(winner_control_microstructure(eligible,min_each=1).empty or
                (winner_control_microstructure(eligible,min_each=1)['N con dato']<=2).all())

    def test_group_comparison_uses_only_qualified_factors(self):
        rows=[(f'EVT{i}',T0+i*60_000, 'LONG' if i%2==0 else 'SHORT',i%2==0)
              for i in range(16)]
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            for key,known,side,hit in rows:
                freeze(root,key,known)
            eligible=enrich_runner_microstructure(frame(rows),root)
            comp=winner_control_microstructure(eligible,2.,min_each=5)
            self.assertIn('Presión taker según LONG/SHORT 5m',comp['Factor'].tolist())
            self.assertTrue((comp['Runners N']==8).all())
            self.assertTrue((comp['Controles N']==8).all())
            self.assertFalse((eligible['telemetry_link_integrity']!='PASS').any())

    def test_sidecar_is_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            freeze(root,'EVT_LONG',T0)
            path=link.output(root)
            before=path.read_bytes()
            enrich_runner_microstructure(frame(),root)
            self.assertEqual(path.read_bytes(),before)

    def test_search_mines_market_pressure_not_quality(self):
        self.assertIn('Presión taker según lado 5m',DEFAULT_PATTERN_FACTORS)
        self.assertIn('Antigüedad microestructura (seg)',EXCLUDED_SEARCH_FACTORS)
        self.assertLessEqual(len(DEFAULT_PATTERN_FACTORS),24)

if __name__=='__main__':
    unittest.main()
