"""Regression tests: experimental SHORT guard without modifying frozen portfolios."""
import json
import tempfile
import unittest
from pathlib import Path

# Self-contained synthetic OHLC fixtures (no LIVE DBs or Binance required).
MINUTE = 60_000
T = 1_000_020_000_000


def record(key="X|LONG|1", ready=T + 2*MINUTE + 7000,
           base=T, room=3., mode="TP", side="LONG"):
    bars = [(base+i*MINUTE, 100., 100.15, 99.85, 100.) for i in range(360)]
    if mode in ("TP", "SL", "AMBIG"):
        if side == "LONG":
            bars[3] = (base+3*MINUTE, 99., 99.2, 97.8, 98.1)
            bars[4] = (base+4*MINUTE, 98.1, 100.3, 98., 100.)
        else:
            bars[3] = (base+3*MINUTE, 101., 102.2, 100.5, 102.)
            bars[4] = (base+4*MINUTE, 102., 102.2, 99.8, 100.)
    return {"key": key, "symbol": "TEST", "side": side,
            "retest_ts": base-MINUTE, "ready_ms": ready,
            "values": (2., room, 70., 70.),
            "bars": tuple(bars), "reference": 100.,
            "context_status": "READY", "regime_status": "READY"}

from dashboard.services.reaction_lab_portfolio import (
    PROFILES, EXPERIMENTAL_NAME, EXPERIMENTAL_GUARD_ID,
    EXPERIMENTAL_REJECT_DECISION, extreme_strength_short_guard,
    run_comparison, export_comparison,
)
from dashboard.services.reaction_lab_portfolio_reader import (
    read_table, load_run_metadata, verify_consistency,
)


def short(key, ret=97, vol=95, ready=T + 2 * MINUTE + 7000):
    e = record(key, ready=ready, side='SHORT', mode='TP')
    e['values'] = (2., 3., float(vol), float(ret))
    return e


class ExtremeStrengthPortfolioTests(unittest.TestCase):
    def test_rule_requires_both_ranks_and_short(self):
        self.assertTrue(extreme_strength_short_guard(short('A', 95, 90)))
        self.assertFalse(extreme_strength_short_guard(short('B', 94.9999, 100)))
        self.assertFalse(extreme_strength_short_guard(short('C', 100, 89.9999)))
        x = short('D', 100, 100)
        x['side'] = 'LONG'
        self.assertFalse(extreme_strength_short_guard(x))
        with self.assertRaises(ValueError):
            x['side'] = 'SHORT'
            x['values'] = (2., 3., float('nan'), 100)
            extreme_strength_short_guard(x)

    def test_opt_in_only_and_frozen_configuration_untouched(self):
        e = short('X|SHORT|1')
        base = run_comparison([e])
        exp = run_comparison([e], include_extreme_strength_guard=True)
        self.assertEqual(set(base), {'Runner V1 Frozen', 'Runner Challenger 220'})
        self.assertEqual(set(exp), set(base) | {EXPERIMENTAL_NAME})
        for name in base:
            self.assertEqual(base[name], exp[name], name)
        self.assertEqual(exp[EXPERIMENTAL_NAME]['parameters'], PROFILES['Runner Challenger 220'])
        self.assertEqual(exp[EXPERIMENTAL_NAME]['summary']['experimental_guard_rejected'], 1)
        self.assertEqual(exp[EXPERIMENTAL_NAME]['summary']['orders_placed'], 0)
        self.assertEqual(exp[EXPERIMENTAL_NAME]['decisions'][0]['reason'], EXPERIMENTAL_GUARD_ID)

    def test_eligible_extreme_veto_before_slot_reallocates(self):
        first = short('X|SHORT|1')
        second = record('Y|LONG|2', base=T + MINUTE,
                        ready=T + 3 * MINUTE + 7000,
                        side='LONG', mode='TP')
        trial = run_comparison([first, second], include_extreme_strength_guard=True)
        frozen = trial['Runner Challenger 220']
        guarded = trial[EXPERIMENTAL_NAME]
        self.assertEqual(frozen['summary']['orders_placed'], 1)
        self.assertEqual(frozen['summary']['skipped_slot_busy'], 1)
        self.assertEqual(guarded['summary']['experimental_guard_rejected'], 1)
        self.assertEqual(guarded['summary']['orders_placed'], 1)
        self.assertEqual(guarded['summary']['skipped_slot_busy'], 0)
        self.assertEqual(guarded['trades'][0]['event_key'], 'Y|LONG|2')
        self.assertEqual(guarded['decisions'][0]['decision'], EXPERIMENTAL_REJECT_DECISION)
        self.assertEqual(guarded['summary']['pre_guard_eligible'], 2)
        self.assertEqual(guarded['summary']['filter_eligible'], 1)

    def test_noneligible_extreme_counts_normal_threshold_rejection(self):
        e = short('X|SHORT|1')
        e['values'] = (0.1, 3, 95, 97)
        x = run_comparison([e], include_extreme_strength_guard=True)[EXPERIMENTAL_NAME]
        self.assertEqual(x['decisions'][0]['decision'], 'FILTER_REJECTED')
        self.assertEqual(x['summary']['experimental_guard_rejected'], 0)

    def test_long_extreme_is_not_blocked(self):
        e = record('X|LONG|1')
        e['values'] = (2, 3, 100, 100)
        x = run_comparison([e], include_extreme_strength_guard=True)[EXPERIMENTAL_NAME]
        self.assertEqual(x['summary']['experimental_guard_rejected'], 0)
        self.assertEqual(x['summary']['orders_placed'], 1)

    def test_isolated_ledger_and_dashboard_reader(self):
        records = [short('X|SHORT|1'), record('Y|LONG|2', base=T + MINUTE,
                   ready=T + 3 * MINUTE + 7000, side='LONG', mode='TP')]
        outcome = run_comparison(records, include_extreme_strength_guard=True)
        with tempfile.TemporaryDirectory() as d:
            project = Path(d)
            folder = project / 'reports/reaction_lab/runner_portfolio_v1' / 'experiment'
            export_comparison(outcome, records, folder, cohort='HISTORICAL_RESEARCH_UNVERIFIED_OR_PARTIAL')
            manifest, summary = load_run_metadata(project, 'experiment')
            self.assertEqual(len(summary), 3)
            self.assertEqual(manifest['experimental_variants'][EXPERIMENTAL_NAME]['return_rank_4h_gte'], 95.)
            self.assertEqual(manifest['experimental_variants'][EXPERIMENTAL_NAME]['volume_rank_4h_gte'], 90.)
            tables = {n: read_table(project, 'experiment', n) for n in
                      ('decisions', 'events', 'trades', 'equity', 'path_traces')}
            self.assertTrue(verify_consistency(summary, tables)['ok'])
            self.assertEqual([x['decision'] for x in tables['decisions']
                              if x['strategy'] == EXPERIMENTAL_NAME][0], EXPERIMENTAL_REJECT_DECISION)
            with self.assertRaises(FileExistsError):
                export_comparison(outcome, records, folder)

    def test_reject_non_boolean_flag(self):
        with self.assertRaisesRegex(ValueError, 'boolean'):
            run_comparison([], include_extreme_strength_guard='yes')


if __name__ == '__main__':
    unittest.main()
