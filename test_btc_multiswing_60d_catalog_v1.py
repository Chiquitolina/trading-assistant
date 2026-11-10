"""Offline tests de catálogo Multi-Swing 60D; no datos de VPS ni LIVE."""
import csv
import hashlib
from pathlib import Path
import tempfile
import unittest
import json
from unittest.mock import patch

from btc_multiswing_60d_catalog_v1 import (
    aggregate_tf, physical_identity, detect_catalog,
    load_verified_file, stable_id, sha256, TF_MS, DETECTOR_BARS, build,
)
from engine.live.research.swing_detector import SwingPoint

M = 60_000


def bars(count=150, peaks=(45,)):
    result=[]
    for i in range(count):
        hi=110.0 if i in peaks else 100.0
        result.append({'timestamp':i*M,'open':95.0,'high':hi,'low':90.0,'close':95.0})
    return result


def pt(tf_ms=15*M):
    return SwingPoint(side='HIGH',price=110.0,pivot_timestamp=45*M,
                      confirmed_timestamp=90*M,left_bars=3,right_bars=3,prominence_pct=10)


class CatalogTests(unittest.TestCase):
    def test_tf_aggregation_peak_remains(self):
        data=aggregate_tf(bars(),15)
        self.assertEqual(len(data),10)
        self.assertEqual(data[3]['timestamp'],45*M)
        self.assertEqual(data[3]['high'],110.0)
        self.assertEqual(data[3]['close'],95.0)

    def test_aggregate_refuses_unaligned_start(self):
        data=bars()
        data[0]['timestamp']+=M
        with self.assertRaisesRegex(ValueError,'TF_UNALIGNED_START'):
            aggregate_tf(data,15)

    def test_aggregate_refuses_gap(self):
        data=bars()
        data[20]['timestamp'] += M
        with self.assertRaisesRegex(ValueError,'TF_INPUT_NOT_CONTIGUOUS'):
            aggregate_tf(data,15)

    def test_physical_minute_unique(self):
        t,state=physical_identity(pt(),'15m',bars(),0)
        self.assertEqual((t,state),(45*M,'VERIFIED'))

    def test_physical_minute_tied_unknown(self):
        t,state=physical_identity(pt(),'15m',bars(peaks=(45,46)),0)
        self.assertIsNone(t)
        self.assertEqual(state,'TIED_EXTREMA')

    def test_same_physical_extreme_three_timeframes_not_three_independent(self):
        data=bars()
        catalog,groups=detect_catalog(data,30*M,150*M,timeframes=('1m','5m','15m'))
        high=[x for x in catalog if x['side']=='HIGH']
        self.assertEqual(len(high),3)
        self.assertEqual({p['timeframe'] for p in high},{'1m','5m','15m'})
        self.assertEqual(len(groups),1)
        self.assertEqual(groups[0]['representation_count'],3)
        self.assertEqual(groups[0]['timeframes'],'1m,5m,15m')
        self.assertEqual([p['physical_minute_ts'] for p in high],[45*M]*3)
        self.assertEqual(max(p['available_ts'] for p in high),105*M)
        self.assertTrue(all(p['available_ts']>p['pivot_ts'] for p in high))

    def test_unconfirmed_15m_excluded_at_cutoff(self):
        catalog,groups=detect_catalog(bars(75),30*M,75*M,timeframes=('1m','5m','15m'))
        self.assertEqual({p['timeframe'] for p in catalog},{'1m','5m'})
        self.assertTrue(all(p['available_ts']<=75*M for p in catalog))
        self.assertEqual(groups[0]['representation_count'],2)

    def test_tied_tf_not_assigned_false_physical_identity(self):
        catalog,groups=detect_catalog(bars(peaks=(45,46)),30*M,150*M,timeframes=('1m','5m','15m'))
        tf={p['timeframe']:p for p in catalog if p['side']=='HIGH'}
        self.assertEqual(tf['5m']['physical_identity_state'],'TIED_EXTREMA')
        self.assertEqual(tf['15m']['physical_identity_state'],'TIED_EXTREMA')
        self.assertFalse(tf['5m']['physical_id'])
        self.assertEqual(len(groups),0)

    def test_gap_is_rejected_even_if_detector_could_run(self):
        data=bars()
        data[30]['timestamp'] += M
        with self.assertRaisesRegex(ValueError,'INPUT_GAP_OR_DUPLICATE'):
            detect_catalog(data,30*M,150*M,timeframes=('1m','5m','15m'))

    def test_source_hash_and_gap_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'sample.csv'
            with path.open('w',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=['timestamp','open','high','low','close'])
                writer.writeheader()
                for row in bars(3): writer.writerow(row)
            digest=sha256(path)
            rows=load_verified_file(path,digest,0,3*M)
            self.assertEqual(len(rows),3)
            self.assertEqual(rows[-1]['timestamp'],2*M)
            with self.assertRaisesRegex(ValueError,'HASH_MISMATCH'):
                load_verified_file(path,'0'*64,0,3*M)
            with self.assertRaisesRegex(ValueError,'BAD_BAR_COUNT'):
                load_verified_file(path,digest,0,4*M)

    def test_stable_ids_are_deterministic(self):
        self.assertEqual(stable_id('BTCUSDT','HIGH',45*M),stable_id('BTCUSDT','HIGH',45*M))
        self.assertNotEqual(stable_id('BTCUSDT','HIGH',45*M),stable_id('BTCUSDT','LOW',45*M))
 
    def test_six_timeframes_frozen_pivot_one_physical_minute(self):
        # Corte a +01:00: ultimo frame 4h (28:00–32:00) aun abierto.
        data=bars(29*60, peaks=(12*60,))
        catalog, groups = detect_catalog(data, 1*M, len(data)*M)
        high=[p for p in catalog if p['side']=='HIGH']
        self.assertEqual(set(x['timeframe'] for x in high), set(TF_MS))
        self.assertEqual(len(high), 6)
        self.assertEqual(len(groups),1)
        self.assertEqual(groups[0]['representation_count'], 6)
        self.assertEqual(groups[0]['timeframes'],'1m,5m,15m,30m,1h,4h')
        self.assertEqual({x['physical_minute_ts'] for x in high}, {12*60*M})
        p4h=next(x for x in high if x['timeframe']=='4h')
        self.assertEqual(p4h['confirmed_candle_open_ts'], 20*60*M)
        self.assertEqual(p4h['available_ts'], 24*60*M)

    def test_4h_drops_incomplete_final_candle(self):
        data=bars(29*60,peaks=(12*60,))
        # La vela 4h de 28:00 a 32:00 solo tiene 60 min: no usarla.
        data[-1]['high']=200.0
        full=aggregate_tf(data, 240, drop_incomplete_tail=True)
        self.assertEqual(len(full),7)
        self.assertEqual(full[-1]['timestamp'], 24*60*M)
        self.assertEqual(max(row['high'] for row in full),110.0)
        with self.assertRaisesRegex(ValueError,'TF_UNALIGNED_LENGTH'):
            aggregate_tf(data,240)

    def test_unconfirmed_4h_pivot_not_known_at_cutoff(self):
        # Pivote 4h en 20:00, faltan dos velas 4h completas para confirmarlo.
        data=bars(29*60,peaks=(20*60,))
        catalog,groups=detect_catalog(data, M, len(data)*M, timeframes=('4h',))
        self.assertEqual(catalog,[])
        self.assertEqual(groups,[])

    def test_4h_pivot_can_be_confirmed_after_more_bars(self):
        data=bars(36*60,peaks=(20*60,))
        catalog,groups=detect_catalog(data, M, len(data)*M, timeframes=('4h',))
        self.assertEqual(len(catalog),1)
        self.assertEqual(catalog[0]['confirmed_candle_open_ts'],28*60*M)
        self.assertEqual(catalog[0]['available_ts'],32*60*M)
        self.assertEqual(groups[0]['timeframes'],'4h')

    def test_tf_parameters_match_existing_view_and_explicit_4h(self):
        self.assertEqual(DETECTOR_BARS,{
            '1m':5,'5m':5,'15m':3,'30m':3,'1h':2,'4h':2
        })

    def test_build_writes_six_tf_manifest_and_refuses_overwrite(self):
        data=bars(29*60,peaks=(12*60,))
        fake_manifest={
            'files_sha256':{
                'BTCUSDT_1m.csv':'a'*64,
                'BTCUSDT_1m_warmup.csv':'b'*64,
            },
            'hll_30m_parity':{'status':'PARITY_OK'},
        }
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'six_tf_catalog'
            with patch('btc_multiswing_60d_catalog_v1.load_frozen',
                       return_value=(data, fake_manifest, M, len(data)*M)):
                summary=build(Path(tmp)/'frozen',output)
                self.assertEqual(summary['timeframes'],list(TF_MS))
                self.assertEqual(summary['tf_discarded_tail_minutes']['4h'],60)
                self.assertEqual(summary['tf_discarded_tail_minutes']['1h'],0)
                self.assertEqual(summary['counts_per_timeframe']['4h'],1)
                self.assertEqual(summary['shared_physical_extrema'],1)
                self.assertTrue((output/'confirmed_swings.csv').is_file())
                self.assertTrue((output/'physical_extrema_groups.csv').is_file())
                saved=json.loads((output/'manifest.json').read_text())
                self.assertEqual(saved['version'],summary['version'])
                for filename,digest in summary['files_sha256'].items():
                    self.assertEqual(sha256(output/filename),digest)
                with self.assertRaises(FileExistsError):
                    build(Path(tmp)/'frozen',output)

    def test_invalid_timeframe_fails_closed(self):
        with self.assertRaisesRegex(ValueError,'INVALID_TIMEFRAMES'):
            detect_catalog(bars(150),30*M,150*M,timeframes=('3h',))

    def test_truncated_tf_input_does_not_invent_candles(self):
        # No basta con 1h para evaluar ni una sola vela 4h.
        data=bars(150)
        catalog,groups=detect_catalog(data,30*M,150*M,timeframes=('4h',))
        self.assertEqual(catalog,[])
        self.assertEqual(groups,[])


if __name__=='__main__': unittest.main()
