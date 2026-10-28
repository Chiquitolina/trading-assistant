"""Offline regression tests for 30m source OPEN vs 1m execution OPEN."""
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from dashboard.views.hll_v301_entry_open_reconcile import reconcile_verified_opens, AUDIT_COLUMNS

T=1788730200000


class OpenSourceReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.base=Path(self.tmp.name)
        self._write(0.03654)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, op):
        pd.DataFrame([{'timestamp':T,'open':op,'high':op+0.00002,
                       'low':op-0.00002,'close':op}]).to_csv(self.base/'CARVUSDT_1m.csv',index=False)

    def _entry(self, route='V29_RECLAIM_30M', price=0.03653):
        return pd.DataFrame([dict(entry_id='verifiedid',attempt_id='attemptid',
             symbol='CARVUSDT',route=route,entry_ts=T,
             entry_utc='2026-09-06T21:30:00+00:00',entry_price=price)])

    def test_carv_reclaim_uses_real_1m_open_records_original(self):
        source=self._entry()
        out,audit=reconcile_verified_opens(source,self.base)
        self.assertEqual(out.entry_price.iloc[0],0.03654)
        self.assertEqual(source.entry_price.iloc[0],0.03653)
        self.assertEqual(out.entry_ts.iloc[0],T)
        self.assertEqual(out.entry_id.iloc[0],'verifiedid')
        self.assertEqual(len(audit),1)
        self.assertEqual(audit.signal_reference_open_30m.iloc[0],0.03653)
        self.assertEqual(audit.execution_open_1m.iloc[0],0.03654)
        self.assertAlmostEqual(audit.difference_pct.iloc[0],0.02737476,places=7)

    def test_continuation_same_rule(self):
        out,audit=reconcile_verified_opens(self._entry('V29_CONTINUATION_30M'),self.base)
        self.assertEqual(out.entry_price.iloc[0],0.03654)
        self.assertEqual(audit.route.iloc[0],'V29_CONTINUATION_30M')

    def test_exact_open_creates_header_only_audit(self):
        out,audit=reconcile_verified_opens(self._entry(price=0.03654),self.base)
        self.assertEqual(out.entry_price.iloc[0],0.03654)
        self.assertTrue(audit.empty)
        self.assertEqual(list(audit.columns),AUDIT_COLUMNS)

    def test_flip_mismatch_is_never_auto_reconciled(self):
        with self.assertRaisesRegex(ValueError,'FLIP_1M source changed'):
            reconcile_verified_opens(self._entry('FLIP_1M'),self.base)

    def test_missing_open_blocks_no_imputation(self):
        pd.DataFrame([{'timestamp':T+60000,'open':0.03654,'high':0.03656,
                       'low':0.03652,'close':0.03654}]).to_csv(self.base/'CARVUSDT_1m.csv',index=False)
        with self.assertRaisesRegex(ValueError,'Missing exact 1m entry OPEN'):
            reconcile_verified_opens(self._entry(),self.base)

    def test_large_source_discrepancy_blocks(self):
        self._write(0.03670)
        with self.assertRaisesRegex(ValueError,'source guard'):
            reconcile_verified_opens(self._entry(),self.base)

    def test_duplicate_id_blocks(self):
        d=self._entry(); d=pd.concat([d,d],ignore_index=True)
        with self.assertRaisesRegex(ValueError,'duplicate entry_id'):
            reconcile_verified_opens(d,self.base)

    def test_v296_detail_accepts_reconciled_1m_open_without_changing_id(self):
        from dashboard.views.hll_v296_entry_audit import detail_for_entries
        from datetime import datetime, timezone
        minute=[]
        for i in range(120):
            minute.append({'timestamp':T+i*60_000, 'open':0.03654,
                           'high':0.03656, 'low':0.03652, 'close':0.03654})
        pd.DataFrame(minute).to_csv(self.base/'CARVUSDT_1m.csv',index=False)
        e=self._entry()
        e['level_id']='L1'
        e['breakout_utc']='2026-09-06T21:00:00+00:00'
        e['entry_month']='2026-09'
        e['symbol_day']='2026-09-06|CARVUSDT'
        e['mechanism_48']='PRESSURE'
        e['pre_pattern_48']='ASCENDING_CLEAN'
        e['post_outcome']='FLIP_CONFIRMADO'
        with self.assertRaisesRegex(ValueError,'OPEN 1m distinto'):
            detail_for_entries(e,self.base,[120],0.10,0.0)
        fixed,audit=reconcile_verified_opens(e,self.base)
        result=detail_for_entries(fixed,self.base,[120],0.10,0.0)
        self.assertEqual(len(result),25)
        self.assertEqual(set(result.entry_id),{'verifiedid'})
        self.assertTrue((result.entry_price == 0.03654).all())
        self.assertEqual(len(audit),1)

    def test_empty_entries(self):
        x,a=reconcile_verified_opens(pd.DataFrame(),self.base)
        self.assertTrue(x.empty)
        self.assertEqual(list(a.columns),AUDIT_COLUMNS)

if __name__=='__main__':unittest.main()
