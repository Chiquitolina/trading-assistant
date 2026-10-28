import argparse
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from dashboard.views import hll_v300_clean_flip_290 as v


class CleanFlip290(unittest.TestCase):
    def test_selection_before_outcome_and_month(self):
        data=pd.DataFrame([
            dict(attempt_id='old',symbol='AUSDT',level_id='x',breakout_closed_ts=v.SEP_START-100000,
                 pre_pattern_48='OTHER',post_outcome='SIN_FLIP_EN_VENTANA',flip_confirmed_closed_ts=float('nan')),
            dict(attempt_id='selected_later',symbol='AUSDT',level_id='x',breakout_closed_ts=v.SEP_START+1800000,
                 pre_pattern_48='ASCENDING_CLEAN',post_outcome='FLIP_CONFIRMADO',flip_confirmed_closed_ts=v.SEP_START+3600000),
            dict(attempt_id='good',symbol='AUSDT',level_id='y',breakout_closed_ts=v.SEP_START+1800000,
                 pre_pattern_48='ASCENDING_CLEAN',post_outcome='FLIP_CONFIRMADO',flip_confirmed_closed_ts=v.SEP_START+3600000),
        ])
        s=v.cohort(data,{'AUSDT'},['AUSDT'])
        self.assertEqual(list(s.attempt_id),['good'])
        self.assertEqual(s.entry_ts.iloc[0],v.SEP_START+3600000+v.MIN)

    def test_join_windows(self):
        df=pd.DataFrame({'entry_ts':[1_800_000,1_800_000+100*v.MIN,1_800_000+900*v.MIN],
                         'end_ts':[1_800_000+v.HORIZON*v.MIN,1_800_000+(100+v.HORIZON)*v.MIN,1_800_000+(900+v.HORIZON)*v.MIN]})
        spans=v.windows_for(df)
        self.assertEqual(len(spans),2)
        self.assertEqual(spans[0][1],1_800_000+(100+v.HORIZON)*v.MIN)

    def test_exact_and_gapped_archive(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'x.csv'; spans=[(0,3*v.MIN),(5*v.MIN,7*v.MIN)]
            rows=[[str(t),'1','1.1','.9','1','100','100'] for t in [0,v.MIN,2*v.MIN,5*v.MIN,6*v.MIN]]
            with p.open('w') as f:
                f.write(','.join(v.FIELDS)+'\n')
                for r in rows:f.write(','.join(r)+'\n')
            self.assertEqual(len(v.validate_archive(p,spans)),5)
            self.assertEqual(v.validate_archive(p,spans,require_exact=False),v.validate_archive(p,spans))
            p.write_text(p.read_text().replace(str(v.MIN)+',','90000,'))
            with self.assertRaises(ValueError):v.validate_archive(p,spans)

    def test_fetch_spans_and_one_minute_first_touch(self):
        def fake(sym,start,end,sleep_s):
            # Test pagination: at most three bars are generated per page.
            rows=[]
            for t in range(start,min(end,start+3*v.MIN),v.MIN):
                rows.append([t,'100','100','100','100','10',0,'1000'])
            return rows
        spans=[(60_000,60_000+8*v.MIN)]
        data,pages=v.fetch_spans('AUSDT',spans,fetcher=fake,sleep_s=0)
        self.assertEqual(pages,3)
        self.assertEqual(len(data),8)
        self.assertEqual(int(data[-1][0]),8*v.MIN)
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'AUSDT_1m.csv'
            v.write_exclusive(p,data)
            self.assertEqual(len(v.validate_archive(p,spans)),8)

    def test_provenance_reuse_and_resume(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            a=argparse.Namespace(output=root/'out',core_1m=root/'source',sleep_s=.2)
            a.core_1m.mkdir()
            g=pd.DataFrame({'entry_ts':[v.MIN],'end_ts':[v.MIN+3*v.MIN]})
            rows=[[str(t),'100','100','100','100','100','100'] for t in [v.MIN,2*v.MIN,3*v.MIN]]
            v.write_exclusive(a.core_1m/'AUSDT_1m.csv',rows)
            r=v.provenance_one('AUSDT',g,a)
            self.assertEqual(r['source'],'CORE15_ARCHIVE_REUSE')
            self.assertEqual(v.provenance_one('AUSDT',g,a)['status'],'SKIP_VERIFIED')
            p=a.output/'minutes'/'AUSDT_1m.csv'
            p.write_text(p.read_text().replace('100,100,100,100','100,101,100,100',1))
            with self.assertRaises(ValueError):v.provenance_one('AUSDT',g,a)

    def test_evaluate_generates_25_with_observed_open(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            args=argparse.Namespace(output=root/'out',core_1m=root/'source',sleep_s=.2)
            args.core_1m.mkdir()
            entry=v.SEP_START+3*v.MIN
            g=pd.DataFrame([{'attempt_id':'att','symbol':'AUSDT','level_id':'L1',
                'group':'ORIGINAL_15','breakout_utc':v.utc(entry-v.MIN*4),
                'entry_ts':entry,'end_ts':entry+v.HORIZON*v.MIN,
                'entry_utc':v.utc(entry),'entry_id':'exampleid',
                'flip_confirmed_closed_ts':entry-v.MIN}])
            rows=[[str(entry+i*v.MIN),'100','103' if i==2 else '100','100','102' if i==2 else '100','1','100']
                for i in range(v.HORIZON)]
            v.write_exclusive(args.core_1m/'AUSDT_1m.csv',rows)
            self.assertEqual(v.provenance_one('AUSDT',g,args)['status'],'FULL')
            v.evaluate(args,g,{'pre_sha256':'frozen'})
            d=pd.read_csv(args.output/'evaluation'/'entry_scenarios_25.csv')
            self.assertEqual(len(d),25)
            r=d.loc[d.tp_pct.eq(2)&d.sl_pct.eq(2)].iloc[0]
            self.assertEqual(r.result,'TP_FIRST')
            self.assertAlmostEqual(r.net_pct,1.9)
            summary=pd.read_csv(args.output/'evaluation'/'primary_tp2_sl2_12h_summary.csv')
            self.assertEqual(summary.loc[summary.group.eq('ORIGINAL_15'),'known'].iloc[0],1)
            self.assertEqual(summary.loc[summary.group.eq('ADDITIONAL_275'),'entries'].iloc[0],0)
            with self.assertRaises(FileExistsError):v.evaluate(args,g,{'pre_sha256':'frozen'})

    def test_source_manifest_freezes_cohort(self):
        with tempfile.TemporaryDirectory() as td:
            args=argparse.Namespace(output=Path(td))
            c=pd.DataFrame({'attempt_id':['a'],'symbol':['AUSDT'],'entry_ts':[v.MIN], 'group':['ORIGINAL_15']})
            v.init_manifest(args,c,{'pre_sha256':'a'})
            v.init_manifest(args,c,{'pre_sha256':'a'})
            with self.assertRaises(ValueError):v.init_manifest(args,c,{'pre_sha256':'b'})


if __name__=='__main__':unittest.main()
