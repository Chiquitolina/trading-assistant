"""Offline-only safety tests for V3 multi-session research."""
import json
import tempfile
import unittest
from pathlib import Path

from dashboard.services.micro_flow_v3_multi_session import (
    analyze_sessions, find_replay_for_closed_tape, isolate_segments,
    load_replay_sessions,
)
from dashboard.services.micro_flow_v3_dashboard_service import V3DashboardError
from dashboard.services.micro_flow_v3_research import detect_pressure_events
from research.micro_flow_v3.tape import TapeStore
from research.micro_flow_v3.replay import replay_tape


BASE_MS = 1_799_000_000_000


def session_buckets(filename, first_ms, *, seg=1, length=240, spike=7, trend=.10):
    result=[]
    for idx in range(length):
        at=first_ms+idx*1000
        big=idx==spike
        price=2300+idx*trend
        result.append({'symbol':'ETHUSDT','event_type':'BUCKET_1S','segment_id':seg,
            'second_start_ms':at,'second_end_ms_exclusive':at+1000,
            'available_ms':at+1100,
            'open':str(price),'high':str(price+.01),'low':str(price-.01),'close':str(price),
            'buy_qty':str(30 if big else 1),'sell_qty':str(.1 if big else 1),
            'delta_qty':str(29.9 if big else 0),'cvd_qty':'1',
            'trades':10 if big else 2})
    return {'tape':filename,'buckets':result,'summary':{'trades_seen':length}}


class MultiSessionIsolationTests(unittest.TestCase):
    def setUp(self):
        self.s1=session_buckets('A.sqlite3',BASE_MS)
        self.s2=session_buckets('B.sqlite3',BASE_MS+3600_000)

    def test_reassigned_ids_and_provenance_immutable(self):
        rows=isolate_segments([self.s1,self.s2])
        self.assertEqual({r['segment_id'] for r in rows}, {1,2})
        self.assertEqual(rows[0]['source_session'],'A.sqlite3')
        self.assertEqual(rows[240]['source_session'],'B.sqlite3')
        self.assertEqual(self.s2['buckets'][0]['segment_id'],1)

    def test_each_session_detected_independently(self):
        output=analyze_sessions([self.s1,self.s2],lookback=5,
                                relative_volume=2.,min_imbalance=.7,
                                min_trades=3,cooldown_seconds=20)
        self.assertEqual(len(output['events']),2)
        self.assertEqual(len(output['episodes']),2)
        self.assertNotEqual(output['events'][0]['event_id'], output['events'][1]['event_id'])
        self.assertNotEqual(output['episodes'][0]['episode_id'],output['episodes'][1]['episode_id'])
        self.assertEqual([x['Episodios'] for x in output['per_session']],[1,1])
        self.assertEqual([x['Señales'] for x in output['per_session']],[1,1])

    def test_last_session_cannot_supply_previous_outcome(self):
        short=session_buckets('short.sqlite3',BASE_MS,length=15,spike=7)
        next_s=session_buckets('next.sqlite3',BASE_MS+15_000,length=240,spike=7)
        output=analyze_sessions([short,next_s],lookback=5)
        first=next(e for e in output['events'] if e['source_session']=='short.sqlite3')
        self.assertIsNone(first['return_30s_pct'])
        self.assertIsNone(first['return_180s_pct'])

    def test_identical_timestamps_do_not_merge_sessions_when_isolating(self):
        same=session_buckets('same_time.sqlite3',BASE_MS)
        rows=isolate_segments([self.s1,same])
        self.assertEqual(len({r['segment_id'] for r in rows}),2)
        self.assertEqual(len(detect_pressure_events(rows,lookback=5)),2)


class ReplayDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.tape_dir=self.root/'reports/micro_flow_v3/tapes/ETHUSDT'
        self.tape_dir.mkdir(parents=True)
        self.tape=self.tape_dir/'aggtrades_raw_20261019T000000_001.sqlite3'

    def build(self, *, closed=True):
        store=TapeStore(self.tape,'ETHUSDT',batch_size=1)
        store.marker('CONNECT_ATTEMPT')
        for i in range(14):
            record={'e':'aggTrade','s':'ETHUSDT','a':101+i,
                    'T':BASE_MS+i*1000,'E':BASE_MS+i*1000+1,
                    'p':str(2300+i),'q':'1','m':bool(i%2)}
            store.ingest(json.dumps(record),connection=1,
                         wall_ns=(BASE_MS+i*1000+200)*1_000_000,
                         mono_ns=(10_000+i*1000)*1_000_000)
        if closed:
            store.marker('PROCESS_STOP')
        store.close()

    def generate(self, *, cli_variant=False):
        out_dir=self.root/'reports/micro_flow_v3/replay'
        out_dir.mkdir(parents=True,exist_ok=True)
        suffix='_1s.jsonl' if cli_variant else '_buckets_1s.jsonl'
        output=out_dir/(self.tape.stem+suffix)
        summary=out_dir/(self.tape.stem+'_summary.json')
        replay_tape(self.tape,output,summary_path=summary)
        return output,summary

    def test_cli_replay_suffix_recognized(self):
        self.build()
        self.generate(cli_variant=True)
        self.assertIsNotNone(find_replay_for_closed_tape(self.root,self.tape))
        sessions=load_replay_sessions(self.root,[self.tape])
        self.assertEqual(len(sessions),1)
        self.assertEqual(len(sessions[0]['buckets']),12)

    def test_canonical_suffix_recognized(self):
        self.build()
        self.generate()
        self.assertIsNotNone(find_replay_for_closed_tape(self.root,self.tape))

    def test_active_tape_rejected(self):
        self.build(closed=False)
        self.assertIsNone(find_replay_for_closed_tape(self.root,self.tape))
        with self.assertRaises(V3DashboardError):
            load_replay_sessions(self.root,[self.tape])

    def test_corrupted_replay_hash_fails_closed(self):
        self.build()
        output,_=self.generate(cli_variant=True)
        with output.open('a') as f:
            f.write('garbage\n')
        with self.assertRaisesRegex(V3DashboardError,'Hash JSONL'):
            load_replay_sessions(self.root,[self.tape])

    def test_duplicate_selection_rejected(self):
        self.build()
        self.generate(cli_variant=True)
        with self.assertRaisesRegex(V3DashboardError,'dos veces'):
            load_replay_sessions(self.root,[self.tape,self.tape])

    def test_invalid_session_limit(self):
        self.build()
        self.generate(cli_variant=True)
        with self.assertRaises(V3DashboardError):
            load_replay_sessions(self.root,[self.tape],max_sessions=0)


if __name__=='__main__':
    unittest.main()
