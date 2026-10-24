"""Offline regression tests for V1.3 validation guards and analysis."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd

import horizontal_breakout_frozen_validation_v13 as v


class FrozenValidationTests(unittest.TestCase):
    def test_cohort_is_preset_and_not_discovery(self):
        self.assertEqual(len(v.COHORT),40)
        self.assertEqual(len(set(v.COHORT)),40)
        self.assertFalse(set(v.COHORT)&set(v.DISCOVERY))
        self.assertEqual(len(v.batches()),8)
        self.assertTrue(all(len(b)==5 for b in v.batches()))

    def test_v12_config_is_exact_frozen_default(self):
        from dataclasses import asdict
        from horizontal_breakout_lifecycle_v12 import LifecycleConfig
        cfg=asdict(LifecycleConfig())
        cfg['windows']=list(cfg['windows'])
        self.assertEqual(cfg,v.FROZEN_SETTINGS)
        self.assertEqual(v.PRIMARY['horizon_minutes'],360)
        self.assertEqual(v.PRIMARY['take_profit_pct'],2.0)

    def test_wilson_handles_small_samples_and_empty(self):
        self.assertEqual(v.wilson(0,0),(None,None))
        lower,upper=v.wilson(4,6)
        self.assertLess(lower,66.7)
        self.assertGreater(upper,66.7)
        self.assertTrue(0<=lower<upper<=100)

    def test_report_counts_explicit_no_hit_and_censor(self):
        f=pd.DataFrame({
            'level_id':['a','b','c','d'],
            'symbol':['AA','BB','AA','BB'],
            'tp2_h360':['TP_FIRST','SL_FIRST','NO_HIT','CENSORED'],
            'net_tp2_h360_pct':[1.9,-1.6,-.2,None],
        })
        r=v.analyze_group(f,'DIRECT_STRONG')
        self.assertEqual(r['N'],4)
        self.assertEqual(r['TP_FIRST'],1)
        self.assertEqual(r['SL_FIRST'],1)
        self.assertEqual(r['NO_HIT'],1)
        self.assertEqual(r['CENSORED'],1)
        self.assertEqual(r['net_count'],3)
        self.assertEqual(r['tp_rate_all_pct'],25.)
        self.assertAlmostEqual(r['mean_theoretical_net_pct'],round((1.9-1.6-.2)/3,4))

    def test_frozen_manifest_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            with patch.object(v,'BASE',base), patch.object(v,'code_hashes',return_value={'frozen':'abc'}):
                v.freeze()
                first=json.loads((base/'freeze_manifest.json').read_text())
                self.assertEqual(first['symbols'],list(v.COHORT))
                v.locked()
                v.freeze()
                second=json.loads((base/'freeze_manifest.json').read_text())
                self.assertEqual(first,second)
                second['frozen_primary_comparison']['take_profit_pct']=1.0
                (base/'freeze_manifest.json').write_text(json.dumps(second))
                with self.assertRaisesRegex(RuntimeError,'FROZEN_MANIFEST_MISMATCH'):
                    v.locked()

    def test_dataset_seal_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            # Test production-cohort 40 x 2 checksum logic with tiny dummy data.
            with patch.object(v,'BASE',base):
                for idx,syms in enumerate(v.batches(),1):
                    bd=v.batch_dir(idx)
                    bd.mkdir(parents=True,exist_ok=True)
                    for sym in syms:
                        for tf in ('1m','30m'):
                            p=bd/f'{sym}_{tf}.csv'
                            p.write_text('timestamp,open,high,low,close,volume,quoteVolume\n')
                            p.with_suffix('.meta.json').write_text(json.dumps({'sha256':v.sha(p)}))
                self.assertEqual(len(v.seal_data(write=True)),80)
                self.assertEqual(len(v.seal_data(write=False)),80)
                f=v.batch_dir(1)/f'{v.COHORT[0]}_1m.csv'
                f.write_text(f.read_text()+'bad\n')
                with self.assertRaisesRegex(RuntimeError,'NATIVE_CHECKSUM_MISMATCH'):
                    v.seal_data(write=False)

    def test_end_to_end_v12_batch_and_validation_report(self):
        import horizontal_breakout_lifecycle_v12 as engine
        from test_horizontal_breakout_lab import sample_series, minutes
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(v,'ROOT',root), patch.object(v,'BASE',root), \
                 patch.object(v,'COHORT',('TESTUSDT',)), patch.object(v,'BATCH_SIZE',5):
                directory=v.batch_dir(1)
                directory.mkdir(parents=True,exist_ok=True)
                for tf,frame in [('30m',sample_series(110)),('1m',minutes(5100))]:
                    path=directory/f'TESTUSDT_{tf}.csv'
                    frame.to_csv(path,index=False)
                    path.with_suffix('.meta.json').write_text(json.dumps({'sha256':v.sha(path)}))
                self.assertEqual(len(v.seal_data(write=True)),2)
                code=engine.main(['--run','--root',str(root),'--candles-dir',str(directory),
                                  '--symbols','TESTUSDT','--run-tag','frozen_v13_b01'])
                self.assertEqual(code,0)
                report=v.report()
                self.assertTrue(report['audit_success'])
                self.assertEqual(report['total_attempts'],1)
                self.assertEqual(report['first_attempts'],1)
                self.assertEqual(report['primary_first_attempt_by_level']['WEAK_CROSS']['N'],1)
                self.assertEqual(report['primary_first_attempt_by_level']['DIRECT_STRONG']['N'],0)

    def test_hash_sources_match_shipped_engine(self):
        # The sha256s intentionally pin the EXACT already-reviewed V1.2 version.
        with patch.object(v,'ENGINE',Path(__file__).parent/'horizontal_breakout_lifecycle_v12.py'), \
             patch.object(v,'CORE',Path(__file__).parent/'horizontal_breakout_lab.py'), \
             patch.object(v,'COLLECTOR',Path(__file__).parent/'horizontal_breakout_native_candles_pilot_v1.py'):
            self.assertEqual(len(v.code_hashes()),3)


if __name__=='__main__':unittest.main()
