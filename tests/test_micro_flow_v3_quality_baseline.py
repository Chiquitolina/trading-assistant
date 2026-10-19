"""Offline-only audit, causal matching, provenance & append-only persistence tests."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from dashboard.services.micro_flow_v3_quality_baseline import (
    QualityError, _published_history, _proxy_outcomes, audit_episodes,
    make_matched_controls, persist_quality_run, verify_saved_run,
)
from dashboard.services.micro_flow_v3_multi_session import (
    analyze_sessions, load_replay_sessions,
)
from research.micro_flow_v3.tape import TapeStore
from research.micro_flow_v3.replay import replay_tape

BASE_MS = 1_799_000_000_000


def synthetic_buckets(n=480, *, start=BASE_MS, segment=1, source='test.sqlite3'):
    rows = []
    for idx in range(n):
        instant=start+idx*1000
        spike=idx in (65, 210, 350)
        price=2500+idx*.018
        rows.append({'symbol':'ETHUSDT','event_type':'BUCKET_1S',
                     'segment_id':segment,'source_session':source,
                     'second_start_ms':instant,'second_end_ms_exclusive':instant+1000,
                     'available_ms':instant+1100,
                     'open':str(price),'high':str(price+.01),
                     'low':str(price-.01),'close':str(price),
                     'buy_qty':str(45 if spike else 1),
                     'sell_qty':str(.2 if spike else 1),
                     'delta_qty':str(44.8 if spike else 0),
                     'cvd_qty':'0','trades':10 if spike else 2})
    return rows


class QualityCausalityTests(unittest.TestCase):
    def setUp(self):
        self.rows=synthetic_buckets()
        session={'tape':'test.sqlite3','buckets':self.rows,
                 'summary':{'trades_seen':len(self.rows)}}
        self.sessions=[session]
        self.result=analyze_sessions(self.sessions,lookback=10,relative_volume=2.,
                                     episode_seconds=90, confirmation_seconds=30,
                                     min_followthrough_pct=.005)

    def test_only_published_before_asof_are_used(self):
        now=BASE_MS+140_000
        a=_published_history(self.rows,now)
        mutated=[dict(x) for x in self.rows]
        for r in mutated:
            if r['available_ms'] >= now:
                r['close']='99999'
        b=_published_history(mutated,now)
        self.assertEqual(a,b)
        self.assertLess(a['last_known_ms'],now)

    def test_same_millisecond_not_available(self):
        now=BASE_MS+160_100
        rows=[dict(x) for x in self.rows]
        for r in rows:
            if r['available_ms']==now:
                r['close']='999999'
        self.assertEqual(_published_history(rows,now),
                         _published_history(self.rows,now))

    def test_missing_prior_coverage_is_explicit(self):
        self.assertIsNone(_published_history(self.rows,BASE_MS+11_000))

    def test_proxy_never_uses_next_segment_and_censors(self):
        short=self.rows[:20]
        v=_proxy_outcomes(short,BASE_MS+10_000,1)
        self.assertEqual(v['outcome_state_30s'],'CENSORED_END_OF_SEGMENT')
        self.assertIsNone(v['return_30s_pct'])

    def test_proxies_do_not_claim_fill(self):
        v=_proxy_outcomes(self.rows,BASE_MS+100_100,1)
        self.assertEqual(v['entry_ms'],BASE_MS+101_000)
        self.assertEqual(v['outcome_state_30s'],'OBSERVED')

    def test_full_episode_audit_contains_all_states(self):
        audit=audit_episodes(self.result)
        self.assertEqual(len(audit),len(self.result['episodes']))
        self.assertEqual(len({x['episode_id'] for x in audit}),len(audit))
        self.assertEqual({x['source_session'] for x in audit},{'test.sqlite3'})

    def test_controls_match_only_same_segment_and_side(self):
        cases=make_matched_controls(self.result)
        for c in cases:
            self.assertEqual(c['source_session'],'test.sqlite3')
            if c['match_state']=='MATCHED':
                self.assertTrue(c['control_decision_ms']>=BASE_MS)
                self.assertLess(c['control_decision_ms'],BASE_MS+480_000)
                self.assertEqual(c['candidate_direction'],1)
        self.assertEqual(len(cases),sum(f['state'] in ('FAILED_AGGRESSION','PRESSURE_CONTINUED')
                                         for f in self.result['failed']))

    def test_matched_control_horizon_ends_before_signal_decision(self):
        # Long enough source to create a true 1:1 matched prior comparator.
        import math
        rows=synthetic_buckets(3000)
        for j,r in enumerate(rows):
            px=2500+1.5*math.sin(j/300)+j*.0001
            r['open']=r['close']=str(px)
            r['high']=str(px+.01);r['low']=str(px-.01)
            if j%180==60:
                r['buy_qty']='90';r['sell_qty']='0.5'
                r['delta_qty']='89.5';r['trades']=30
        result=analyze_sessions([{'tape':'test.sqlite3','buckets':rows,
                                  'summary':{'trades_seen':3000}}],
                                 confirmation_seconds=30,
                                 min_followthrough_pct=.005,
                                 min_opposite_pct=.005)
        matches=[m for m in make_matched_controls(result) if m['match_state']=='MATCHED']
        self.assertGreater(len(matches),0)
        for m in matches:
            self.assertLessEqual(m['control_decision_ms']+303_000,
                                 m['candidate_decision_ms'])

    def test_no_future_outcomes_used_for_control_selection(self):
        original=make_matched_controls(self.result)
        cloned=dict(self.result)
        cloned['failed']=[dict(x) for x in self.result['failed']]
        for r in cloned['failed']:
            for h in (30,60,180,300):
                r[f'return_{h}s_pct']=999.9
        altered=make_matched_controls(cloned)
        self.assertEqual([(x['episode_id'],x['control_decision_ms']) for x in original],
                         [(x['episode_id'],x['control_decision_ms']) for x in altered])

    def test_no_baseline_if_no_comparable_control(self):
        cases=make_matched_controls(self.result,max_trend_difference_pct=0,
                                    max_vol_difference_pct=0,match_window_minutes=1)
        self.assertTrue(all(x['match_state']=='UNMATCHED_NO_COMPARABLE_CONTROL'
                            or x['match_state']=='UNMATCHED_NO_CANDIDATE_HISTORY'
                            or x['match_state']=='MATCHED' for x in cases))
        for c in cases:
            if c['match_state']!='MATCHED':
                self.assertIsNone(c['matched_difference_180s_pct'])

    def test_controls_do_not_share_state_across_sessions(self):
        second=synthetic_buckets(start=BASE_MS+3_600_000, segment=1,source='other.sqlite3')
        sessions=self.sessions+[{'tape':'other.sqlite3','buckets':second,
                                 'summary':{'trades_seen':len(second)}}]
        r=analyze_sessions(sessions,lookback=10,episode_seconds=90,
                           min_followthrough_pct=.005)
        matches=make_matched_controls(r)
        self.assertEqual({x['source_session'] for x in matches},
                         {'test.sqlite3','other.sqlite3'})
        for x in matches:
            if x['match_state']=='MATCHED':
                expected=BASE_MS if x['source_session']=='test.sqlite3' else BASE_MS+3_600_000
                self.assertTrue(expected<=x['control_decision_ms']<expected+480_000)


class QualityPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        directory=self.root/'reports/micro_flow_v3/tapes/ETHUSDT'
        directory.mkdir(parents=True)
        self.tape=directory/'aggtrades_raw_quality_fixture.sqlite3'
        store=TapeStore(self.tape,'ETHUSDT',batch_size=20)
        store.marker('CONNECT_ATTEMPT',connection=1,
                     wall_ns=BASE_MS*1_000_000,mono_ns=10_000)
        for i in range(230):
            packet={'e':'aggTrade','s':'ETHUSDT','a':1000+i,
                    'T':BASE_MS+i*1000,'E':BASE_MS+i*1000,
                    'p':str(2500+i*.015),'q':'45' if i==61 else '1',
                    'm':False if i==61 else bool(i%2)}
            store.ingest(json.dumps(packet),connection=1,
                         wall_ns=(BASE_MS+i*1000+200)*1_000_000,
                         mono_ns=(100000+i*1000)*1_000_000)
        store.marker('PROCESS_STOP',connection=1,
                     wall_ns=(BASE_MS+231_000)*1_000_000,mono_ns=350_000_000)
        store.close()
        replay_dir=self.root/'reports/micro_flow_v3/replay'
        replay_dir.mkdir(parents=True)
        replay_tape(self.tape,replay_dir/(self.tape.stem+'_1s.jsonl'),
                    summary_path=replay_dir/(self.tape.stem+'_summary.json'))
        self.sessions=load_replay_sessions(self.root,[self.tape])
        self.result=analyze_sessions(self.sessions,lookback=10,
                                     confirmation_seconds=30,episode_seconds=90)
        self.params={'lookback':10,'confirmation_seconds':30,'episode_seconds':90}

    def save(self,settings=None):
        return persist_quality_run(self.root,self.sessions,self.result,
                                   parameters=self.params,control_settings=settings)

    def test_full_source_and_artifact_provenance_verified(self):
        path,manifest=self.save()
        self.assertEqual(verify_saved_run(path,verify_sources=True,project_root=self.root)['run_id'],
                         manifest['run_id'])
        self.assertEqual(len((path/'episodes.jsonl').read_text().splitlines()),
                         len(self.result['episodes']))
        self.assertIn('source_tape_digest_sha256',self.sessions[0]['summary'])
        self.assertEqual(len(manifest['identity']['software_sha256']),9)
        self.assertTrue(manifest['identity']['sources'][0]['tape_path'].startswith('reports/'))

    def test_identical_snapshot_no_overwrite(self):
        path,_=self.save()
        initial=(path/'manifest.json').stat().st_mtime_ns
        again,_=self.save()
        self.assertEqual(path,again)
        self.assertEqual((path/'manifest.json').stat().st_mtime_ns,initial)

    def test_config_version_creates_new_directory(self):
        first,_=self.save({'estimated_round_trip_cost_bps':12})
        other,_=self.save({'estimated_round_trip_cost_bps':18})
        self.assertNotEqual(first,other)

    def test_modified_replay_rejected(self):
        with Path(self.sessions[0]['replay_path']).open('a') as f:
            f.write('{}\n')
        with self.assertRaisesRegex(QualityError,'Replay modificado'):
            self.save()

    def test_replay_summary_changed_after_loading_rejected(self):
        summary=Path(self.sessions[0]['summary_path'])
        payload=json.loads(summary.read_text())
        payload['bucket_time_basis']='INVALID_REWRITTEN'
        summary.write_text(json.dumps(payload))
        with self.assertRaisesRegex(QualityError,'summary changed'):
            self.save()

    def test_manifest_identity_tampering_detected(self):
        path,_=self.save()
        manifest_file=path/'manifest.json'
        payload=json.loads(manifest_file.read_text())
        payload['identity']['parameters']['lookback']=999
        manifest_file.write_text(json.dumps(payload))
        with self.assertRaisesRegex(QualityError,'Run ID'):
            verify_saved_run(path)

    def test_modified_source_rejected(self):
        with sqlite3.connect(self.tape) as con:
            con.execute("UPDATE tape SET price='999' WHERE kind='TRADE' AND seq=(SELECT MIN(seq) FROM tape WHERE kind='TRADE')")
        with self.assertRaisesRegex(QualityError,'hash del tape'):
            self.save()

    def test_result_corruption_detected(self):
        path,_=self.save()
        with (path/'matched_controls.jsonl').open('a') as out:
            out.write('{"tampered":1}\n')
        with self.assertRaisesRegex(QualityError,'hash mismatch'):
            verify_saved_run(path)

    def test_original_tape_not_mutated(self):
        from research.micro_flow_v3.tape import replay_digest
        before=replay_digest(self.tape)
        self.save()
        after=replay_digest(self.tape)
        self.assertEqual(before,after)


if __name__ == '__main__':
    unittest.main()
