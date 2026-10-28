import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from dashboard.views import hll_v301_full290 as mod
from dashboard.views.hll_v301_full_inspector import add_statuses,verify_artifacts


class Full290Tests(unittest.TestCase):
    def test_batch_final_slice(self):
        s=['A','B','C','D']
        self.assertEqual(mod.batch_symbols(s,3,10),['D'])
        with self.assertRaises(ValueError):mod.batch_symbols(s,4,10)

    def test_duplicated_signal_is_refused(self):
        pre=pd.DataFrame({'attempt_id':['a','b'],'symbol':['BTCUSDT','ETHUSDT']})
        events=pd.DataFrame({'attempt_id':['a','a'],'branch':['BREAKOUT_CONTINUATION']*2,'status':['CONFIRMED']*2})
        with self.assertRaises(ValueError):add_statuses(pre,events)

    def test_direct_and_reclaim_are_separate(self):
        pre=pd.DataFrame({'attempt_id':['a','b'],'symbol':['BTCUSDT','ETHUSDT']})
        events=pd.DataFrame({'attempt_id':['a','a','b','b'],
             'branch':['BREAKOUT_CONTINUATION','RECLAIM_CONTINUATION']*2,
             'status':['CONFIRMED','NO_ORIGINAL_FAILURE','NO_EARLY_CONTINUATION','CONFIRMED']})
        x=add_statuses(pre,events)
        self.assertEqual(x.loc[0,'reclaim_status'],'NO_ORIGINAL_FAILURE')
        self.assertEqual(x.loc[1,'reclaim_status'],'CONFIRMED')
        self.assertEqual(x.loc[0,'continuation_status'],'CONFIRMED')

    def test_hash_guard(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);pre=base/'pre.csv';pre.write_text('x\n')
            mm=base/'v291'/'manifest.json';mm.parent.mkdir()
            with patch.object(__import__('dashboard.views.hll_v301_full_inspector',fromlist=['V291']),'V291',mm.parent), \
                 patch.object(__import__('dashboard.views.hll_v301_full_inspector',fromlist=['V29']),'V29',base/'v29'), \
                 patch.object(__import__('dashboard.views.hll_v301_full_inspector',fromlist=['MECH']),'MECH',base/'v292'):
                mm.write_text(json.dumps({'original_sha256':'bad'}))
                with self.assertRaises(ValueError):verify_artifacts(pre)
                mm.write_text(json.dumps({'original_sha256':mod.digest(pre)}))
                self.assertEqual(verify_artifacts(pre),{'V291':True,'V29':False,'V292':False})

    def test_csv_batch_schema(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/'x.csv').write_text('x,y\n1,2\n')
            (root/'y.csv').write_text('x,y\n3,4\n')
            mod.combine_csvs([root/'x.csv',root/'y.csv'],root/'out.csv')
            self.assertEqual(len(pd.read_csv(root/'out.csv')),2)
            (root/'y.csv').write_text('different\n3\n')
            with self.assertRaises(ValueError):mod.combine_csvs([root/'x.csv',root/'y.csv'],root/'bad.csv')

    def test_partitions_cannot_overlap_or_be_partial(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            for i,syms in enumerate((['BTCUSDT','ETHUSDT'],['SOLUSDT'])):
                p=root/str(i);p.mkdir();(p/'metadata.json').write_text(json.dumps({'source':{'pre_sha256':'x'},'symbols':syms,'status':'COMPLETE'}))
            parts=mod.collect_batches(root,['BTCUSDT','ETHUSDT','SOLUSDT'],{'pre_sha256':'x'},'test')
            self.assertEqual(len(parts),2)
            with self.assertRaises(ValueError):mod.collect_batches(root,['BTCUSDT','ETHUSDT','SOLUSDT','XRPUSDT'],{'pre_sha256':'x'},'test')
            (root/'1'/'metadata.json').write_text(json.dumps({'source':{'pre_sha256':'x'},'symbols':['BTCUSDT'],'status':'COMPLETE'}))
            with self.assertRaises(ValueError):mod.collect_batches(root,['BTCUSDT','ETHUSDT'],{'pre_sha256':'x'},'test')

    def test_fetch_strict_and_resume(self):
        from dashboard.views import hll_v300_clean_flip_290 as old
        with tempfile.TemporaryDirectory() as td:
            start=1_800_000; end=start+3*mod.MINUTE
            def fake(symbol,ts,finish,sleep_s=0.1):
                n=(finish-ts)//mod.MINUTE
                return [[ts+i*mod.MINUTE,'1','1.2','0.8','1.1','3',None,'4'] for i in range(n)]
            with patch.object(mod,'OUT',Path(td)),patch.object(mod,'START',start),patch.object(mod,'END',end),\
                 patch.object(old,'fetch_page',side_effect=fake):
                r=mod.fetch_symbol('BTCUSDT',{'pre_sha256':'abc'},.1)
                self.assertEqual(r['bars'],3)
                self.assertEqual(mod.minute_status('BTCUSDT',{'pre_sha256':'abc'}),'FULL')
                self.assertEqual(mod.fetch_symbol('BTCUSDT',{'pre_sha256':'abc'})['status'],'FULL_REUSED')
                self.assertEqual(mod.minute_status('BTCUSDT',{'pre_sha256':'another'}),'WRONG_SOURCE')

if __name__=='__main__':unittest.main()
