#!/usr/bin/env python3
"""Tests offline for unified HLL lab; sources made synthetically in temp dir."""
from __future__ import annotations
import csv
import json
import tempfile
import unittest
from pathlib import Path

import hll_unified_structure_lifecycle_v1 as u

T0 = 1787184000000


def write_csv(path, rs, fields=None):
    path.parent.mkdir(parents=True,exist_ok=True)
    names=fields or list(rs[0])
    with path.open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=names);w.writeheader();w.writerows(rs)


def write_json(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(obj),encoding='utf-8')


class UnifiedLabTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.p=u.file_paths(self.root)
        self.aids=('aaa111','bbb222')
        a,b=self.aids
        base=dict(symbol='WAXPUSDT',level_id='levelxyz',zone_low='100',zone_high='102',atr_pre='2',
                  status='COMPLETE_PRE',family_1p0='X',episode_id='episode1',episode_first_attempt_id=a,
                  episode_anchor_pre_ts=str(T0),episode_attempt_count='2')
        self.maps=[
           dict(**base,attempt_id=a,pre_closed_ts=str(T0),breakout_closed_ts=str(T0+u.TF30),
                episode_ordinal='1',is_first_in_episode='1'),
           dict(**base,attempt_id=b,pre_closed_ts=str(T0+u.TF30),breakout_closed_ts=str(T0+u.TF30*2),
                episode_ordinal='2',is_first_in_episode='0')]
        pre=[]
        for m in self.maps:
            pre.append({**m,'v26_status':'UNRESOLVED_FIRST4','post_outcome':'INDETERMINADO',
                        'pre_pattern_24':'ASCENDING_CLEAN','pre_pattern_48':'ASCENDING_CLEAN',
                        'pre_pattern_96':'ASCENDING_CLEAN',
                        'pre_24_coverage':'True','pre_48_coverage':'True','pre_96_coverage':'True'})
        write_csv(self.p['map'],self.maps)
        write_csv(self.p['pre'],pre)
        fp_features={f'geo_extra_feature_{i:02}':'0.42' for i in range(31)}
        fp_features.update({'geo_low_up_share_96':'.9','rebuild_last_low_step_atr_96':'0.7',
                            'level_near_share_24_96':'.3','movement_signed_efficiency_24_96':'.5'})
        assert len(fp_features)==35
        write_csv(self.p['fingerprints'],[dict(self.maps[0],**fp_features,
                  status='COMPLETE_PRE_96_288')])
        write_csv(self.p['coverage'],[
            dict(attempt_id=a,symbol='WAXPUSDT',level_id='levelxyz',route=route,
                 status=('ENTRY_VERIFIED' if route=='FLIP_1M' else 'NO_SIGNAL'))
            for route in u.ROUTES])
        self.entrytime=T0+2*u.TF30
        v29=[]
        for aid in self.aids:
            for branch,status in [('BREAKOUT_CONTINUATION','NO_EARLY_CONTINUATION'),
                                  ('RECLAIM_CONTINUATION','NO_ORIGINAL_FAILURE')]:
                v29.append(dict(attempt_id=aid,origin_symbol='WAXPUSDT',level_id='levelxyz',
                                branch=branch,status=status,confirmed_ts='',v26_status='UNRESOLVED_FIRST4'))
        write_csv(self.p['v29'],v29)
        write_json(self.p['v29'].parent/'manifest.json',dict(
            source_sha256=u.digest(self.p['pre']),total_rows=len(v29)))
        write_csv(self.p['entries'],[dict(entry_id='entryflip1',attempt_id=a,symbol='WAXPUSDT',
                  level_id='levelxyz',route='FLIP_1M',status='ENTRY_VERIFIED',
                  entry_ts=str(self.entrytime),signal_ts=str(self.entrytime-u.TF1),
                  entry_price='103',pre_closed_ts=str(T0),breakout_closed_ts=str(T0+u.TF30))])
        minute=[]
        for i in range(8):
            minute.append({'timestamp':str(self.entrytime+i*u.TF1),'open':'103',
                  'high':'103.5' if i==0 else '107','low':'102.8','close':'103.2'})
        write_csv(self.p['minute']/'WAXPUSDT_1m.csv',minute)
        write_json(self.p['minute_manifest']/'WAXPUSDT.json',dict(
            source_sha256=u.digest(self.p['pre']),
            sha256=u.digest(self.p['minute']/'WAXPUSDT_1m.csv'),bars=len(minute)))
        v2=self.p['v2'];v2.mkdir(parents=True,exist_ok=True)
        pid='physical1'
        write_csv(v2/'physical_processes.csv',[dict(process_id=pid,symbol='WAXPUSDT',
                    level_id='levelxyz',split='DISCOVERY_SYMBOL',first_contact_known_closed_ts=str(self.entrytime))])
        write_csv(v2/'process_attempt_links.csv',[
             dict(process_id=pid,attempt_id=aid,symbol='WAXPUSDT',level_id='levelxyz',episode_id='episode1',
                  attempt_pre_closed_ts=m['pre_closed_ts'],attempt_observed_until=str(self.entrytime+u.TF30),
                  attempt_v26_status='UNRESOLVED_FIRST4',attempt_continuation_status='NO_EARLY_CONTINUATION',
                  attempt_reclaim_status='NO_ORIGINAL_FAILURE',link_basis='OVERLAP_RETROSPECTIVE')
             for aid,m in zip(self.aids,self.maps)])
        write_csv(v2/'process_checkpoints.csv',[
            dict(checkpoint_id='c1',process_id=pid,known_closed_ts=str(self.entrytime),event_type='FIRST_CONTACT'),
            dict(checkpoint_id='c2',process_id=pid,known_closed_ts=str(self.entrytime+u.TF30),event_type='DESCRIPTIVE_DEFENSE')])
        summary=dict(version='HLL_RESISTANCE_PROCESSES_CAUSAL_V2',symbols=1,source_attempts=2,
                   source_episodes=1,unique_physical_processes_by_symbol_level=1,
                   physical_attempt_links=2,causal_checkpoint_rows=2)
        write_json(v2/'summary.json',summary)
        file_hashes={name:u.digest(v2/name) for name in
                    ['physical_processes.csv','process_attempt_links.csv','process_checkpoints.csv','summary.json']}
        write_json(v2/'manifest.json',dict(no_detector_changes=True,no_pre_rerank=True,
                  candle_source_sha256={'WAXPUSDT':'unit'},files_sha256=file_hashes))
        write_json(self.p['map'].parent/'manifest.json',dict(outputs_sha256={
                      'attempt_episode_map.csv':u.digest(self.p['map'])},
                     source_sha256={'cohort':u.digest(self.p['pre'])}))
        write_json(self.p['pre'].parent/'manifest.json',dict(version='HLL_V282_PRE_COHORT'))
        write_json(self.p['fingerprints'].parent/'manifest.json',dict(
                  fingerprints_sha256=u.digest(self.p['fingerprints']),
                  source_map_sha256=u.digest(self.p['map'])))
        write_json(self.p['entries'].parent/'manifest.json',dict(
                  scope='FIRST_PER_LEVEL',source={'pre_sha256':u.digest(self.p['pre'])},entries=1))

    def rebuild_hashes(self,source):
        if source=='v2':
            m=u.read_manifest(self.p['v2']/'manifest.json')
            m['files_sha256']={n:u.digest(self.p['v2']/n) for n in m['files_sha256']}
            write_json(self.p['v2']/'manifest.json',m)
        elif source=='fingerprints':
            m=u.read_manifest(self.p['fingerprints'].parent/'manifest.json')
            m['fingerprints_sha256']=u.digest(self.p['fingerprints'])
            write_json(self.p['fingerprints'].parent/'manifest.json',m)
        elif source=='pre':
            m=u.read_manifest(self.p['map'].parent/'manifest.json')
            m['source_sha256']['cohort']=u.digest(self.p['pre'])
            write_json(self.p['map'].parent/'manifest.json',m)
            e=u.read_manifest(self.p['entries'].parent/'manifest.json')
            e['source']['pre_sha256']=u.digest(self.p['pre'])
            write_json(self.p['entries'].parent/'manifest.json',e)
            m29=u.read_manifest(self.p['v29'].parent/'manifest.json')
            m29['source_sha256']=u.digest(self.p['pre'])
            write_json(self.p['v29'].parent/'manifest.json',m29)
            mm=u.read_manifest(self.p['minute_manifest']/'WAXPUSDT.json')
            mm['source_sha256']=u.digest(self.p['pre'])
            write_json(self.p['minute_manifest']/'WAXPUSDT.json',mm)
        elif source=='entry':
            e=u.read_manifest(self.p['entries'].parent/'manifest.json')
            e['entries']=len(list(u.rows(self.p['entries'])))
            write_json(self.p['entries'].parent/'manifest.json',e)
            m29=u.read_manifest(self.p['v29'].parent/'manifest.json')
            m29['source_sha256']=u.digest(self.p['pre'])
            write_json(self.p['v29'].parent/'manifest.json',m29)
            mm=u.read_manifest(self.p['minute_manifest']/'WAXPUSDT.json')
            mm['source_sha256']=u.digest(self.p['pre'])
            write_json(self.p['minute_manifest']/'WAXPUSDT.json',mm)

    def test_end_to_end_unified_cohort_real_shape_synthetic_values(self):
        s=u.run(self.root)
        self.assertEqual(s['pilot_attempts'],2)
        self.assertEqual(s['split_symbol_counts'],{'DISCOVERY_SYMBOL':1})
        self.assertEqual(s['physical_processes_v2'],1)
        self.assertEqual(s['verified_entry_events'],1)
        self.assertEqual(s['entry_coverage_attempts'],1)
        self.assertEqual(s['not_covered_attempts'],1)
        self.assertEqual(s['verified_entries_by_route'],{'FLIP_1M':1})
        self.assertEqual(s['flip_reference_state_counts'],{'TP_FIRST':1})
        attempt=list(u.rows(self.p['output']/'unified_attempts.csv'))
        self.assertEqual(len(attempt),2)
        self.assertEqual(attempt[1]['fingerprint_status'],'NOT_INDEXED_NONFIRST_ATTEMPT')
        self.assertEqual(attempt[1]['coverage_FLIP_1M'],'NOT_COVERED')
        self.assertEqual(attempt[0]['coverage_V29_CONTINUATION_30M'],'NO_SIGNAL')
        self.assertEqual(len(list(u.rows(self.p['output']/'entry_process_context_RETROSPECTIVE.csv'))),2)
        self.assertEqual(len(list(u.rows(self.p['output']/'entry_outcomes_1m.csv'))),3)

    def test_archived_1m_hash_guard(self):
        archive=self.p['minute']/'WAXPUSDT_1m.csv'
        archive.write_text('tampered',encoding='utf8')
        with self.assertRaisesRegex(ValueError,'1m archive.*hash mismatch'):
            u.run(self.root)

    def test_source_hash_guard(self):
        self.p['fingerprints'].write_text('tampered',encoding='utf8')
        with self.assertRaisesRegex(ValueError,'hash mismatch'):
            u.verify_sources(self.p)

    def test_v2_hash_guard(self):
        self.p['v2'].joinpath('physical_processes.csv').write_text('tampered',encoding='utf8')
        with self.assertRaisesRegex(ValueError,'hash mismatch'):
            u.verify_sources(self.p)

    def test_nonfirst_fingerprint_fails_even_if_hashes_updated(self):
        rs=list(u.rows(self.p['fingerprints']))
        rs.append({**rs[0], 'attempt_id':'bbb222','pre_closed_ts':str(T0+u.TF30),
                   'status':'COMPLETE_PRE_96_288'})
        write_csv(self.p['fingerprints'],rs,list(rs[0]))
        self.rebuild_hashes('fingerprints')
        man=u.verify_sources(self.p)
        with self.assertRaisesRegex(ValueError,'Non-episode-first fingerprint'):
            u.load_core(self.p,man)

    def test_pre_changed_zone_fails_after_hash_updates(self):
        rs=list(u.rows(self.p['pre']))
        rs[0]['zone_high']='190'
        write_csv(self.p['pre'],rs,list(rs[0]));self.rebuild_hashes('pre')
        with self.assertRaisesRegex(ValueError,'PRE changed zone_high'):
            u.load_core(self.p,u.verify_sources(self.p))

    def test_wrong_process_id_fails_even_if_hashes_updated(self):
        rs=list(u.rows(self.p['v2']/'process_attempt_links.csv'))
        rs[0]['level_id']='not_level'
        write_csv(self.p['v2']/'process_attempt_links.csv',rs,list(rs[0]));self.rebuild_hashes('v2')
        man=u.verify_sources(self.p);att,_=u.load_core(self.p,man);u.load_v29(self.p,att,man)
        with self.assertRaisesRegex(ValueError,'mismatch level_id'):
            u.load_processes(self.p,att,man)

    def test_frozen_v29_mismatch_fails(self):
        rs=list(u.rows(self.p['v29']))
        rs[0]['status']='CONFIRMED';rs[0]['confirmed_ts']=str(T0+u.TF30*3)
        write_csv(self.p['v29'],rs,list(rs[0]))
        man=u.verify_sources(self.p);att,_=u.load_core(self.p,man)
        u.load_v29(self.p,att,man)
        with self.assertRaisesRegex(ValueError,'Frozen V29 process-link mismatch'):
            u.load_processes(self.p,att,man)

    def test_entry_not_covered_rejected(self):
        rs=list(u.rows(self.p['coverage']))
        rs[0]['status']='NO_SIGNAL';write_csv(self.p['coverage'],rs,list(rs[0]))
        man=u.verify_sources(self.p);att,_=u.load_core(self.p,man)
        with self.assertRaisesRegex(ValueError,'not supported'):
            u.load_entry_records(self.p,att,man)

    def test_entry_wrong_1m_open_fails(self):
        p=self.p['minute']/'WAXPUSDT_1m.csv';rs=list(u.rows(p));rs[0]['open']='99'
        rs[0]['high']='103.5';rs[0]['low']='98'
        write_csv(p,rs,list(rs[0]))
        mm=u.read_manifest(self.p['minute_manifest']/'WAXPUSDT.json')
        mm['sha256']=u.digest(p)
        write_json(self.p['minute_manifest']/'WAXPUSDT.json',mm)
        with self.assertRaisesRegex(ValueError,'OPEN 1m mismatch'):
            u.run(self.root)

    def test_duplicate_entry_id_fails_after_manifest_recount(self):
        rs=list(u.rows(self.p['entries']))
        rs.append(rs[0]);write_csv(self.p['entries'],rs,list(rs[0]));self.rebuild_hashes('entry')
        man=u.verify_sources(self.p);att,_=u.load_core(self.p,man)
        with self.assertRaisesRegex(ValueError,'Duplicated entry'):
            u.load_entry_records(self.p,att,man)

    def test_existing_output_not_overwritten(self):
        self.p['output'].mkdir(parents=True)
        with self.assertRaises(FileExistsError):u.run(self.root)

    def test_output_inside_source_rejected(self):
        with self.assertRaisesRegex(ValueError,'Refuse output'):
            u.run(self.root,{'output':str(self.p['v2']/'unified')})

    def test_manifest_and_sha_of_outputs(self):
        u.run(self.root)
        m=u.read_manifest(self.p['output']/'manifest.json')
        self.assertTrue(m['no_live'])
        for name,sha in m['outputs_sha256'].items():
            self.assertEqual(u.digest(self.p['output']/name),sha)

    def test_timestamp_decimal_exact_not_float_round(self):
        self.assertEqual(u.clock(str(T0)+'.0',step=u.TF30),T0)
        for wrong in (str(T0)+'.5','nan','0','1787184000123','oops'):
            with self.assertRaises(ValueError):u.clock(wrong,step=u.TF30)

    def test_barrier_sl_first(self):
        ts=self.entrytime
        o=([100,100,100],[100.2,100.2,100.2],[99.9,98.9,98.9],[100,99,99])
        r=u.score_entry(ts,100,[ts+i*u.TF1 for i in range(3)],o,('foo',3,1,3))
        self.assertEqual(r['state'],'SL_FIRST')

    def test_barrier_ambiguity_same_minute(self):
        ts=self.entrytime
        o=([100],[104],[97],[101]);r=u.score_entry(ts,100,[ts],o,('foo',3,1.5,1))
        self.assertEqual(r['state'],'AMBIGUOUS_1M');self.assertEqual(r['net_pct'],'')

    def test_barrier_1m_gap_first(self):
        ts=self.entrytime
        o=([100,96],[101,98],[99.5,95],[100,97]);r=u.score_entry(ts,100,[ts,ts+u.TF1],o,('foo',3,1.5,2))
        self.assertEqual(r['state'],'SL_GAP_OPEN');self.assertLess(r['net_pct'],-1.5)

    def test_missing_1m_censored(self):
        ts=self.entrytime
        o=([100],[101],[99.8],[100.5]);r=u.score_entry(ts,100,[ts],o,('foo',3,1.5,100))
        self.assertEqual(r['state'],'CENSORED_GAP_OR_TAIL');self.assertEqual(r['net_pct'],'')

    def test_no_1m_file_and_no_entry_time(self):
        ts=self.entrytime
        self.assertEqual(u.score_entry(ts,100,None,None,('foo',3,1,20))['state'],'NO_1M_FILE')
        self.assertEqual(u.score_entry(ts,100,[ts+u.TF1],([100],[100],[100],[100]),('foo',3,1,20))['state'],'NO_ENTRY_MINUTE')

    def test_checkpoint_after_entry_marked_post(self):
        u.run(self.root)
        cx=list(u.rows(self.p['output']/'entry_process_context_RETROSPECTIVE.csv'))
        self.assertEqual([r['relative_to_entry'] for r in cx],['ALREADY_KNOWN','AFTER_ENTRY'])
        self.assertTrue(all(r['link_is_retrospective']=='YES_NOT_PREDICTIVE_FEATURE' for r in cx))

    def test_development_only_after_entry(self):
        u.run(self.root)
        dev=list(u.rows(self.p['output']/'entry_development_post.csv'))
        self.assertEqual(len(dev),1)
        self.assertEqual(dev[0]['horizon_min'],'5')
        self.assertEqual(dev[0]['post_only'],'YES_NOT_PRE_FEATURE')

    def test_holdout_split_is_preserved_and_separate(self):
        rs=list(u.rows(self.p['v2']/'physical_processes.csv'))
        rs[0]['split']='HOLDOUT_SYMBOL'
        write_csv(self.p['v2']/'physical_processes.csv',rs,list(rs[0]))
        self.rebuild_hashes('v2')
        out=u.run(self.root)
        self.assertEqual(out['split_symbol_counts'],{'HOLDOUT_SYMBOL':1})
        self.assertEqual(out['verified_entries_by_split'],{'HOLDOUT_SYMBOL':1})
        entries=list(u.rows(self.p['output']/'unified_entries.csv'))
        self.assertEqual(entries[0]['split'],'HOLDOUT_SYMBOL')
        self.assertEqual(len(list(u.rows(self.p['output']/'matched_flip_review_pairs.csv'))),0)

    def test_inconsistent_symbol_split_rejected(self):
        rs=list(u.rows(self.p['v2']/'physical_processes.csv'))
        rs.append(dict(rs[0],process_id='process2',split='HOLDOUT_SYMBOL'))
        write_csv(self.p['v2']/'physical_processes.csv',rs,list(rs[0]))
        v2_summary=u.read_manifest(self.p['v2']/'summary.json')
        v2_summary['unique_physical_processes_by_symbol_level']=2
        write_json(self.p['v2']/'summary.json',v2_summary)
        self.rebuild_hashes('v2')
        man=u.verify_sources(self.p);att,_=u.load_core(self.p,man);u.load_v29(self.p,att,man)
        with self.assertRaisesRegex(ValueError,'split changed'):
            u.load_processes(self.p,att,man)

    def test_review_verified_report_and_attempt_lookup(self):
        import hll_unified_lifecycle_review_v1 as rev
        u.run(self.root)
        r=rev.review(self.p['output'],symbol='WAXPUSDT',attempt=self.aids[0])
        self.assertEqual(r['attempts'],1)
        self.assertEqual(r['verified_entries'],1)
        self.assertEqual(r['reference_tp3_sl1p5_48h_by_route']['FLIP_1M']['TP_FIRST'],1)

    def test_review_fails_if_output_tampered(self):
        import hll_unified_lifecycle_review_v1 as rev
        u.run(self.root)
        (self.p['output']/'entry_outcomes_1m.csv').write_text('tamper',encoding='utf8')
        with self.assertRaisesRegex(ValueError,'damaged'):
            rev.review(self.p['output'])


if __name__=='__main__':unittest.main()
