"""Tests for causal episode-by-episode structural fingerprint evolution."""
import copy
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hll_structural_fingerprint_evolution_v1 as ev
import hll_structural_fingerprint_v1 as fp

HERE=Path(__file__).resolve().parent
SRC=HERE/'HLL_FUENTES_ANATOMIA_PRE_V2.zip'
MAP=HERE/'attempt_episode_map.csv'


def make_data(n=5):
    pre=10_000*ev.BAR_MS
    candle={}
    for i in range(288+2*n*4):
        v=100+i*.11+math.sin(i*.55)*.65
        ts=pre-288*ev.BAR_MS+i*ev.BAR_MS
        candle[ts]={"open_ts":ts,"close_ts":ts+ev.BAR_MS,"open":v-.04,
                    "high":v+.3,"low":v-.4,"close":v}
    members=[]
    for i in range(n):
        members.append({"symbol":"TESTUSDT","episode_id":"ep1","level_id":"lv1",
                        "attempt_id":f"a{i+1}","episode_ordinal":i+1,
                        "pre_closed_ts":pre+2*i*ev.BAR_MS,"zone_low":108.,"zone_high":109.,
                        "atr_pre":1.0,"status":"COMPLETE_PRE",
                        "future_episode_attempt_count":20})
    return members,candle


class PureEvolutionTests(unittest.TestCase):
    def setUp(self):
        self.members,self.candles=make_data(5)

    def test_cut_one_sees_only_one_snapshot(self):
        x=ev.build_timeline(self.members,self.candles,'a1',1,'CAUSAL')
        self.assertEqual(len(x['snapshots']),1)
        self.assertEqual(x['selected_ordinal'],1)
        self.assertEqual(x['total_episode_attempts_report_only'],5)
        self.assertFalse(x['snapshots'][0]['is_future_relative_to_selected'])
        self.assertTrue(x['snapshots'][0]['observed_features']>10)
        c=ev.get_comparison(x)
        self.assertIsNone(c['previous'])
        self.assertEqual(c['first']['attempt_id'],'a1')

    def test_cut_three_is_exact_n_minus_one(self):
        x=ev.build_timeline(self.members,self.candles,'a3',1,'CAUSAL')
        self.assertEqual([r['attempt_id'] for r in x['snapshots']],['a1','a2','a3'])
        c=ev.get_comparison(x)
        self.assertEqual(c['previous']['attempt_id'],'a2')
        self.assertEqual(c['selected']['attempt_id'],'a3')
        self.assertEqual(len(ev.compare_features(x)),35)

    def test_later_candle_change_cannot_mutate_prefix(self):
        first=ev.build_timeline(self.members,self.candles,'a3',1,'CAUSAL')
        future=copy.deepcopy(self.candles)
        # Perturb only candles strictly AFTER selected PRE.
        selected_pre=self.members[2]['pre_closed_ts']
        for ts in list(future):
            if ts>=selected_pre:
                future[ts].update(open=50000,high=50001,low=49999,close=50000)
        second=ev.build_timeline(self.members,future,'a3',1,'CAUSAL')
        self.assertEqual(first,second)

    def test_future_attempt_metadata_ignored_for_features(self):
        a=ev.build_timeline(self.members,self.candles,'a2',1,'CAUSAL')
        m=copy.deepcopy(self.members)
        for r in m[2:]:
            r.update(atr_pre=55, zone_low=900,zone_high=901)
        b=ev.build_timeline(m,self.candles,'a2',1,'CAUSAL')
        self.assertEqual(a['snapshots'],b['snapshots'])

    def test_historical_mode_show_future_explicitly(self):
        x=ev.build_timeline(self.members,self.candles,'a2',1,'HISTORICAL')
        self.assertEqual(len(x['snapshots']),5)
        self.assertEqual([r['is_future_relative_to_selected'] for r in x['snapshots']],
                         [False,False,True,True,True])
        self.assertEqual(ev.get_comparison(x)['selected']['attempt_id'],'a2')
        self.assertEqual(ev.get_comparison(x)['previous']['attempt_id'],'a1')

    def test_no_quality_or_post_features(self):
        x=ev.build_timeline(self.members,self.candles,'a3',1)
        self.assertEqual(x['snapshots'][0]['fingerprint_version'],fp.VERSION)
        self.assertEqual(len(ev.compare_features(x)),35)
        for row in ev.compare_features(x):
            self.assertNotIn('quality_score',row)
            self.assertNotIn('post',row)
        m=copy.deepcopy(self.members)
        for r in m:
            r.update(visual_grade='A',post_pnl=88)
        self.assertEqual(x['snapshots'],ev.build_timeline(m,self.candles,'a3',1)['snapshots'])

    def test_incomplete_288_is_not_fake_zero(self):
        missing=copy.deepcopy(self.candles)
        # Drop a candle needed only for the second snapshot; first is still complete.
        selected=self.members[1]['pre_closed_ts']
        missing.pop(selected-288*ev.BAR_MS)
        x=ev.build_timeline(self.members,missing,'a2',1)
        r=x['snapshots'][1]
        self.assertEqual(r['status'],'INCOMPLETE_PRE_288')
        self.assertEqual(r['observed_features'],0)
        self.assertTrue(all(r[f] is None for f in fp.FEATURES))
        c=ev.compare_features(x)
        self.assertTrue(all(row['selected_value'] is None and row['delta_vs_first'] is None for row in c))

    def test_missing_immediate_previous_not_skipped(self):
        missing=copy.deepcopy(self.candles)
        # Missing earliest candle #2 but present #1? #2 and #3 overlap; chosen #3
        # might be incomplete as well. Compare still points to #2 exactly.
        missing.pop(self.members[1]['pre_closed_ts']-288*ev.BAR_MS)
        x=ev.build_timeline(self.members,missing,'a3',1)
        self.assertEqual(ev.get_comparison(x)['previous']['attempt_id'],'a2')
        self.assertEqual(ev.get_comparison(x)['previous']['observed_features'],0)

    def test_duplicate_and_gap_and_cross_level_refused(self):
        with self.assertRaisesRegex(ValueError,'Ordinales'):
            ev.validate_episode([self.members[0],self.members[2]])
        with self.assertRaisesRegex(ValueError,'repetido'):
            ev.validate_episode([self.members[0],self.members[0]])
        bad=copy.deepcopy(self.members)
        bad[3]['level_id']='lv2'
        with self.assertRaisesRegex(ValueError,'niveles'):
            ev.validate_episode(bad)
        with self.assertRaisesRegex(ValueError,'no pertenece'):
            ev.build_timeline(self.members,self.candles,'unknown')

    def test_deltas_are_arithmetic_not_percentages_or_votes(self):
        x=ev.build_timeline(self.members,self.candles,'a3')
        cmp=ev.compare_features(x)
        key='movement_net_advance_48_atr_96'
        r=next(row for row in cmp if row['feature_id']==key)
        if all(isinstance(r[k],(int,float)) for k in ('first_value','selected_value')):
            self.assertAlmostEqual(r['delta_vs_first'],r['selected_value']-r['first_value'])
        self.assertEqual(r['unit'],'ATR')

    def test_no_overwrite_csv_and_preview(self):
        x=ev.build_timeline(self.members,self.candles,'a3')
        p=ev.html_gallery([x])
        self.assertIn('Huella Estructural Dinámica',p)
        self.assertIn('Todo el episodio (retrospectivo)',p)
        self.assertIn('Intento',p)
        with tempfile.TemporaryDirectory() as tmp:
            f=Path(tmp)/'timeline.csv'
            ev.write_csv(f,ev.timeline_table(x))
            self.assertTrue(f.is_file())
            with self.assertRaises(FileExistsError):
                ev.write_csv(f,ev.timeline_table(x))


@unittest.skipUnless(SRC.is_file() and MAP.is_file(), 'Se necesita el zip/map de referencias')
class ReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evolutions,_,_=ev.reference_run(SRC,MAP,1.0)

    def test_all_fifteen_recovered_with_contiguous_attempts(self):
        self.assertEqual(len(self.evolutions),15)
        self.assertEqual(sum(len(e['snapshots']) for e in self.evolutions),63)
        self.assertEqual(len({r['attempt_id'] for e in self.evolutions for r in e['snapshots']}),63)
        for e in self.evolutions:
            self.assertEqual([r['episode_ordinal_report_only'] for r in e['snapshots']],
                             list(range(1,len(e['snapshots'])+1)))

    def test_bsv_and_waxp_and_band_real_trajectory(self):
        by_sym={e['symbol']:e for e in self.evolutions}
        self.assertEqual(len(by_sym['WAXPUSDT']['snapshots']),9)
        self.assertEqual(len(by_sym['BSVUSDT']['snapshots']),14)
        self.assertEqual(len(by_sym['BANDUSDT']['snapshots']),6)
        self.assertEqual(len(by_sym['AXLUSDT']['snapshots']),5)
        self.assertEqual(ev.get_comparison(by_sym['BANDUSDT'])['previous']['episode_ordinal_report_only'],5)

    def test_real_cut_is_same_in_causal_vs_historical(self):
        from hll_structure_anatomy_pre_v1 import take_pre
        e=next(e for e in self.evolutions if e['symbol']=='WAXPUSDT')
        data=e['snapshots']
        prefix=copy.deepcopy(data[:3])
        selected=data[2]
        # The first three actual fingerprints cannot depend on #4..#9.
        self.assertEqual([x['attempt_id'] for x in prefix],
                         [x['attempt_id'] for x in data[:3]])
        self.assertEqual(selected['episode_ordinal_report_only'],3)

if __name__=='__main__':
    unittest.main()
