import csv
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from dashboard.views import hll_v298_history_30m as h


def k(t, price='1'):
    return [t, price, '2', '0.5', '1.5', '10', t + h.TF - 1, '15']


class V298History30m(unittest.TestCase):
    def test_download_pages_and_no_gaps(self):
        calls = []
        def fake(sym, start, end, sleep_s):
            calls.append(start)
            return [k(t) for t in range(start, min(end, start + 1000*h.TF), h.TF)]
        rows, diag = h.download('BTCUSDT', 0, 1003*h.TF, fetcher=fake)
        self.assertEqual(len(rows),1003)
        self.assertEqual(diag['status'],'FULL')
        self.assertEqual(calls,[0,1000*h.TF])

    def test_partial_listing_censored_not_interpolated(self):
        def late(sym, start, end, sleep_s):
            return [k(t) for t in range(max(start,2*h.TF),end,h.TF)]
        rows, diag = h.download('NEWUSDT', 0, 5*h.TF, fetcher=late)
        self.assertEqual(diag['status'],'PARTIAL_LISTING_OR_END')
        self.assertEqual(diag['missing_start_bars'],2)
        self.assertEqual(len(rows),3)

    def test_internal_gap_rejected(self):
        def missing(sym, start, end, sleep_s):
            return [k(0),k(h.TF),k(3*h.TF)]
        with self.assertRaisesRegex(ValueError,'Internal gap'):
            h.download('BTCUSDT',0,4*h.TF,fetcher=missing)

    def test_exclusive_and_resume(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'x.csv'
            rows=[[str(i*h.TF),'1','2','0.5','1.5','10','15'] for i in range(3)]
            h.write_exclusive(p,rows)
            stat=h.inspect_csv(p,0,3*h.TF)
            self.assertEqual(stat['status'],'FULL')
            with self.assertRaises(FileExistsError):
                h.write_exclusive(p,rows)
            with p.open('a') as f: f.write('5400000,1,2,0.5,1.5,10,15\n')
            with self.assertRaises(ValueError):
                h.inspect_csv(p,0,3*h.TF)

    def test_reuse_crops_without_mutating_baseline(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'base.csv'; dst=Path(td)/'out.csv'
            rows=[[str(i*h.TF),'1','2','0.5','1.5','10','15'] for i in range(5)]
            h.write_exclusive(src,rows)
            prev=h.digest(src)
            stat=h.slice_reusable(src,dst,h.TF,4*h.TF)
            self.assertEqual(stat['source'],'CORE15_REUSE')
            self.assertEqual(h.inspect_csv(dst,h.TF,4*h.TF)['bars30'],3)
            self.assertEqual(h.digest(src),prev)

    def test_resume_checks_manifest_hash(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); out=root/'out'; out.mkdir()
            baseline=root/'original';baseline.mkdir()
            args=Namespace(output_dir=out,existing_dir=baseline,reuse_core15=True,
                           start_utc=h.START,end_utc=h.END,sleep_s=.35)
            start=h.ms(h.START)
            def fake(sym,t,end,sleep_s):return [k(start)] if t == start else []
            # Short-window helper allows writing one 30m bar as partial.
            r=h.process_one('NEWUSDT',args,start,start+3*h.TF,fetcher=fake)
            self.assertEqual(r['status'],'PARTIAL_LISTING_OR_END')
            again=h.process_one('NEWUSDT',args,start,start+3*h.TF,fetcher=fake)
            self.assertEqual(again['source'],'EXISTING_VALID_SKIP')
            (out/'NEWUSDT_30m.csv').write_text('broken',encoding='utf-8')
            with self.assertRaises(ValueError):
                h.process_one('NEWUSDT',args,start,start+3*h.TF,fetcher=fake)

    def test_freeze_cannot_be_changed(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/'manifest.json').write_text(json.dumps({'version':'HLL_V297_290_PREFLIGHT','symbols':290,'source_universe':'missing','source_universe_sha256':'x'}))
            (root/'universe_290.txt').write_text('BTCUSDT\n')
            with self.assertRaises(ValueError): h.frozen_universe(root)


    def test_plan_uses_exact_frozen_source(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            syms=['T%04dUSDT' % i for i in range(290)]
            src=root/'events.csv'
            with src.open('w',newline='') as f:
                w=csv.writer(f);w.writerow(['symbol']);w.writerows([[s] for s in syms])
            freeze=root/'preflight';freeze.mkdir()
            (freeze/'universe_290.txt').write_text(''.join(s+'\n' for s in syms))
            (freeze/'manifest.json').write_text(json.dumps({
                'version':'HLL_V297_290_PREFLIGHT','symbols':290,
                'source_universe':str(src),'source_universe_sha256':h.digest(src),
                'symbol_column':'symbol'}))
            rc=h.main(['--plan','--preflight-dir',str(freeze),'--output-dir',str(root/'out'),
                       '--batch-start','280','--batch-size','20'])
            self.assertEqual(rc,0)
            self.assertFalse((root/'out').exists())
            self.assertEqual(h.main(['--report','--preflight-dir',str(freeze),'--output-dir',str(root/'out')]),0)

    def test_unsafe_symbol_paths(self):
        self.assertTrue(h.validate_symbol('BTCUSDT'))
        self.assertTrue(h.validate_symbol('币安人生USDT'))
        for name in ('../evil','../../x','A/B','xUSDT','SYMBOL?X','X\nY',''):
            self.assertFalse(h.validate_symbol(name))

if __name__ == '__main__': unittest.main()
