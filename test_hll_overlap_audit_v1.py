"""Offline unit tests for HLL overlapping attempts audit. No VPS report needed."""
from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

import hll_overlap_audit_v1 as lab


def entry(symbol='AIOTUSDT', level='A', ts=1000*lab.TF, aid='a',
          low=.0380, high=.0382, atr=.0005, family='ASCENSO_ESCALONADO', covered=True):
    return dict(symbol=symbol,level_id=level,pre_closed_ts=ts,breakout_closed_ts=ts+lab.TF,
                attempt_id=aid,zone_low=low,zone_high=high,atr_pre=atr,
                status='COMPLETE_PRE' if covered else 'INCOMPLETE_PRE_96',
                family_1p0=family,covered=covered)


def write_csv(path,rows,fields):
    with path.open('w',newline='',encoding='utf8') as f:
        wr=csv.DictWriter(f,fieldnames=fields);wr.writeheader();wr.writerows(rows)


class EpisodeGroupingTests(unittest.TestCase):
    def test_same_level_close_attempts_one_episode_and_kept_individually(self):
        rows=[entry(aid='a',ts=1000*lab.TF),entry(aid='b',ts=1012*lab.TF),
              entry(aid='c',ts=1025*lab.TF)]
        a,l,e,edges=lab.episodes(rows)
        self.assertEqual(len(a),3)
        self.assertEqual(len(l),1)
        self.assertEqual(len(e),1)
        self.assertEqual([x['episode_ordinal'] for x in a],[1,2,3])
        self.assertEqual([x['episode_attempt_count'] for x in a],[3]*3)
        self.assertEqual(a[-1]['pre96_overlap_with_prev_same_level_bars'],83)
        self.assertFalse(edges)

    def test_time_gap_and_bounded_anchor_split_chain(self):
        rows=[entry(aid=str(t),ts=t*lab.TF) for t in (1000,1024,1048,1072,1096,1120)]
        a,l,e,_=lab.episodes(rows,gap_bars=24,cap_bars=96)
        self.assertEqual(len(e),2)
        self.assertEqual([x['attempts'] for x in e],[4,2])
        self.assertNotEqual(a[0]['episode_id'],a[-1]['episode_id'])
        self.assertEqual(l[0]['episodes_gap24'],2)
        # Gap bigger than 24 splits even when PRE 96 context overlaps.
        b=lab.episodes([rows[0],rows[3]])
        self.assertEqual(len(b[2]),2)

    def test_different_levels_not_merged_even_same_candle_same_zone(self):
        rows=[entry(aid='a',ts=1000*lab.TF),
              entry(level='B',aid='b',ts=1000*lab.TF),
              entry(level='C',aid='c',ts=1010*lab.TF)]
        a,l,e,edges=lab.episodes(rows)
        self.assertEqual(len(e),3)
        self.assertEqual(len(l),3)
        self.assertEqual(a[0]['other_levels_same_candle'],1)
        self.assertEqual(a[1]['other_levels_same_candle'],1)
        self.assertEqual(a[2]['candidate_other_level_prior_count'],2)
        self.assertEqual(len(edges),1)
        self.assertEqual(edges[0]['interpretation'],'POSIBLE_ZONA_EQUIVALENTE_NO_FUSIONADA')

    def test_far_zones_or_far_times_have_no_collision(self):
        rows=[entry(aid='a',ts=1000*lab.TF),
              entry(level='B',aid='b',ts=1010*lab.TF,low=.041,high=.0412),
              entry(level='C',aid='c',ts=1050*lab.TF)]
        a,_,_,edges=lab.episodes(rows)
        self.assertFalse(edges)
        self.assertEqual(a[-1]['candidate_other_level_prior_count'],0)

    def test_same_level_wrong_zone_aborts(self):
        rows=[entry(),entry(aid='b',ts=1001*lab.TF,low=.039)]
        with self.assertRaisesRegex(ValueError,'distintas zonas'):
            lab.episodes(rows)

    def test_scope_same_symbol_required(self):
        rows=[entry(),entry(symbol='ALTUSDT',aid='z')]
        a,l,e,edges=lab.episodes(sorted(rows,key=lambda x:(x['symbol'],x['pre_closed_ts'])))
        self.assertEqual((len(l),len(e),len(edges)),(2,2,0))

    def test_future_append_does_not_change_prior_episode_assignment(self):
        base=[entry(aid='a',ts=1000*lab.TF),entry(aid='b',ts=1005*lab.TF)]
        a=lab.episodes(base)[0]
        x=lab.episodes(base+[entry(aid='later',ts=1010*lab.TF)])[0]
        # The episode count grows retrospectively but identity, order, anchor stay causal.
        fields=['episode_id','episode_first_attempt_id','episode_anchor_pre_ts','episode_ordinal','is_first_in_episode']
        for left,right in zip(a,x):
            self.assertEqual([left[k] for k in fields],[right[k] for k in fields])

    def test_coverage_and_families_not_used_for_grouping(self):
        rows=[entry(),entry(aid='b',ts=1005*lab.TF,family='OSCILACION_EN_RESISTENCIA',covered=False)]
        a,l,e,_=lab.episodes(rows)
        self.assertEqual(len(e),1)
        self.assertEqual(e[0]['family_changes_within_episode'],1)
        self.assertEqual(a[1]['status'],'INCOMPLETE_PRE_96')
        self.assertTrue(l)

    def test_conservation_and_sensitivity(self):
        rows=[entry(aid=str(t),ts=t*lab.TF) for t in (1000,1018,1045,1075)]
        a,l,e,edges=lab.episodes(rows)
        scenarios={str(g):len(lab.episodes(rows,gap_bars=g,collect_edges=False)[2]) for g in (12,24,48)}
        m=lab.summarize(rows,a,l,e,edges,scenarios)
        self.assertEqual(m['attempts'],4)
        self.assertEqual(m['distinct_level_ids_with_attempts'],1)
        self.assertEqual(m['attempts_after_first_on_same_level'],3)
        self.assertEqual(len(e),3)
        self.assertNotEqual(scenarios['12'],scenarios['48'])
        self.assertTrue(m['no_post_used'])


class SourceAndOutputTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.cp=self.root/'cohort'/'pre_cohort.csv';self.cp.parent.mkdir()
        self.dd=self.root/'discovery';self.dd.mkdir()
        self.r=[entry(aid='a'),entry(aid='b',ts=1001*lab.TF),
                entry(level='B',aid='c',ts=1002*lab.TF,covered=False)]
        self.make_sources()

    def make_sources(self):
        cf=[];df=[]
        for r in self.r:
            cf.append({k: str(r[k]) for k in ('attempt_id','symbol','level_id','pre_closed_ts',
                'breakout_closed_ts','zone_low','zone_high','atr_pre')}|{
                'pre_96_coverage':'True' if r['covered'] else 'False',
                'post_outcome':'DO_NOT_READ_FUTURE'})
            df.append({k: str(r[k]) for k in ('attempt_id','symbol','level_id','pre_closed_ts',
                'zone_low','zone_high','status','family_1p0')})
        write_csv(self.cp,cf,[*lab.REQUIRED_COHORT,'post_outcome'])
        write_csv(self.dd/'attempts_pre.csv',df,lab.REQUIRED_DISCOVERY)
        (self.cp.parent/'manifest.json').write_text(json.dumps({'version':'HLL_V282_PRE_COHORT',
            'mode':'OFFLINE_READ_ONLY','original_attempts':len(cf),'symbols':1}))
        self.dm={'version':'HLL_STRUCTURAL_DISCOVERY_V1','scope':'CROSS_UP_CONDITIONED_PRE_ONLY',
            'attempts':len(df),'symbols':1,'reads_post':False,'all_attempts_preserved':True,
            'source_sha256':{'cohort':lab.digest(self.cp)},
            'output_sha256':{'attempts_pre.csv':lab.digest(self.dd/'attempts_pre.csv')}}
        (self.dd/'manifest.json').write_text(json.dumps(self.dm))

    def test_sources_join_all_and_ignore_post(self):
        rows,hashes,cm,dm=lab.read_sources(self.cp,self.dd,enforce_full=False)
        self.assertEqual(len(rows),3)
        self.assertNotIn('post_outcome',rows[0])
        self.assertEqual(rows[-1]['status'],'INCOMPLETE_PRE_96')
        self.assertIn('cohort',hashes)

    def test_manifest_hash_and_mismatch_fail_closed(self):
        rows,_,_,_=lab.read_sources(self.cp,self.dd,enforce_full=False)
        self.assertEqual(len(rows),3)
        with (self.dd/'attempts_pre.csv').open('a') as f:f.write('unexpected\n')
        with self.assertRaisesRegex(ValueError,'Hash'):
            lab.read_sources(self.cp,self.dd,enforce_full=False)

    def test_bad_join_or_coverage_fails_closed(self):
        self.r[-1]['status']='COMPLETE_PRE'
        self.make_sources()
        with self.assertRaisesRegex(ValueError,'Cobertura discordante'):
            lab.read_sources(self.cp,self.dd,enforce_full=False)

    def test_full_requires_290_55717(self):
        with self.assertRaisesRegex(ValueError,'full 290'):
            lab.read_sources(self.cp,self.dd,enforce_full=True)

    def test_export_and_no_overwrite(self):
        rows,hashes,_,_=lab.read_sources(self.cp,self.dd,enforce_full=False)
        out=self.root/'reports'/'pilot'
        m=lab.run(rows,hashes,out)
        self.assertEqual(m['attempts'],3)
        for filename,expected in [('attempt_episode_map.csv',3),('level_reuse.csv',2),
                                  ('episode_candidates.csv',2),('symbol_summary.csv',1)]:
            with (out/filename).open(encoding='utf8',newline='') as file:
                self.assertEqual(len(list(csv.DictReader(file))),expected)
        self.assertEqual(m['outputs_sha256']['attempt_episode_map.csv'],lab.digest(out/'attempt_episode_map.csv'))
        with self.assertRaises(FileExistsError):lab.run(rows,hashes,out)
        self.assertTrue(self.cp.is_file())


if __name__=='__main__':unittest.main()
