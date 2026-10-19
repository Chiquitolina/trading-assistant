"""Offline tests for read-only causal replay on actual TapeStore SQLite schema."""
import json
from pathlib import Path
import tempfile
import unittest

from research.micro_flow_v3.replay import ReplayError, replay_tape
from research.micro_flow_v3.tape import TapeStore, replay_digest

BASE_MS = 1700000000000


def add(store, agg_id, received_offset_ms, *, maker=False, price='100', qty='2',
        generation=1, trade_offset_ms=None):
    payload = {
        'e': 'aggTrade', 's': 'ETHUSDT', 'a': agg_id,
        'T': BASE_MS + (trade_offset_ms if trade_offset_ms is not None else received_offset_ms),
        'E': BASE_MS + received_offset_ms,
        'p': price, 'q': qty, 'm': maker,
    }
    recv_ns = (BASE_MS + received_offset_ms) * 1_000_000
    return store.ingest(json.dumps(payload), wall_ns=recv_ns,
                        mono_ns=received_offset_ms * 1_000_000 + 1,
                        connection=generation)


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'tape.sqlite3'

    def make(self):
        return TapeStore(self.db)

    def closed(self, tape):
        tape.marker('PROCESS_STOP')
        tape.close()

    def run_replay(self, name='result'):
        out = self.root / (name + '.jsonl')
        summary_path = self.root / (name + '.json')
        summary = replay_tape(self.db, out, summary_path=summary_path)
        lines = [json.loads(x) for x in out.read_text().splitlines()]
        return summary, lines, out, summary_path

    def test_causal_full_seconds_and_aggressor_side(self):
        t = self.make()
        add(t, 1, 100, price='100', qty='9')  # incomplete first second
        add(t, 2, 1100, maker=False, price='101', qty='2.5')
        add(t, 3, 1400, maker=True, price='99', qty='1.25')
        add(t, 4, 2100, maker=False, price='102', qty='7')  # trigger publishing second 1
        self.closed(t)
        before = replay_digest(self.db)
        summary, events, _, _ = self.run_replay()
        self.assertEqual(before, replay_digest(self.db))  # read-only
        self.assertEqual(summary['buckets_emitted'], 1)
        self.assertEqual(summary['head_buckets_discarded'], 1)
        self.assertEqual(summary['tail_buckets_discarded'], 1)
        e = events[0]
        self.assertEqual(e['trades'], 2)
        self.assertEqual(e['buy_qty'], '2.5')
        self.assertEqual(e['sell_qty'], '1.25')
        self.assertEqual(e['delta_qty'], '1.25')
        self.assertEqual(e['cvd_qty'], '1.25')
        self.assertEqual(e['open'], '101')
        self.assertEqual(e['high'], '101')
        self.assertEqual(e['low'], '99')
        self.assertEqual(e['close'], '99')
        self.assertEqual(e['available_wall_ns'], (BASE_MS + 2100)*1_000_000)
        self.assertLess(e['second_end_ms_exclusive'], e['available_ms'])
        self.assertEqual(e['first_agg_id'], 2)
        self.assertEqual(e['last_agg_id'], 3)

    def test_multiple_complete_seconds_cvd_ignores_initial_partial_second(self):
        t = self.make()
        add(t, 1, 100, qty='100', maker=False)
        add(t, 2, 1100, qty='3', maker=True)
        add(t, 3, 2100, qty='4', maker=False)
        add(t, 4, 3100, qty='1', maker=True)
        self.closed(t)
        _, events, _, _ = self.run_replay()
        self.assertEqual([e['cvd_qty'] for e in events], ['-3', '1'])

    def test_gap_splits_segments_and_drops_partial_seconds(self):
        t = self.make()
        add(t, 1, 100)
        add(t, 2, 1100)
        add(t, 3, 2100)
        t.marker('WS_CLOSED', connection=1)
        t.marker('RECONNECT_COVERAGE_UNVERIFIED', connection=1)
        add(t, 11, 3200, generation=2)   # gap becomes quality flagged
        add(t, 12, 4100, generation=2)
        add(t, 13, 5100, generation=2)
        self.closed(t)
        summary, events, _, _ = self.run_replay()
        self.assertEqual(len(summary['segments']), 2)
        self.assertEqual([e['segment_id'] for e in events], [1, 2])
        self.assertEqual([e['first_agg_id'] for e in events], [2, 12])
        self.assertIn('TRADE_QUALITY:ID_JUMP_SUSPECTED', summary['segments'][1]['start_reason'])
        self.assertEqual(summary['buckets_emitted'], 2)

    def test_detects_silent_generation_change(self):
        t = self.make()
        add(t, 1, 100)
        add(t, 2, 1100)
        add(t, 3, 2100, generation=2)  # ids continuous, transport generation isn't
        add(t, 4, 3100, generation=2)
        add(t, 5, 4100, generation=2)
        self.closed(t)
        summary, events, _, _ = self.run_replay()
        self.assertEqual(len(summary['segments']), 2)
        self.assertEqual(summary['segments'][1]['start_reason'], 'OBSERVED_ORDER_OR_GENERATION_DISCONTINUITY')
        self.assertEqual([e['first_agg_id'] for e in events], [4])

    def test_rejects_unclosed_tape(self):
        t = self.make()
        add(t, 1, 100)
        t.close()
        with self.assertRaisesRegex(ReplayError, 'not closed'):
            self.run_replay()

    def test_output_stable_across_replays_and_existing_output_protected(self):
        t = self.make()
        for i in range(6):
            add(t, i+1, i*1000+100, maker=i%2 == 1)
        self.closed(t)
        s1, e1, f1, _ = self.run_replay('one')
        s2, e2, f2, _ = self.run_replay('two')
        self.assertEqual(e1, e2)
        self.assertEqual(f1.read_bytes(), f2.read_bytes())
        self.assertEqual(s1['replay_jsonl_sha256'], s2['replay_jsonl_sha256'])
        with self.assertRaises(FileExistsError):
            replay_tape(self.db, f1)
        self.assertTrue(f1.exists())

    def test_one_second_segment_emits_no_bucket(self):
        t = self.make()
        add(t, 1, 100)
        add(t, 2, 200)
        self.closed(t)
        summary, events, _, _ = self.run_replay()
        self.assertEqual(events, [])
        self.assertEqual(summary['buckets_emitted'], 0)


if __name__ == '__main__':
    unittest.main()
