"""Contract tests for causal Failed Aggression research on V3 replay buckets."""
import unittest

from dashboard.services.micro_flow_v3_failed_aggression import (
    analyze_failed_aggression, summarize_failed_aggression,
)

BASE = 1_792_365_000_000


def make_buckets(*, direction='UP', n=240, segment=1, start_ms=BASE):
    rows = []
    for i in range(n):
        px = 100.0 + (0.01 * i if direction == 'UP' else -0.01 * i)
        rows.append({
            'segment_id': segment,
            'second_start_ms': start_ms + i * 1000,
            'available_ms': start_ms + i * 1000 + 1300,
            'open': str(px), 'close': str(px), 'high': str(px + .001),
            'low': str(px - .001), 'buy_qty': '2', 'sell_qty': '1',
            'delta_qty': '1', 'trades': 3,
        })
    return rows


def signal(*, direction=-1, ready_offset_ms=1000, segment=1, px=100., ident='test'):
    return {
        'event_id': ident, 'episode_id': f'episode:{ident}', 'segment_id': segment,
        'ready_ms': BASE + ready_offset_ms,
        'second_start_ms': BASE,
        'side': 'SELL_PRESSURE' if direction == -1 else 'BUY_PRESSURE',
        'direction': direction, 'signal_price': px,
        'is_episode_anchor': True,
    }


class FailedAggressionTests(unittest.TestCase):
    def test_sell_failure_confirmed_then_long_proxy(self):
        row = analyze_failed_aggression(make_buckets(direction='UP'),
                                        [signal()], confirmation_seconds=10,
                                        min_opposite_pct=.03)[0]
        self.assertEqual(row['state'], 'FAILED_AGGRESSION')
        self.assertEqual(row['candidate_side'], 'LONG')
        self.assertEqual(row['decision_ms'], BASE + 11_000)
        self.assertEqual(row['entry_ms'], BASE + 11_000)
        self.assertTrue(row['observed_asof_ms'] <= row['decision_ms'])
        self.assertLess(row['directional_progress_pct'], 0)
        self.assertGreaterEqual(row['peak_original_favorable_pct'], 0)
        self.assertEqual(row['outcome_state_30s'], 'OBSERVED')
        self.assertGreater(row['return_30s_pct'], 0)
        self.assertGreater(row['directional_mfe_30s_pct'], 0)
        self.assertLessEqual(row['directional_mae_30s_pct'], 0)

    def test_buy_failure_confirmed_then_short_proxy(self):
        row = analyze_failed_aggression(make_buckets(direction='DOWN'),
                                        [signal(direction=1)],
                                        confirmation_seconds=10)[0]
        self.assertEqual(row['state'], 'FAILED_AGGRESSION')
        self.assertEqual(row['candidate_side'], 'SHORT')
        self.assertGreater(row['return_60s_pct'], 0)

    def test_same_direction_continuation_is_comparison_not_failure(self):
        row = analyze_failed_aggression(make_buckets(direction='DOWN'),
                                        [signal(direction=-1)],
                                        confirmation_seconds=10)[0]
        self.assertEqual(row['state'], 'PRESSURE_CONTINUED')
        self.assertEqual(row['candidate_side'], 'SHORT')

    def test_stale_and_missing_are_censored_not_failed(self):
        tape = make_buckets(n=7)
        row = analyze_failed_aggression(tape, [signal()],
                                        confirmation_seconds=30)[0]
        self.assertEqual(row['state'], 'CENSORED_STALE')
        self.assertEqual(row['entry_state'], 'NOT_APPLICABLE')
        self.assertIsNone(row['return_30s_pct'])
        missing = analyze_failed_aggression([], [signal()])[0]
        self.assertEqual(missing['state'], 'CENSORED_NO_DATA')

    def test_late_published_bucket_cannot_confirm_at_nominal_second(self):
        rows = make_buckets(n=200)
        # Highly favorable price for hypothetical LONG, but published much later.
        idx = 10
        rows[idx]['open'] = rows[idx]['close'] = '105.0'
        rows[idx]['high'] = '105.01'
        rows[idx]['low'] = '104.99'
        rows[idx]['available_ms'] = BASE + 15_300  # outside decision at +11s
        # Subsequent published rows must also not go backwards in availability.
        for k in range(11, 15):
            rows[k]['available_ms'] = BASE + 15_300
        r = analyze_failed_aggression(rows, [signal()],
                                      confirmation_seconds=10)[0]
        self.assertTrue(r['observed_asof_ms'] <= r['decision_ms'])
        self.assertAlmostEqual(r['observed_price'], 100.09)
        self.assertEqual(r['state'], 'FAILED_AGGRESSION')

    def test_same_millisecond_publication_is_not_read_at_decision(self):
        rows = make_buckets(n=200)
        rows[10]['available_ms'] = BASE + 11_000  # exact truncated-ms boundary
        rows[10]['open'] = rows[10]['close'] = '105.0'
        rows[10]['high'] = '105.01'
        rows[10]['low'] = '104.99'
        result = analyze_failed_aggression(
            rows, [signal()], confirmation_seconds=10)[0]
        self.assertEqual(result['decision_ms'], BASE + 11_000)
        self.assertAlmostEqual(result['observed_price'], 100.09)
        self.assertEqual(result['state'], 'FAILED_AGGRESSION')

    def test_segment_boundaries_no_other_segment_outcome_leak(self):
        a = make_buckets(n=28, segment=1)
        b = make_buckets(n=240, segment=2)
        r = analyze_failed_aggression(a + b, [signal()],
                                      confirmation_seconds=10)[0]
        self.assertEqual(r['state'], 'FAILED_AGGRESSION')
        self.assertEqual(r['outcome_state_30s'], 'CENSORED_END_OF_SEGMENT')
        self.assertIsNone(r['return_30s_pct'])

    def test_no_available_nearby_entry_is_censored(self):
        rows = make_buckets(n=200)
        rows = [r for i, r in enumerate(rows) if i < 11 or i > 17]
        r = analyze_failed_aggression(rows, [signal()], confirmation_seconds=10)[0]
        self.assertEqual(r['state'], 'FAILED_AGGRESSION')
        self.assertEqual(r['entry_state'], 'CENSORED_NO_NEARBY_BUCKET')
        self.assertIsNone(r['entry_proxy'])

    def test_horizon_gap_censors_without_price_filling(self):
        rows = make_buckets(n=200)
        rows = [r for i, r in enumerate(rows) if i < 41 or i > 47]
        r = analyze_failed_aggression(rows, [signal()], confirmation_seconds=10)[0]
        self.assertEqual(r['outcome_state_30s'], 'CENSORED_HORIZON_GAP')
        self.assertIsNone(r['return_30s_pct'])
        self.assertEqual(r['outcome_state_60s'], 'OBSERVED')

    def test_only_episode_anchors_are_used_and_deterministic(self):
        tape = make_buckets()
        primary = signal()
        secondary = signal(ready_offset_ms=2000, ident='secondary')
        secondary['is_episode_anchor'] = False
        a = analyze_failed_aggression(tape, [primary, secondary], confirmation_seconds=10)
        b = analyze_failed_aggression(tape[::-1], [secondary, primary], confirmation_seconds=10)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 1)

    def test_invalid_parameters_rejected(self):
        with self.assertRaises(ValueError):
            analyze_failed_aggression([], [], confirmation_seconds=0)
        with self.assertRaises(ValueError):
            analyze_failed_aggression([], [], min_opposite_pct=-.1)
        with self.assertRaises(ValueError):
            analyze_failed_aggression([], [], freshness_seconds=0)

    def test_report_never_calls_censored_success(self):
        rows = analyze_failed_aggression(make_buckets(n=28), [signal()],
                                          confirmation_seconds=10)
        table = summarize_failed_aggression(rows)
        sell = [r for r in table if r['state'] == 'FAILED_AGGRESSION'
                and r['original_side'] == 'SELL_PRESSURE'
                and r['horizon_s'] == 30][0]
        self.assertEqual(sell['episodes'], 1)
        self.assertEqual(sell['observed'], 0)
        self.assertEqual(sell['censored'], 1)
        self.assertIsNone(sell['positive_pct'])

    def test_signal_snapshot_unresolved_no_contrarian_entry(self):
        tape = make_buckets(direction='UP')
        tape = [{**r, 'open': '100', 'close': '100', 'high': '100', 'low': '100'} for r in tape]
        result = analyze_failed_aggression(tape, [signal()], confirmation_seconds=10)[0]
        self.assertEqual(result['state'], 'UNRESOLVED')
        self.assertIsNone(result['candidate_side'])
        self.assertIsNone(result['entry_ms'])


if __name__ == '__main__':
    unittest.main()
