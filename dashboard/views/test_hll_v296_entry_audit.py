import json
import tempfile
import unittest
from pathlib import Path
import numpy as np
import pandas as pd

from dashboard.views.hll_v296_entry_audit import (
    PRE, MECH, MECH_MANIFEST, V29, CANDLES, MINUTES,
    validate_sources, build_entries, detail_for_entries, summarize,
    paired_route_comparison, run,
)

M=60_000
T=1_800_000*3000


class V296Audit(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.ids=['FIRST','REPEAT','OTHER']
        self.pre=pd.DataFrame([
          dict(attempt_id='FIRST',symbol='TESTUSDT',level_id='L',breakout_closed_ts=T,
               pre_closed_ts=T-1_800_000,flip_confirmed_closed_ts=T+60_000,
               post_outcome='FLIP_CONFIRMADO',v26_status='FLIP_SUSTAINED_4B',
               pre_pattern_48='ASCENDING_CLEAN',zone_low=98,zone_high=99),
          dict(attempt_id='REPEAT',symbol='TESTUSDT',level_id='L',breakout_closed_ts=T+1_800_000,
               pre_closed_ts=T,flip_confirmed_closed_ts=np.nan,
               post_outcome='SIN_FLIP_EN_VENTANA',v26_status='FAILED_BREAKOUT',
               pre_pattern_48='ASCENDING_CLEAN',zone_low=98,zone_high=99),
          dict(attempt_id='OTHER',symbol='TESTUSDT',level_id='M',breakout_closed_ts=T+3_600_000,
               pre_closed_ts=T+1_800_000,flip_confirmed_closed_ts=np.nan,
               post_outcome='SIN_FLIP_EN_VENTANA',v26_status='DIRECT_NO_RETEST_24B',
               pre_pattern_48='HORIZONTAL_BASE',zone_low=98,zone_high=99),
        ])
        self.mech=self.pre.copy()
        self.mech['mechanism_48']='BASE_CON_PRESION_FINAL'
        self.mech['touch_v26_independent_48']=1
        self.mech['physical_overlap_clusters_48']=3
        self.mech['confirmed_pivots_48']=3
        self.mech['close_efficiency_48']=0.4
        self.mech['pre_30m_coverage_48']='COMPLETE_30M'
        self.v29=pd.DataFrame([dict(attempt_id='FIRST',branch='BREAKOUT_CONTINUATION',status='CONFIRMED',
                   confirmed_ts=T+1_800_000,signal_open_ts=T,signal_close=100.0,
                   next_open_ts=T+1_800_000,next_open=100.0)])
        for p in (PRE,MECH,V29): (self.root/p).parent.mkdir(parents=True,exist_ok=True)
        self.pre.to_csv(self.root/PRE,index=False)
        self.mech.to_csv(self.root/MECH,index=False)
        self.v29.to_csv(self.root/V29,index=False)
        import hashlib
        h=hashlib.sha256((self.root/PRE).read_bytes()).hexdigest()
        (self.root/MECH_MANIFEST).write_text(json.dumps({'source_prehash':h}))
        (self.root/CANDLES).mkdir(parents=True,exist_ok=True)
        (self.root/MINUTES).mkdir(parents=True,exist_ok=True)
        pd.DataFrame([{'timestamp':T,'open':100,'high':101,'low':99,'close':100},
                      {'timestamp':T+1_800_000,'open':100,'high':100.5,'low':99,'close':100}]).to_csv(
                        self.root/CANDLES/'TESTUSDT_30m.csv',index=False)
        start=T+60_000
        candles=[]
        for i in range(121):
            candles.append(dict(timestamp=start+i*M,open=100,close=100,
                high=104 if i in (2,32) else 100.5,low=99.8))
        pd.DataFrame(candles).to_csv(self.root/MINUTES/'TESTUSDT_1m.csv',index=False)

    def tearDown(self):self.tmp.cleanup()

    def test_source_matches_and_first_global(self):
        p,m,v,h=validate_sources(self.root)
        sel,cov,e=build_entries(p,m,v,self.root/CANDLES,self.root/MINUTES)
        self.assertEqual(set(sel.attempt_id),{'FIRST','OTHER'})
        self.assertEqual(len(cov),6)
        self.assertEqual(len(e),2) # FLIP + continuation for FIRST
        self.assertTrue(e.entry_id.is_unique)
        self.assertEqual(set(e.route),{'FLIP_1M','V29_CONTINUATION_30M'})

    def test_row_matches_first_touch_and_clock(self):
        p,m,v,_=validate_sources(self.root)
        _,_,entries=build_entries(p,m,v,self.root/CANDLES,self.root/MINUTES)
        d=detail_for_entries(entries,self.root/MINUTES,[120],0.1,0)
        self.assertEqual(len(d),50)
        f=d[(d.route=='FLIP_1M')&(d.tp_pct==3)&(d.sl_pct==2)].iloc[0]
        self.assertEqual(f.result,'TP_FIRST')
        self.assertEqual(f.first_hit_min,1)
        self.assertAlmostEqual(f.net_pct,2.9)
        self.assertEqual(f.first_hit_minute_utc,pd.Timestamp(T+3*M,unit='ms',tz='UTC').isoformat())
        self.assertEqual(f.entry_price,100)
        self.assertEqual(f.mechanism_pre_48,'BASE_CON_PRESION_FINAL')

    def test_censored_no_fake_pnl(self):
        p,m,v,_=validate_sources(self.root)
        _,_,entries=build_entries(p,m,v,self.root/CANDLES,self.root/MINUTES)
        d=detail_for_entries(entries,self.root/MINUTES,[720],0.1,0)
        c=d[(d.route=='FLIP_1M')&(d.tp_pct==5)&(d.sl_pct==5)].iloc[0]
        self.assertEqual(c.result,'CENSORED_TAIL')
        self.assertTrue(pd.isna(c.net_pct))

    def test_first_touch_ambiguous_and_timeout(self):
        p,m,v,_=validate_sources(self.root)
        _,_,e=build_entries(p,m,v,self.root/CANDLES,self.root/MINUTES)
        f=self.root/MINUTES/'TESTUSDT_1m.csv'
        x=pd.read_csv(f)
        x.loc[x.index[1],'high']=103
        x.loc[x.index[1],'low']=97
        x.to_csv(f,index=False)
        d=detail_for_entries(e,self.root/MINUTES,[120],0.1,0)
        a=d[(d.route=='FLIP_1M')&(d.tp_pct==2)&(d.sl_pct==2)].iloc[0]
        self.assertEqual(a.result,'AMBIGUOUS_1M')
        self.assertTrue(pd.isna(a.net_pct))

    def test_no_overwrite_manifest_and_summaries(self):
        out=self.root/'report_out'
        m=run(self.root,out,'FIRST_PER_LEVEL',[120],0.10,0.0)
        self.assertEqual(m['scenario_rows'],50)
        self.assertEqual(len(pd.read_csv(out/'entry_events.csv')),2)
        self.assertEqual(len(pd.read_csv(out/'entry_scenarios_25.csv')),50)
        self.assertEqual(len(pd.read_csv(out/'attempt_route_coverage.csv')),6)
        self.assertTrue((out/'route_month_prespecified.csv').is_file())
        self.assertTrue((out/'same_attempt_route_pairs.csv').is_file())
        with self.assertRaises(FileExistsError):
            run(self.root,out,'FIRST_PER_LEVEL',[120],0.1,0.0)

    def test_source_hash_mismatch_blocks_run(self):
        (self.root/MECH_MANIFEST).write_text(json.dumps({'source_prehash':'bad'}))
        with self.assertRaisesRegex(ValueError,'SHA'):
            validate_sources(self.root)

    def test_all_attempts_separate_from_first(self):
        p,m,v,_=validate_sources(self.root)
        allsel,coverage,entries=build_entries(p,m,v,self.root/CANDLES,self.root/MINUTES,'ALL_ATTEMPTS')
        self.assertEqual(len(allsel),3)
        self.assertEqual(len(coverage),9)
        self.assertEqual(len(entries),2)

    def test_prespecified_no_maximum_search_and_matched_only(self):
        p,m,v,_=validate_sources(self.root)
        _,_,e=build_entries(p,m,v,self.root/CANDLES,self.root/MINUTES)
        d=detail_for_entries(e,self.root/MINUTES,[120],0.1,0)
        sumry=summarize(d,['route','entry_month'])
        self.assertEqual(set(zip(sumry.tp_pct,sumry.sl_pct)),{(2,2),(3,2)})
        pairs=paired_route_comparison(d)
        self.assertTrue(set(pairs.attempt_id)=={'FIRST'})
        self.assertEqual(len(pairs),2)

if __name__=='__main__':unittest.main()
