"""Offline and causal regression tests; synthetic sources only."""
from __future__ import annotations
import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from hll_resistance_process_causal_v2 import (
    TF, V1_FILES, build_physical_groups, causal_trace, load_attempts,
    match_controls, run, scenario_path, split_symbol, stable_id,
    validate_v1, validate_v1_processes, validate_v1_identity,
)
from hll_detector_miss_audit_v1 import PRE, V29, EPISODES, CANDLES, sha256
from hll_resistance_process_cohort_v1 import LEVELS, OUTPUT as V1_OUTPUT
from hll_resistance_universal_audit_v1 import price_behavior

START = 1788899400000


def csvout(path, rows, columns=None):
    path.parent.mkdir(parents=True,exist_ok=True)
    fields=columns or list(rows[0])
    with path.open('w',encoding='utf-8',newline='') as handle:
        w=csv.DictWriter(handle,fieldnames=fields)
        w.writeheader();w.writerows(rows)


def fixture(root, *, with_second=True, change_v1_contact=False):
    # Two attempts (even across different heuristic episodes) seeing the SAME
    # two physical touches of the SAME level must create ONE physical process.
    symbol='WAXPUSDT'
    candles=[]
    for i in range(-1,120):
        if i==1: o,h,l,c = 1.03,1.04,1.01,1.015
        elif i==2: o,h,l,c = 1.015,1.04,1.012,1.03
        elif i==3: o,h,l,c = 1.03,1.05,1.025,1.041
        else: o,h,l,c = 1.04,1.05,1.03,1.04
        candles.append(dict(timestamp=START+i*TF,open=o,high=h,low=l,close=c))
    csvout(root/CANDLES/f'{symbol}_30m.csv',candles)
    meta=[]
    for j in range(2 if with_second else 1):
        aid=f'A{j+1}'
        ts=START+j*TF
        meta.append(dict(attempt_id=aid,symbol=symbol,level_id='L0',
            episode_id=f'E{j+1}',pre_closed_ts=ts,breakout_closed_ts=ts+TF,
            zone_low=1.0,zone_high=1.02,formation_atr_status='VERIFIED',
            v26_status='UNRESOLVED_FIRST4',
            continuation_status='NO_EARLY_CONTINUATION',
            reclaim_status='NO_ORIGINAL_FAILURE',audit_state='OK',
            early4_state='KNOWN',early4_intermit=j==0,
            early4_pattern='ABOVE>IN_ZONE>ABOVE>IN_ZONE' if j==0 else 'ABOVE>ABOVE>ABOVE>ABOVE',
            known_closed_ts=ts+4*TF,
            observed_post_bars=8,raw_physical_contacts=2 + (1 if change_v1_contact else 0),
            contact_processes=1))
    p=root/V1_OUTPUT/'pilot15_seed20261103_v1'
    p.mkdir(parents=True)
    csvout(p/'attempt_processes.csv',meta)
    processes=[]
    for j,a in enumerate(meta):
        offset=1-j
        processes.append(dict(attempt_id=a['attempt_id'],symbol=symbol,
            episode_id=a['episode_id'],start_offset=offset,last_offset=offset+1,
            first_known_closed_ts=START+2*TF,last_known_closed_ts=START+3*TF))
    csvout(p/'contact_processes.csv',processes)
    csvout(p/'episode_rollup.csv',[dict(symbol=symbol,episode_id='E1',attempts=1)])
    (p/'summary.json').write_text(json.dumps(dict(attempts=len(meta),
        contact_process_rows=len(processes),mode='PILOT15',
        attempt_status_counts={'OK':len(meta)})))
    # Frozen provenance; V2 only reads and verifies these inputs.
    csvout(root/PRE,[dict(attempt_id=a['attempt_id'],symbol=symbol,level_id='L0',
        pre_closed_ts=a['pre_closed_ts'],breakout_closed_ts=a['breakout_closed_ts'],
        zone_low=1.0,zone_high=1.02,v26_status=a['v26_status']) for a in meta])
    csvout(root/V29,[dict(attempt_id=a['attempt_id'],origin_symbol=symbol,
        branch=branch,status=a[statuskey]) for a in meta for branch,statuskey in
        [('BREAKOUT_CONTINUATION','continuation_status'),
         ('RECLAIM_CONTINUATION','reclaim_status')]])
    csvout(root/EPISODES,[dict(attempt_id=a['attempt_id'],symbol=symbol,
        level_id='L0',episode_id=a['episode_id'],
        pre_closed_ts=a['pre_closed_ts'],
        breakout_closed_ts=a['breakout_closed_ts']) for a in meta])
    csvout(root/LEVELS,[dict(symbol=symbol,level_id='L0',
        zone_low='1.0',zone_high='1.02',atr_at_formation='.02')])
    import inspect
    engine=Path(inspect.getfile(price_behavior))
    manifest=dict(version='HLL_RESISTANCE_PROCESS_COHORT_V1',
      pre_sha256=sha256(root/PRE),v29_sha256=sha256(root/V29),
      episodes_sha256=sha256(root/EPISODES),engine_sha256=sha256(engine),
      files_sha256={name:sha256(p/name) for name in V1_FILES})
    (p/'manifest.json').write_text(json.dumps(manifest))
    return p


class ProcessCausalV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.v1=fixture(self.root)

    def test_source_hash_rejects_v1_tampering(self):
        validate_v1(self.root,V1_OUTPUT/'pilot15_seed20261103_v1',check_hash=True)
        with (self.v1/'summary.json').open('a') as f:f.write(' ')
        with self.assertRaisesRegex(ValueError,'hash mismatch'):
            validate_v1(self.root,V1_OUTPUT/'pilot15_seed20261103_v1',check_hash=True)

    def test_canonical_physical_dedups_attempts_and_episodes(self):
        attempts,levels=load_attempts(self.v1)
        self.assertEqual(validate_v1_processes(self.v1,attempts),2)
        groups,links,_=build_physical_groups(self.root,levels,gap=2)
        self.assertEqual(len(groups),1)
        self.assertEqual(groups[0]['physical_touch_bars'],2)
        self.assertEqual(groups[0]['related_attempt_count'],2)
        self.assertEqual(groups[0]['related_episode_count'],2)
        self.assertEqual(len(links),2)
        self.assertEqual(groups[0]['first_contact_known_closed_ts'],START+2*TF)

    def test_causal_checkpoints_only_use_known_prefix(self):
        _,levels=load_attempts(self.v1)
        p=build_physical_groups(self.root,levels,gap=2)[0][0]
        cp,evolution=causal_trace(p)
        self.assertTrue(cp)
        self.assertTrue(evolution)
        self.assertIn('CONTACT_FROM_ABOVE',{r['event_type'] for r in cp})
        self.assertIn('DESCRIPTIVE_DEFENSE',{r['event_type'] for r in cp})
        for c in cp:
            self.assertEqual(c['known_closed_ts'],c['hypothetical_next_open_ts'])
            self.assertNotIn('last_contact_known_closed_ts',c)
            self.assertNotIn('v26_status',c)
            self.assertTrue(c['no_entry_signal'])
            self.assertLessEqual(c['contact_bars_known'],p['physical_touch_bars'])
        first=min(cp,key=lambda c:c['known_closed_ts'])
        self.assertEqual(first['known_closed_ts'],START+2*TF)
        self.assertFalse(first['intermittent4_known'])
        intermit=next(c for c in cp if c['event_type']=='INTERMITTENT4_KNOWN')
        self.assertGreaterEqual(intermit['known_closed_ts'],START+4*TF)

    def test_process_fails_on_stale_ohlc(self):
        # Modify true price path without refreshing V1 raw-contact counts.
        file=self.root/CANDLES/'WAXPUSDT_30m.csv'
        with file.open() as f:rows=list(csv.DictReader(f))
        rows[2].update(open='1.03',high='1.04',low='1.025',close='1.03')
        csvout(file,rows)
        _,levels=load_attempts(self.v1)
        with self.assertRaisesRegex(ValueError,'V1/OHLC physical-contact mismatch'):
            build_physical_groups(self.root,levels)

    def test_zone_mismatch_fails_closed(self):
        with (self.v1/'attempt_processes.csv').open() as f:rows=list(csv.DictReader(f))
        rows[1]['zone_high']='1.08'
        csvout(self.v1/'attempt_processes.csv',rows)
        with self.assertRaisesRegex(ValueError,'Conflicting resistance'):
            load_attempts(self.v1)

    def test_censored_observed_ranges_not_extended(self):
        with (self.v1/'attempt_processes.csv').open() as f:rows=list(csv.DictReader(f))
        for row in rows:
            row['observed_post_bars']='3'
            row['audit_state']='CENSORED'
        csvout(self.v1/'attempt_processes.csv',rows)
        _,levels=load_attempts(self.v1)
        processes,_,_=build_physical_groups(self.root,levels)
        self.assertEqual(len(processes),1)
        self.assertTrue(all(ts<=START+3*TF for ts in processes[0]['_trace_ts']))

    def test_split_stable_and_symbol_based(self):
        self.assertEqual(split_symbol('WAXPUSDT'),split_symbol('WAXPUSDT'))
        self.assertIn(split_symbol('WAXPUSDT'),('DISCOVERY_SYMBOL','HOLDOUT_SYMBOL'))

    def test_sl_first_and_theoretical_fee(self):
        b={START:(100,101,97,99),START+TF:(98,99,97,98)}
        x=scenario_path(b,START,horizon=2,tp_pct=3,sl_pct=1)
        self.assertEqual(x['state'],'SL_FIRST')
        self.assertAlmostEqual(x['net_pct'],-1.1)

    def test_tp_first(self):
        b={START:(100,104,99.5,103),START+TF:(104,105,103,105)}
        x=scenario_path(b,START,horizon=2,tp_pct=3,sl_pct=1)
        self.assertEqual(x['state'],'TP_FIRST')
        self.assertAlmostEqual(x['net_pct'],2.9)

    def test_same_bar_ambiguous_not_winner(self):
        b={START:(100,105,98,102)}
        x=scenario_path(b,START,horizon=1,tp_pct=3,sl_pct=1)
        self.assertEqual(x['state'],'AMBIGUOUS_SAME_BAR')
        self.assertEqual(x['net_pct'],'')

    def test_missing_next_open_censored(self):
        self.assertEqual(scenario_path({},START,horizon=4,tp_pct=3,sl_pct=1)['state'],'NO_REFERENCE_OPEN')
        x=scenario_path({START:(100,100.1,99.9,100)},START,horizon=4,tp_pct=3,sl_pct=1)
        self.assertEqual(x['state'],'CENSORED_PATH')

    def test_open_gap_overrides_ohlc_threshold(self):
        b={START:(100,101,99.9,100),START+TF:(96,98,95,97)}
        x=scenario_path(b,START,horizon=2,tp_pct=3,sl_pct=1)
        self.assertEqual(x['state'],'SL_GAP_OPEN')
        self.assertAlmostEqual(x['gross_pct'],-4)

    def test_timeout_mark_to_market(self):
        b={START:(100,101,99.5,100.4)}
        x=scenario_path(b,START,horizon=1,tp_pct=3,sl_pct=1)
        self.assertEqual(x['state'],'TIMEOUT')
        self.assertAlmostEqual(x['net_pct'],.3)

    def test_matched_controls_never_mix_holdout(self):
        def mk(sym,st,idx):
            return {'checkpoint_id':str(idx),'process_id':'P'+str(idx),
                    'signature':'S','symbol':sym,'split':st}
        cps=[mk('AAA','DISCOVERY_SYMBOL',1),mk('BBB','DISCOVERY_SYMBOL',2),
             mk('CCC','HOLDOUT_SYMBOL',3)]
        scenarios=[{'checkpoint_id':'1','scenario':'TP3_SL1_48','state':'TP_FIRST',
                    'split':'DISCOVERY_SYMBOL'},
                   {'checkpoint_id':'2','scenario':'TP3_SL1_48','state':'SL_FIRST',
                    'split':'DISCOVERY_SYMBOL'},
                   {'checkpoint_id':'3','scenario':'TP3_SL1_48','state':'SL_FIRST',
                    'split':'HOLDOUT_SYMBOL'}]
        pairs=match_controls(cps,scenarios)
        self.assertEqual(len(pairs),1)
        self.assertEqual(pairs[0]['good_symbol'],'AAA')
        self.assertEqual(pairs[0]['bad_symbol'],'BBB')

    def test_end_to_end_writes_only_new_reports(self):
        before={name:sha256(self.v1/name) for name in [*V1_FILES,'manifest.json']}
        result=run(self.root)
        self.assertEqual(result['status'],'DONE')
        self.assertEqual(result['physical_processes'],1)
        out=Path(result['output'])
        x=json.loads((out/'summary.json').read_text())
        self.assertEqual(x['v1_per_attempt_contact_process_rows'],2)
        self.assertEqual(x['unique_physical_touch_bars'],2)
        with (out/'process_checkpoints.csv').open() as f:rows=list(csv.DictReader(f))
        self.assertTrue(rows)
        with (out/'checkpoint_scenarios.csv').open() as f:scene=list(csv.DictReader(f))
        self.assertEqual(len(scene),len(rows)*4)
        self.assertEqual(before,{name:sha256(self.v1/name) for name in before})
        with self.assertRaises(FileExistsError):run(self.root)


    def test_frozen_identity_disagreement_is_rejected(self):
        attempts,_=load_attempts(self.v1)
        file=self.root/V29
        with file.open() as f:rows=list(csv.DictReader(f))
        rows[0]['status']='CONFIRMED'
        csvout(file,rows)
        with self.assertRaisesRegex(ValueError,'Frozen V2.9 status mismatch'):
            validate_v1_identity(self.root,attempts)

    def test_causal_prefix_invariant_to_future_bars_and_frozen_outcomes(self):
        _,levels=load_attempts(self.v1)
        proc=build_physical_groups(self.root,levels,gap=2)[0][0]
        original,_=causal_trace(proc)
        earliest=sorted(original,key=lambda r:r['known_closed_ts'])[0]
        modified=dict(proc)
        modified['_trace_ts']=proc['_trace_ts'][:2]
        # An original detector's eventual status may change independently; it
        # must not alter a physical event known after its closing candle.
        modified['_members']=[dict(a,v26_status='FLIP_SUSTAINED_4B',
                continuation_status='CONFIRMED') for a in proc['_members']]
        truncated,_=causal_trace(modified)
        same=next(r for r in truncated if r['checkpoint_id']==earliest['checkpoint_id'])
        self.assertEqual(earliest['signature'],same['signature'])
        self.assertEqual(earliest['contact_bars_known'],same['contact_bars_known'])
        self.assertEqual(earliest['known_closed_ts'],same['known_closed_ts'])

    def test_distinct_levels_are_never_merged(self):
        attempts,levels=load_attempts(self.v1)
        other=dict(next(iter(attempts.values())))
        other['attempt_id']='A3'
        other['level_id']='L1'
        levels[(other['symbol'],'L1')]=[other]
        # Missing formation ATR for L1 is permitted for behavior, never
        # materialized as a false defense.
        grouped,_,_=build_physical_groups(self.root,levels)
        self.assertEqual(len(grouped),2)
        self.assertNotEqual(grouped[0]['process_id'],grouped[1]['process_id'])

    def test_intermit_flag_cannot_be_used_before_known_clock(self):
        _,levels=load_attempts(self.v1)
        proc=build_physical_groups(self.root,levels)[0][0]
        checkpoints,_=causal_trace(proc)
        for c in checkpoints:
            if c['known_closed_ts']<START+4*TF:
                self.assertFalse(c['intermittent4_known'])
        exact=[c for c in checkpoints if c['event_type']=='INTERMITTENT4_KNOWN']
        self.assertTrue(exact)
        self.assertTrue(all(c['known_closed_ts']==START+4*TF for c in exact))

    def test_attempt_to_output_into_source_rejected(self):
        with self.assertRaisesRegex(ValueError,'source directory'):
            run(self.root,output_dir=V1_OUTPUT/'pilot15_seed20261103_v1'/'v2')


if __name__=='__main__':unittest.main()
