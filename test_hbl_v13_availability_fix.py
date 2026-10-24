"""Administrative-only symbol failure remediation tests, no production data."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import horizontal_breakout_frozen_validation_v13 as v

class AvailabilityCorrectionTests(unittest.TestCase):
    def test_amendment_does_not_change_freeze(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            with patch.object(v,'BASE',base), patch.object(v,'code_hashes',return_value={'pinned':'same'}):
                v.freeze()
                before=(base/'freeze_manifest.json').read_bytes()
                self.assertEqual(len(v.eligible_batch(6)),5)
                v.locked()
                v.record_unavailable()
                self.assertEqual((base/'freeze_manifest.json').read_bytes(),before)
                self.assertEqual(len(v.eligible_batch(6)),4)
                self.assertEqual(len([s for i in range(1,9) for s in v.eligible_batch(i)]),39)
                self.assertNotIn('1000PEPEUSDT',v.COHORT)
                self.assertIn('1000PEPEUSDT',v.DISCOVERY)
                self.assertEqual(v.verified_amendment()['original_frozen_cohort_size'],40)
                v.record_unavailable()

    def test_cannot_amend_after_outcome_or_seal(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            with patch.object(v,'BASE',base),patch.object(v,'code_hashes',return_value={'pinned':'same'}):
                v.freeze()
                (base/'dataset_seal.json').write_text('{}')
                with self.assertRaisesRegex(RuntimeError,'AMENDMENT_AFTER_VALIDATION'):
                    v.record_unavailable()

    def test_78_file_seal_and_failure_on_tamper(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            with patch.object(v,'BASE',base), patch.object(v,'code_hashes',return_value={'pinned':'same'}):
                v.freeze();v.record_unavailable()
                for i in range(1,9):
                    d=v.batch_dir(i); d.mkdir(parents=True,exist_ok=True)
                    for s in v.eligible_batch(i):
                        for tf in ('1m','30m'):
                            p=d/f'{s}_{tf}.csv';p.write_text('timestamp,open,high,low,close,volume,quoteVolume\n')
                            p.with_suffix('.meta.json').write_text(json.dumps({'sha256':v.sha(p)}))
                self.assertEqual(len(v.seal_data(write=True)),78)
                self.assertEqual(len(v.seal_data(write=False)),78)
                self.assertFalse((v.batch_dir(6)/'PEPEUSDT_1m.csv').exists())
                f=v.batch_dir(6)/'TIAUSDT_1m.csv'
                f.write_text(f.read_text()+'invalid row\n')
                with self.assertRaisesRegex(RuntimeError,'NATIVE_CHECKSUM_MISMATCH'):
                    v.seal_data(write=False)

    def test_batch6_survivors_are_verified_without_download(self):
        import horizontal_breakout_native_candles_pilot_v1 as collector
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            with patch.object(v,'BASE',base), patch.object(v,'code_hashes',return_value={'pinned':'same'}):
                v.freeze();v.record_unavailable()
                d=v.batch_dir(6);d.mkdir(parents=True,exist_ok=True)
                (d/'dataset_config.json').write_text(json.dumps({'symbols':list(v.batches()[5]),'start':v.START,'end':v.END}))
                (d/'native_audit.json').write_text(json.dumps({'success':False,'errors':[{'symbol':'PEPEUSDT','timeframe':'30m','error':'HTTP Error 400: Bad Request'}]}))
                for sym in v.eligible_batch(6):
                    for tf in ('30m','1m'):
                        f=d/f'{sym}_{tf}.csv';f.write_text('dummy')
                        f.with_suffix('.meta.json').write_text(json.dumps({'symbol':sym,'timeframe':tf,'sha256':v.sha(f)}))
                with patch.object(collector,'validate_csv',return_value={'rows':1}) as checker:
                    v.validate_recovered_batch(6)
                    self.assertEqual(checker.call_count,8)
                (d/'native_audit.json').write_text(json.dumps({'errors':[{'symbol':'PEPEUSDT','timeframe':'30m','error':'HTTP Error 500'}]}))
                with self.assertRaisesRegex(RuntimeError,'BATCH_06_FAILURE_EVIDENCE_CHANGED'):
                    v.validate_recovered_batch(6)

if __name__=='__main__':unittest.main()
