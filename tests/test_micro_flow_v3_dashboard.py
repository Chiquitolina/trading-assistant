"""Offline V3 Streamlit-data and research tests; no service, Redis or Binance."""
import json
import tempfile
import unittest
from pathlib import Path

from dashboard.services.micro_flow_v3_dashboard_service import (
    inspect_v3_tape, list_v3_tapes, replay_paths, existing_replay,
    load_buckets_jsonl, V3DashboardError,
)
from dashboard.services.micro_flow_v3_research import detect_pressure_events
from research.micro_flow_v3.tape import TapeStore


class MicroFlowV3DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'reports/micro_flow_v3/tapes/ETHUSDT/aggtrades_raw_test.sqlite3'

    def _create_tape(self, closed):
        writer = TapeStore(self.path, 'ETHUSDT', batch_size=1)
        writer.marker('CONNECT_ATTEMPT')
        for n in range(2):
            event = {'e': 'aggTrade', 's': 'ETHUSDT', 'a': 100 + n,
                     'T': 1_799_000_000_000 + n, 'E': 1_799_000_000_001 + n,
                     'p': '2300', 'q': '1', 'm': n % 2 == 0}
            writer.ingest(json.dumps(event), connection=1,
                          wall_ns=(1_799_000_000_200+n)*1_000_000,
                          mono_ns=(10_000+n)*1_000_000)
        if closed:
            writer.marker('PROCESS_STOP')
        writer.close()

    def test_closed_tape_and_live_monitor(self):
        self._create_tape(closed=True)
        result = inspect_v3_tape(self.path)
        self.assertTrue(result['closed'])
        self.assertEqual(result['trades'], 2)
        self.assertEqual(result['id_jumps'], 0)
        self.assertEqual(len(list_v3_tapes(self.root)), 1)
        self.assertTrue(result['recent_markers'])

    def test_unclosed_tape_refuses_replay(self):
        self._create_tape(closed=False)
        result = inspect_v3_tape(self.path)
        self.assertFalse(result['closed'])
        self.assertIsNone(existing_replay(self.root, self.path, result))

    def test_existing_replay_requires_matching_source_rows(self):
        self._create_tape(closed=True)
        info = inspect_v3_tape(self.path)
        out, summ = replay_paths(self.root, self.path)
        out.parent.mkdir(parents=True)
        out.write_text('{}\n')
        good = {'schema': 'micro-flow-v3-causal-buckets-v1',
                'symbol': 'ETHUSDT', 'source_tape_rows': info['total_rows'],
                'closed_source_required': True}
        summ.write_text(json.dumps(good))
        self.assertIsNotNone(existing_replay(self.root, self.path, info))
        good['source_tape_rows'] += 1
        summ.write_text(json.dumps(good))
        self.assertIsNone(existing_replay(self.root, self.path, info))

    def test_real_tape_to_replay_to_dashboard(self):
        from research.micro_flow_v3.replay import replay_tape
        writer = TapeStore(self.path, 'ETHUSDT', batch_size=1)
        writer.marker('CONNECT_ATTEMPT')
        for n in range(12):
            payload = {'e': 'aggTrade', 's': 'ETHUSDT', 'a': 300 + n,
                       'T': 1_799_000_000_000 + n * 1000,
                       'E': 1_799_000_000_001 + n * 1000,
                       'p': str(2300 + n*.1), 'q': '1', 'm': n % 2 == 0}
            writer.ingest(json.dumps(payload), connection=1,
                          wall_ns=(1_799_000_000_200 + n * 1000)*1_000_000,
                          mono_ns=(10_000 + n * 1000)*1_000_000)
        writer.marker('PROCESS_STOP')
        writer.close()
        output, summ = replay_paths(self.root,self.path)
        replay_tape(self.path,output,summary_path=summ)
        info=inspect_v3_tape(self.path)
        pair=existing_replay(self.root,self.path,info)
        self.assertIsNotNone(pair)
        rows=load_buckets_jsonl(pair[0])
        self.assertEqual(len(rows),10)
        self.assertTrue(all(int(x['available_ms']) >= int(x['second_end_ms_exclusive'])
                            for x in rows))

    def test_replay_loader_fail_closed_on_limit(self):
        out = self.root/'test.jsonl'
        out.write_text(''.join(json.dumps({'event_type':'BUCKET_1S'})+'\n' for _ in range(4)))
        self.assertEqual(len(load_buckets_jsonl(out, limit=4)), 4)
        with self.assertRaises(V3DashboardError):
            load_buckets_jsonl(out, limit=3)


def make_buckets(length=220, segment_id=1, *, spike_at=7, ready_delay_ms=200):
    data = []
    for i in range(length):
        x = 1_799_000_000_000 + i*1000
        spike = i == spike_at
        buy = 30 if spike else 1
        sell = .1 if spike else 1
        px = 2300 + i*.1
        data.append({'segment_id':segment_id, 'second_start_ms':x,
                     'available_ms':x+1000+ (ready_delay_ms if spike else 100),
                     'buy_qty':str(buy),'sell_qty':str(sell),
                     'trades':10 if spike else 2,
                     'open':str(px),'high':str(px+.01),
                     'low':str(px-.01),'close':str(px)})
    return data


class MicroFlowV3StrategyTests(unittest.TestCase):
    def _detect(self, buckets, **kwargs):
        return detect_pressure_events(buckets, lookback=5,relative_volume=2,
                                      min_imbalance=.7,min_trades=3,
                                      cooldown_seconds=20, **kwargs)

    def test_causal_signal_and_outcomes(self):
        rows = make_buckets()
        events = self._detect(rows)
        self.assertEqual(len(events),1)
        event = events[0]
        self.assertEqual(event['side'],'BUY_PRESSURE')
        self.assertGreaterEqual(event['entry_ms'],event['ready_ms'])
        self.assertGreater(event['return_30s_pct'],0)
        self.assertGreater(event['mfe_30s_pct'],0)
        self.assertLess(event['mae_30s_pct'], event['mfe_30s_pct'])

    def test_censors_outcome_without_future(self):
        rows = make_buckets(length=35,spike_at=7)
        event = self._detect(rows)[0]
        self.assertIsNotNone(event['entry_ms'])
        self.assertIsNone(event['return_60s_pct'])
        self.assertIsNone(event['return_180s_pct'])

    def test_never_crosses_segment_or_uses_future_for_signal(self):
        early = make_buckets(length=9,segment_id=1,spike_at=7)
        later = make_buckets(length=80,segment_id=2,spike_at=7)
        events = self._detect(early+later)
        self.assertEqual(len(events),2)
        self.assertIsNone(events[0]['entry_ms'])
        self.assertIsNotNone(events[1]['entry_ms'])
        self.assertNotEqual(events[0]['segment_id'],events[1]['segment_id'])

    def test_delayed_availability_moves_entry_forward(self):
        regular = self._detect(make_buckets())[0]
        delayed = self._detect(make_buckets(ready_delay_ms=10100))[0]
        self.assertGreater(delayed['entry_ms'], regular['entry_ms'])
        self.assertGreaterEqual(delayed['entry_ms'], delayed['ready_ms'])

    def test_extreme_threshold_suppresses_event(self):
        self.assertEqual(detect_pressure_events(make_buckets(), lookback=5,
            relative_volume=1000, min_imbalance=.70), [])


if __name__ == '__main__':
    unittest.main()
