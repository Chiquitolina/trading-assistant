"""Synthetic boundary/latency contract regression, no live candles or external I/O."""
import json
import unittest

from dashboard.services import reaction_lab_historical as hist
from dashboard.services import reaction_lab_runner_optimizer as opt
from dashboard.services.reaction_lab_portfolio import PROFILES, validate_profiles

M = 60_000


def fixtures(*, observed_ms=M, room_ms=M, ranks_ms=M):
    key = f'TESTUSDT|LONG|0'
    event = {
        'event_key': key, 'symbol': 'TESTUSDT', 'side': 'LONG',
        'retest_ts': '0', 'known_ts': str(M),
        'first_replay_observed_ms': str(observed_ms),
        'causal_json': json.dumps({'max_departure_pct': 2.5, 'retest_timestamp': 0}),
    }
    factor = {
        'event_key': key, 'room_1h_pct': '3',
        'volume_rank_pct_4h': '80', 'return_rank_pct_4h': '80',
        'room_available_ms': str(room_ms), 'ranks_available_ms': str(ranks_ms),
        'room_asof_ms': str(room_ms), 'ranks_asof_ms': str(ranks_ms),
        'room_method': 'ASOF_CONFIRMED_SWINGS_1H',
        'ranks_method': 'ASOF_CLOSED_4H_CROSS_SECTION',
    }
    return {key: event}, {key: factor}


def path():
    bars = [(M + i * M, 100.0, 101.0, 100.0, 100.5)
            for i in range(240)]
    # OPEN == reference => marketable if same boundary bar is erroneously allowed.
    # First subsequent minute opens above buy limit, then touches it: maker.
    bars[1] = (2*M, 101.0, 101.5, 99.9, 100.5)
    bars[2] = (3*M, 100.5, 102.5, 100.2, 101.5)
    return tuple(bars)


class HistoricalTimingTests(unittest.TestCase):
    def test_exact_close_boundary_not_eligible_at_same_open(self):
        events, factors = fixtures()
        rows, issues = hist.prepare_candidates(events, factors)
        self.assertEqual(issues, [])
        self.assertEqual(rows[0]['theoretical_ready_ms'], M)
        self.assertEqual(rows[0]['ready_ms'], M + 1)
        self.assertEqual(rows[0]['assumed_execution_delay_ms'], 0)

        guarded = {**rows[0], 'reference': 100.0, 'bars': path()}
        optimistic = {**guarded, 'ready_ms': M}
        params = dict(PROFILES['Runner Challenger 220'])
        old = opt.simulate(optimistic, params)
        fixed = opt.simulate(guarded, params)
        self.assertEqual(old['entry_liquidity'], 'TAKER')
        self.assertEqual(old['fill_ts'], M)
        self.assertEqual(fixed['entry_liquidity'], 'MAKER')
        self.assertEqual(fixed['fill_ts'], 2*M)

    def test_lag_scenario_is_explicit_and_not_live_observation(self):
        events, factors = fixtures()
        rows, issues = hist.prepare_candidates(
            events, factors, assumed_execution_delay_ms=M)
        self.assertEqual(issues, [])
        self.assertEqual(rows[0]['theoretical_ready_ms'], M)
        self.assertEqual(rows[0]['ready_ms'], 2*M + 1)
        self.assertEqual(rows[0]['first_seen_ms'], M)
        self.assertEqual(rows[0]['assumed_execution_delay_ms'], M)

    def test_subminute_ready_remains_on_next_full_bar(self):
        events, factors = fixtures(observed_ms=M+45_000)
        rows, issues = hist.prepare_candidates(events, factors)
        self.assertEqual(issues, [])
        self.assertEqual(rows[0]['ready_ms'], M + 45_001)
        first_eligible = ((rows[0]['ready_ms'] + M - 1) // M) * M
        self.assertEqual(first_eligible, 2*M)

    def test_factor_availability_later_than_scanner_is_respected(self):
        events, factors = fixtures(room_ms=2*M, ranks_ms=M)
        rows, issues = hist.prepare_candidates(events, factors)
        self.assertEqual(issues, [])
        self.assertEqual(rows[0]['theoretical_ready_ms'], 2*M)
        self.assertEqual(rows[0]['ready_ms'], 2*M + 1)

    def test_reject_negative_or_bool_lag(self):
        events, factors = fixtures()
        for value in (-1, 1.5, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                hist.prepare_candidates(events, factors,
                                        assumed_execution_delay_ms=value)

    def test_frozen_profiles_unchanged(self):
        validated = validate_profiles(PROFILES)
        self.assertEqual(validated['Runner Challenger 220']['limit'], 0)
        self.assertEqual(validated['Runner Challenger 220']['ttl'], 30)
        self.assertEqual(validated['Runner V1 Frozen'], opt.BASELINE)


if __name__ == '__main__':
    unittest.main()
