#!/usr/bin/env python3
"""Regression tests for HLL Flip Entry Timing V1.2 (offline)."""
import csv
import json
import tempfile
import unittest
from pathlib import Path

import hll_flip_entry_timing_v12 as m
import hll_unified_structure_lifecycle_v1 as u

T0=1787184000000


def csv_write(path, records):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=list(records[0]));w.writeheader();w.writerows(records)


def json_write(path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data),encoding='utf-8')


def record(ts=T0, eid='entry1', aid='attempt1',symbol='WAXPUSDT'):
    return dict(entry_id=eid,attempt_id=aid,symbol=symbol,
        episode_id='episode_'+aid,level_id='level1',episode_ordinal='1',
        route='FLIP_1M',split='DISCOVERY_SYMBOL',pre_pattern_96='ASCENDING_CLEAN',
        family_0p5='ASCENSO_ESCALONADO',family_1p0='ASCENSO_ESCALONADO',
        family_1p5='ASCENSO_ESCALONADO',scale_agreement='IGUAL_3_ATR',
        fingerprint_status='COMPLETE_PRE_96_288',
        pre_closed_ts=str(ts),breakout_closed_ts=str(ts+u.TF30),
        entry_ts=str(ts+u.TF30+2*u.TF1),signal_ts=str(ts+u.TF30+u.TF1),
        entry_price='104',zone_low='99',zone_high='100',atr_pre='2',
        entry_1m_verified='YES_SOURCE_ENTRY_VERIFIED',coverage_FLIP_1M='ENTRY_VERIFIED')


def minute_bars(e, *, spike=106):
    start=u.clock(e['pre_closed_ts'],step=u.TF30)
    entry=u.clock(e['entry_ts'])
    result=[]
    for t in range(start,entry+20*u.TF1,u.TF1):
        if t==entry:
            o=104;hi=105;lo=103.5;cl=104.4
        elif t==entry-u.TF1:
            o=102;hi=spike;lo=101;cl=103
        elif t==entry-2*u.TF1:
            o=101;hi=102.2;lo=100.8;cl=102
        else:
            o=100;hi=100.7;lo=99.8;cl=100
        result.append(dict(timestamp=str(t),open=str(o),high=str(hi),low=str(lo),close=str(cl)))
    return result


class TimingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.p=m.anchored_paths(self.root)
        self.e=record()
        self.ts=[int(r['timestamp']) for r in minute_bars(self.e)]
        self.arr=([float(r[k]) for r in minute_bars(self.e)] for k in ('open','high','low','close'))
        self.arr=tuple(self.arr)

    def build_fixture(self):
        original={k:v for k,v in self.e.items() if k not in ('family_0p5','family_1p0','family_1p5','scale_agreement')}
        outcome=dict(entry_id=self.e['entry_id'],scenario=m.REFERENCE,state='TP_FIRST',
            route=m.FLIP,attempt_id=self.e['attempt_id'],symbol=self.e['symbol'],
            exit_ts=str(int(self.e['entry_ts'])+4*u.TF1),net_pct='2.9',minutes_observed='4')
        csv_write(self.p['v1']/'unified_entries.csv',[original])
        csv_write(self.p['v11']/'unified_entries_family_v11.csv',[self.e])
        csv_write(self.p['v1']/'entry_outcomes_1m.csv',[outcome])
        csv_write(self.p['v11']/'entry_outcomes_family_v11.csv',[{**outcome,'family_1p0':'ASCENSO_ESCALONADO'}])
        pair_path=self.p['v11']/'family_geometry_flip_review_pairs_v11.csv'
        pair_path.parent.mkdir(parents=True,exist_ok=True)
        pair_path.write_text('winner_entry_id,loser_entry_id,pre_pattern_96,family_1p0\n',encoding='utf-8')
        json_write(self.p['v1']/'summary.json',{ 
            'verified_entry_events':1,'outcome_scenarios':1,
            'verified_entries_by_route':{m.FLIP:1}})
        minutes=self.p['minute']/f"{self.e['symbol']}_1m.csv"
        csv_write(minutes,minute_bars(self.e))
        metap=self.p['minute_manifest']/f"{self.e['symbol']}.json"
        json_write(metap,{'sha256':m.sha(minutes),'source_sha256':'prehash',
                          'bars':len(minute_bars(self.e))})
        v1_manifest={'version':'HLL_UNIFIED_STRUCTURE_LIFECYCLE_V1',
            'entry_scope':'FIRST_PER_LEVEL','fingerprint_scope':'EPISODE_FIRST_ONLY',
            'source_sha256':{'pre':'prehash'},
            'outputs_sha256':{name:m.sha(self.p['v1']/name) for name in (
                'unified_entries.csv','entry_outcomes_1m.csv','summary.json')},
            'minute_sha256':{self.e['symbol']:m.sha(minutes)},
            'minute_manifest_sha256':{self.e['symbol']:m.sha(metap)}}
        json_write(self.p['v1']/'manifest.json',v1_manifest)
        v11_manifest={'version':'HLL_UNIFIED_FAMILY_LIFECYCLE_V1_1',
            'outcomes_copied_no_recompute':True,'scope_first_per_level':True,
            'output_sha256':{name:m.sha(self.p['v11']/name) for name in (
                'unified_entries_family_v11.csv','entry_outcomes_family_v11.csv',
                'family_geometry_flip_review_pairs_v11.csv')},
            'sources_sha256':{'v1_manifest':m.sha(self.p['v1']/'manifest.json'),
                'v1_entries':m.sha(self.p['v1']/'unified_entries.csv'),
                'v1_outcomes':m.sha(self.p['v1']/'entry_outcomes_1m.csv')}}
        json_write(self.p['v11']/'manifest.json',v11_manifest)
        return minutes,metap

    def test_complete_1m_prior_peak(self):
        v=m.prefix_metrics(self.e,self.ts,self.arr)
        self.assertEqual(v['preentry_quality'],'COMPLETE_PREOPEN_1M')
        self.assertTrue(v['has_preentry_full_1m_prefix'])
        self.assertEqual(v['preentry_peak_price'],106)
        self.assertAlmostEqual(v['entry_pullback_from_preentry_peak_pct'],round(100*2/106,6))
        self.assertEqual(v['lag_from_breakout_min'],2)
        self.assertEqual(v['signal_to_open_min'],1)

    def test_after_entry_changes_do_not_modify_preentry(self):
        v=m.prefix_metrics(self.e,self.ts,self.arr)
        arrays=[list(a) for a in self.arr]
        eidx=self.ts.index(int(self.e['entry_ts']))
        for j in range(eidx,len(self.ts)):
            arrays[1][j]=99999
            arrays[2][j]=.01
            arrays[3][j]=10000
        self.assertEqual(v,m.prefix_metrics(self.e,self.ts,arrays))

    def test_entry_high_is_not_preopen_peak(self):
        arrays=[list(a) for a in self.arr]
        arrays[1][self.ts.index(int(self.e['entry_ts']))]=999
        self.assertEqual(m.prefix_metrics(self.e,self.ts,arrays)['preentry_peak_price'],106)

    def test_missing_middle_minute_fails_closed(self):
        index=9
        t=self.ts[:index]+self.ts[index+1:]
        a=tuple(x[:index]+x[index+1:] for x in self.arr)
        v=m.prefix_metrics(self.e,t,a)
        self.assertEqual(v['preentry_quality'],'INCOMPLETE_1M_PREFIX')
        self.assertEqual(v['preentry_peak_above_zone_pct'],'')

    def test_missing_start_minute_fails_closed(self):
        v=m.prefix_metrics(self.e,self.ts[1:],tuple(x[1:] for x in self.arr))
        self.assertEqual(v['preentry_quality'],'INCOMPLETE_1M_PREFIX')

    def test_wrong_entry_price_rejected(self):
        e={**self.e,'entry_price':'103'}
        with self.assertRaisesRegex(ValueError,'OPEN price'):
            m.prefix_metrics(e,self.ts,self.arr)

    def test_future_signal_rejected(self):
        e={**self.e,'signal_ts':str(int(self.e['entry_ts'])+u.TF1)}
        with self.assertRaisesRegex(ValueError,'Signal after'):
            m.prefix_metrics(e,self.ts,self.arr)

    def test_noncausal_cross_rejected(self):
        e={**self.e,'breakout_closed_ts':self.e['pre_closed_ts']}
        with self.assertRaisesRegex(ValueError,'Noncausal'):
            m.prefix_metrics(e,self.ts,self.arr)

    def test_closed_prefix_at_entry_is_available(self):
        v=m.prefix_metrics(self.e,self.ts,self.arr)
        self.assertEqual(v['last_closed_1m_price'],103)
        self.assertEqual(v['entry_price'],104)

    def test_postcross_is_separated_from_breakout_candle(self):
        v=m.prefix_metrics(self.e,self.ts,self.arr)
        self.assertEqual(v['postcross_closes_above'],2)
        self.assertEqual(v['postcross_closes_inside'],0)
        self.assertEqual(v['postcross_closes_below'],0)
        self.assertEqual(v['postcross_last_above_streak'],2)

    def test_bins_are_frozen(self):
        self.assertEqual(m.fixed_bin('lag_from_breakout_min',30),'LAG_0_30')
        self.assertEqual(m.fixed_bin('lag_from_breakout_min',31),'LAG_31_90')
        self.assertEqual(m.fixed_bin('entry_above_zone_high_pct',0),'AT_OR_BELOW_ZONE')
        self.assertEqual(m.fixed_bin('entry_pullback_from_preentry_peak_pct',''),'NO_PREENTRY_COVERAGE')

    def test_no_data_without_archive_not_mistaken_for_winner(self):
        v=m.prefix_metrics(self.e,None,None)
        self.assertEqual(v['preentry_quality'],'NO_1M_FILE')
        self.assertEqual(v['preentry_postcross_peak_pct'],'')

    def test_result_states_distinct(self):
        self.assertEqual(m.outcome_label('TP_GAP_OPEN'),'TP_FIRST')
        self.assertEqual(m.outcome_label('SL_GAP_OPEN'),'SL_FIRST')
        self.assertEqual(m.outcome_label('AMBIGUOUS_1M'),'UNRESOLVED')
        self.assertEqual(m.outcome_label('TIMEOUT'),'UNRESOLVED')

    def test_group_keeps_distinct_episode_count(self):
        d={'split':'DISCOVERY_SYMBOL','pre_pattern_96':'ASCENDING_CLEAN',
           'family_1p0':'ASCENSO_ESCALONADO','episode_id':'epi1','symbol':'WAXPUSDT',
           'outcome_class':'TP_FIRST','lag_from_breakout_min':30,
           'entry_above_zone_high_pct':.5,'entry_pullback_from_preentry_peak_pct':1}
        grouped=m.summary_group([d,{**d,'outcome_class':'SL_FIRST'}],('split','family_1p0'))
        self.assertEqual(grouped[0]['entries'],2)
        self.assertEqual(grouped[0]['episodes'],1)
        self.assertEqual(grouped[0]['tp_share_resolved_pct'],50.)

    def test_verified_sources_end_to_end(self):
        self.build_fixture()
        m.run(self.p)
        self.assertTrue((self.p['output']/'manifest.json').is_file())
        rows,_=m.csv_read(self.p['output']/'flip_entry_timing_preopen.csv')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['outcome_class'],'TP_FIRST')
        self.assertEqual(rows[0]['family_1p0'],'ASCENSO_ESCALONADO')
        result=m.inspect(self.p,self.e['attempt_id'])
        self.assertEqual(len(result),1)

    def test_existing_output_does_not_overwrite(self):
        self.build_fixture();m.run(self.p)
        with self.assertRaises(FileExistsError):m.run(self.p)

    def test_source_v1_outcome_tamper_blocked(self):
        self.build_fixture()
        file=self.p['v1']/'entry_outcomes_1m.csv'
        content=file.read_text().replace('TP_FIRST','SL_FIRST')
        file.write_text(content)
        with self.assertRaisesRegex(ValueError,'Changed frozen report'):
            m.create_artifacts(self.p)

    def test_source_v11_entry_tamper_blocked(self):
        self.build_fixture()
        file=self.p['v11']/'unified_entries_family_v11.csv'
        file.write_text(file.read_text().replace('ASCENSO_ESCALONADO','RECONSTRUCCION_ENTRE_CONTACTOS'))
        with self.assertRaisesRegex(ValueError,'Changed frozen report'):
            m.create_artifacts(self.p)

    def test_minute_hash_tamper_blocked(self):
        path,_=self.build_fixture()
        path.write_text(path.read_text().replace('106.0','109.0').replace('106,','109,'))
        # Change is demonstrable regardless of actual float formatting.
        with self.assertRaisesRegex(ValueError,'Changed original 1m archive'):
            m.create_artifacts(self.p)

    def test_mismatch_v1_enrichment_blocked_even_if_rehashed(self):
        self.build_fixture()
        path=self.p['v11']/'unified_entries_family_v11.csv'
        path.write_text(path.read_text().replace('entry1','entry99'))
        manifest=json.loads((self.p['v11']/'manifest.json').read_text())
        manifest['output_sha256']['unified_entries_family_v11.csv']=m.sha(path)
        json_write(self.p['v11']/'manifest.json',manifest)
        with self.assertRaisesRegex(ValueError,'entry inventories disagree'):
            m.create_artifacts(self.p)

    def test_reject_output_path_inside_source(self):
        self.build_fixture()
        p={**self.p,'output':self.p['v1']/'new_subdir'}
        with self.assertRaisesRegex(ValueError,'Refusing to write'):
            m.run(p)

    def test_no_reference_case_is_not_fabricated(self):
        self.build_fixture()
        artifacts,_=m.create_artifacts(self.p)
        self.assertEqual(artifacts['dydx_cake_reference_cases.csv'],[])


    def test_incomplete_and_complete_prefix_have_identical_metric_columns(self):
        good=m.prefix_metrics(self.e,self.ts,self.arr)
        bad=m.prefix_metrics(self.e,self.ts[1:],tuple(x[1:] for x in self.arr))
        self.assertEqual(set(good),set(bad))
        self.assertEqual(bad['last_closed_1m_relation_to_zone'],'')
        self.assertEqual(bad['preentry_peak_price'],'')

    def test_unknown_historical_result_blocked(self):
        self.build_fixture()
        path=self.p['v1']/'entry_outcomes_1m.csv'
        path.write_text(path.read_text().replace('TP_FIRST','HYPOTHETICAL'))
        v1=json.loads((self.p['v1']/'manifest.json').read_text())
        v1['outputs_sha256']['entry_outcomes_1m.csv']=m.sha(path)
        json_write(self.p['v1']/'manifest.json',v1)
        # Even though V1 was rehashed, the unchanged V1.1 dependency must reject it.
        with self.assertRaisesRegex(ValueError,'V1.1 is not derived'):
            m.create_artifacts(self.p)

    def test_fake_pair_must_not_be_silently_dropped(self):
        self.build_fixture()
        path=self.p['v11']/'family_geometry_flip_review_pairs_v11.csv'
        path.write_text(path.read_text()+
            'entry1,missing,ASCENDING_CLEAN,ASCENSO_ESCALONADO\n')
        manifest=json.loads((self.p['v11']/'manifest.json').read_text())
        manifest['output_sha256'][path.name]=m.sha(path)
        json_write(self.p['v11']/'manifest.json',manifest)
        with self.assertRaisesRegex(ValueError,'missing Flip entry'):
            m.create_artifacts(self.p)

    def test_output_tamper_inspection_blocked(self):
        self.build_fixture();m.run(self.p)
        file=self.p['output']/'flip_entry_timing_preopen.csv'
        file.write_text(file.read_text().replace('TP_FIRST','SL_FIRST'))
        with self.assertRaisesRegex(ValueError,'Changed frozen report'):
            m.inspect(self.p,self.e['attempt_id'])

    def test_output_preserves_preexisting_inputs(self):
        self.build_fixture()
        paths=[self.p['v1']/'unified_entries.csv',
               self.p['v11']/'unified_entries_family_v11.csv',
               self.p['minute']/f"{self.e['symbol']}_1m.csv"]
        old={str(p):m.sha(p) for p in paths}
        m.run(self.p)
        self.assertEqual(old,{str(p):m.sha(p) for p in paths})

if __name__=='__main__':unittest.main()
