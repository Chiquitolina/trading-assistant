#!/usr/bin/env python3
"""Pure + local-reference tests for HLL Structural Fingerprint PRE V1."""
import copy
import csv
import math
import tempfile
import unittest
from pathlib import Path

import hll_structural_fingerprint_v1 as fp

HERE=Path(__file__).resolve().parent
LOCAL_SOURCE=HERE/'HLL_FUENTES_ANATOMIA_PRE_V2.zip'
LOCAL_MAP=fp.DEFAULT_MAP if fp.DEFAULT_MAP.is_file() else HERE/'attempt_episode_map.csv'


def meta(pre):
    return {'symbol':'TESTUSDT','episode_id':'ep','attempt_id':'a1','level_id':'lv',
            'pre_closed_ts':pre,'episode_ordinal':1,'atr_pre':1.,
            'zone_low':100.,'zone_high':101.}


def bars(n,pre,trend=0.05):
    out=[]
    for i in range(n):
        v=90.+i*trend+math.sin(i*0.5)*0.9
        op=v-0.05
        out.append({'open_ts':pre-(n-i)*fp.anatomy.BAR_MS,
            'close_ts':pre-(n-i-1)*fp.anatomy.BAR_MS,
            'open':op,'high':max(op,v)+0.3,'low':min(op,v)-0.3,'close':v})
    return out


class FingerprintPureTests(unittest.TestCase):
    def setUp(self):
        self.pre=10000*fp.anatomy.BAR_MS
        self.b288=bars(288,self.pre)
        self.b96=self.b288[-96:]
        self.m=meta(self.pre)

    def test_catalog_six_families_and_no_scores(self):
        c=fp.feature_catalog()
        self.assertEqual(len(c['families']),6)
        self.assertEqual(c['features'].__len__(),len(fp.FEATURES))
        self.assertEqual(len(set(x['id'] for x in c['features'])),len(fp.FEATURES))
        self.assertFalse(c['uses_post']);self.assertFalse(c['uses_review_labels'])
        self.assertTrue(all(x['family'] in fp.FAMILIES for x in c['features']))
        self.assertTrue(all(x['definition'] and x['unit'] and x['method'] for x in c['features']))

    def test_stable_ids_and_families(self):
        row=fp.build_fingerprint(self.m,self.b96,self.b288)
        self.assertEqual(row['attempt_id'],'a1')
        self.assertEqual(row['pre_closed_ts'],self.pre)
        self.assertEqual(row['total_features'],len(fp.FEATURES))
        self.assertTrue(set(fp.FEATURES).issubset(row))
        self.assertTrue(all(f'coverage_{f}' in row for f in fp.FAMILIES))

    def test_only_closed_pre_candles_used(self):
        candles={b['open_ts']:b for b in self.b288}
        a=fp.build_fingerprint(self.m,
              fp.anatomy.take_pre(candles,self.pre,96),
              fp.anatomy.take_pre(candles,self.pre,288))
        candles[self.pre]={'open_ts':self.pre,'close_ts':self.pre+fp.anatomy.BAR_MS,
                           'open':1e6,'high':1e6,'low':1e6,'close':1e6}
        b=fp.build_fingerprint(self.m,
              fp.anatomy.take_pre(candles,self.pre,96),
              fp.anatomy.take_pre(candles,self.pre,288))
        self.assertEqual(a,b)

    def test_external_labels_and_episode_future_counts_ignored(self):
        a=fp.build_fingerprint(self.m,self.b96,self.b288)
        alt={**self.m,'human_grade':'A','post_profit_pct':800,
             'future_episode_attempt_count':999, 'review_note':'TREE is nice'}
        b=fp.build_fingerprint(alt,self.b96,self.b288)
        self.assertEqual(a,b)

    def test_explicit_selected_cut_not_replaced_by_first(self):
        row=fp.build_fingerprint({**self.m,'episode_ordinal':5,'attempt_id':'later'},self.b96,self.b288)
        self.assertEqual(row['attempt_id'],'later')
        self.assertEqual(row['episode_ordinal_report_only'],5)

    def test_incomplete_window_and_mismatch_rejected(self):
        with self.assertRaisesRegex(ValueError,'incompleto'):
            fp.build_fingerprint(self.m,self.b96[:-1],self.b288)
        mutated=copy.deepcopy(self.b288)
        mutated[-1]['close']+=0.1
        with self.assertRaisesRegex(ValueError,'mismas velas'):
            fp.build_fingerprint(self.m,self.b96,mutated)
        altered=copy.deepcopy(self.b96)
        altered[2]['open_ts']+=fp.anatomy.BAR_MS
        with self.assertRaisesRegex(ValueError,'no contigua'):
            fp.build_fingerprint(self.m,altered,self.b288)

    def test_invalid_atr_zone_and_scale_rejected(self):
        with self.assertRaisesRegex(ValueError,'ATR/zona'):
            fp.build_fingerprint({**self.m,'atr_pre':0},self.b96,self.b288)
        with self.assertRaisesRegex(ValueError,'Zona invertida'):
            fp.build_fingerprint({**self.m,'zone_low':105},self.b96,self.b288)
        with self.assertRaisesRegex(ValueError,'Escala'):
            fp.build_fingerprint(self.m,self.b96,self.b288,2.0)

    def test_approach_rearmed_not_cross_up_count(self):
        def make(prices):return [{'close':p} for p in prices]
        low,high,atr=100,101,2
        self.assertEqual(fp._approach_runs(make([97,100,100.5,100.6]),low,high,atr),1)
        self.assertEqual(fp._approach_runs(make([97,100,101,97,100]),low,high,atr),2)
        self.assertEqual(fp._approach_runs(make([100,99.5,101]),low,high,atr),0)

    def test_missing_is_none_not_zero(self):
        # A drift-only series has no confirmed reversals or L-H-L chains.
        deterministic=bars(288,self.pre,trend=0.5)
        for i,b in enumerate(deterministic):
            v=70.0+i*0.5
            b.update(open=v,high=v+0.1,low=v-0.1,close=v)
        row=fp.build_fingerprint(self.m,deterministic[-96:],deterministic)
        self.assertIsNone(row['rebuild_last_low_step_atr_96'])
        self.assertEqual(row['rebuild_chain_count_96'],0)
        self.assertIsNone(row['geo_low_up_share_96'])
        self.assertIsNone(row['compress_downleg_ratio_new_old_96'])
        self.assertLess(row['observed_features'],row['total_features'])

    def test_detrended_residual_not_raw_range(self):
        # A perfect drift has large price range but zero residual oscillation.
        deterministic=bars(288,self.pre,trend=0.5)
        for i,b in enumerate(deterministic):
            v=70.0+i*0.5
            b.update(open=v,high=v+0.1,low=v-0.1,close=v)
        row=fp.build_fingerprint(self.m,deterministic[-96:],deterministic)
        self.assertLess(row['compress_residual_osc24_atr_96'],1e-10)
        self.assertGreater(row['movement_net_advance_48_atr_96'],20)

    def test_no_overwrite_plan_and_csv_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'fp.csv'
            row=fp.build_fingerprint(self.m,self.b96,self.b288)
            fp.save_csv(p,[row]);self.assertTrue(p.is_file())
            with p.open(encoding='utf-8',newline='') as f:
                loaded=list(csv.DictReader(f))
            self.assertEqual(loaded[0]['attempt_id'],row['attempt_id'])
            with self.assertRaises(FileExistsError):
                fp.save_csv(p,[row])

    def test_static_preview_has_ids_and_no_quality_score(self):
        row=fp.build_fingerprint(self.m,self.b96,self.b288)
        page=fp.preview_html([row])
        self.assertIn("a1",page)
        self.assertIn("TESTUSDT",page)
        self.assertIn("Reconstruccion",page)
        self.assertIn("NO senales",page)

    def test_family_coverage_no_imputation(self):
        r=fp.build_fingerprint(self.m,self.b96,self.b288)
        report=fp.family_coverage([r])
        self.assertEqual(len(report),6)
        self.assertEqual(sum(x['possible_cells'] for x in report),len(fp.FEATURES))
        self.assertEqual(sum(x['available_cells'] for x in report),r['observed_features'])

    def test_window_independence_first_low_age(self):
        row=fp.build_fingerprint(self.m,self.b96,self.b288)
        self.assertTrue(0 <= row['maturity_first_low_known_age_96'] <= 96 or row['maturity_first_low_known_age_96'] is None)
        self.assertTrue(0 <= row['maturity_first_low_known_age_288'] <= 288 or row['maturity_first_low_known_age_288'] is None)


@unittest.skipUnless(LOCAL_SOURCE.exists() and LOCAL_MAP.exists(), 'VPS source/reference package not locally available')
class First15IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.first,cls.all_rows,cls.candles,_,_=fp.load_reference_inputs(LOCAL_SOURCE,LOCAL_MAP)

    def test_real_15_first_and_40_cuts(self):
        self.assertEqual(len(self.first),15)
        self.assertEqual(len(self.all_rows),40)
        out=fp.build_reference_rows(self.all_rows,self.candles,1.0)
        self.assertEqual(len(out),40)
        self.assertEqual(len({r['attempt_id'] for r in out}),40)
        self.assertEqual(len({r['episode_id'] for r in out}),15)
        self.assertEqual(sum(r['episode_ordinal_report_only']==1 for r in out),15)
        self.assertTrue(all(r['status']=='COMPLETE_PRE_96_288' for r in out))

    def test_first_and_reviewed_are_distinct_where_necessary(self):
        out=fp.build_reference_rows(self.all_rows,self.candles,1.0)
        grouped={}
        for r in out: grouped.setdefault(r['symbol'],[]).append(r)
        for symbol in ('BANDUSDT','BICOUSDT','AXLUSDT','AWEUSDT'):
            self.assertEqual(len(grouped[symbol]),2)
            self.assertNotEqual(grouped[symbol][0]['attempt_id'],grouped[symbol][1]['attempt_id'])

    def test_stable_multi_scale_and_pivot_knowledge(self):
        row=next(x for x in self.first if x['symbol']=='TREEUSDT')
        b288=fp.anatomy.take_pre(self.candles[row['symbol']],row['pre_closed_ts'],288)
        b96=b288[-96:]
        for scale in (0.5,1.0,1.5):
            a=fp.build_fingerprint(row,b96,b288,scale)
            b=fp.build_fingerprint(row,b96,b288,scale)
            self.assertEqual(a,b)

if __name__=='__main__':unittest.main()
