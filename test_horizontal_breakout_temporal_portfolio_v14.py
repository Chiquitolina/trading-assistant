import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import json

import numpy as np
import pandas as pd

import horizontal_breakout_temporal_portfolio_v14 as m


class TemporalPortfolioTests(unittest.TestCase):
    def synthetic(self, limit=500, start=1_800_000_000_000):
        ts=start+np.arange(limit,dtype='int64')*m.ONE
        return pd.DataFrame({'timestamp':ts,'open':np.full(limit,100.,dtype=float),
                             'high':np.full(limit,100.2,dtype=float),
                             'low':np.full(limit,99.8,dtype=float),
                             'close':np.full(limit,100.,dtype=float)})

    def test_frozen_settings_and_cohort(self):
        self.assertEqual(len(m.COHORT),39)
        self.assertEqual(len(m.batches()),8)
        self.assertEqual([len(x) for x in m.batches()], [5]*7+[4])
        self.assertNotIn('1000PEPEUSDT', m.COHORT)
        self.assertEqual(m.PRIMARY['tp_pct'],2.)
        self.assertEqual(m.PORTFOLIO['slippage_bps_per_side'],5.)

    def test_first_attempt_from_warmup_is_not_reclassified(self):
        start=pd.Timestamp(m.SIGNAL_START,tz='UTC').value//10**6
        d=pd.DataFrame([
            dict(attempt_id='a',level_id='L1',symbol='BTCUSDT',signal_closed_ts=start-60_000,
                 strength_class='BREAKOUT_ATTEMPT',trigger_kind='LEVEL_CROSS'),
            dict(attempt_id='b',level_id='L1',symbol='BTCUSDT',signal_closed_ts=start+60_000,
                 strength_class='STRONG_BREAKOUT',trigger_kind='LEVEL_CROSS'),
            dict(attempt_id='c',level_id='L2',symbol='ETHUSDT',signal_closed_ts=start+60_000,
                 strength_class='STRONG_BREAKOUT',trigger_kind='LEVEL_CROSS'),
        ])
        v=m.select_temporal_events(d)
        self.assertEqual(len(v),2)
        self.assertFalse(bool(v[v.attempt_id=='b'].first_on_level.iloc[0]))
        self.assertTrue(bool(v[v.attempt_id=='c'].first_on_level.iloc[0]))

    def test_next_minute_fill_and_target_with_slippage(self):
        df=self.synthetic()
        df.loc[10,'high']=104.
        x=m.simulated_long({'signal_closed_ts':int(df.timestamp.iloc[0])},df)
        self.assertEqual(x['exit_reason'],'TP_FIRST')
        self.assertEqual(x['minutes_held'],11)
        # 2% target above slipped entry; exit slippage and fees reduce true net.
        self.assertGreater(x['net_pct'],1.7)
        self.assertLess(x['net_pct'],1.9)

    def test_same_minute_both_conservative_sl(self):
        df=self.synthetic()
        df.loc[0,['high','low']]=[104.,97.]
        x=m.simulated_long({'signal_closed_ts':int(df.timestamp.iloc[0])},df)
        self.assertEqual(x['exit_reason'],'SL_FIRST')
        self.assertTrue(x['both_same_minute'])
        self.assertLess(x['net_pct'],-1.5)

    def test_missing_entry_is_not_fabricated(self):
        df=self.synthetic()
        x=m.simulated_long({'signal_closed_ts':int(df.timestamp.iloc[0]+m.ONE)},df.iloc[2:])
        self.assertIsNone(x)

    def test_gap_is_not_interpolated(self):
        df=self.synthetic().drop(index=[200]).reset_index(drop=True)
        with self.assertRaises(RuntimeError):
            m.simulated_long({'signal_closed_ts':int(df.timestamp.iloc[0])},df)

    def test_one_slot_busy_and_reentry_after_exit(self):
        start=1_800_000_000_000
        m1=self.synthetic(start=start)
        m1.loc[9,'high']=104.0
        m2=self.synthetic(start=start)
        m2.loc[3,'low']=97.
        m3=self.synthetic(start=start)
        m3.loc[35,'high']=104.
        events=pd.DataFrame([
            dict(attempt_id='a',level_id='L1',symbol='AAAUSDT',signal_closed_ts=start,
                 first_on_level=True,group='DIRECT_STRONG'),
            dict(attempt_id='b',level_id='L2',symbol='BBBUSDT',signal_closed_ts=start+m.ONE,
                 first_on_level=True,group='DIRECT_STRONG'),
            dict(attempt_id='c',level_id='L3',symbol='CCCUSDT',signal_closed_ts=start+30*m.ONE,
                 first_on_level=True,group='DIRECT_STRONG'),
        ])
        report, trades=m.replay_portfolio(events,lambda sym:{'AAAUSDT':m1,'BBBUSDT':m2,'CCCUSDT':m3}[sym])
        self.assertEqual(report['trades'],2)
        self.assertEqual(report['skipped_slot_busy'],1)
        self.assertEqual(report['exit_counts']['TP_FIRST'],2)
        self.assertGreater(report['end_equity_usd'],200.)
        self.assertEqual([x['symbol'] for x in trades],['AAAUSDT','CCCUSDT'])

    def test_simultaneous_uses_lexicographic_tie_break(self):
        start=1_800_000_000_000
        df=self.synthetic(start=start)
        df.loc[6,'high']=104.
        e=pd.DataFrame([
            dict(attempt_id='x',level_id='x',symbol='ZZZUSDT',signal_closed_ts=start,first_on_level=True,group='DIRECT_STRONG'),
            dict(attempt_id='y',level_id='y',symbol='AAAUSDT',signal_closed_ts=start,first_on_level=True,group='DIRECT_STRONG'),
        ])
        rep, trades=m.replay_portfolio(e,lambda s:df)
        self.assertEqual([x['symbol'] for x in trades],['AAAUSDT'])
        self.assertEqual(rep['skipped_slot_busy'],1)

    def test_freeze_rejects_mutated_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(m,'BASE',Path(td)), patch.object(m,'checked_sources',return_value={'pin':'fixed'}), \
                 patch.object(m,'COHORT',m.COHORT):
                m.freeze()
                m.locked()
                path=Path(td)/'freeze_manifest.json'
                record=json.loads(path.read_text())
                record['primary']['tp_pct']=1.
                path.write_text(json.dumps(record))
                with self.assertRaisesRegex(RuntimeError,'FROZEN_MANIFEST_OR_CODE_CHANGED'):
                    m.locked()

    def test_data_checksum_tampering_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)
            with patch.object(m,'BASE',folder),patch.object(m,'COHORT',('TESTUSDT',)),patch.object(m,'BATCH_SIZE',5):
                import horizontal_breakout_native_candles_pilot_v1 as collector
                d=m.batch_dir(1)
                d.mkdir(parents=True)
                settings={'version':collector.VERSION,'symbols':['TESTUSDT'],'start':m.WARMUP_START,
                          'end':m.SIGNAL_END,'tail_hours':m.TAIL_HOURS,
                          'source':'Binance USD-M Futures public klines','no_live_writes':True}
                (d/'dataset_config.json').write_text(json.dumps(settings))
                for tf in ['30m','1m']:
                    f=d/f'TESTUSDT_{tf}.csv'
                    f.write_text('timestamp,open,high,low,close,volume,quoteVolume\n')
                    f.with_suffix('.meta.json').write_text(json.dumps({'symbol':'TESTUSDT','timeframe':tf,'sha256':m.sha(f)}))
                with patch.object(collector,'validate_csv',return_value={'rows':0}):
                    self.assertEqual(len(m.inspect_seal(create=True)),2)
                    self.assertEqual(len(m.inspect_seal()),2)
                    (d/'TESTUSDT_1m.csv').write_text('tampered')
                    with self.assertRaisesRegex(RuntimeError,'OHLCV_HASH_OR_META_MISMATCH'):
                        m.inspect_seal()

    def test_audit_signals_with_frozen_first_attempts(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)
            sample=pd.DataFrame([
                dict(attempt_id='A',level_id='L1',symbol='BTCUSDT',signal_closed_ts=1,
                     first_on_level=True,group='DIRECT_STRONG',tp2_h360='TP_FIRST',net_tp2_h360_pct=1.9,signal_day_utc='d1'),
                dict(attempt_id='B',level_id='L2',symbol='ETHUSDT',signal_closed_ts=2,
                     first_on_level=True,group='WEAK_CROSS',tp2_h360='SL_FIRST',net_tp2_h360_pct=-1.6,signal_day_utc='d2'),
            ])
            with patch.object(m,'BASE',base),patch.object(m,'inspect_seal',return_value={}), \
                 patch.object(m,'aggregate',return_value=sample):
                rep=m.audit_signals()
                self.assertEqual(rep['first_attempts'],2)
                self.assertEqual(rep['primary_first_attempt_per_level']['DIRECT_STRONG']['N'],1)
                self.assertEqual(rep['primary_first_attempt_per_level']['WEAK_CROSS']['N'],1)
                self.assertTrue((base/'temporal_report.json').exists())

    def test_interval_reproducible_and_empty_strong_safe(self):
        frame=pd.DataFrame({'symbol':['AAAUSDT','AAAUSDT','BBBUSDT','BBBUSDT'],
                            'signal_day_utc':['d1','d1','d2','d2'],
                            'group':['DIRECT_STRONG','WEAK_CROSS','DIRECT_STRONG','WEAK_CROSS'],
                            'net_tp2_h360_pct':[1.,-.5,1.2,-.3],
                            'tp2_h360':['TP_FIRST','SL_FIRST','TP_FIRST','NO_HIT']})
        self.assertEqual(m.cluster_interval(frame,repetitions=50),m.cluster_interval(frame,repetitions=50))


if __name__=='__main__':unittest.main()
