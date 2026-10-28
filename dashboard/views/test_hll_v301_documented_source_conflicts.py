"""Frozen preflight source-conflict regression, offline research only."""
import tempfile
import unittest
from pathlib import Path
import pandas as pd
from dashboard.views.hll_v301_entry_open_reconcile import (
    reconcile_verified_opens, DOCUMENTED_SOURCE_CONFLICTS, AUDIT_COLUMNS,
)

class DocumentedConflictTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.mins = self.base/'minutes'; self.mins.mkdir()
        self.bars = self.base/'candles'; self.bars.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def mk(self, entry_id, **changes):
        d = DOCUMENTED_SOURCE_CONFLICTS[entry_id]
        rec = dict(entry_id=entry_id, attempt_id=d['attempt_id'], symbol=d['symbol'],
                   route=d['route'], entry_ts=d['entry_ts'], entry_utc='2026-09-20T16:30:00+00:00',
                   entry_price=d['reference'])
        rec.update(changes)
        return pd.DataFrame([rec])

    def _source(self, d, minute_override=None, archive_override=None):
        op = d['minute'] if minute_override is None else minute_override
        pd.DataFrame([dict(timestamp=d['entry_ts'],open=op,
                           high=op*1.01,low=op*.99,close=op)]).to_csv(
                           self.mins/f"{d['symbol']}_1m.csv",index=False)
        op30=d['reference'] if archive_override is None else archive_override
        pd.DataFrame([dict(timestamp=d['entry_ts'],open=op30)]).to_csv(
            self.bars/f"{d['symbol']}_30m.csv",index=False)

    def check_one(self, id):
        d=DOCUMENTED_SOURCE_CONFLICTS[id]
        self._source(d)
        src=self.mk(id)
        fixed,audit=reconcile_verified_opens(src,self.mins,candles_30m_dir=self.bars)
        self.assertEqual(len(audit),1)
        self.assertEqual(list(audit.columns),AUDIT_COLUMNS)
        self.assertAlmostEqual(fixed.entry_price.iloc[0],d['minute'])
        self.assertAlmostEqual(src.entry_price.iloc[0],d['reference'])
        self.assertEqual(fixed.entry_id.iloc[0],id)
        self.assertGreater(abs(float(audit.difference_pct.iloc[0])),0.1)

    def test_all_three_exact_preflight_cases_are_auditable(self):
        for id in DOCUMENTED_SOURCE_CONFLICTS:
            with self.subTest(id=id):self.check_one(id)

    def test_fail_closed_if_minute_price_changed(self):
        id='355e8982760189778d4eedc8'; d=DOCUMENTED_SOURCE_CONFLICTS[id]
        self._source(d,minute_override=d['minute']*.995)
        with self.assertRaisesRegex(ValueError,'1m execution OPEN changed'):
            reconcile_verified_opens(self.mk(id),self.mins,candles_30m_dir=self.bars)

    def test_fail_closed_if_original_30m_archive_changed(self):
        id='c4f114c1c55fed843e0cf254';d=DOCUMENTED_SOURCE_CONFLICTS[id]
        self._source(d,archive_override=d['reference']*.999)
        with self.assertRaisesRegex(ValueError,'original 30m archive changed'):
            reconcile_verified_opens(self.mk(id),self.mins,candles_30m_dir=self.bars)

    def test_fail_closed_if_identity_changed(self):
        id='31df38a818bb7a2c455057f7'; d=DOCUMENTED_SOURCE_CONFLICTS[id]
        self._source(d)
        with self.assertRaisesRegex(ValueError,'identity changed'):
            reconcile_verified_opens(self.mk(id,attempt_id='wrong'),self.mins,candles_30m_dir=self.bars)

    def test_fail_closed_if_timestamp_changed(self):
        id='31df38a818bb7a2c455057f7';d=DOCUMENTED_SOURCE_CONFLICTS[id]
        self._source(d)
        pd.DataFrame([dict(timestamp=d['entry_ts']+60000,open=d['minute'],high=d['minute']*1.01,
                           low=d['minute']*.99,close=d['minute'])]).to_csv(self.mins/'OGUSDT_1m.csv',index=False)
        with self.assertRaisesRegex(ValueError,'timestamp changed|Missing exact 1m entry OPEN'):
            reconcile_verified_opens(self.mk(id,entry_ts=d['entry_ts']+60000),self.mins,candles_30m_dir=self.bars)

    def test_other_over_guard_must_still_block(self):
        id='c4f114c1c55fed843e0cf254';d=DOCUMENTED_SOURCE_CONFLICTS[id]
        self._source(d)
        with self.assertRaisesRegex(ValueError,'exceeds source guard'):
            reconcile_verified_opens(self.mk(id).assign(entry_id='unknown'),self.mins,candles_30m_dir=self.bars)

    def test_flips_are_not_exempt(self):
        id='c4f114c1c55fed843e0cf254';d=DOCUMENTED_SOURCE_CONFLICTS[id]
        self._source(d)
        with self.assertRaisesRegex(ValueError,'FLIP_1M source changed'):
            reconcile_verified_opens(self.mk(id,route='FLIP_1M'),self.mins,candles_30m_dir=self.bars)

    def test_same_headers_as_completed_batches(self):
        self.assertEqual(AUDIT_COLUMNS, [
          'entry_id','attempt_id','symbol','route','entry_ts','entry_utc',
          'signal_reference_open_30m','execution_open_1m','difference_pct'])

if __name__=='__main__': unittest.main()
